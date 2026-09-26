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
| `cryptic_error` | info |
| `vague_error` | info |
| `unconstructive_error` | info |
| `mixed_abstraction` | info |

Anything ruff, a type checker or a security scanner already catches is deliberately out of scope.

Two rules read the project's build, lint and CI configuration and check that it follows rule 10 of NASA's [Power of Ten](https://spinroot.com/gerard/pdf/P10.pdf): all code compiles with every warning enabled at the most pedantic setting and no warnings, and at least one strong static analyser checks it at least daily with zero warnings. `relaxed_warnings` asks about every line of those files on its own, so each ignored rule, skipped check, excluded file, loosened threshold or step allowed to fail is its own finding on its own line. `unscheduled_static_analysis` asks once about the files together, because a missing analyser is not on any line.

| Rule | Severity |
| --- | --- |
| `relaxed_warnings` | warning |
| `unscheduled_static_analysis` | warning |

They read files such as `pyproject.toml`, `ruff.toml`, `tsconfig.json`, `Cargo.toml`, `go.mod`, `CMakeLists.txt`, `Makefile`, `.pre-commit-config.yaml` and `.github/workflows/*.yml`, and report on the first one they find, or on the project root when there are none.

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
    message: Debit and credit signs look inverted. Subtract debits and add credits
    severity: error
    languages: [python, go]
  test_not_isolated:
    files: ["*_test.py", "*/integration/*.py"]
```

`lint_on: save` stops the language server checking while you type. `store` moves the SQLite file.

`files` limits a rule to file names or paths matching any of its globs. Setting it replaces the default list.

`scope: project` asks a rule once about the whole project instead of once per function, and `scope: setting` asks it once about every line of the matching files that sets something, skipping comments, headers and lines that only open a block. Their `files` are globs relative to the project root, the nearest directory above the target with a `.git` or nouls config, and they are read even when `exclude` would skip them. A project finding sits on line 1 of the first match in the order the globs are listed, and a setting finding on its own line. The language server checks both when you open or save one of their files, and `nouls label` takes the setting's line.

nouls detects each file's language from its name, using [tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack), and finds functions with the grammar's tags query, so any language whose grammar marks functions works without configuration. Files in languages without function tags, such as Markdown, YAML and plain text, are skipped. A `languages` entry only adds what detection cannot know: `extensions` maps extra file endings to the language, and `units` lists the node types to send as individual questions, replacing the tags query where it misses functions, as it does for C, C++, JavaScript and TypeScript. The diagnostic sits on the node's `name` field, or its first line when it has none. `attached` lists wrapper or preceding sibling node types, such as decorators, that belong to a unit. `calls` makes calls with a matching callee name into units, but only in files matching `test_files`.

Every question is answered against this state:

```json
{"language": "python", "function": "<source of the unit>"}
```

Write questions as a single yes/no judgement about that function.

Project rules are answered against the matching files, keyed by path relative to the project root:

```json
{"files": {"pyproject.toml": "<contents>", ".github/workflows/ci.yml": "<contents>"}}
```

Setting rules are answered against one line, with the keys it sits under in TOML, YAML and JSON files:

```json
{"file": "pyproject.toml", "setting": "tool.ruff.lint.ignore", "line": "\"E501\","}
```

Write each `message` in plain language, say precisely what is wrong, then suggest how to fix it. The reader may be a user, a developer or an agent, so name the exact command, setting or code to change.

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
