"""Task + project analysis -> workflow.

The plan follows the project's own dependency graph:

  brief (lead, read)            — only when the task does not name components
    -> <concern>-<component>    — e.g. database work before the API that uses it
    -> impl-<component>          — one per component, routed by its stack,
                                   after the components it depends on
    -> test (write)              — tests across everything that changed
    -> review + intent reviews   — read-only, in parallel

When the task names no component, every implementation step is conditional on
the brief marking that component as affected ("AFFECTED: component:<name>;"),
so the architect decides scope and the graph decides order and owner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .project import Component, Project
from .roster import Roster, slugify
from .routing import Rules
from .workflow import Step, Workflow

AFFECTED_MARK = "AFFECTED: component:{name};"

# Commands a step may run for its component's stack. Headless agents cannot
# ask for permission, so these are pre-approved; anything that installs
# packages, deploys or applies infrastructure is deliberately not here — add
# it per workflow (llm.allowed_tools) when a project needs it.
CHECK_TOOLS = {
    "npm": ["Bash(npm test:*)", "Bash(npm run test:*)", "Bash(npm run lint:*)", "Bash(npm run build:*)",
            "Bash(npm run typecheck:*)", "Bash(npx tsc:*)", "Bash(npx vitest:*)", "Bash(npx jest:*)"],
    "deno": ["Bash(deno test:*)", "Bash(deno check:*)", "Bash(deno lint:*)"],
    "python": ["Bash(pytest:*)", "Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)",
               "Bash(ruff check:*)", "Bash(mypy:*)"],
    "go": ["Bash(go test:*)", "Bash(go build:*)", "Bash(go vet:*)"],
    "cargo": ["Bash(cargo test:*)", "Bash(cargo build:*)", "Bash(cargo check:*)", "Bash(cargo clippy:*)"],
    "maven": ["Bash(mvn test:*)", "Bash(mvn -q test:*)", "Bash(./mvnw test:*)"],
    "gradle": ["Bash(./gradlew test:*)", "Bash(./gradlew build:*)", "Bash(gradle test:*)"],
    "dotnet": ["Bash(dotnet test:*)", "Bash(dotnet build:*)"],
    "ruby": ["Bash(bundle exec rspec:*)", "Bash(bundle exec rake test:*)"],
    "dart": ["Bash(flutter test:*)", "Bash(dart test:*)", "Bash(flutter analyze:*)"],
    "composer": ["Bash(vendor/bin/phpunit:*)", "Bash(composer test:*)"],
    "elixir": ["Bash(mix test:*)", "Bash(mix compile:*)"],
    "solidity": ["Bash(forge test:*)", "Bash(forge build:*)", "Bash(npx hardhat test:*)"],
    "terraform": ["Bash(terraform fmt:*)", "Bash(terraform validate:*)"],
}


def check_tools(comps: list[Component]) -> list[str]:
    tools: list[str] = []
    for c in comps:
        for eco in c.ecosystems:
            tools += CHECK_TOOLS.get(eco, [])
        if c.test_command:
            tools.append(f"Bash({c.test_command}:*)")
    return list(dict.fromkeys(tools))


@dataclass
class PlanOptions:
    components: list[str] = field(default_factory=list)   # force these (names or paths)
    impact: bool = False        # also change components that depend on the selected ones
    lead: bool | None = None    # None = only when the task names no component
    tests: bool = True
    review: bool = True
    intents: bool = True
    base: str = "HEAD"


def _step_id(prefix: str, name: str) -> str:
    return f"{prefix}-{slugify(name) or 'root'}"


def _var(prefix: str, name: str) -> str:
    return f"{prefix}_{(slugify(name) or 'root').replace('-', '_')}"


def _mentions(task: str, comp: Component) -> bool:
    low = task.lower()
    candidates = {comp.name, *comp.package_names}
    if comp.path != ".":
        candidates.add(comp.path)
        candidates.add(PurePosixPath(comp.path).name)
    for cand in candidates:
        cand = cand.lower().strip()
        if len(cand) < 3:
            continue
        if re.search(r"(?<![\w/@.-])" + re.escape(cand) + r"(?![\w-])", low):
            return True
    return False


def _reach(project: Project, start: str, extra: dict[str, set[str]] | None = None) -> set[str]:
    """Components `start` depends on, directly or transitively."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        nxt = project.deps_of(cur) | (extra or {}).get(cur, set())
        for n in nxt:
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return seen


