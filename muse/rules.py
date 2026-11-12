"""Routing rules: which model alias each kind of task uses (rule 2, "fit the task").

Why a file: the user decides how strong a model each kind of work deserves,
and must be able to read and change that without code. config/routing.yaml
(or MUSE_ROUTING) is validated strictly; a broken file never stops the proxy:
the built-in rules below are used instead and the problem is reported once.
The file is re-read when it changes (mtime and size), so an edit takes effect
on the next request without a restart.

Who reads it:
- muse/hook.py, when a request carries `x-muse-task: <task>` or asks for the
  model `auto`: the model is replaced by the task's alias, the task's
  max_output_tokens becomes a ceiling, and `handoff: suggest` marks a frontier
  task (rule 6).
- `muse rules` (prints the rules as sentences), `muse rules check`, and
  `muse ask/queue add --task`.

Hints look only at a task NAME the caller sends (e.g. `x-muse-task:
classify-emails`), never at prompt content.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from muse import paths

DEFAULT_PATH = paths.REPO_ROOT / "config" / "routing.yaml"
MODES = ("chat", "embedding", "rerank")
TASK_KEYS = ("alias", "mode", "max_output_tokens", "long_input_alias", "long_input_over_tokens", "handoff",
             "deterministic", "examples")
TOP_KEYS = ("version", "tasks", "defaults", "apps", "hints")
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ALIAS = re.compile(r"[A-Za-z0-9._:/-]{1,80}")
_APP = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_WORD = re.compile(r"[a-z0-9]+")
MAX_TOKENS_CEILING = 1_000_000

# The same rules as the shipped config/routing.yaml (a test keeps them equal).
DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "tasks": {
        "light": {"alias": "fast", "max_output_tokens": 300, "deterministic": True,
                  "examples": ["classify", "extract", "tag", "short rewrite"]},
        "everyday": {"alias": "cheap", "max_output_tokens": 1200, "examples": ["chat", "summarise", "draft", "translate"]},
        "heavy": {"alias": "smart", "max_output_tokens": 4000, "long_input_alias": "long-context",
                  "long_input_over_tokens": 60000, "examples": ["long documents", "multi-step research", "synthesis"]},
        "frontier": {"alias": "smart", "handoff": "suggest", "examples": ["questions where being wrong is costly"]},
        "embed": {"alias": "embed", "mode": "embedding"},
        "rerank": {"alias": "rerank", "mode": "rerank"},
    },
    "defaults": {"task": "everyday"},
    "apps": {"queue": {"default_task": "light"}},
    "hints": {
        "light": ["classify", "extract", "tag", "label", "rewrite"],
        "everyday": ["summarise", "summarize", "translate", "draft", "chat"],
        "heavy": ["research", "synthesis", "synthesise", "report"],
    },
}


class RulesError(ValueError):
    """The rules file is not valid. `problems` lists every problem, each one line."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class UnknownTask(ValueError):
    """A task name that is neither a task in the rules nor matched by a hint."""

    def __init__(self, name: str, valid: list[str]) -> None:
        self.name = name
        self.valid = valid
        super().__init__(f"unknown task {name!r}; valid tasks: {', '.join(valid)}")


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    alias: str
    mode: str = "chat"
    max_output_tokens: int | None = None
    long_input_alias: str | None = None
    long_input_over_tokens: int | None = None
    handoff: bool = False
    deterministic: bool = False
    examples: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Route:
    """What a request for a task becomes. `reason` is one plain sentence."""

    alias: str
    max_output_tokens: int | None
    handoff: bool
    reason: str
    task: str = ""
    mode: str = "chat"
    deterministic: bool = False


