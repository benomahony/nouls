# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Load and merge nouls configuration, and select the rules for a file or project."""

from difflib import get_close_matches
from fnmatch import fnmatch
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import BaseModel, model_validator
from tree_sitter_language_pack import (
    SupportedLanguage,
    detect_language_from_path,
    get_tags_query,
)

from nouls.store import default_path

CONFIG_NAMES = ("nouls.yaml", "nouls.yml", ".nouls.yaml", ".nouls.yml")

Severity = Literal["error", "warning", "info", "hint"]
Scope = Literal["function", "project"]
type Tree = dict[str, object]
TAG_KINDS = ("definition.function", "definition.method")


class Calls(BaseModel):
    """Which calls in test files become units, such as ``it`` and ``beforeEach``."""

    node: str
    callee: str
    names: list[str]


class Language(BaseModel):
    """How to parse a language and which nodes are functions.

    Languages are detected from file names, so only settings that differ from the defaults need
    configuring. ``extensions`` adds file endings detection does not know. ``units`` lists the
    node types that are functions; when empty, the grammar's tags query finds them.
    """

    grammar: SupportedLanguage
    extensions: list[str] = []
    units: list[str] = []
    attached: list[str] = []
    calls: Calls | None = None


class MissingProjectFilesError(ValueError):
    """A rule with scope: project lists no files to read."""

    def __init__(self) -> None:
        """Explain that the rule needs files and how to add them."""
        super().__init__(
            "a rule with scope: project has no files, so there is nothing to read; "
            "add files with globs relative to the project root, such as [pyproject.toml]"
        )
        assert self.args, "The error must carry its message for pydantic to show"
        assert "add files" in str(self), "The error must say how to fix the rule"


class Rule(BaseModel):
    """One yes/no question, and how to report a yes."""

    question: str
    message: str
    severity: Severity = "warning"
    threshold: float | None = None
    languages: list[str] | None = None
    files: list[str] | None = None
    scope: Scope = "function"
    enabled: bool = True

    @model_validator(mode="after")
    def project_rules_name_their_files(self) -> "Rule":
        """Reject project rules that have no files to read.

        Returns:
            The rule, unchanged.

        Raises:
            MissingProjectFilesError: When ``scope`` is ``project`` and ``files`` is empty.

        """
        assert self.scope in {"function", "project"}, (
            f"Rule scope is {self.scope!r}; pydantic must restrict it to function or project"
        )
        if self.scope == "project" and not self.files:
            raise MissingProjectFilesError
        assert self.scope == "function" or self.files, (
            "A project rule passed validation without files; the check above must raise first"
        )
        return self


