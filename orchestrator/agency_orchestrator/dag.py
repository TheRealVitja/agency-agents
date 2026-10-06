"""The dependency graph of a workflow.

Edges point from a step to the steps it depends on. `levels` groups steps the
way AO's executor does (Kahn layers: everything in a level can run at once),
which is what `graph` prints. The runner does not wait for whole levels,
though: a step starts as soon as its own dependencies are done, so one slow
step does not hold back an unrelated branch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .workflow import Step, Workflow


class CycleError(ValueError):
    pass


@dataclass
class DAG:
    steps: dict[str, Step]
    deps: dict[str, list[str]]
    dependents: dict[str, list[str]] = field(default_factory=dict)
    levels: list[list[str]] = field(default_factory=list)

    def ancestors(self, step_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.deps[step_id])
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.deps[cur])
        return seen

    def descendants(self, step_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.dependents[step_id])
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.dependents[cur])
        return seen

    def order(self) -> list[str]:
        return [sid for level in self.levels for sid in level]


def _find_cycle(deps: dict[str, list[str]]) -> list[str]:
    color: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = 1
        path.append(node)
        for nxt in deps[node]:
            if color.get(nxt) == 1:
                return path[path.index(nxt):] + [nxt]
            if color.get(nxt) is None:
                found = visit(nxt)
                if found:
                    return found
        path.pop()
        color[node] = 2
        return None

    for node in deps:
        if color.get(node) is None:
            found = visit(node)
            if found:
                return found
    return []


def build(workflow: Workflow) -> DAG:
    steps = {s.id: s for s in workflow.steps}
    deps = {s.id: [d for d in s.depends_on if d in steps] for s in workflow.steps}
    dependents: dict[str, list[str]] = {sid: [] for sid in steps}
    for sid, ds in deps.items():
        for d in ds:
            dependents[d].append(sid)

    # Kahn layers, keeping file order inside a layer so output is stable.
    indegree = {sid: len(ds) for sid, ds in deps.items()}
    remaining = [s.id for s in workflow.steps]
    levels: list[list[str]] = []
    while remaining:
        level = [sid for sid in remaining if indegree[sid] == 0]
        if not level:
            cycle = _find_cycle({sid: deps[sid] for sid in remaining})
            raise CycleError("dependency cycle: " + " -> ".join(cycle) if cycle else "dependency cycle")
        for sid in level:
            for d in dependents[sid]:
                indegree[d] -= 1
        remaining = [sid for sid in remaining if sid not in level]
        levels.append(level)
    return DAG(steps=steps, deps=deps, dependents=dependents, levels=levels)


# --- rendering -----------------------------------------------------------------

def _label(step: Step, agent_name: str | None = None) -> str:
    who = agent_name or step.agent or step.type
    mode = "" if step.type != "agent" else (" ✎" if step.mode == "write" else " 👁")
    scope = f" @{step.workdir}" if step.workdir and step.workdir != "." else ""
    return f"[{step.id}] {who}{mode}{scope}"


def render_text(dag: DAG, names: dict[str, str] | None = None) -> str:
    names = names or {}
    lines = ["Execution plan (✎ writes to the project, 👁 reads only)", ""]
    for i, level in enumerate(dag.levels, 1):
        parallel = len(level) > 1
        for j, sid in enumerate(level):
            step = dag.steps[sid]
            if parallel:
                prefix = "┌" if j == 0 else ("└" if j == len(level) - 1 else "├")
            else:
                prefix = "→"
            lines.append(f"  L{i} {prefix} {_label(step, names.get(sid))}")
            if dag.deps[sid]:
                lines.append(f"        after: {', '.join(dag.deps[sid])}")
            if step.condition:
                lines.append(f"        when:  {step.condition}")
        if i < len(dag.levels):
            lines.append("  │")
    return "\n".join(lines)


def render_mermaid(dag: DAG, names: dict[str, str] | None = None) -> str:
    names = names or {}
    out = ["flowchart TD"]
    for sid in dag.order():
        step = dag.steps[sid]
        text = _label(step, names.get(sid)).replace('"', "'")
        shape = ('{{"%s"}}' % text) if step.type == "approval" else ('["%s"]' % text)
        out.append(f"  {sid.replace('-', '_')}{shape}")
    for sid in dag.order():
        for d in dag.deps[sid]:
            out.append(f"  {d.replace('-', '_')} --> {sid.replace('-', '_')}")
    return "\n".join(out)


def render_dot(dag: DAG, names: dict[str, str] | None = None) -> str:
    names = names or {}
    out = ["digraph workflow {", "  rankdir=LR;", "  node [shape=box];"]
    for sid in dag.order():
        text = _label(dag.steps[sid], names.get(sid)).replace('"', "'")
        out.append(f'  "{sid}" [label="{text}"];')
    for sid in dag.order():
        for d in dag.deps[sid]:
            out.append(f'  "{d}" -> "{sid}";')
    out.append("}")
    return "\n".join(out)
