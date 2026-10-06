"""Workflow files: load, validate, render templates, evaluate conditions.

The format is a superset of the agency-orchestrator (AO) YAML schema, so AO
workflows load as they are (`role:` is accepted for `agent:`, `agents_dir` is
honoured). Additions for working on an existing project:

  project:            the target project ({path, base}); `run` may override it
  steps[].mode:       write (edits the project, runs alone) | read (analysis/review)
  steps[].workdir:    the part of the project the step owns, relative to its root
  steps[].component:  informational: the component the planner routed this for
  steps[].llm:        per-step provider settings (model, allowed_tools, ...)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .roster import Roster, UnknownAgentError

try:  # PyYAML is optional: JSON workflows always work.
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only without PyYAML
    yaml = None

STEP_TYPES = ("agent", "approval")
# AO fields this engine does not act on. They load with a warning instead of
# failing, so AO workflows stay usable; media steps (image/video/tts/concat)
# are a different kind of work and are rejected by type instead.
AO_IGNORED_FIELDS = ("emoji", "skill", "skills", "verify", "assert")
AO_UNSUPPORTED_TYPES = ("image", "video", "tts", "concat", "human_input")
STEP_MODES = ("write", "read")
DEPENDS_MODES = ("all", "any_completed")
# Values the runner provides to every template without a step producing them.
BUILTIN_VARS = frozenset({"project_path", "project_name", "run_id", "base_ref", "branch"})
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class WorkflowError(ValueError):
    """A workflow that cannot run, with every problem found listed."""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("\n".join(f"- {p}" for p in self.problems))


@dataclass
class Step:
    id: str
    task: str = ""
    agent: str = ""
    type: str = "agent"
    mode: str = "read"
    output: str | None = None
    depends_on: list[str] = field(default_factory=list)
    depends_on_mode: str = "all"
    condition: str | None = None
    workdir: str | None = None
    component: str | None = None
    prompt: str | None = None
    acceptance: str | None = None
    name: str | None = None
    llm: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id}
        if self.type != "agent":
            d["type"] = self.type
        if self.agent:
            d["agent"] = self.agent
        if self.name:
            d["name"] = self.name
        d["mode"] = self.mode
        if self.component:
            d["component"] = self.component
        if self.workdir:
            d["workdir"] = self.workdir
        if self.depends_on:
            d["depends_on"] = list(self.depends_on)
        if self.depends_on_mode != "all":
            d["depends_on_mode"] = self.depends_on_mode
        if self.condition:
            d["condition"] = self.condition
        if self.task:
            d["task"] = self.task
        if self.prompt:
            d["prompt"] = self.prompt
        if self.acceptance:
            d["acceptance"] = self.acceptance
        if self.output:
            d["output"] = self.output
        if self.llm:
            d["llm"] = dict(self.llm)
        return d


@dataclass
class Workflow:
    name: str
    steps: list[Step]
    description: str = ""
    project: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    concurrency: int = 2
    inputs: list[dict[str, Any]] = field(default_factory=list)
    agents_dir: str | None = None
    source: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name}
        if self.description:
            d["description"] = self.description
        if self.project:
            d["project"] = dict(self.project)
        if self.agents_dir:
            d["agents_dir"] = self.agents_dir
        if self.llm:
            d["llm"] = dict(self.llm)
        d["concurrency"] = self.concurrency
        if self.inputs:
            d["inputs"] = [dict(i) for i in self.inputs]
        d["steps"] = [s.to_dict() for s in self.steps]
        return d


# --- loading -----------------------------------------------------------------

def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def from_dict(data: dict[str, Any], source: Path | None = None) -> Workflow:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise WorkflowError("a workflow must be a mapping at the top level")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError("`steps` must be a non-empty list")
    steps: list[Step] = []
    warnings: list[str] = []
    for i, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            problems.append(f"step #{i} is not a mapping")
            continue
        sid = raw.get("id", f"#{i}")
        known = {"id", "task", "agent", "role", "type", "mode", "output", "depends_on",
                 "depends_on_mode", "condition", "workdir", "component", "prompt",
                 "acceptance", "name", "llm", *AO_IGNORED_FIELDS}
        extra = sorted(set(raw) - known)
        if extra:
            problems.append(f"step '{sid}' has unknown field(s): {', '.join(extra)}")
        ignored = sorted(set(raw) & set(AO_IGNORED_FIELDS))
        if ignored:
            warnings.append(f"step '{sid}': ignoring AO field(s) {', '.join(ignored)}")
        step_type = str(raw.get("type", "agent") or "agent")
        if step_type == "normal":  # AO's spelling
            step_type = "agent"
        if step_type in AO_UNSUPPORTED_TYPES:
            problems.append(f"step '{sid}': type '{step_type}' (AO media/input step) is not supported here")
        llm = raw.get("llm") or {}
        if not isinstance(llm, dict):
            problems.append(f"step '{raw.get('id', f'#{i}')}': `llm` must be a mapping")
            llm = {}
        steps.append(Step(
            id=str(raw.get("id", "")),
            task=str(raw.get("task", "") or ""),
            agent=str(raw.get("agent") or raw.get("role") or ""),
            type=step_type,
            # Read-only unless the workflow says otherwise: nothing edits a
            # project because a field was left out.
            mode=str(raw.get("mode", "read")),
            output=(str(raw["output"]) if raw.get("output") else None),
            depends_on=_as_list(raw.get("depends_on")),
            depends_on_mode=str(raw.get("depends_on_mode", "all")),
            condition=(str(raw["condition"]) if raw.get("condition") else None),
            workdir=(str(raw["workdir"]) if raw.get("workdir") else None),
            component=(str(raw["component"]) if raw.get("component") else None),
            prompt=(str(raw["prompt"]) if raw.get("prompt") else None),
            acceptance=(str(raw["acceptance"]) if raw.get("acceptance") else None),
            name=(str(raw["name"]) if raw.get("name") else None),
            llm=dict(llm),
        ))
    if problems:
        raise WorkflowError(problems)
    try:
        concurrency = int(data.get("concurrency", 2))
    except (TypeError, ValueError):
        raise WorkflowError("`concurrency` must be an integer")
    project = data.get("project") or {}
    if isinstance(project, str):
        project = {"path": project}
    return Workflow(
        name=str(data.get("name") or (source.stem if source else "workflow")),
        description=str(data.get("description", "") or ""),
        steps=steps,
        project=dict(project),
        llm=dict(data.get("llm") or {}),
        concurrency=concurrency,
        inputs=[dict(i) for i in (data.get("inputs") or [])],
        agents_dir=(str(data["agents_dir"]) if data.get("agents_dir") else None),
        source=source,
        warnings=warnings,
    )


def parse_text(text: str, suffix: str = ".yaml") -> dict[str, Any]:
    if suffix == ".json" or text.lstrip().startswith("{"):
        return json.loads(text)
    if yaml is None:
        raise WorkflowError("PyYAML is not installed — install it (pip install pyyaml) or use a .json workflow")
    return yaml.safe_load(text)


def load(path: Path | str) -> Workflow:
    path = Path(path)
    try:
        data = parse_text(path.read_text(encoding="utf-8"), path.suffix.lower())
    except (json.JSONDecodeError, getattr(yaml, "YAMLError", ValueError)) as e:
        raise WorkflowError(f"{path}: not valid YAML/JSON: {e}") from e
    return from_dict(data, source=path)


def dump(workflow: Workflow, path: Path | str | None = None, fmt: str | None = None) -> str:
    """Serialize a workflow (YAML when PyYAML is available, else JSON)."""
    fmt = fmt or ("json" if (path and str(path).endswith(".json")) or yaml is None else "yaml")
    data = workflow.to_dict()
    if fmt == "json":
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:
        class _Dumper(yaml.SafeDumper):  # type: ignore[misc,name-defined]
            pass

        def _str(dumper, value):  # multi-line tasks stay readable as block scalars
            style = "|" if "\n" in value else None
            return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

        _Dumper.add_representer(str, _str)
        text = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


# --- templates and conditions --------------------------------------------------

def template_vars(text: str | None) -> list[str]:
    return list(dict.fromkeys(_VAR.findall(text or "")))


def render(text: str, context: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in context:
            raise WorkflowError(f"template variable {{{{{name}}}}} is not defined")
        return context[name]
    return _VAR.sub(sub, text)


_COND = re.compile(r"^(.*?)\s+(contains|equals)\s+(.+)$", re.S)


def parse_condition(condition: str) -> tuple[str, str, str]:
    """`<template> contains|equals <literal>` — the operator is found in the
    template before substitution, so a model output that happens to contain
    the word "contains" cannot move the split (AO learned this the hard way)."""
    m = _COND.match(condition.strip())
    if not m:
        raise WorkflowError(f"condition '{condition}' must read '<text> contains <word>' or '<text> equals <word>'")
    left, op, right = m.group(1), m.group(2), m.group(3).strip().strip("\"'")
    if re.search(r"(^|\s)(not|!)\s*$", left, re.I):
        raise WorkflowError(f"condition '{condition}': negation is not supported — put the condition on the other branch")
    return left, op, right


def evaluate_condition(condition: str, context: dict[str, str]) -> bool:
    left, op, right = parse_condition(condition)
    value = render(left, context).strip()
    if op == "contains":
        return right.lower() in value.lower()
    return value.lower() == right.lower()


# --- validation ----------------------------------------------------------------

def validate(workflow: Workflow, roster: Roster | None = None) -> list[str]:
    """Every problem that would stop or derail a run. Empty list = runnable."""
    from .dag import CycleError, build  # local import: dag imports workflow

    problems: list[str] = []
    ids: dict[str, Step] = {}
    for s in workflow.steps:
        if not s.id or not _ID.match(s.id):
            problems.append(f"step id '{s.id}' must be letters, digits, '-' or '_'")
        elif s.id in ids:
            problems.append(f"duplicate step id '{s.id}'")
        ids[s.id] = s
    outputs: dict[str, str] = {}
    for s in workflow.steps:
        if s.type not in STEP_TYPES:
            problems.append(f"step '{s.id}': type '{s.type}' is not one of {', '.join(STEP_TYPES)}")
        if s.mode not in STEP_MODES:
            problems.append(f"step '{s.id}': mode '{s.mode}' is not one of {', '.join(STEP_MODES)}")
        if s.depends_on_mode not in DEPENDS_MODES:
            problems.append(f"step '{s.id}': depends_on_mode must be 'all' or 'any_completed'")
        if s.type == "agent":
            if not s.agent:
                problems.append(f"step '{s.id}' names no agent")
            elif roster is not None:
                try:
                    roster.resolve(s.agent)
                except UnknownAgentError as e:
                    problems.append(f"step '{s.id}': {e}")
            if not s.task.strip():
                problems.append(f"step '{s.id}' has no task")
        for dep in s.depends_on:
            if dep not in ids:
                problems.append(f"step '{s.id}' depends on unknown step '{dep}'")
            elif dep == s.id:
                problems.append(f"step '{s.id}' depends on itself")
        if s.workdir and (Path(s.workdir).is_absolute() or ".." in Path(s.workdir).parts):
            problems.append(f"step '{s.id}': workdir must be a path inside the project, got '{s.workdir}'")
        if s.output:
            if s.output in outputs:
                problems.append(f"output '{s.output}' is written by both '{outputs[s.output]}' and '{s.id}'")
            outputs[s.output] = s.id
        if s.condition:
            try:
                parse_condition(s.condition)
            except WorkflowError as e:
                problems.extend(f"step '{s.id}': {p}" for p in e.problems)
    if workflow.concurrency < 1:
        problems.append("concurrency must be at least 1")
    if problems:
        return problems

    try:
        dag = build(workflow)
    except CycleError as e:
        return [str(e)]

    # A template may only read inputs, built-ins, and outputs of steps that are
    # guaranteed to have run before it: its ancestors in the graph.
    input_names = {str(i.get("name")) for i in workflow.inputs}
    for s in workflow.steps:
        ancestors = dag.ancestors(s.id)
        available = input_names | BUILTIN_VARS | {ids[a].output for a in ancestors if ids[a].output}
        for text in (s.task, s.prompt, s.acceptance, s.condition):
            for var in template_vars(text):
                if var in available:
                    continue
                producer = outputs.get(var)
                if producer:
                    problems.append(f"step '{s.id}' reads {{{{{var}}}}} from '{producer}' but does not depend on it")
                else:
                    problems.append(f"step '{s.id}' reads {{{{{var}}}}}, which no input or step defines")
    return problems


def check(workflow: Workflow, roster: Roster | None = None) -> None:
    problems = validate(workflow, roster)
    if problems:
        raise WorkflowError(problems)
