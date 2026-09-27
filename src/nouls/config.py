# Copyright 2026 Ben O'Mahony
# SPDX-License-Identifier: MIT
"""Load and merge nouls configuration, and select the rules for a file or project."""

from difflib import get_close_matches
from fnmatch import fnmatch
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, cast

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from tree_sitter_language_pack import (
    SupportedLanguage,
    detect_language_from_path,
    get_tags_query,
)

from nouls.metrics import FILE_METRICS, Metric, check_for
from nouls.store import default_path

CONFIG_NAMES = ("nouls.yaml", "nouls.yml", ".nouls.yaml", ".nouls.yml")
DEFAULTS = "defaults.yaml"
CATALOGUE = "catalogue.yaml"  # One rule per entry of the engineering error catalogue.

Severity = Literal["error", "warning", "info", "hint"]
Scope = Literal["function", "file", "project", "setting"]
type Tree = dict[str, object]
TAG_KINDS = ("definition.function", "definition.method")
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Pattern = Annotated[str, Field(min_length=1)]


class ConfigError(ValueError):
    """A nouls config file cannot be read, or does not describe a valid configuration."""

    def __init__(self, source: str, problem: str) -> None:
        """Say which file is wrong, what is wrong with it and how to fix it.

        Args:
            source: The config file, or ``defaults.yaml`` for the built in settings.
            problem: What is wrong and how to fix it, continuing a sentence about the file.

        """
        super().__init__(f"nouls: {source} {problem}")
        assert source, "ConfigError needs the file it is about"
        assert str(self).startswith("nouls: "), "Errors start with nouls: so users know the source"


class StoreIsDirectoryError(ConfigError):
    """The store setting names a directory, where nouls needs a SQLite file."""

    def __init__(self, path: Path) -> None:
        """Say which directory was given and what to set instead.

        Args:
            path: The directory the store setting names.

        """
        super().__init__(
            "the nouls config",
            f"sets store to {path}, which is a directory. "
            "Set store to a file path, such as ~/.cache/nouls/nouls.db.",
        )
        assert str(path) in str(self), "The error must name the directory"
        assert "Set store" in str(self), "The error must say how to fix the setting"


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

    @model_validator(mode="after")
    def finds_functions(self) -> "Language":
        """Reject languages nouls cannot find functions in, or whose units wrap themselves.

        Returns:
            The language, unchanged.

        Raises:
            ValueError: When there is no way to find functions, or a node type is both a unit
                and attached to one.

        """
        both = sorted(set(self.units) & set(self.attached))
        if not self.units and not has_function_tags(self.grammar):
            message = (
                f"nouls cannot find functions in {self.grammar}, because its grammar has no "
                "function tags; list the node types of its functions under units"
            )
            raise ValueError(message)
        if both:
            message = f"{', '.join(both)} is listed in both units and attached; remove it from one"
            raise ValueError(message)
        assert self.units or has_function_tags(self.grammar), "Functions can be found"
        assert not both, "No node type is both a unit and attached to one"
        return self


class MissingProjectFilesError(ValueError):
    """A rule with scope: project or setting lists no files to read."""

    def __init__(self) -> None:
        """Explain that the rule needs files and how to add them."""
        super().__init__(
            "a rule with scope: project or setting has no files, so there is nothing to read; "
            "add files with globs relative to the project root, such as [pyproject.toml]"
        )
        assert self.args, "The error must carry its message for pydantic to show"
        assert "add files" in str(self), "The error must say how to fix the rule"


class InvalidDetectorError(ValueError):
    """A rule does not have exactly one way to decide whether it fires."""

    def __init__(self, problem: str) -> None:
        """Explain what is wrong with how the rule decides and how to fix it.

        Args:
            problem: What is wrong and what to change.

        """
        super().__init__(
            f"{problem}; a rule needs either a question, or a metric with a limit, such as "
            "metric: parameters and limit: 7"
        )
        assert problem, "The error must say what is wrong"
        assert "metric:" in str(self), "The error must show how to write a metric rule"