@dataclass(frozen=True)
class Rules:
    tasks: Mapping[str, Task]
    default_task: str
    apps: Mapping[str, str] = field(default_factory=dict)  # app -> default task
    hints: tuple[tuple[str, str], ...] = ()  # (normalised phrase, task), file order
    source: str = "built-in"
    problems: tuple[str, ...] = ()  # why the file was not used (defaults are in force)

    @property
    def valid_tasks(self) -> list[str]:
        return list(self.tasks)

    @property
    def handoff_aliases(self) -> frozenset[str]:
        """Aliases of tasks that suggest the hand-off: the 'strong' allowance."""
        return frozenset(t.alias for t in self.tasks.values() if t.handoff)

    def hint_for(self, name: str) -> str | None:
        """The task a free-form task NAME points to through `hints`, if any."""
        text = f" {' '.join(_WORD.findall(str(name).lower()))} "
        for phrase, task in self.hints:
            if f" {phrase} " in text:
                return task
        return None

    def default_for(self, app: str | None) -> tuple[str, str]:
        """(task, why) used when a request names no task."""
        if app and app in self.apps:
            return self.apps[app], f"app '{app}' defaults to task '{self.apps[app]}'"
        return self.default_task, f"no task given; the default task is '{self.default_task}'"

    def resolve(self, task: str | None, *, app: str | None = None, input_tokens_estimate: int | None = None) -> Route:
        """The route for a task name (None = the app's or the global default). Raises UnknownTask."""
        raw = (task or "").strip().lower()
        if not raw:
            name, why = self.default_for(app)
        elif raw in self.tasks:
            name, why = raw, f"task '{raw}'"
        else:
            hinted = self.hint_for(raw)
            if hinted is None:
                raise UnknownTask(raw, self.valid_tasks)
            name, why = hinted, f"task name '{raw}' matches a hint for '{hinted}'"
        spec = self.tasks[name]
        alias = spec.alias
        if (spec.long_input_alias and spec.long_input_over_tokens is not None and input_tokens_estimate is not None
                and input_tokens_estimate > spec.long_input_over_tokens):
            alias = spec.long_input_alias
            why += f"; the input (about {input_tokens_estimate} tokens) is over {spec.long_input_over_tokens}, so alias '{alias}'"
        else:
            why += f"; alias '{alias}'"
        if spec.max_output_tokens:
            why += f", at most {spec.max_output_tokens} output tokens"
        if spec.handoff:
            why += "; hand-off to your own subscription app is suggested"
        return Route(alias, spec.max_output_tokens, spec.handoff, why, name, spec.mode, spec.deterministic)


# ---------------------------------------------------------------- parsing


def _pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_TOKENS_CEILING


def parse_rules(data: Any, source: str = "built-in") -> Rules:
    """Validate a loaded YAML document. Raises RulesError listing every problem."""
    problems: list[str] = []
    if not isinstance(data, Mapping):
        raise RulesError([f"{source}: the file must be a mapping with a 'tasks:' section"])
    for key in data:
        if key not in TOP_KEYS:
            problems.append(f"unknown top-level key {key!r} (allowed: {', '.join(TOP_KEYS)})")
    if "version" in data and data["version"] != 1:
        problems.append(f"version: must be 1 (got {data['version']!r})")
    raw_tasks = data.get("tasks")
    tasks: dict[str, Task] = {}
    if not isinstance(raw_tasks, Mapping) or not raw_tasks:
        problems.append("tasks: must be a mapping with at least one task")
        raw_tasks = {}
    for name, spec in raw_tasks.items():
        where = f"tasks.{name}"
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            problems.append(f"{where}: a task name is lower-case letters, digits, '-' or '_' (at most 32)")
            continue
        if not isinstance(spec, Mapping):
            problems.append(f"{where}: must be a mapping such as {{alias: cheap}}")
            continue
        for key in spec:
            if key not in TASK_KEYS:
                problems.append(f"{where}: unknown key {key!r} (allowed: {', '.join(TASK_KEYS)})")
        alias = spec.get("alias")
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            problems.append(f"{where}.alias: required, a model alias such as cheap or smart (got {alias!r})")
        mode = spec.get("mode", "chat")
        if mode not in MODES:
            problems.append(f"{where}.mode: one of {', '.join(MODES)} (got {mode!r})")
        mot = spec.get("max_output_tokens")
        if mot is not None and not _pos_int(mot):
            problems.append(f"{where}.max_output_tokens: a whole number from 1 to {MAX_TOKENS_CEILING} (got {mot!r})")
        if mot is not None and mode != "chat":
            problems.append(f"{where}.max_output_tokens: only for chat tasks (this one is {mode})")
        lia, lio = spec.get("long_input_alias"), spec.get("long_input_over_tokens")
        if (lia is None) != (lio is None):
            problems.append(f"{where}: long_input_alias and long_input_over_tokens go together")
        if lia is not None and (not isinstance(lia, str) or not _ALIAS.fullmatch(lia)):
            problems.append(f"{where}.long_input_alias: a model alias such as long-context (got {lia!r})")
        if lio is not None and not _pos_int(lio):
            problems.append(f"{where}.long_input_over_tokens: a whole number from 1 to {MAX_TOKENS_CEILING} (got {lio!r})")
        handoff = spec.get("handoff", "none")
        if handoff is True:
            handoff = "suggest"
        elif handoff is False or handoff is None:
            handoff = "none"
        if handoff not in ("suggest", "none"):
            problems.append(f"{where}.handoff: 'suggest' or 'none' (got {handoff!r})")
        det = spec.get("deterministic", False)
        if not isinstance(det, bool):
            problems.append(f"{where}.deterministic: true or false (got {det!r})")
        examples = spec.get("examples", [])
        if not isinstance(examples, list) or not all(isinstance(e, str) and e.strip() for e in examples):
            problems.append(f"{where}.examples: a list of short phrases")
            examples = []
        tasks[name] = Task(name, str(alias), str(mode), mot if _pos_int(mot) else None,
                           lia if isinstance(lia, str) else None, lio if _pos_int(lio) else None,
                           handoff == "suggest", det is True, tuple(e.strip() for e in examples))

    defaults = data.get("defaults") or {}
    default_task = "everyday" if "everyday" in tasks else (next(iter(tasks)) if tasks else "")
    if not isinstance(defaults, Mapping):
        problems.append("defaults: must be a mapping such as {task: everyday}")
    else:
        for key in defaults:
            if key != "task":
                problems.append(f"defaults: unknown key {key!r} (allowed: task)")
        if "task" in defaults:
            default_task = defaults["task"]
    if default_task not in tasks:
        problems.append(f"defaults.task: {default_task!r} is not one of the tasks ({', '.join(tasks) or 'none'})")

    app_defaults: dict[str, str] = {}
    raw_apps = data.get("apps", {}) or {}
    if not isinstance(raw_apps, Mapping):
        problems.append("apps: must be a mapping such as {queue: {default_task: light}}")
        raw_apps = {}
    for app, spec in raw_apps.items():
        where = f"apps.{app}"
        if not isinstance(app, str) or not _APP.fullmatch(app):
            problems.append(f"{where}: an app name is lower-case letters, digits, '-' or '_'")
            continue
        if not isinstance(spec, Mapping) or set(spec) - {"default_task"}:
            problems.append(f"{where}: must be {{default_task: <task>}}")
            continue
        task = spec.get("default_task")
        if task not in tasks:
            problems.append(f"{where}.default_task: {task!r} is not one of the tasks ({', '.join(tasks)})")
            continue
        app_defaults[app] = str(task)

    hints: list[tuple[str, str]] = []
    raw_hints = data.get("hints", {}) or {}
    if not isinstance(raw_hints, Mapping):
        problems.append("hints: must map a task to a list of words, such as {light: [classify, tag]}")
        raw_hints = {}
    seen: dict[str, str] = {}
    for task, words in raw_hints.items():
        where = f"hints.{task}"
        if task not in tasks:
            problems.append(f"{where}: {task!r} is not one of the tasks ({', '.join(tasks)})")
            continue
        if not isinstance(words, list) or not words:
            problems.append(f"{where}: a list of words or short phrases")
            continue
        for word in words:
            phrase = " ".join(_WORD.findall(str(word).lower())) if isinstance(word, str) else ""
            if not phrase:
                problems.append(f"{where}: {word!r} is not a word")
                continue
            if phrase in seen and seen[phrase] != task:
                problems.append(f"{where}: {word!r} is already a hint for '{seen[phrase]}'")
                continue
            seen[phrase] = task
            hints.append((phrase, str(task)))

    if problems:
        raise RulesError([f"{source}: {p}" for p in problems])
    return Rules(tasks, str(default_task), app_defaults, tuple(hints), source)


