#!/usr/bin/env python3
"""
Recursively expand gograph context through interface and struct fields.
Finds every `receiver.field.Method` call in the target's source,
resolves field types, follows interface implementations, and recurses.

Usage:
  python3 .gograph/expand_context.py "(*Handler).Register" --root /path/to/go/project > context.json
  python3 .gograph/expand_context.py "(*AuthService).Register" --depth 8 --root . > context.json
"""
from __future__ import annotations
import datetime
import fnmatch
import json, sys, os, argparse, re, shutil, asyncio, subprocess
from pathlib import Path

def find_executable(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    home = Path.home()
    go_path = Path(os.environ.get("GOPATH", home / "go"))
    go_bin = go_path / "bin" / name
    if sys.platform == "win32" and not go_bin.suffix.lower() == ".exe":
        go_bin = go_bin.with_name(go_bin.name + ".exe")
    if go_bin.is_file():
        return str(go_bin)
    goroot = os.environ.get("GOROOT")
    if goroot:
        goroot_bin = Path(goroot) / "bin" / name
        if sys.platform == "win32" and not goroot_bin.suffix.lower() == ".exe":
            goroot_bin = goroot_bin.with_name(goroot_bin.name + ".exe")
        if goroot_bin.is_file():
            return str(goroot_bin)
    try:
        go_env_result = subprocess.run(["go", "env", "GOPATH"], capture_output=True, text=True)
        if go_env_result.returncode == 0:
            go_path_str = go_env_result.stdout.strip()
            if go_path_str:
                go_path_from_env = Path(go_path_str)
                go_bin_from_env = go_path_from_env / "bin" / name
                if sys.platform == "win32" and not go_bin_from_env.suffix.lower() == ".exe":
                    go_bin_from_env = go_bin_from_env.with_name(go_bin_from_env.name + ".exe")
                if go_bin_from_env.is_file():
                    return str(go_bin_from_env)
    except FileNotFoundError:
        pass
    return name


async def run_tool(cmd: list[str], cwd: str | Path = ".", check: bool = True) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    result = {
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    if check and proc.returncode != 0:
        stderr_msg = result["stderr"].strip()
        raise RuntimeError(
            f"Command {cmd!r} failed with exit code {proc.returncode}"
            + (f": {stderr_msg}" if stderr_msg else "")
        )
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "returncode": result["returncode"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "status": "raw",
        }

def parse_roots(raw: list[str]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for entry in raw:
        for part in entry.split(","):
            p = Path(part).resolve()
            if p not in seen:
                seen.add(p)
                result.append(p)
    return result


def is_go_project(path: Path) -> bool:
    return (path / "go.mod").is_file() or (path / "go.work").is_file()


BUILTIN_IGNORE = frozenset({".git", "node_modules", "vendor", ".gograph", "dist", "build"})

GO_BUILTINS = frozenset({
    "bool", "byte", "complex64", "complex128", "error", "float32", "float64",
    "int", "int8", "int16", "int32", "int64", "rune", "string", "uint",
    "uint8", "uint16", "uint32", "uint64", "uintptr", "nil", "true", "false",
    "append", "cap", "close", "complex", "copy", "delete", "imag", "len",
    "make", "new", "panic", "print", "println", "real", "recover",
})


def is_callable_symbol(name: str) -> bool:
    """Heuristic: should this callee name be expanded recursively?

    Local variables and built-in types are skipped. Package-qualified
    identifiers, method receivers, and exported single identifiers are kept.
    """
    if not name:
        return False
    if name in GO_BUILTINS:
        return False
    # Method receiver forms: (*Type).Method or (Type).Method
    if name.startswith("("):
        return True
    # Skip single lowercase words (local vars / unexported fields)
    if "." not in name and name.isidentifier() and name.islower():
        return False
    last = name.split(".")[-1]
    if not last.isidentifier():
        return False
    # Skip field reads where the final segment is unexported / lowercase
    if last.islower():
        return False
    return True


def discover_replace_modules(root: Path) -> list[Path]:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return []
    result: list[Path] = []
    in_block = False
    for line in go_mod.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not in_block:
            if s.startswith("replace ") and s.endswith("("):
                in_block = True
                continue
            if s.startswith("replace ") and "=>" in s:
                parts = s.split("=>")
                if len(parts) == 2:
                    target = parts[1].strip().split("//")[0].strip()
                    p = (root / target).resolve()
                    if p.is_dir():
                        result.append(p)
        else:
            if s == ")":
                in_block = False
                continue
            if "=>" in s:
                parts = s.split("=>")
                if len(parts) == 2:
                    target = parts[1].strip().split("//")[0].strip()
                    p = (root / target).resolve()
                    if p.is_dir():
                        result.append(p)
    return result


def discover_go_projects(root: Path, max_depth: int, exclude: set[str]) -> list[Path]:
    if is_go_project(root):
        projects: list[Path] = [root]
        projects.extend(discover_replace_modules(root))
        return projects
    ignore = BUILTIN_IGNORE | frozenset(exclude)
    result: list[Path] = []
    root_depth = len(root.parents)
    for dirpath, dirnames, _ in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parents) - root_depth
        if depth > 0 and current.name in ignore:
            dirnames.clear()
            continue
        if depth > 0 and depth <= max_depth and is_go_project(current):
            result.append(current)
        if depth >= max_depth:
            dirnames.clear()
    return result


GOGRAPH_CMD = find_executable("gograph")
GO_CMD = find_executable("go")


async def build_graph(root: Path, force_precise: bool = False) -> tuple[bool, list[str]]:
    result = await run_tool(
        [GOGRAPH_CMD, "build", ".", "--precise"],
        cwd=str(root),
        check=False,
    )
    if result.get("returncode", -1) == 0:
        return True, []
    if force_precise:
        raise RuntimeError(
            f"Precise graph build failed for {root} (force_precise=True)"
        )
    warning = "Precise graph build failed, falling back to heuristic mode."
    await run_tool(
        [GOGRAPH_CMD, "build", "."],
        cwd=str(root),
        check=False,
    )
    return False, [warning]


async def gograph_context(symbol: str, root: Path) -> dict:
    return await run_tool([GOGRAPH_CMD, "context", symbol, "--json"], cwd=root)


async def gograph_callees(symbol: str, root: Path, depth: int = 1) -> dict:
    return await run_tool(
        [GOGRAPH_CMD, "callees", symbol, "--depth", str(depth), "--json"], cwd=root
    )


async def gograph_implementers(iface: str, root: Path) -> list[str]:
    result = await run_tool([GOGRAPH_CMD, "implementers", iface, "--json"], cwd=root)
    if not result or result.get("status") == "raw":
        return []
    return [item["name"] for item in (result.get("results") or [])]


async def gograph_constructors(type_name: str, root: Path) -> list[dict]:
    result = await run_tool([GOGRAPH_CMD, "constructors", type_name, "--json"], cwd=root)
    if not result or result.get("status") == "raw":
        return []
    return result.get("results") or []


async def gograph_literals(type_name: str, root: Path) -> list[dict]:
    result = await run_tool([GOGRAPH_CMD, "literals", type_name, "--json"], cwd=root)
    if not result or result.get("status") == "raw":
        return []
    return result.get("results") or []


async def gograph_plan(symbol: str, root: Path) -> dict:
    return await run_tool([GOGRAPH_CMD, "plan", symbol, "--json"], cwd=root)


async def gograph_explain(symbol: str, root: Path) -> dict:
    return await run_tool([GOGRAPH_CMD, "explain", symbol, "--json"], cwd=root)


def _exact_function_matches(symbol: str, base_context: dict | None) -> list[dict]:
    if not base_context or base_context.get("status") != "ok":
        return []
    results = base_context.get("results") or {}
    if not isinstance(results, dict):
        return []
    nodes = results.get("Node", [])
    if isinstance(nodes, dict):
        nodes = [nodes]
    return [n for n in nodes if n.get("kind") == "function" and n.get("name") == symbol]


def _symbol_path_from_node(node: dict, project_root: Path) -> str | None:
    file = node.get("file")
    if not file:
        return None
    file_path = project_root / file
    try:
        rel = file_path.parent.relative_to(project_root)
    except ValueError:
        return None
    if rel == Path("."):
        return None
    return str(rel).replace(os.sep, "/")


async def _disambiguate_symbol(
    symbol: str, base_context: dict, project_root: Path
) -> str | None:
    candidates = _exact_function_matches(symbol, base_context)
    if len(candidates) <= 1:
        # Use the single exact match if it can be qualified; otherwise keep the
        # original symbol to avoid expensive substring matching.
        if len(candidates) == 1:
            rel_path = _symbol_path_from_node(candidates[0], project_root)
            if rel_path:
                return f"{rel_path}.{symbol}"
        return symbol

    root_label = str(project_root)

    if not sys.stdin.isatty():
        print(
            f"Warning: symbol '{symbol}' is ambiguous ({len(candidates)} matches) "
            f"in {root_label}. Run interactively to select one; using first match.",
            file=sys.stderr,
        )
        rel_path = _symbol_path_from_node(candidates[0], project_root)
        return f"{rel_path}.{symbol}" if rel_path else symbol

    print(
        f"Symbol '{symbol}' is ambiguous in {root_label}. "
        f"Multiple functions named '{symbol}' found:",
        file=sys.stderr,
    )
    for i, node in enumerate(candidates, 1):
        file = node.get("file", "unknown")
        line = node.get("line", "")
        detail = node.get("detail", "")
        loc = f"{file}:{line}" if line else file
        print(f"  {i}. {detail} ({loc})", file=sys.stderr)

    while True:
        try:
            choice = await asyncio.to_thread(
                input,
                f"Select one (1-{len(candidates)}), or press Enter to keep '{symbol}': ",
            )
        except EOFError:
            return symbol
        choice = choice.strip()
        if not choice:
            return symbol
        try:
            idx = int(choice)
            if 1 <= idx <= len(candidates):
                selected = candidates[idx - 1]
                break
        except ValueError:
            pass
        print("Invalid choice. Please enter a number.", file=sys.stderr)

    rel_path = _symbol_path_from_node(selected, project_root)
    if rel_path:
        return f"{rel_path}.{symbol}"
    return symbol


def _go_mod_cache(project_root: Path) -> Path:
    try:
        result = subprocess.run(
            [GO_CMD, "env", "GOMODCACHE"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except FileNotFoundError:
        pass
    return Path.home() / "go" / "pkg" / "mod"


def _go_root(project_root: Path) -> Path | None:
    try:
        result = subprocess.run(
            [GO_CMD, "env", "GOROOT"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except FileNotFoundError:
        pass
    return None


_IMPORT_ALIAS_CACHE: dict[str, dict[str, str]] = {}


def _package_alias_from_callee(name: str) -> str | None:
    if name.startswith("("):
        return None
    if "." not in name:
        return None
    return name.split(".")[0]


def _module_matches(module_path: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    for pat in patterns:
        if pat in ("*", "all"):
            return True
        if fnmatch.fnmatch(module_path, pat):
            return True
    return False


async def _resolve_import_path(
    alias: str, call_site_file: str, project_root: Path
) -> str | None:
    """Map a package alias used in a source file to its full import path."""
    if not call_site_file:
        return None
    call_site_path = project_root / call_site_file
    try:
        call_site_path.relative_to(project_root)
    except ValueError:
        return None
    importing_dir = call_site_path.parent
    dir_key = str(importing_dir.resolve())

    if dir_key not in _IMPORT_ALIAS_CACHE:
        _IMPORT_ALIAS_CACHE[dir_key] = {}
        result = await run_tool([GO_CMD, "list", "-json", "."], cwd=importing_dir, check=False)
        if result.get("status") == "raw" and result.get("returncode", -1) != 0:
            return None
        imports = result.get("Imports")
        if imports is None:
            return None
        for import_path in imports:
            name_result = await run_tool(
                [GO_CMD, "list", "-f", "{{.Name}}", import_path],
                cwd=project_root,
                check=False,
            )
            if name_result.get("returncode", -1) != 0:
                continue
            name = (name_result.get("stdout") or "").strip()
            if name:
                _IMPORT_ALIAS_CACHE[dir_key][name] = import_path

    return _IMPORT_ALIAS_CACHE[dir_key].get(alias)


async def _resolve_module_dir(import_path: str, project_root: Path) -> Path | None:
    result = await run_tool([GO_CMD, "list", "-json", import_path], cwd=project_root, check=False)
    if result.get("status") == "raw" and result.get("returncode", -1) != 0:
        return None
    if result.get("Standard"):
        return None
    module = result.get("Module", {})
    if not module:
        return None
    module_path = module.get("Path", "")
    module_dir = module.get("Dir", "")
    version = module.get("Version", "")
    cache = _go_mod_cache(project_root)

    if module_dir and Path(module_dir).is_dir():
        return Path(module_dir)

    if module_path:
        await _download_module(module_path, project_root)

    if module_dir and Path(module_dir).is_dir():
        return Path(module_dir)

    if version and module_path:
        candidate = cache / f"{module_path}@{version}"
        if candidate.is_dir():
            return candidate

    return None


async def _download_module(module_path: str, project_root: Path) -> bool:
    result = await run_tool([GO_CMD, "mod", "download", module_path], cwd=project_root, check=False)
    return result.get("returncode", -1) == 0


async def _prepare_module_graph(module_dir: Path, precise: bool) -> tuple[Path, bool, list[str]]:
    """Copy a read-only module into a writable worktree and build gograph there."""
    safe_name = module_dir.name.replace("/", "_").replace("\\", "_")
    work_dir = Path("/tmp/opencode/gograph_modules") / safe_name
    if not work_dir.exists():
        shutil.copytree(
            module_dir,
            work_dir,
            ignore=shutil.ignore_patterns(".git", "vendor"),
        )
        # Module cache files and directories are read-only; make the copy writable.
        work_dir.chmod(work_dir.stat().st_mode | 0o200)
        for path in work_dir.rglob("*"):
            try:
                path.chmod(path.stat().st_mode | 0o200)
            except OSError:
                pass
    # Always build/rebuild so a stale or failed previous run is recovered.
    build_mode, warnings = await build_graph(work_dir, force_precise=precise)
    return work_dir, build_mode, warnings


class FollowState:
    def __init__(self, patterns: list[str], known_roots: set[str] | None = None):
        self.patterns = patterns
        self.known_roots: set[str] = known_roots or set()
        self.queue: list[tuple[str, Path]] = []
        self.processed: set[tuple[str, str]] = set()
        self.work_dirs: dict[str, Path] = {}

    async def maybe_follow(
        self,
        callee_name: str,
        current_root: Path,
        current_graph_root: Path,
        call_site_file: str = "",
    ) -> None:
        if not self.patterns:
            return
        alias = _package_alias_from_callee(callee_name)
        if not alias:
            return

        import_path = await _resolve_import_path(alias, call_site_file, current_root)
        if not import_path:
            return

        module_dir = await _resolve_module_dir(import_path, current_root)
        if not module_dir:
            return

        result = await run_tool([GO_CMD, "list", "-json", import_path], cwd=current_root, check=False)
        if result.get("status") == "raw" and result.get("returncode", -1) != 0:
            return
        module = result.get("Module", {})
        module_path = module.get("Path", "")
        if not module_path or not _module_matches(module_path, self.patterns):
            return

        key = (callee_name, str(module_dir))
        if key in self.processed:
            return
        self.processed.add(key)
        self.queue.append((callee_name, module_dir))


def strip_pkg(raw_type):
    """Strip package qualifier and leading '*' to get short type name."""
    t = raw_type.lstrip("*")
    return t.split(".")[-1]


def _get_source_from_context(ctx: dict) -> str:
    results = ctx.get("results", {})
    if isinstance(results, dict):
        return results.get("Source", "") or ""
    return ""


def _extract_field_type(source: str, field_name: str) -> str | None:
    """Parse a struct source block and return the type of a named field."""
    if not source:
        return None
    # Remove line comments.
    source = re.sub(r"//.*", "", source)
    match = re.search(r"type\s+\w+\s+struct\s*\{(.*?)\}", source, re.DOTALL)
    if not match:
        return None
    body = match.group(1)
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"(\w+)\s+(.+)", line)
        if not m:
            continue
        name, typ = m.group(1), m.group(2).strip()
        if name == field_name:
            # Drop struct tags if present.
            return typ.split("`")[0].strip()
    return None


async def _resolve_chained_interface_method(callee_name: str, root: Path) -> list[str]:
    """For 'var.Field.Method' callees, find concrete implementation methods.

    Returns symbols like '(*PostgresInventoryRepository).AddProductItemToInventory'.
    """
    parts = callee_name.split(".")
    if len(parts) < 3:
        return []
    base = parts[0]
    method = parts[-1]
    field_chain = parts[1:-1]

    base_ctx = await gograph_context(base, root)
    if base_ctx.get("status") != "ok":
        return []
    source = _get_source_from_context(base_ctx)

    current_type: str | None = None
    for field in field_chain:
        current_type = _extract_field_type(source, field)
        if not current_type:
            return []
        # Use the short type name for gograph lookup.
        current_type = current_type.lstrip("*").split("/")[-1]
        type_ctx = await gograph_context(current_type, root)
        if type_ctx.get("status") != "ok":
            return []
        source = _get_source_from_context(type_ctx)

    if not current_type:
        return []

    interface_name = current_type.split(".")[-1]
    impls = await gograph_implementers(interface_name, root)
    results: list[str] = []
    for impl in impls:
        if impl.startswith("("):
            results.append(f"{impl}.{method}")
        else:
            results.append(f"(*{impl}).{method}")
    return results


async def expand_symbol(
    symbol: str,
    root: Path,
    depth: int,
    max_depth: int,
    visited: set[tuple[str, str]],
    follow_state: FollowState | None = None,
    raw_callees: list[dict] | None = None,
) -> dict | None:
    key = (symbol, str(root))
    if key in visited or depth > max_depth:
        return None
    # Use a copy so sibling branches do not pollute each other's visited sets.
    visited = visited | {key}
    if follow_state is not None:
        follow_state.processed.add(key)

    ctx = await gograph_context(symbol, root)
    if ctx.get("status") != "ok":
        return {"symbol": symbol, "status": "not_found", "depth": depth}

    result = {
        "symbol": symbol,
        "status": "ok",
        "depth": depth,
        "context": ctx,
        "expansions": {},
        "raw_callees": [],
    }

    node_list = ctx.get("results", {}).get("Node", [])
    if isinstance(node_list, dict):
        node_list = [node_list]

    interface_node = next((n for n in node_list if n.get("kind") == "interface"), None)
    if interface_node is None:
        interface_node = next((n for n in node_list if "interface" in n.get("name", "")), None)

    if interface_node is not None:
        interface_name = interface_node.get("name", symbol).split(".")[-1] or symbol
        impls = await gograph_implementers(interface_name, root)
        for impl in impls:
            sub = await expand_symbol(impl, root, depth + 1, max_depth, visited, follow_state)
            if sub is not None:
                result["expansions"][impl] = sub

    if "." in symbol:
        parts = symbol.rsplit(".", 1)
        if len(parts) == 2:
            type_part, method_part = parts
            type_ctx = await gograph_context(type_part, root)
            type_node_list = type_ctx.get("results", {}).get("Node", [])
            if isinstance(type_node_list, dict):
                type_node_list = [type_node_list]
            type_interface_node = next((n for n in type_node_list if n.get("kind") == "interface"), None)
            if type_interface_node is not None:
                impls = await gograph_implementers(type_part, root)
                for impl in impls:
                    impl_method = f"(*{impl}).{method_part}"
                    sub = await expand_symbol(impl_method, root, depth + 1, max_depth, visited, follow_state)
                    if sub is not None:
                        result["expansions"][impl_method] = sub

    if raw_callees is None:
        callees_result = await gograph_callees(symbol, root, depth=1)
        raw_callees = callees_result.get("results") or []
    result["raw_callees"] = raw_callees

    for callee in raw_callees:
        name = callee.get("name", "")
        if not is_callable_symbol(name):
            continue
        sub = await expand_symbol(name, root, depth + 1, max_depth, visited, follow_state)
        if sub is not None and sub.get("status") == "ok":
            result["expansions"][name] = sub
            continue

        # Try to resolve chained interface method calls to concrete implementations.
        impl_methods = await _resolve_chained_interface_method(name, root)
        if impl_methods:
            resolved_any = False
            for impl_method in impl_methods:
                impl_sub = await expand_symbol(
                    impl_method, root, depth + 1, max_depth, visited, follow_state
                )
                if impl_sub is not None:
                    result["expansions"][impl_method] = impl_sub
                    resolved_any = True
            if resolved_any:
                continue

        if sub is not None:
            result["expansions"][name] = sub
        if follow_state is not None:
            await follow_state.maybe_follow(name, root, root, callee.get("call_site_file", ""))

    return result


def _collect_concrete_types(node: dict, types: set[str] | None = None) -> set[str]:
    if types is None:
        types = set()

    context = node.get("context", {})
    if context.get("status") == "ok":
        results = context.get("results", {})
        node_info = results.get("Node", {})
        if isinstance(node_info, dict):
            node_info = [node_info]
        for info in node_info:
            kind = info.get("kind", "")
            if kind in ("struct", "type"):
                raw_name = info.get("name", "")
                if raw_name:
                    types.add(strip_pkg(raw_name))

    for sub in node.get("expansions", {}).values():
        _collect_concrete_types(sub, types)

    return types


async def gather_initialization_sites(expansions: dict, root: Path) -> dict:
    concrete_types: set[str] = set()
    for node in expansions.values():
        _collect_concrete_types(node, concrete_types)

    result: dict = {}
    for type_name in sorted(concrete_types):
        constructors = await gograph_constructors(type_name, root)
        literals = await gograph_literals(type_name, root)
        result[type_name] = {
            "constructors": constructors,
            "literals": literals,
        }
    return result


def _render_call_graph(expansions: dict, prefix: str = "") -> str:
    lines = []
    for name, node in sorted(expansions.items()):
        symbol = node.get("symbol", name) if isinstance(node, dict) else name
        lines.append(f"{prefix}- {symbol}")
        if isinstance(node, dict):
            sub = node.get("expansions", {})
            if sub:
                child = _render_call_graph(sub, prefix + "  ")
                if child:
                    lines.append(child)
    return "\n".join(lines)


def render_json(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


def render_markdown(result: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = result.get("metadata", {})
    symbol = metadata.get("symbol", "unknown")

    lines = [f"# Context: {symbol}", "", "## Metadata", ""]

    roots = metadata.get("roots", [])
    if roots:
        lines.append(f"- **Roots:** {', '.join(roots)}")
    gts = metadata.get("generated_at", "")
    if gts:
        lines.append(f"- **Generated At:** {gts}")
    gv = metadata.get("gograph_version", "")
    if gv:
        lines.append(f"- **gograph Version:** {gv}")
    lines.append("")

    for project in result.get("projects", []):
        root = project.get("root", "unknown")
        resolved_symbol = project.get("symbol", "unknown")
        build_mode = project.get("build_mode", "unknown")
        lines.extend([f"## Project: {root}", ""])
        lines.append(f"- **Symbol:** `{resolved_symbol}`")
        lines.extend([f"- **Build Mode:** {build_mode}", ""])

        explain = project.get("explain", {})
        if isinstance(explain, dict):
            explain_text = explain.get("detail", "")
            if not explain_text and isinstance(explain.get("results"), dict):
                explain_text = explain["results"].get("explain", "")
            if explain_text:
                lines.extend(["### Summary", "", explain_text, ""])

        base_context = project.get("base_context", {})
        source = ""
        if isinstance(base_context, dict):
            bc_results = base_context.get("results", {})
            if isinstance(bc_results, dict):
                source = bc_results.get("Source", "")
        if source:
            lines.extend(["### Source", "", "```go", source, "```", ""])

        plan = project.get("plan", {})
        lines.extend(["### Plan / Risk", ""])
        if not plan or plan.get("status") != "ok":
            lines.append("No plan data available.")
        else:
            lines.extend(["```json", json.dumps(plan, indent=2), "```"])
        lines.append("")

        expansions = project.get("expansions", {})
        if expansions:
            lines.extend(["### Call Graph", "", _render_call_graph(expansions), ""])

        init_sites = project.get("initialization_sites", {})
        if init_sites:
            lines.extend(["### Initialization Sites", ""])
            for type_name, sites in sorted(init_sites.items()):
                lines.extend([f"#### {type_name}", ""])
                constructors = sites.get("constructors", [])
                if constructors:
                    for ctor in constructors:
                        ctor_name = ctor.get("name", "unknown")
                        loc = ""
                        if ctor.get("file"):
                            loc = ctor["file"]
                            if ctor.get("line"):
                                loc += f":{ctor['line']}"
                            loc = f" ({loc})"
                        lines.append(f"- Constructor: `{ctor_name}`{loc}")
                literals = sites.get("literals", [])
                if literals:
                    for lit in literals:
                        loc = ""
                        if lit.get("file"):
                            loc = lit["file"]
                            if lit.get("line"):
                                loc += f":{lit['line']}"
                        else:
                            loc = "(unknown location)"
                        lines.append(f"- Literal: {loc}")
                if not constructors and not literals:
                    lines.append("_(none)_")
                lines.append("")

        project_warnings = project.get("warnings", [])
        if project_warnings:
            lines.extend(["### Warnings", ""])
            for w in project_warnings:
                lines.append(f"- {w}")
            lines.append("")

    global_warnings = result.get("warnings", [])
    if global_warnings:
        lines.extend(["## Global Warnings", ""])
        for w in global_warnings:
            lines.append(f"- {w}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def _comma_split_type(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _sanitize_filename(symbol: str) -> str:
    s = symbol.replace("(", "").replace(")", "").replace("*", "star_")
    s = s.replace("/", "_").replace("\\", "_").replace(".", "_")
    s = s.replace(" ", "_")
    return s


async def _process_project(
    symbol: str,
    root: Path,
    depth: int,
    precise: bool,
    follow_state: FollowState | None = None,
    graph_root: Path | None = None,
) -> dict:
    work_root = graph_root or root
    build_mode, warnings = await build_graph(work_root, force_precise=precise)

    current_symbol = symbol
    base_context = await gograph_context(current_symbol, work_root)
    resolved_symbol = await _disambiguate_symbol(current_symbol, base_context, root)
    if resolved_symbol != current_symbol:
        current_symbol = resolved_symbol
        base_context = await gograph_context(current_symbol, work_root)

    if not base_context or base_context.get("status") != "ok":
        return {
            "root": str(root),
            "symbol": current_symbol,
            "build_mode": "precise" if build_mode else "heuristic",
            "base_context": base_context
            or {"status": "not_found", "query": current_symbol},
            "plan": {},
            "explain": {},
            "expansions": {},
            "initialization_sites": {},
            "warnings": warnings
            + [f"Symbol {current_symbol!r} not found in {root}"],
        }

    plan = await gograph_plan(current_symbol, work_root)
    explain = await gograph_explain(current_symbol, work_root)

    # Keep every operand the LLM might need for line-level debugging, but do
    # not expand local variables / built-ins in the recursive tree.
    callees_result = await gograph_callees(current_symbol, work_root, depth=1)
    raw_callees = callees_result.get("results") or []
    base_context["raw_callees"] = raw_callees

    visited: set[tuple[str, str]] = set()
    expansion_root = await expand_symbol(
        current_symbol,
        work_root,
        depth=0,
        max_depth=depth,
        visited=visited,
        follow_state=follow_state,
        raw_callees=raw_callees,
    )
    expansions = expansion_root.get("expansions", {}) if expansion_root else {}

    init_sites = await gather_initialization_sites(expansions, work_root)

    return {
        "root": str(root),
        "symbol": current_symbol,
        "build_mode": "precise" if build_mode else "heuristic",
        "base_context": base_context,
        "plan": plan,
        "explain": explain,
        "expansions": expansions,
        "initialization_sites": init_sites,
        "warnings": warnings,
    }


async def async_main():
    parser = argparse.ArgumentParser(
        description="Recursively expand gograph context through interface and struct fields"
    )
    parser.add_argument("symbol", help="gograph symbol name, e.g. '(*Handler).Register'")
    parser.add_argument(
        "--root",
        action="append",
        type=_comma_split_type,
        default=None,
        help="root path(s) for Go project discovery (comma-separated or repeated)",
    )
    parser.add_argument("--depth", type=int, default=5, help="max recursion depth (default: 5)")
    parser.add_argument("--output-dir", default=".", help="output directory (default: .)")
    parser.add_argument("--max-discover-depth", type=int, default=3, help="max depth for project discovery (default: 3)")
    parser.add_argument("--exclude", action="append", default=None, help="directory names to exclude")
    parser.add_argument("--precise", action="store_true", help="fail if precise graph build fails")
    parser.add_argument(
        "--follow-imports",
        type=_comma_split_type,
        default=[],
        help="comma-separated glob patterns of modules to follow into the Go module cache (e.g. 'github.com/labstack/echo/*', '*')",
    )
    parser.add_argument("--no-md", action="store_true", help="skip markdown output")
    parser.add_argument("--no-json", action="store_true", help="skip JSON output")
    args = parser.parse_args()

    if shutil.which(GOGRAPH_CMD) is None:
        print("Error: gograph not found. Install from https://github.com/obra/gograph", file=sys.stderr)
        sys.exit(1)

    raw_roots = args.root or ["."]
    flat_roots = []
    for item in raw_roots:
        flat_roots.extend(item)
    roots = parse_roots(flat_roots)

    exclude_set = set(args.exclude) if args.exclude else set()
    follow_patterns = args.follow_imports or []

    gograph_version = ""
    try:
        ver_result = await run_tool([GOGRAPH_CMD, "version"], check=False)
        if isinstance(ver_result, dict):
            gograph_version = (ver_result.get("stdout") or "").strip()
    except Exception:
        pass

    projects = []
    global_warnings: list[str] = []
    known_roots: set[str] = set()

    for root in roots:
        discovered = discover_go_projects(root, args.max_discover_depth, exclude_set)
        if not discovered:
            global_warnings.append(f"No Go projects discovered under {root}")
            continue
        for project_root in discovered:
            known_roots.add(str(project_root.resolve()))

    follow_state = FollowState(follow_patterns, known_roots)

    for root in roots:
        discovered = discover_go_projects(root, args.max_discover_depth, exclude_set)
        if not discovered:
            continue
        for project_root in discovered:
            project_result = await _process_project(
                args.symbol, project_root, args.depth, args.precise, follow_state
            )
            # Skip roots where the requested symbol is absent; followed imports
            # may still populate them later with concrete callees.
            base_ctx = project_result.get("base_context", {})
            if base_ctx.get("status") == "empty" and not project_result.get("expansions"):
                continue
            projects.append(project_result)

    # Drain the import-following queue. Each queued item is a symbol inside a
    # third-party module; we copy the module to a writable worktree, build a
    # gograph database there, and recursively expand.
    while follow_state.queue:
        callee_name, module_dir = follow_state.queue.pop(0)
        if str(module_dir) in follow_state.work_dirs:
            work_dir = follow_state.work_dirs[str(module_dir)]
        else:
            try:
                work_dir, _, mod_warnings = await _prepare_module_graph(module_dir, precise=args.precise)
                follow_state.work_dirs[str(module_dir)] = work_dir
            except Exception as exc:
                global_warnings.append(f"Could not follow {callee_name} from {module_dir}: {exc}")
                continue
        try:
            followed_project = await _process_project(
                callee_name, module_dir, args.depth, args.precise, follow_state, graph_root=work_dir
            )
            projects.append(followed_project)
        except Exception as exc:
            global_warnings.append(f"Could not expand {callee_name} in {module_dir}: {exc}")

    result = {
        "metadata": {
            "symbol": args.symbol,
            "roots": [str(r) for r in roots],
            "follow_imports": follow_patterns,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "gograph_version": gograph_version,
        },
        "projects": projects,
        "warnings": global_warnings,
    }

    safe_symbol = _sanitize_filename(args.symbol)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_json:
        json_path = output_dir / f"{safe_symbol}.context.json"
        render_json(result, json_path)
        print(f"Wrote JSON: {json_path}", file=sys.stderr)

    if not args.no_md:
        md_path = output_dir / f"{safe_symbol}.context.md"
        render_markdown(result, md_path)
        print(f"Wrote Markdown: {md_path}", file=sys.stderr)


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("Expansion cancelled.", file=sys.stderr)


if __name__ == "__main__":
    main()