class Rule(BaseModel):
    """One yes/no question or one measurement, and how to report a finding.

    A rule with a ``question`` asks TypeSafe. A rule with a ``metric`` measures the syntax tree and
    fires when the measurement is above ``limit``. ``lines`` measures a file, so it needs
    ``scope: file``; every other metric measures a function.
    """

    question: str | None = None
    metric: Metric | None = None
    limit: int | None = None
    message: str
    severity: Severity = "warning"
    threshold: Probability | None = None
    languages: list[str] | None = None
    files: list[Pattern] | None = None
    scope: Scope = "function"
    enabled: bool = True

    @model_validator(mode="after")
    def project_rules_name_their_files(self) -> "Rule":
        """Reject project and setting rules that have no files to read.

        Returns:
            The rule, unchanged.

        Raises:
            MissingProjectFilesError: When ``scope`` is not ``function`` and ``files`` is empty.

        """
        assert self.scope in {"function", "file", "project", "setting"}, (
            f"Rule scope is {self.scope!r}; pydantic must restrict it to the Scope literal"
        )
        if self.scope in {"project", "setting"} and not self.files:
            raise MissingProjectFilesError
        assert self.scope in {"function", "file"} or self.files, (
            "A project or setting rule passed validation without files; the check must raise first"
        )
        return self

    @model_validator(mode="after")
    def decides_one_way(self) -> "Rule":
        """Reject rules without exactly one of a question or a metric, or with a bad limit.

        Returns:
            The rule, unchanged.

        Raises:
            InvalidDetectorError: When the rule cannot decide, or could decide two ways.

        """
        problem = self.question_problem() or self.metric_problem()
        if problem:
            raise InvalidDetectorError(problem)
        assert (self.question is None) != (self.metric is None), "A rule decides exactly one way"
        assert self.metric is None or self.limit is not None, "A metric rule has a limit"
        return self

    def question_problem(self) -> str | None:
        """Find what is wrong with whether the rule asks or measures.

        Returns:
            The problem, or None when the rule has exactly one usable way to decide.

        """
        problem = None
        if (self.question is None) == (self.metric is None):
            problem = "the rule has both a question and a metric, or neither"
        elif self.question is not None and not self.question.strip():
            problem = "the rule's question is blank"
        elif self.metric is None and self.limit is not None:
            problem = "the rule has a limit but no metric to compare it with"
        assert problem is None or problem.startswith("the rule"), "Problems describe the rule"
        assert problem or self.question is not None or self.metric is not None, "It can decide"
        return problem

    def metric_problem(self) -> str | None:
        """Find what is wrong with a metric rule's limit or scope.

        Returns:
            The problem, or None for a question rule or a well formed metric rule.

        """
        if self.metric is None:
            return None
        needed = "file" if self.metric in FILE_METRICS else "function"
        problem = None
        if self.limit is None or self.limit < 0:
            problem = f"metric {self.metric} needs a limit of 0 or more"
        elif self.scope != needed:
            problem = f"metric {self.metric} measures a {needed}, so the rule needs scope: {needed}"
        assert problem is None or self.metric in problem, "Problems name the metric"
        assert needed in {"file", "function"}, "Metrics measure files or functions"
        return problem

    def describe_check(self) -> str:
        """Say how the rule decides.

        Returns:
            The question, or the metric and its limit.

        """
        text = self.question
        if self.metric is not None and self.limit is not None:
            text = check_for(self.metric, self.limit)
        assert text, "The validator guarantees a question or a metric with a limit"
        assert "?" in text, "A rule's check asks a question, perhaps followed by an instruction"
        assert self.question is None or text == self.question, "Questions are shown as written"
        return text


class Config(BaseModel):
    """The merged nouls configuration."""

    model: Annotated[str, Field(min_length=1)]
    threshold: Probability
    concurrency: Annotated[int, Field(gt=0)]
    debounce_ms: Annotated[int, Field(ge=0)]
    show_probability: bool
    lint_on: Literal["change", "save"]
    store: Path | None = None
    exclude: list[Pattern]
    test_files: list[Pattern] = []
    languages: Annotated[dict[str, Language], Field(min_length=1)]
    rules: dict[str, Rule]

    def store_path(self) -> Path:
        """Find the SQLite file that holds answers and labels.

        Returns:
            The configured store, or the default under the user's cache directory.

        Raises:
            StoreIsDirectoryError: When ``store`` names a directory.

        """
        path = self.store.expanduser() if self.store else default_path()
        if not path.name or path.is_dir():
            raise StoreIsDirectoryError(path)
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

    def rules_for(
        self, language: str, path: Path, scope: Literal["function", "file"] = "function"
    ) -> dict[str, Rule]:
        """Select the function or file rules that apply to a file.

        Args:
            language: The file's language.
            path: The file, matched against each rule's ``files``.
            scope: Whether to select rules about each function or about the whole file.

        Returns:
            The enabled rules with that scope for this language and file, by name.

        """
        assert language, "rules_for needs a language name; get one from language_for(path)"
        selected = {
            name: rule
            for name, rule in self.rules.items()
            if rule.enabled
            and rule.scope == scope
            and (rule.languages is None or language in rule.languages)
            and (rule.files is None or matches(path, rule.files))
        }
        assert all(rule.enabled for rule in selected.values()), (
            "rules_for selected a disabled rule; the filter above must check rule.enabled"
        )
        return selected

    def scoped_rules(self, scope: Scope) -> dict[str, Rule]:
        """Select the enabled rules with one scope.

        Args:
            scope: ``project`` or ``setting``; function and file rules come from rules_for.

        Returns:
            Every enabled rule with that scope, by name.

        """
        assert scope in {"project", "setting"}, (
            "Select function and file rules with rules_for, which checks languages"
        )
        selected = {
            name: rule for name, rule in self.rules.items() if rule.enabled and rule.scope == scope
        }
        assert all(rule.files for rule in selected.values()), (
            f"A {scope} rule has no files; Rule's validator must reject it"
        )
        return selected

    def file_patterns(self) -> list[list[str]]:
        """List the file globs of every enabled project and setting rule.

        Returns:
            Each rule's globs, relative to the project root.

        """
        found = [
            rule.files or []
            for scope in ("project", "setting")
            for rule in self.scoped_rules(scope).values()
        ]
        assert all(found), "Project and setting rules always have files"
        assert len(found) <= len(self.rules), "Each rule gives at most one list of globs"
        return found

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
    tree = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in tree):
        return None
    assert all(isinstance(key, str) for key in tree), "Only maps with text keys are settings"
    assert tree is value, "as_tree must return the same map, not a copy"
    return cast("Tree", tree)