BUILTIN = parse_rules(DEFAULT_RULES)


# ---------------------------------------------------------------- loading


def rules_path(environ: Mapping[str, str] | None = None) -> Path:
    """MUSE_ROUTING, else config/routing.yaml next to the catalogue."""
    env = os.environ if environ is None else environ
    raw = str(env.get("MUSE_ROUTING", "") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_PATH


_cache: dict[str, tuple[tuple[int, int] | None, Rules]] = {}
_lock = threading.Lock()


def load_rules(environ: Mapping[str, str] | None = None) -> Rules:
    """The rules in force: the file if it is valid, else the built-in rules with
    `problems` saying why. Cached until the file's mtime or size changes."""
    path = rules_path(environ)
    try:
        st = path.stat()
        stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    key = str(path)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    rules = _read(path, stamp is not None, explicit=bool(str((os.environ if environ is None else environ).get("MUSE_ROUTING", "") or "").strip()))
    with _lock:
        _cache[key] = (stamp, rules)
    return rules


def _read(path: Path, exists: bool, explicit: bool) -> Rules:
    if not exists:
        if explicit:
            return _fallback([f"{path}: MUSE_ROUTING names a file that does not exist"])
        return BUILTIN
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return _fallback([f"{path}: cannot read the file ({type(exc).__name__})"])
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        return _fallback([f"{path}: not valid YAML{where} ({getattr(exc, 'problem', None) or 'syntax error'})"])
    try:
        return parse_rules(data, str(path))
    except RulesError as exc:
        return _fallback(exc.problems)


def _fallback(problems: list[str]) -> Rules:
    return Rules(BUILTIN.tasks, BUILTIN.default_task, BUILTIN.apps, BUILTIN.hints, "built-in", tuple(problems))


def check(path: Path) -> list[str]:
    """Problems in one file (empty = valid). For `muse rules check`."""
    if not path.exists():
        return [f"{path}: no such file"]
    rules = _read(path, True, explicit=True)
    return list(rules.problems)


def resolve(task: str | None, *, app: str | None = None, input_tokens_estimate: int | None = None,
            environ: Mapping[str, str] | None = None) -> Route:
    """Route for a task under the rules in force (see Rules.resolve)."""
    return load_rules(environ).resolve(task, app=app, input_tokens_estimate=input_tokens_estimate)


# ---------------------------------------------------------------- explaining


_TEXT = {
    "en": {
        "task": "Task '{name}'{examples} uses the alias '{alias}'",
        "examples": " (for example: {items})",
        "max": ", and an answer may use at most {n} tokens",
        "long": ". Inputs over about {n} tokens use '{alias}' instead",
        "handoff": ". For these, Muse first suggests opening your own subscription app (ChatGPT, Claude or Gemini) "
                   "and never spends money without your approval",
        "det": ". Exact repeats may be answered from the cache",
        "mode": " ({mode})",
        "default": "A request that names no task is treated as '{task}'.",
        "app": "The app '{app}' uses '{task}' unless it names a task.",
        "hints": "Words in a task name choose the task: {items}.",
        "hint": "'{word}' means {task}",
        "source": "Rules from {source}.",
        "builtin": "These are the built-in rules (no rules file is in use).",
        "problem": "The rules file could not be used, so the built-in rules are in force: {problem}",
    },
    "zh": {
        "task": "「{name}」類工作{examples}使用別名「{alias}」",
        "examples": "（例如：{items}）",
        "max": "，回答最多 {n} 個 token",
        "long": "。輸入超過約 {n} 個 token 時改用「{alias}」",
        "handoff": "。這類問題 Muse 會先建議你打開自己訂閱的 App（ChatGPT、Claude 或 Gemini），未經你同意絕不花錢",
        "det": "。完全相同的請求可能直接用快取回答",
        "mode": "（{mode}）",
        "default": "沒有指定工作類型的請求一律當作「{task}」。",
        "app": "應用程式「{app}」沒有指定工作類型時使用「{task}」。",
        "hints": "工作名稱中的字詞決定類型：{items}。",
        "hint": "「{word}」→ {task}",
        "source": "規則來源：{source}。",
        "builtin": "目前使用內建規則（沒有使用規則檔）。",
        "problem": "規則檔無法使用，目前改用內建規則：{problem}",
    },
}


def explain(rules: Rules | None = None, lang: str = "en") -> list[str]:
    """The rules as plain sentences, in English ('en') or Traditional Chinese ('zh')."""
    rules = rules or load_rules()
    t = _TEXT["zh" if str(lang).lower().startswith("zh") else "en"]
    sep = "、" if t is _TEXT["zh"] else ", "
    lines: list[str] = []
    for task in rules.tasks.values():
        examples = t["examples"].format(items=sep.join(task.examples)) if task.examples else ""
        line = t["task"].format(name=task.name, examples=examples, alias=task.alias)
        if task.mode != "chat":
            line += t["mode"].format(mode=task.mode)
        if task.max_output_tokens:
            line += t["max"].format(n=f"{task.max_output_tokens:,}")
        if task.long_input_alias and task.long_input_over_tokens:
            line += t["long"].format(n=f"{task.long_input_over_tokens:,}", alias=task.long_input_alias)
        if task.handoff:
            line += t["handoff"]
        if task.deterministic:
            line += t["det"]
        lines.append(line + ("。" if t is _TEXT["zh"] else "."))
    lines.append(t["default"].format(task=rules.default_task))
    for app, task in rules.apps.items():
        lines.append(t["app"].format(app=app, task=task))
    if rules.hints:
        lines.append(t["hints"].format(items=sep.join(t["hint"].format(word=w, task=k) for w, k in rules.hints)))
    lines.append(t["source"].format(source=rules.source) if rules.source != "built-in" else t["builtin"])
    for problem in rules.problems:
        lines.append(t["problem"].format(problem=problem))
    return lines


def as_dict(rules: Rules) -> dict[str, Any]:
    """JSON-friendly view for `muse rules --json`."""
    return {
        "source": rules.source,
        "problems": list(rules.problems),
        "default_task": rules.default_task,
        "tasks": {
            name: {"alias": t.alias, "mode": t.mode, "max_output_tokens": t.max_output_tokens,
                   "long_input_alias": t.long_input_alias, "long_input_over_tokens": t.long_input_over_tokens,
                   "handoff": "suggest" if t.handoff else "none", "deterministic": t.deterministic, "examples": list(t.examples)}
            for name, t in rules.tasks.items()
        },
        "apps": {app: {"default_task": task} for app, task in rules.apps.items()},
        "hints": [{"words": w, "task": k} for w, k in rules.hints],
    }
