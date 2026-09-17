from fnmatch import fnmatch
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel
from tree_sitter_language_pack import SupportedLanguage

CONFIG_NAMES = ("nouls.yaml", "nouls.yml", ".nouls.yaml", ".nouls.yml")

Severity = Literal["error", "warning", "info", "hint"]


class Language(BaseModel):
    grammar: SupportedLanguage
    extensions: list[str]
    units: list[str]


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
    exclude: list[str]
    languages: dict[str, Language]
    rules: dict[str, Rule]

    def language_for(self, path: Path) -> str | None:
        return next(
            (name for name, lang in self.languages.items() if path.suffix in lang.extensions),
            None,
        )

    def rules_for(self, language: str, path: Path) -> dict[str, Rule]:
        return {
            name: rule
            for name, rule in self.rules.items()
            if rule.enabled
            and (rule.languages is None or language in rule.languages)
            and (rule.files is None or any(fnmatch(path.name, p) or fnmatch(path.as_posix(), p) for p in rule.files))
        }

    def excluded(self, path: Path) -> bool:
        return any(fnmatch(part, pattern) for part in path.parts for pattern in self.exclude)


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def find_config(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            if (directory / name).is_file():
                return directory / name
    return None


def load_config(start: Path, explicit: Path | None = None) -> Config:
    data = yaml.safe_load(files("nouls").joinpath("defaults.yaml").read_text())
    path = explicit or find_config(start.resolve())
    if path is not None:
        data = merge(data, yaml.safe_load(path.read_text()) or {})
    return Config.model_validate(data)
