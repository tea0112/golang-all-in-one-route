# `expand_context.py` Usage Guide

A cross-platform, Go-project-agnostic context expander built on top of [`gograph`](https://github.com/obra/gograph). It takes a Go symbol, discovers the project(s) it lives in, and emits a recursive context bundle in JSON and Markdown so you can paste it into an LLM chat.

---

## Table of Contents

1. [What it does](#what-it-does)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Quick start](#quick-start)
5. [CLI reference](#cli-reference)
6. [Output format](#output-format)
7. [Following imports](#following-imports)
8. [Interface method resolution](#interface-method-resolution)
9. [Line-level debugging data](#line-level-debugging-data)
10. [Performance tips](#performance-tips)
11. [Troubleshooting](#troubleshooting)

---

## What it does

Given a symbol such as `main`, `HandleConsumeCreateProduct`, or `(*Handler).Register`, the script:

1. Discovers Go projects (`go.mod`/`go.work`) under the supplied `--root` paths.
2. Builds a gograph database for each project, preferring `--precise` mode and falling back to heuristic mode if necessary.
3. Fetches `context`, `plan`, and `explain` for the symbol.
4. Recursively expands callees, interface implementations, constructors, and literal initialization sites.
5. Optionally follows third-party imports into the Go module cache.
6. Writes:
   - `<symbol>.context.json` — complete machine-readable tree.
   - `<symbol>.context.md` — human-readable summary for LLM prompts.

The script is designed to work on Linux, macOS, Windows, Git Bash, and PowerShell.

---

## Requirements

- Python 3.9+
- Go toolchain (`go` on PATH)
- [`gograph`](https://github.com/obra/gograph) installed and on PATH, or in `$GOPATH/bin` / `$GOROOT/bin`

---

## Installation

1. Place `expand_context.py` anywhere you like (e.g. next to your project or in a shared tools directory).
2. Make sure `gograph` is installed:
   ```bash
   gograph version
   ```
3. Make the script executable (optional on Unix):
   ```bash
   chmod +x expand_context.py
   ```

---

## Quick start

```bash
# From inside a Go project
python3 expand_context.py "(*Handler).Register" --root . --depth 4

# Multiple roots (monorepo)
python3 expand_context.py "HandleConsumeCreateProduct" \
  --root ./internal/services/inventory_service \
  --root ./internal/pkg \
  --depth 3 \
  --output-dir ./contexts

# Expand the main() of the command you are currently in
python3 expand_context.py "main" --root . --depth 2

# Follow third-party imports
python3 expand_context.py "main" \
  --root ./internal/services/inventory_service \
  --depth 2 \
  --follow-imports "*" \
  --output-dir ./contexts
```

After it finishes you get:

```text
./contexts/
  star_Handler_Register.context.json
  star_Handler_Register.context.md
```

---

## CLI reference

| Flag | Default | Description |
|------|---------|-------------|
| `symbol` | — | The gograph symbol to expand. Examples: `main`, `(*Handler).Register`, `HandleConsumeCreateProduct`. |
| `--root` | `.` | One or more root paths for Go project discovery. Comma-separated or repeated. |
| `--depth` | `5` | Maximum recursion depth for callee expansion. |
| `--output-dir` | `.` | Directory for output files. |
| `--max-discover-depth` | `3` | How deep to walk from a root looking for `go.mod`/`go.work`. |
| `--exclude` | — | Directory names to ignore during discovery (repeatable). |
| `--precise` | off | Fail immediately if gograph `--precise` build fails instead of falling back. |
| `--follow-imports` | — | Comma-separated glob patterns of modules to follow into the Go module cache. `*` follows all non-stdlib modules. |
| `--no-md` | off | Skip Markdown output. |
| `--no-json` | off | Skip JSON output. |

### `--follow-imports` patterns

```bash
# All third-party modules
--follow-imports "*"

# Only modules you own
--follow-imports "github.com/myorg/*"

# Specific modules
--follow-imports "github.com/labstack/echo/v4,github.com/sirupsen/logrus"
```

Stdlib packages are intentionally skipped because they live in `GOROOT/src` without a `go.mod`.

---

## Output format

### JSON

Top-level structure:

```json
{
  "metadata": {
    "symbol": "HandleConsumeCreateProduct",
    "roots": [...],
    "follow_imports": [...],
    "generated_at": "...",
    "gograph_version": "..."
  },
  "projects": [
    {
      "root": "/path/to/project",
      "symbol": "HandleConsumeCreateProduct",
      "build_mode": "precise",
      "base_context": { "status": "ok", "results": { "Node": [...], "Source": "...", "Callees": [...] }, "raw_callees": [...] },
      "plan": { ... },
      "explain": { ... },
      "expansions": { ... },
      "initialization_sites": { ... },
      "warnings": []
    }
  ],
  "warnings": []
}
```

Key sections:

- `base_context.results.Source` — source code of the requested symbol.
- `base_context.raw_callees` — **every** operand gograph found, including local variables. Each entry has `file`, `line`, `call_site_file`, `call_site_line`, and `detail`.
- `expansions` — recursive expansion tree of callable symbols.
- `initialization_sites` — constructors and literal sites for concrete types discovered during expansion.
- `warnings` — non-fatal issues (build fallback, missing modules, etc.).

### Markdown

The Markdown file is a condensed human-readable version:

- Metadata
- One section per project
- Source code block
- Plan / Risk block
- Clean call graph
- Initialization sites
- Warnings

The Markdown call graph only shows expanded callable symbols, not the raw operands. If you need line-level operands, use the JSON.

---

## Following imports

By default the script only expands symbols inside the discovered project roots (plus any `replace` modules it finds). If you want the source code of third-party library functions too, use `--follow-imports`.

How it works:

1. When a callee cannot be resolved locally, the script extracts its package alias (e.g. `uuid` from `uuid.NewV4`).
2. It maps the alias to the real import path using `go list`.
3. It locates the module in `GOMODCACHE`.
4. If the module is missing, it runs `go mod download`.
5. It copies the module to a writable worktree under `/tmp/opencode/gograph_modules/` and builds a gograph database there.
6. It expands the callee inside that module and adds it as a separate `project` entry.

This is recursive: if a followed module calls another followed module, that one is expanded too.

> **Warning:** `--follow-imports "*"` can be slow and produce large output. Start with a small `--depth` (e.g. `2`) and narrow patterns.

---

## Interface method resolution

Calls through interface fields such as:

```go
inventoryDeliveryBase.InventoryRepository.AddProductItemToInventory(...)
```

are automatically resolved to concrete implementations when the interface and implementation live in the same gograph database:

```text
inventoryDeliveryBase.InventoryRepository.AddProductItemToInventory
  -> (*PostgresInventoryRepository).AddProductItemToInventory
```

The script does this by:

1. Looking up the base variable type (`inventoryDeliveryBase` → `*InventoryDeliveryBase`).
2. Parsing the struct source to find the field type (`InventoryRepository` → `contracts.InventoryRepository`).
3. Querying gograph implementers of that interface (`PostgresInventoryRepository`).
4. Expanding `(*PostgresInventoryRepository).AddProductItemToInventory`.

If the interface implementation lives in a different root or third-party module, resolution may fail. In that case the original chained callee remains in `raw_callees` and appears as `not_found` in the expansion tree.

---

## Symbol disambiguation

Unqualified top-level function names such as `main` are ambiguous when several packages define them (for example, multiple commands under `cmd/`). The script resolves them automatically:

1. It fetches the gograph context for the requested name.
2. It collects exact function matches (`kind: function`, `name: <symbol>`).
3. If there is exactly one match, it qualifies the symbol with the package path, e.g. `main` → `cmd/spoofdpi.main`. This also avoids expensive substring matching that would otherwise match names like `TestMain` or `DomainTrie`.
4. If there are multiple matches, it prompts you interactively to choose one.
5. In non-interactive mode it warns and uses the first match.

The resolved symbol is recorded in each project's `"symbol"` field in JSON and in the Markdown output.

---

## Line-level debugging data

Even though the recursive expansion filters out local variables and built-ins, the full operand list is preserved in `base_context.raw_callees` (and in nested `raw_callees` for each expanded symbol).

Example entry:

```json
{
  "kind": "callee",
  "name": "count",
  "file": "inventory/consumers/handlers/consume_create_product_handler.go",
  "line": 33,
  "detail": "called by HandleConsumeCreateProduct  ->  `p, err := inventoryDeliveryBase.InventoryRepository.AddProductItemToInventory(...)`",
  "call_site_file": "inventory/consumers/handlers/consume_create_product_handler.go",
  "call_site_line": 33
}
```

You can ask the LLM "what operands appear on line 33?" and it has the answer.

---

## Performance tips

| Goal | Recommendation |
|------|----------------|
| Fastest run | Omit `--follow-imports`. |
| Smaller output | Lower `--depth`. |
| Follow only what matters | Use specific patterns, e.g. `--follow-imports "github.com/myorg/*"`. |
| Huge monorepo | Pass the exact service directory as `--root` instead of the repo root. |
| Avoid re-copying modules | The script reuses `/tmp/opencode/gograph_modules/` across runs. |
| Expanding `main` | Start with `--depth 2` or `--depth 3`; depth 5 from `main` can be very large. |

Typical timings on `shop-golang-microservices` (`HandleConsumeCreateProduct`, depth 3):

- Without follow imports: ~5–7 s, ~40 KB JSON.
- With `--follow-imports "*"` depth 2: ~30–35 s, ~1.5 MB JSON.

---

## Troubleshooting

### `gograph not found`

Install gograph and ensure it is on PATH or in `$GOPATH/bin`.

### `Precise graph build failed`

Either fix the Go project so it builds, or omit `--precise` to allow heuristic fallback.

### Missing third-party source after `--follow-imports`

The script auto-downloads modules with `go mod download`. If a module fails, check the `warnings` array in the JSON output.

### Stdlib calls not followed

This is by design. Stdlib packages do not have a `go.mod` and cannot be built by gograph in the module cache.

### `not_found` for chained interface methods

The interface implementation may be in a different root or third-party module. Try adding that root explicitly with `--root`, or use `--follow-imports` if it is a third-party module.

### Duplicate or empty project entries

Empty projects (where the requested symbol does not exist) are filtered out automatically. `replace` modules that contain a followed callee will appear as a non-empty project when relevant.

---

## Example workflow

```bash
# 1. Generate context for a handler
python3 expand_context.py "HandleConsumeCreateProduct" \
  --root ./internal/services/inventory_service \
  --depth 3 \
  --output-dir ./contexts

# 2. Inspect the clean Markdown call graph
cat ./contexts/HandleConsumeCreateProduct.context.md

# 3. If you need library source too, re-run with follow imports
python3 expand_context.py "HandleConsumeCreateProduct" \
  --root ./internal/services/inventory_service \
  --depth 2 \
  --follow-imports "*" \
  --output-dir ./contexts

# 4. Copy the JSON into your LLM prompt
#    (the Markdown is usually enough for the first pass)
```
