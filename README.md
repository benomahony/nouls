# nouls

A semantic linter and language server for the problems deterministic tools cannot see.

nouls splits every file into functions with tree-sitter and asks [TypeSafe](https://docs.typesafe.ai) a batch of yes/no questions about each one. A rule fires when the probability of yes reaches its threshold. Every question is scored independently, so one call per function answers every rule, and unchanged functions are never asked again.

The default rules target intent, not syntax:

| Rule | Severity |
| --- | --- |
| `name_behaviour_mismatch` | warning |
| `docstring_drift` | warning |
| `query_with_side_effect` | warning |
| `partial_failure` | warning |
| `check_then_act` | warning |
| `missing_authorisation` | warning |
| `non_idempotent_retry` | error |
| `unit_mismatch` | error |
| `boundary_error` | error |
| `misleading_error` | info |
| `mixed_abstraction` | info |

Anything ruff, a type checker or a security scanner already catches is deliberately out of scope.

## Install

```bash
uv tool install git+https://github.com/benomahony/nouls --index typesafe=https://pypi.typesafe.ai/
export TYPESAFE_API_KEY="your-api-key"
```

## Usage

```bash
nouls check src/
nouls rules
nouls serve
```

`check` prints `path:line:column: severity [rule] message (probability)` and exits 1 when an error level rule fires.

## Configuration

nouls merges its built in defaults with the first `nouls.yaml`, `nouls.yml`, `.nouls.yaml` or `.nouls.yml` found walking up from the target, or the file passed with `--config`. Maps merge key by key, so you only write what changes.

```yaml
model: jev-1.12
threshold: 0.8
concurrency: 8
debounce_ms: 1000
exclude: [".*", node_modules, __pycache__, target, dist, build, venv]

languages:
  kotlin:
    grammar: kotlin
    extensions: [.kt, .kts]
    units: [function_declaration]

rules:
  mixed_abstraction:
    enabled: false
  unit_mismatch:
    threshold: 0.9
  ledger_sign:
    question: Does the function add a debit where the domain requires subtracting it, or the reverse?
    message: Debit and credit signs look inverted
    severity: error
    languages: [python, go]
```

Languages are pure configuration. `grammar` is any name from [tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack), and `units` lists the node types to send as individual questions. The diagnostic sits on the node's `name` field, or its first line when it has none.

Every question is answered against this state:

```json
{"language": "python", "function": "<source of the unit>"}
```

Write questions as a single yes/no judgement about that function.

## Neovim

```lua
vim.lsp.config("nouls", {
  cmd = { "nouls", "serve" },
  filetypes = { "python", "javascript", "typescript", "typescriptreact", "go", "rust", "java", "c", "cpp", "lua", "ruby" },
  root_markers = { "nouls.yaml", ".nouls.yaml", ".git" },
})
vim.lsp.enable("nouls")
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check
```