def select_components(project: Project, task: str, options: PlanOptions) -> tuple[list[Component], bool, list[str]]:
    """(components to plan for, whether they were named, notes)."""
    notes: list[str] = []
    named: list[Component] = []
    for ref in options.components:
        match = next((c for c in project.components if ref in (c.name, c.path) or ref in c.package_names), None)
        if match is None:
            raise ValueError(f"no component '{ref}' — known: {', '.join(c.name for c in project.components)}")
        named.append(match)
    # Names in the task text are only a hint for the architect: "show it in
    # the web app" mentions `web`, but the change may well start in the API.
    # Only --component narrows the plan deterministically.
    mentioned = [c.name for c in project.components if _mentions(task, c)] if len(project.components) > 1 else []
    if mentioned and not named:
        notes.append("task mentions: " + ", ".join(mentioned) + " (a hint for the brief, not a limit)")
    explicit = bool(named)
    if not named:
        named = list(project.components)
    if options.impact:
        selected = {c.name for c in named}
        grew = True
        while grew:
            grew = False
            for c in project.components:
                if c.name not in selected and project.deps_of(c.name) & selected:
                    selected.add(c.name)
                    grew = True
                    notes.append(f"impact: {c.name} depends on a changed component")
        named = [c for c in project.components if c.name in selected]
    return named, explicit, notes


def order_components(project: Project, comps: list[Component], rules: Rules) -> tuple[dict[str, set[str]], list[str]]:
    """For each component, the selected components it must come after.

    The project graph decides first (transitively, so A -> X -> B orders A
    after B even when X is not part of the change). Where the graph says
    nothing, the layer order from routing.json (data before API before UI)
    breaks the tie — recorded as a heuristic in the notes."""
    names = {c.name for c in comps}
    after: dict[str, set[str]] = {c.name: _reach(project, c.name) & names for c in comps}
    notes: list[str] = []
    ranked = sorted(comps, key=lambda c: (rules.layer_rank(c), c.name))
    for i, low in enumerate(ranked):
        for high in ranked[i + 1:]:
            if rules.layer_rank(high) <= rules.layer_rank(low):
                continue
            if low.name in after[high.name] or high.name in after[low.name]:
                continue
            # adding high -> low must not close a cycle through earlier additions
            if high.name in _closure(after, low.name):
                continue
            after[high.name].add(low.name)
            notes.append(f"order: {high.name} after {low.name} (layer heuristic, no dependency found)")
    return after, notes