class Config(BaseModel):
    """The merged nouls configuration."""

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
        """Find the SQLite file that holds answers and labels.

        Returns:
            The configured store, or the default under the user's cache directory.

        """
        path = self.store.expanduser() if self.store else default_path()
        assert path.name, "Store path must name a file"
        assert not path.is_dir(), "Store path must not be a directory"
        return path

    def language_for(self, path: Path) -> str | None:
        """Find the configured language for a file.

        Args:
            path: The file to look up.

        Returns:
            A configured language whose extensions include the file's, otherwise the language
            detected from the file name when nouls can find functions in it, or None.

        """
        assert path.name, f"language_for needs a file path but got {path!r}; pass a file"
        configured = next(
            (name for name, lang in self.languages.items() if path.suffix in lang.extensions),
            None,
        )
        found = configured or detected_language(path)
        assert found is None or found in self.languages or has_function_tags(found), (
            f"{path} was matched to {found}, which nouls cannot find functions in; "
            "language_for must only return languages with units or function tags"
        )
        return found

    def language(self, name: str) -> Language:
        """Find how to parse a language.

        Args:
            name: A language from language_for.

        Returns:
            The configured settings, or defaults that find functions with the tags query.

        """
        assert name, "language needs a language name; get one from language_for(path)"
        found = self.languages.get(name) or Language.model_validate({"grammar": name})
        assert found.units or has_function_tags(found.grammar), (
            f"nouls cannot find functions in {name}: it has no units and no function tags; "
            f"add units under languages.{name} in your nouls config"
        )
        return found

    def is_test(self, path: Path) -> bool:
        """Tell whether a file is a test file.

        Args:
            path: The file to check.

        Returns:
            True when the file matches ``test_files``.

        """
        assert path.name, f"is_test needs a file path but got {path!r}; pass a source file path"
        assert all(self.test_files), (
            "test_files contains a blank pattern, which would match nothing; "
            "remove the empty entry from test_files in your nouls config"
        )
        return matches(path, self.test_files)

    def rules_for(self, language: str, path: Path) -> dict[str, Rule]:
        """Select the function rules that apply to a file.

        Args:
            language: The file's language.
            path: The file, matched against each rule's ``files``.

        Returns:
            The enabled function rules for this language and file, by name.

        """
        assert language, "rules_for needs a language name; get one from language_for(path)"
        selected = {
            name: rule
            for name, rule in self.rules.items()
            if rule.enabled
            and rule.scope == "function"
            and (rule.languages is None or language in rule.languages)
            and (rule.files is None or matches(path, rule.files))
        }
        assert all(rule.enabled for rule in selected.values()), (
            "rules_for selected a disabled rule; the filter above must check rule.enabled"
        )
        return selected

    def project_rules(self) -> dict[str, Rule]:
        """Select the enabled project rules.

        Returns:
            Every enabled rule with scope: project, by name.

        """
        selected = {
            name: rule
            for name, rule in self.rules.items()
            if rule.enabled and rule.scope == "project"
        }
        assert all(rule.files for rule in selected.values()), (
            "A project rule has no files; Rule's validator must reject scope: project without files"
        )
        assert all(rule.enabled for rule in selected.values()), (
            "project_rules selected a disabled rule; the filter must check rule.enabled"
        )
        return selected

    def unknown_rule(self, rule: str) -> str:
        """Explain that a rule name does not exist.

        Args:
            rule: The name the user typed.

        Returns:
            An error message with the closest rule name and how to list them all.

        """
        assert rule, "unknown_rule needs the rule name the user typed; pass it through unchanged"
        assert rule not in self.rules, "Only an unconfigured rule is unknown"
        close = get_close_matches(rule, self.rules, n=1)
        hint = f"Did you mean {close[0]}? " if close else ""
        return f"nouls: there is no rule called {rule}. {hint}Run nouls rules to list every rule."

    def unsupported(self, path: Path) -> str:
        """Explain that a file has no configured language.

        Args:
            path: The file the user asked about.

        Returns:
            An error message listing the supported extensions and how to add one.

        """
        assert path.name, f"unsupported needs a file path but got {path!r}; pass the file to label"
        assert self.language_for(path) is None, "Only a file with no language is unsupported"
        return (
            f"nouls: nouls cannot find functions in {path}, because its file type has no "
            "tree-sitter grammar with function tags. Choose a source file, or add "
            f"{path.suffix or 'its extension'} with the node types of its functions under "
            "languages in your nouls config, such as languages: {name: {grammar: name, "
            "extensions: [.ext], units: [function_definition]}}."
        )

    def excluded(self, path: Path) -> bool:
        """Tell whether a path under a search root is excluded.

        Args:
            path: The path relative to the search root.

        Returns:
            True when any part of the path matches ``exclude``.

        """
        assert not path.is_absolute(), "Exclusion applies to paths relative to the search root"
        assert all(self.exclude), "Exclude patterns must not be empty"
        return any(fnmatch(part, pattern) for part in path.parts for pattern in self.exclude)


def has_function_tags(grammar: str) -> bool:
    """Tell whether a grammar's tags query marks functions or methods.

    Args:
        grammar: A tree-sitter-language-pack name.

    Returns:
        True when the tags query has a function or method definition capture.

    """
    assert grammar, "has_function_tags needs a grammar name, such as python"
    query = get_tags_query(grammar) or ""
    found = any(f"@{kind}" in query for kind in TAG_KINDS)
    assert found or "@definition.function" not in query, "TAG_KINDS must include functions"
    return found


def detected_language(path: Path) -> str | None:
    """Detect a file's language from its name, when nouls can find functions in it.

    Args:
        path: The file.

    Returns:
        The tree-sitter-language-pack name, or None for unknown files and data formats.

    """
    assert path.name, f"detected_language needs a file path but got {path!r}"
    name = detect_language_from_path(str(path))
    found = name if name and has_function_tags(name) else None
    assert found is None or found == name, "detected_language must not rename languages"
    return found


def matches(path: Path, patterns: list[str]) -> bool:
    """Tell whether a file matches any glob by name or path.

    Args:
        path: The file to check.
        patterns: The globs to try.

    Returns:
        True when the file's name or path matches one of the globs.

    """
    assert all(patterns), "File patterns must not be empty"
    assert path.name, "Path must name a file"
    return any(fnmatch(path.name, p) or fnmatch(path.as_posix(), p) for p in patterns)