def load_yaml(text: str, source: str) -> Tree:
    """Parse a YAML config file into a map of settings.

    Args:
        text: The file's text.
        source: The file's name, for error messages.

    Returns:
        The file's settings, or an empty map for an empty file.

    Raises:
        ConfigError: When the text is not YAML, or its top level is not a map of settings.

    """
    try:
        loaded = cast("object", yaml.safe_load(text))
    except yaml.YAMLError as error:
        mark = error.problem_mark if isinstance(error, yaml.MarkedYAMLError) else None
        where = f" on line {mark.line + 1}" if mark else ""
        problem = error.problem if isinstance(error, yaml.MarkedYAMLError) else None
        raise ConfigError(
            source,
            f"is not valid YAML{where}: {problem or error}. Fix the syntax on that line.",
        ) from error
    tree = {} if loaded is None else as_tree(loaded)
    if tree is None:
        raise ConfigError(
            source,
            f"must be a map of settings, but its top level is a {type(loaded).__name__}. "
            "Start it with keys such as rules:, and quote keys that are numbers or booleans.",
        )
    assert loaded is None or tree is loaded, "load_yaml must return the parsed map itself"
    assert isinstance(tree, dict), "load_yaml returns a map of settings"
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


def load_builtin(name: str) -> Tree:
    """Load one of the config files that ship with nouls.

    Args:
        name: The file's name inside the nouls package, such as ``defaults.yaml``.

    Returns:
        The file's settings.

    """
    assert name in {DEFAULTS, CATALOGUE}, f"{name} is not a config file that ships with nouls"
    data = load_yaml(files("nouls").joinpath(name).read_text(encoding="utf-8"), name)
    assert "rules" in data, (
        f"nouls's built in {name} has no rules section, so the package is broken; "
        f"restore src/nouls/{name} or reinstall nouls"
    )
    return data


def load_config(start: Path, explicit: Path | None = None) -> Config:
    """Load the defaults and catalogue rules merged with the project's configuration.

    Args:
        start: The directory to search up from for a config file.
        explicit: A config file to use instead of searching.

    Returns:
        The validated configuration.

    Raises:
        ConfigError: When the config file cannot be read, is not YAML, or has invalid settings.

    """
    data = merge(load_builtin(DEFAULTS), load_builtin(CATALOGUE))
    assert "rules" in data, "Merging the built in files must keep their rules section"
    path = explicit or find_config(start.resolve())
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(
                str(path),
                f"cannot be read: {error.strerror or error}. Check the path passed to --config.",
            ) from error
        data = merge(data, load_yaml(text, str(path)))
    try:
        config = Config.model_validate(data)
    except ValidationError as error:
        raise ConfigError(str(path or DEFAULTS), invalid_settings(error)) from error
    assert config.languages, "Config requires at least one language"
    return config


def invalid_settings(error: ValidationError) -> str:
    """Describe every invalid setting in a config file, by where it sits.

    Args:
        error: What pydantic found wrong.

    Returns:
        The rest of a sentence about the file, naming each setting and what is wrong with it.

    """
    problems = [
        f"{'.'.join(map(str, detail['loc'])) or 'the top level'}: "
        f"{detail['msg'].removeprefix('Value error, ')}"
        for detail in error.errors(include_url=False)
    ]
    assert problems, "A validation error names at least one problem"
    text = (
        f"has {len(problems)} invalid setting{'s' if len(problems) > 1 else ''}: "
        f"{'; '.join(problems)}. Fix them, then run nouls rules to check the config loads."
    )
    assert text.endswith("loads."), "The message ends by saying how to check the fix"
    return text
