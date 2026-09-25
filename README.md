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

Test files also get rules drawn from Kent Beck's [Test Desiderata](https://testdesiderata.com). Each asks whether a test violates one property.

| Rule | Desideratum | Severity |
| --- | --- | --- |
| `test_not_isolated` | Isolated | warning |
| `test_not_composable` | Composable | info |
| `test_nondeterministic` | Deterministic | error |
| `test_slow` | Fast | warning |
| `test_hard_to_write` | Writable | info |
| `test_unreadable` | Readable | info |
| `test_not_behavioural` | Behavioural | error |
| `test_structure_sensitive` | Structure insensitive | warning |
| `test_not_automated` | Automated | error |
| `test_not_specific` | Specific | warning |
| `test_not_predictive` | Predictive | warning |
| `test_not_inspiring` | Inspiring | warning |

Two more rules cover fixtures, setup and teardown hooks and shared test helpers.

| Rule | Severity |
| --- | --- |
| `fixture_leaks_state` | warning |
| `fixture_hides_behaviour` | warning |

The test rules only run on files matching `test_files`, such as `test_*.py`, `conftest.py`, `*_test.go`, `*.spec.ts`, `*Test.java`, `spec/*.rb` and `*/tests/*.rs`. Rust unit tests inside `mod tests` in a source file are not matched.

Every test rule asks about a test case and every fixture rule about a fixture or hook, so each kind of function is judged only by its own rules. Decorators and attributes such as `@pytest.fixture`, `#[fixture]` and `@BeforeEach` are sent along with the function so the model can tell them apart. In test files, calls such as Jest's `it` and `beforeEach`, RSpec's `it`, `let` and `before`, and busted's `it` and `before_each` are also sent as functions.

## Installation

```bash
uv tool install nouls --index typesafe=https://pypi.typesafe.ai/
export TYPESAFE_API_KEY="your-api-key"
```

## Usage

```bash
nouls check src/
nouls rules
nouls serve
nouls label src/billing.py 42 unit_mismatch false
nouls review unit_mismatch
nouls stats rules
nouls stats hotspots
nouls stats cost
nouls stats thresholds unit_mismatch --ask
```

`check` prints `path:line:column: severity [rule] message (probability)` and exits 1 when an error level rule fires. Set `show_probability: false` to drop the probability from both the command line and editor diagnostics.

## Store

Every answer lives in one SQLite file, `~/.cache/nouls/nouls.db` by default, shared by the language server, the command line and every repo on the machine. It runs in WAL mode, so several processes can use it at once.

- `answers` caches one probability per model, question and function source. Rewording a rule only re-asks that rule. Editing a function only re-asks that function.
- `observations` holds the latest answer for every rule on every function in every file checked.
- `labels` holds your verdicts. A finding labelled `false` is no longer reported for that function.
- `runs` records questions asked, cache hits and tokens for every file checked.

## Labels and thresholds

Label findings from the editor with the `not a problem` and `confirm finding` code actions, from the command line with `nouls label`, or in bulk with `nouls review`. `review` shows unlabelled functions for one rule, sampled evenly across probability bands, so the labels cover misses as well as hits.

`nouls stats thresholds` compares your labels with the current wording of each question and prints precision and recall at a range of thresholds. Labels belong to the rule, not the wording, so you can rewrite a question, run `nouls stats thresholds --ask` to re-ask it for every labelled function, and compare.

`nouls stats rules` shows how often each rule fires, how many answers sit in the ambiguous 0.35 to 0.65 band, and a histogram of probabilities. A well posed question piles up at both ends.

For anything else, attach the store read only from DuckDB:

```sql
ATTACH '~/.cache/nouls/nouls.db' AS nouls (TYPE sqlite, READ_ONLY);
SELECT rule, quantile_cont(probability, [0.1, 0.5, 0.9]) FROM nouls.observations GROUP BY rule;
```

## Configuration

nouls merges its built in defaults with the first `nouls.yaml`, `nouls.yml`, `.nouls.yaml` or `.nouls.yml` found walking up from the target, or the file passed with `--config`. Maps merge key by key, so you only write what changes.

```yaml
model: jev-latest
threshold: 0.8
concurrency: 8
debounce_ms: 1000
show_probability: true
lint_on: change
store: ~/.cache/nouls/nouls.db
exclude: [".*", node_modules, __pycache__, target, dist, build, venv]

languages:
  kotlin:
    grammar: kotlin
    extensions: [.kt, .kts]
    units: [function_declaration]
    attached: [annotation]
    calls:
      node: call_expression
      callee: function
      names: [test, beforeTest]

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
  test_not_isolated:
    files: ["*_test.py", "*/integration/*.py"]
```

`lint_on: save` stops the language server checking while you type. `store` moves the SQLite file.

`files` limits a rule to file names or paths matching any of its globs. Setting it replaces the default list.

Languages are pure configuration. `grammar` is any name from [tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack), and `units` lists the node types to send as individual questions. The diagnostic sits on the node's `name` field, or its first line when it has none. `attached` lists wrapper or preceding sibling node types, such as decorators, that belong to a unit. `calls` makes calls with a matching callee name into units, but only in files matching `test_files`.

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
uv sync --all-extras
uv run prek install
uv run prek run --all-files
uv run pytest
```

## AI Integration

An MCP server for the docs lives in `src/nouls/mcp_server.py`:

```bash
claude mcp add nouls --transport stdio uv run --with mcp python src/nouls/mcp_server.py
```

A Claude Code Agent Skill lives in `.skills/nouls`:

```bash
claude skill add .skills/nouls
```