def as_tree(value: object) -> Tree | None:
    """Treat a parsed YAML value as a map of settings when it is one.

    Args:
        value: Any value parsed from YAML.

    Returns:
        The value as a map with string keys, or None when it is not a map.

    """
    if not isinstance(value, dict):
        return None
    tree = cast("Tree", value)
    assert all(isinstance(key, str) for key in tree), (
        f"A nouls config map has a key that is not text, in {sorted(map(repr, tree))}; "
        "quote numeric or boolean keys in the YAML"
    )
    assert tree is value, "as_tree must return the same map, not a copy"
    return tree


def load_yaml(text: str, source: str) -> Tree:
    """Parse a YAML config file into a map of settings.

    Args:
        text: The file's text.
        source: The file's name, for error messages.

    Returns:
        The file's settings, or an empty map for an empty file.

    """
    loaded = cast("object", yaml.safe_load(text))
    tree = {} if loaded is None else as_tree(loaded)
    assert tree is not None, (
        f"{source} must be a YAML map of settings, but its top level is a "
        f"{type(loaded).__name__}; start it with keys such as rules:"
    )
    assert loaded is None or tree is loaded, "load_yaml must return the parsed map itself"
    return tree


def merge(base: Tree, override: Tree) -> Tree:
    """Merge configuration maps key by key.

    Args:
        base: The defaults.
        override: The project's configuration.

    Returns:
        A new map with every override applied, nested maps merged recursively.

    """
    merged = dict(base)
    stack: list[tuple[Tree, Tree]] = [(merged, override)]
    while stack:
        target, source = stack.pop()
        for key, value in source.items():
            nested, existing = as_tree(value), as_tree(target.get(key))
            if nested is not None and existing is not None:
                copy = dict(existing)
                target[key] = copy
                stack.append((copy, nested))
            else:
                target[key] = value
    assert set(base) <= set(merged), "Every base key must survive the merge"
    assert set(override) <= set(merged), "Every override key must reach the result"
    return merged


def find_root(start: Path) -> Path:
    """Find the project root above a directory.

    Args:
        start: An absolute directory to search up from.

    Returns:
        The nearest directory with a ``.git`` or nouls config, or ``start`` when none has.

    """
    assert start.is_absolute(), (
        f"find_root needs an absolute path but got {start}; resolve it first"
    )
    for directory in [start, *start.parents]:
        if (directory / ".git").exists() or any((directory / n).is_file() for n in CONFIG_NAMES):
            return directory
    assert start.is_dir(), f"{start} is not a directory; pass the directory to start from"
    return start


def project_files(root: Path, patterns: list[str]) -> list[Path]:
    """Find the files matching a project rule's globs.

    Args:
        root: The project root.
        patterns: Globs relative to the root.

    Returns:
        Each matching file once, in the order of the globs.

    """
    assert root.is_dir(), f"Project root {root} is not a directory; pass the result of find_root"
    assert all(patterns), "A project rule has a blank file pattern; remove it from files"
    found = [path for pattern in patterns for path in sorted(root.glob(pattern)) if path.is_file()]
    return list(dict.fromkeys(found))


def find_config(start: Path) -> Path | None:
    """Find the nearest nouls config file.

    Args:
        start: An absolute directory to search up from.

    Returns:
        The first nouls.yaml, nouls.yml, .nouls.yaml or .nouls.yml found, or None.

    """
    assert start.is_absolute(), "Config search must start from an absolute path"
    for directory in [start, *start.parents]:
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                assert candidate.parent == directory, "Config must sit in a searched directory"
                return candidate
    return None


def load_config(start: Path, explicit: Path | None = None) -> Config:
    """Load the defaults merged with the project's configuration.

    Args:
        start: The directory to search up from for a config file.
        explicit: A config file to use instead of searching.

    Returns:
        The validated configuration.

    """
    data = load_yaml(
        files("nouls").joinpath("defaults.yaml").read_text(encoding="utf-8"), "defaults.yaml"
    )
    assert "rules" in data, (
        "nouls's built in defaults.yaml has no rules section, so the package is broken; "
        "restore src/nouls/defaults.yaml or reinstall nouls"
    )
    path = explicit or find_config(start.resolve())
    if path is not None:
        data = merge(data, load_yaml(path.read_text(encoding="utf-8"), str(path)))
    config = Config.model_validate(data)
    assert config.languages, (
        f"{path or 'defaults.yaml'} leaves no languages configured, so nouls has nothing to lint; "
        "add at least one entry under languages"
    )
    return config
