from difflib import get_close_matches
from fnmatch import fnmatch
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel
from tree_sitter_language_pack import SupportedLanguage

from nouls.store import default_path

CONFIG_NAMES = ("nouls.yaml", "nouls.yml", ".nouls.yaml", ".nouls.yml")

Severity = Literal["error", "warning", "info", "hint"]


class Calls(BaseModel):
    node: str
    callee: str
    names: list[str]


class Language(BaseModel):
    grammar: SupportedLanguage
    extensions: list[str]
    units: list[str]
    attached: list[str] = []
    calls: Calls | None = None


class Rule(BaseModel):
    question: str
    message: str
    severity: Severity = "warning"
    threshold: float | None = None
    languages: list[str] | None = None
    files: list[str] | None = None
    enabled: bool = True


class Config(BaseModel):
    model: str
    threshold: float
    concurrency: int
    debounce_ms: int
    show_probability: bool
    lint_on: Literal["change", "save"]
    store: Path | None = None
    exclude: list[str]
    test_files: list[str] = []
    languages: dict[str, Language]
    rules: dict[str, Rule]

    def store_path(self) -> Path:
        path = self.store.expanduser() if self.store else default_path()
        assert path.name, "Store path must name a file"
        assert not path.is_dir(), "Store path must not be a directory"
        return path

    def language_for(self, path: Path) -> str | None:
        assert path.name, "Path must name a file"
        assert self.languages, "At least one language must be configured"
        return next(
            (name for name, lang in self.languages.items() if path.suffix in lang.extensions),
            None,
        )

    def is_test(self, path: Path) -> bool:
        assert path.name, f"is_test needs a file path but got {path!r}; pass a source file path"
        assert all(self.test_files), (
            "test_files contains a blank pattern, which would match nothing; "
            "remove the empty entry from test_files in your nouls config"
        )
        return matches(path, self.test_files)

    def rules_for(self, language: str, path: Path) -> dict[str, Rule]:
        assert language in self.languages, (
            f"No language called {language!r} is configured; get the name from language_for(path) "
            f"or add {language} under languages in your nouls config"
        )
        selected = {
            name: rule
            for name, rule in self.rules.items()
            if rule.enabled
            and (rule.languages is None or language in rule.languages)
            and (rule.files is None or matches(path, rule.files))
        }
        assert all(rule.enabled for rule in selected.values()), (
            "rules_for selected a disabled rule; the filter above must check rule.enabled"
        )
        return selected

    def unknown_rule(self, rule: str) -> str:
        assert rule, "unknown_rule needs the rule name the user typed; pass it through unchanged"
        assert rule not in self.rules, "Only an unconfigured rule is unknown"
        close = get_close_matches(rule, self.rules, n=1)
        hint = f"Did you mean {close[0]}? " if close else ""
        return f"nouls: there is no rule called {rule}. {hint}Run nouls rules to list every rule."

    def unsupported(self, path: Path) -> str:
        assert path.name, f"unsupported needs a file path but got {path!r}; pass the file to label"
        assert self.language_for(path) is None, "Only a file with no language is unsupported"
        extensions = sorted({ext for lang in self.languages.values() for ext in lang.extensions})
        return (
            f"nouls: {path} has no configured language, so it has no functions to lint. "
            f"Choose a file ending in {', '.join(extensions)}, "
            f"or add {path.suffix or 'its extension'} to a language in your nouls config."
        )

    def excluded(self, path: Path) -> bool:
        assert not path.is_absolute(), "Exclusion applies to paths relative to the search root"
        assert all(self.exclude), "Exclude patterns must not be empty"
        return any(fnmatch(part, pattern) for part in path.parts for pattern in self.exclude)


def matches(path: Path, patterns: list[str]) -> bool:
    assert all(patterns), "File patterns must not be empty"
    assert path.name, "Path must name a file"
    return any(fnmatch(path.name, p) or fnmatch(path.as_posix(), p) for p in patterns)


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    stack = [(merged, override)]
    while stack:
        target, source = stack.pop()
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                target[key] = dict(target[key])
                stack.append((target[key], value))
            else:
                target[key] = value
    assert set(base) <= set(merged), "Every base key must survive the merge"
    assert set(override) <= set(merged), "Every override key must reach the result"
    return merged


def find_config(start: Path) -> Path | None:
    assert start.is_absolute(), "Config search must start from an absolute path"
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                assert candidate.parent == directory, "Config must sit in a searched directory"
                return candidate
    return None


def load_config(start: Path, explicit: Path | None = None) -> Config:
    data = yaml.safe_load(files("nouls").joinpath("defaults.yaml").read_text())
    assert "rules" in data, (
        "nouls's built in defaults.yaml has no rules section, so the package is broken; "
        "restore src/nouls/defaults.yaml or reinstall nouls"
    )
    path = explicit or find_config(start.resolve())
    if path is not None:
        data = merge(data, yaml.safe_load(path.read_text()) or {})
    config = Config.model_validate(data)
    assert config.languages, (
        f"{path or 'defaults.yaml'} leaves no languages configured, so nouls has nothing to lint; "
        "add at least one entry under languages"
    )
    return config