def _closure(after: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        for n in after.get(stack.pop(), set()):
            if n not in seen:
                seen.add(n)
                stack.append(n)
    return seen


def _direct(after: dict[str, set[str]], name: str) -> set[str]:
    """Drop transitively implied predecessors so the graph stays readable."""
    preds = set(after[name])
    implied: set[str] = set()
    for p in preds:
        implied |= _closure(after, p)
    return preds - implied


def _component_line(project: Project, c: Component, agent_name: str) -> str:
    deps = sorted(project.deps_of(c.name))
    tags = ", ".join(c.tags) or "untagged"
    langs = ", ".join(c.main_languages) or "-"
    line = f"- {c.name} (`{c.path}/`) — {tags}; {langs}; owner: {agent_name}"
    if deps:
        line += f"; depends on {', '.join(deps)}"
    return line


def plan(task: str, project: Project, rules: Rules, roster: Roster, options: PlanOptions | None = None) -> tuple[Workflow, list[str]]:
    options = options or PlanOptions()
    task = task.strip()
    if not task:
        raise ValueError("empty task")
    comps, explicit, notes = select_components(project, task, options)
    mentioned = [c.name for c in comps if _mentions(task, c)] if len(comps) > 1 else []
    after, order_notes = order_components(project, comps, rules)
    notes += order_notes
    use_lead = (not explicit) if options.lead is None else options.lead
    by_name = {c.name: c for c in comps}

    owner: dict[str, str] = {}
    for c in comps:
        agent, reason = rules.implementer(c)
        owner[c.name] = agent
        notes.append(f"route: {c.name} -> {agent} ({reason})")
    name_of = lambda slug: (roster.get(slug).name if roster.get(slug) else slug)  # noqa: E731

    topo = sorted(comps, key=lambda c: (len(_closure(after, c.name)), rules.layer_rank(c), c.name))
    overview = "\n".join(_component_line(project, c, name_of(owner[c.name])) for c in topo)
    steps: list[Step] = []

    lead_id = None
    if use_lead:
        lead_id = "brief"
        marks = "\n".join(AFFECTED_MARK.format(name=c.name) for c in topo)
        steps.append(Step(
            id=lead_id, agent=rules.lead(), mode="read", output="brief",
            task=(
                f"Plan this change to the project \"{project.name}\". Read whatever code you need, "
                f"but do not edit any file.\n\nTASK:\n{task}\n\n"
                f"COMPONENTS (in the order they will be worked on, with the agent that owns each):\n{overview}\n\n"
                + (f"The task text mentions {', '.join(mentioned)} — check whether the change also needs "
                   "the components those depend on.\n\n" if mentioned else "")
                +
                "Write an implementation brief for the agents that come after you:\n"
                "1. What changes in each affected component — files and functions where you can tell.\n"
                "2. The contracts between components (API shapes, types, schema/migrations), so each agent "
                "can work in its own component in the order above without guessing.\n"
                "3. Risks, and how to verify the change.\n\n"
                "Finish with one line per component that has to change, copied exactly from this list "
                "(leave out the ones that need no change):\n" + marks
            ),
        ))

    impl_of: dict[str, str] = {}
    for c in topo:
        workdir = c.path
        position = [x.name for x in topo].index
        upstream = sorted(_direct(after, c.name), key=position)
        deps_ids = ([lead_id] if lead_id else []) + [impl_of[u] for u in upstream if u in impl_of]
        # Reports to read: every component this one really depends on (the
        # project graph, transitively), plus whatever runs directly before it.
        # depends_on stays reduced; templates may read any ancestor's output.
        readers = sorted((_reach(project, c.name) & set(by_name)) | set(upstream), key=position)
        condition = f"{{{{brief}}}} contains {AFFECTED_MARK.format(name=c.name)}" if lead_id else None
        context_lines = []
        if lead_id:
            context_lines.append("Implementation brief from the architect:\n{{brief}}")
        for u in readers:
            relation = "which this component depends on" if u in _reach(project, c.name) else "worked on just before"
            context_lines.append(f"What the agent on {u} ({relation}) reported:\n{{{{{_var('impl', u)}}}}}")

        concern_ids = []
        for tag, agent, kw in rules.concerns(c, task):
            sid = _step_id(tag, c.name)
            steps.append(Step(
                id=sid, agent=agent, mode="write", workdir=workdir, component=c.name,
                depends_on=list(deps_ids), condition=condition, output=_var(tag, c.name),
                task=(
                    f"TASK:\n{task}\n\nYou handle the {tag} part of this task in the component "
                    f"\"{c.name}\" (`{workdir}/`), before {name_of(owner[c.name])} builds on it. "
                    f"Change only the {tag} layer: leave the rest of the component to them.\n\n"
                    + ("\n\n".join(context_lines) + "\n\n" if context_lines else "")
                    + "End your answer with a HANDOFF section: what you changed and what the next agent must know."
                ),
            ))
            concern_ids.append(sid)
            context_lines.append(f"What the {tag} specialist already did in this component:\n{{{{{_var(tag, c.name)}}}}}")
            notes.append(f"concern: {c.name} gets {agent} first ('{kw}' in the task, tag {tag})")

        tools = check_tools([c])
        llm = {"allowed_tools": tools} if tools else {}
        for st in steps:
            if st.id in concern_ids:
                st.llm = dict(llm)
        sid = _step_id("impl", c.name)
        test_hint = f" Run `{c.test_command}` (or the component's own checks) before you finish." if c.test_command else \
            " Run the component's build or tests before you finish, if it has any."
        steps.append(Step(
            id=sid, agent=owner[c.name], mode="write", workdir=workdir, component=c.name,
            depends_on=deps_ids + concern_ids, condition=condition, output=_var("impl", c.name), llm=dict(llm),
            task=(
                f"TASK:\n{task}\n\nYou own the component \"{c.name}\" at `{workdir}/` "
                f"({', '.join(c.tags) or 'no stack tags'}). Make the part of the change that belongs here. "
                "Stay inside this component unless the brief says a change elsewhere is part of your job."
                + test_hint + "\n\n"
                + ("\n\n".join(context_lines) + "\n\n" if context_lines else "")
                + "Do not commit — the orchestrator commits after each step. End your answer with a HANDOFF "
                "section: what you changed, how you verified it, and what components that depend on this one must know."
            ),
        ))
        impl_of[c.name] = sid

    impl_ids = list(impl_of.values())
    summary_refs = "\n\n".join(f"{c.name}:\n{{{{{_var('impl', c.name)}}}}}" for c in topo)
    verify_ids: list[str] = []
    if options.tests and impl_ids:
        tester = rules.tester(comps)
        commands = sorted({c.test_command for c in comps if c.test_command})
        tools = check_tools(comps)
        steps.append(Step(
            id="test", agent=tester, mode="write", depends_on=impl_ids, output="test_report",
            llm={"allowed_tools": tools} if tools else {},
            task=(
                f"TASK that was just implemented:\n{task}\n\nVerify it across the project. Read the changes "
                "(`git diff {{base_ref}}...HEAD`), add or update tests where the change is not covered, and run "
                "the test suites" + (f" ({', '.join(f'`{x}`' for x in commands)})" if commands else "")
                + ". Fix test code you wrote; for a defect in the product code, report it instead of rewriting it.\n\n"
                "What each component's agent reported:\n" + summary_refs
                + "\n\nFinish with a line `RESULT: PASS` or `RESULT: FAIL`, then the evidence."
            ),
        ))
        verify_ids = ["test"]

    final_deps = verify_ids or impl_ids
    if options.review and impl_ids:
        steps.append(Step(
            id="review", agent=rules.reviewer(), mode="read", depends_on=final_deps, output="review",
            task=(
                f"Review the change made for this task:\n{task}\n\nThe diff is `git diff {{base_ref}}...HEAD` "
                "on the current branch. Do not edit files. Check correctness, the contracts between components, "
                "tests, and anything that would block merging.\n\n"
                + ("Test report:\n{{test_report}}\n\n" if verify_ids else "")
                + "Finish with `VERDICT: APPROVE` or `VERDICT: REQUEST CHANGES`, then the findings, most severe first."
            ),
        ))
    if options.intents and impl_ids:
        for intent, agent, kw in rules.intents_for(task):
            steps.append(Step(
                id=f"{intent}-review", agent=agent, mode="read", depends_on=final_deps,
                output=f"{intent.replace('-', '_')}_review",
                task=(
                    f"The task below touched {intent} ('{kw}'). Review the change "
                    "(`git diff {{base_ref}}...HEAD`) from that angle only. Do not edit files.\n\n"
                    f"TASK:\n{task}\n\nFinish with `VERDICT: APPROVE` or `VERDICT: REQUEST CHANGES` and your findings."
                ),
            ))
            notes.append(f"intent: {intent} review by {agent} ('{kw}' in the task)")

    workflow = Workflow(
        name=f"{project.name}: {task.splitlines()[0][:60]}",
        description=task,
        project={"path": str(project.root), "base": options.base},
        concurrency=2,
        steps=steps,
    )
    return workflow, notes
