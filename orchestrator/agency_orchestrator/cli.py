"""Command line: agency-orchestrate <command> ...

  agents    [--search Q]                  list or search the roster
  analyze   PROJECT                       components, stacks, dependency graph, owners
  route     TEXT [--project P]            which agents a task would go to
  plan      TASK --project P [-o FILE]    write a workflow for a task
  validate  WORKFLOW                      check a workflow without running it
  graph     WORKFLOW [--format F]         show the execution graph (text, mermaid, dot)
  run       WORKFLOW [--provider ...]     execute a workflow (or --resume a run)
  do        TASK --project P              plan and run in one go
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, dag, planner, project as proj, providers, workflow as wf
from .roster import REPO_ROOT, Roster, UnknownAgentError
from .routing import Rules
from .runner import Runner, default_runs_dir


def _roster(args) -> Roster:
    return Roster.load(getattr(args, "agents_dir", None) or REPO_ROOT)


def _rules(args) -> Rules:
    return Rules.load(getattr(args, "rules", None))


def _inputs(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--input expects name=value, got '{pair}'")
        k, v = pair.split("=", 1)
        if v.startswith("@"):
            v = Path(v[1:]).read_text(encoding="utf-8")
        out[k] = v
    return out


def cmd_agents(args) -> int:
    roster = _roster(args)
    if args.search:
        for score, a in roster.search(args.search, limit=args.limit):
            print(f"{score:6.1f}  {a.slug:<40} {a.division:<18} {a.description[:70]}")
        return 0
    for a in roster.agents:
        if args.division and a.division != args.division:
            continue
        print(f"{a.slug:<40} {a.division:<18} {a.name}")
    return 0


def _print_project(p: proj.Project, rules: Rules, roster: Roster, task: str = "") -> None:
    print(f"Project {p.name}  ({p.root})")
    print(f"  git: {'yes' if p.is_git else 'no'}  ·  graphify: {p.graphify or 'not used (no graphify-out/graph.json)'}")
    print(f"  {len(p.components)} component(s), {len(p.edges)} dependency edge(s)\n")
    for c in p.components:
        agent, reason = rules.implementer(c)
        a = roster.get(agent)
        print(f"■ {c.name}  `{c.path}/`  [{', '.join(c.ecosystems) or 'no manifest'}]  {len(c.files)} files")
        print(f"    stack:  {', '.join(c.tags) or '-'}  ·  languages: {', '.join(c.main_languages) or '-'}")
        print(f"    owner:  {a.label if a else agent}  ← {reason}")
        for tag, ag, kw in rules.concerns(c, task) if task else []:
            print(f"    first:  {roster.get(ag).label if roster.get(ag) else ag}  ← concern '{tag}' ('{kw}' in the task)")
        deps = sorted(p.deps_of(c.name))
        if deps:
            print(f"    needs:  {', '.join(deps)}")
    if p.edges:
        print("\nDependencies (A → B: A depends on B)")
        for e in p.edges:
            ex = f"  e.g. {e.examples[0]}" if e.examples else ""
            print(f"  {e.source} → {e.target}  [{e.kind} ×{e.weight}]{ex}")
    for n in p.notes:
        print(f"  note: {n}")


def cmd_analyze(args) -> int:
    rules, roster = _rules(args), _roster(args)
    p = proj.analyze(args.project, graphify=args.graphify, rules=rules)
    if args.json:
        data = p.to_dict()
        for c, cd in zip(p.components, data["components"]):
            cd["owner"], cd["owner_reason"] = rules.implementer(c)
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_project(p, rules, roster)
    return 0


def cmd_route(args) -> int:
    rules, roster = _rules(args), _roster(args)
    if args.project:
        p = proj.analyze(args.project, graphify=args.graphify, rules=rules)
        comps, explicit, notes = planner.select_components(p, args.text, planner.PlanOptions())
        print(f"Components {'named by the task' if explicit else '(task names none — the architect decides)'}:")
        for c in comps:
            agent, reason = rules.implementer(c)
            print(f"  {c.name:<24} → {agent:<32} {reason}")
            for tag, ag, kw in rules.concerns(c, args.text):
                print(f"  {'':<24}   first {ag} (concern {tag}, '{kw}')")
    for intent, ag, kw in rules.intents_for(args.text):
        print(f"  intent {intent:<17} → {ag} ('{kw}')")
    print("\nBest matches in the roster for the text itself:")
    for score, a in roster.search(args.text, limit=args.limit):
        print(f"  {score:6.1f}  {a.slug:<40} {a.description[:70]}")
    return 0


def _make_plan(args, task: str):
    rules, roster = _rules(args), _roster(args)
    p = proj.analyze(args.project, graphify=args.graphify, rules=rules)
    opts = planner.PlanOptions(components=args.component or [], impact=args.impact,
                               lead=args.lead, tests=not args.no_tests, review=not args.no_review,
                               intents=not args.no_intents, base=args.base)
    w, notes = planner.plan(task, p, rules, roster, opts)
    if args.provider:
        w.llm["provider"] = args.provider
    if getattr(args, "model", None):
        w.llm["model"] = args.model
    problems = wf.validate(w, roster)
    if problems:
        raise wf.WorkflowError(problems)
    return w, notes, roster


def cmd_plan(args) -> int:
    w, notes, roster = _make_plan(args, args.task)
    names = {s.id: roster.resolve(s.agent).name for s in w.steps if s.agent}
    if args.output:
        wf.dump(w, args.output)
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(wf.dump(w, fmt="json" if args.json else None))
    if not args.quiet:
        print("", file=sys.stderr)
        for n in notes:
            print(f"  · {n}", file=sys.stderr)
        print("\n" + dag.render_text(dag.build(w), names), file=sys.stderr)
    return 0


def cmd_validate(args) -> int:
    roster = _roster(args)
    w = wf.load(args.workflow)
    for warning in w.warnings:
        print(f"  warning: {warning}")
    problems = wf.validate(w, roster)
    if problems:
        print(f"{args.workflow}: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"{args.workflow}: OK — {len(w.steps)} steps")
    return 0


def cmd_graph(args) -> int:
    roster = _roster(args)
    w = wf.load(args.workflow)
    g = dag.build(w)
    names = {}
    for s in w.steps:
        if s.agent:
            try:
                names[s.id] = roster.resolve(s.agent).name
            except UnknownAgentError:
                names[s.id] = s.agent
    render = {"text": dag.render_text, "mermaid": dag.render_mermaid, "dot": dag.render_dot}[args.format]
    print(render(g, names))
    return 0


def _runner(args, w: wf.Workflow, roster: Roster) -> Runner:
    settings = {}
    if args.model:
        settings["model"] = args.model
    if args.allow:
        settings["allowed_tools"] = list(w.llm.get("allowed_tools") or []) + args.allow
    if args.command:
        settings["command"] = args.command
    return Runner(w, roster, provider=args.provider, project=args.project, isolation=args.isolation,
                  runs_dir=args.runs_dir, resume=getattr(args, "resume", None),
                  from_step=getattr(args, "from_step", None), yes=args.yes,
                  commit=True if args.commit else None, inputs=_inputs(args.input),
                  settings=settings, concurrency=args.concurrency)


def cmd_run(args) -> int:
    roster = _roster(args)
    if args.resume and not args.workflow:
        args.workflow = str(Path(args.resume) / "workflow.json")
    if not args.workflow:
        raise SystemExit("run needs a workflow file, or --resume <run dir>")
    w = wf.load(args.workflow)
    for warning in w.warnings:
        print(f"  warning: {warning}")
    result = _runner(args, w, roster).run()
    return 0 if result.ok else 1


def cmd_do(args) -> int:
    w, notes, roster = _make_plan(args, args.task)
    for n in notes:
        print(f"  · {n}")
    names = {s.id: roster.resolve(s.agent).name for s in w.steps if s.agent}
    print("\n" + dag.render_text(dag.build(w), names) + "\n")
    if args.save_plan:
        wf.dump(w, args.save_plan)
    result = _runner(args, w, roster).run()
    return 0 if result.ok else 1


def _add_plan_flags(sp) -> None:
    sp.add_argument("task", help="what should be done")
    sp.add_argument("--project", "-p", required=True, help="path to the project to work on")
    sp.add_argument("--component", "-c", action="append", help="limit the work to this component (repeatable)")
    sp.add_argument("--impact", action="store_true", help="also update components that depend on the changed ones")
    lead = sp.add_mutually_exclusive_group()
    lead.add_argument("--lead", dest="lead", action="store_true", default=None, help="always start with an architect brief")
    lead.add_argument("--no-lead", dest="lead", action="store_false", help="never start with an architect brief")
    sp.add_argument("--no-tests", action="store_true", help="skip the test step")
    sp.add_argument("--no-review", action="store_true", help="skip the review step")
    sp.add_argument("--no-intents", action="store_true", help="skip security/performance/... reviews")
    sp.add_argument("--base", default="HEAD", help="git ref the run branches from (default HEAD)")
    sp.add_argument("--graphify", help="path to a Graphify graph.json (default: <project>/graphify-out/graph.json if present)")


def _add_run_flags(sp, workflow_project: bool = True) -> None:
    sp.add_argument("--provider", choices=sorted(providers.PROVIDERS), help="default: the workflow's llm.provider, else claude-code")
    sp.add_argument("--model", help="model for every step (provider-specific)")
    sp.add_argument("--command", help="command template for --provider command")
    sp.add_argument("--allow", action="append", metavar="TOOL", help="extra pre-approved tool, e.g. 'Bash(make test:*)'")
    sp.add_argument("--isolation", choices=["worktree", "inplace"], default="worktree",
                    help="worktree (default): own branch, one commit per step; inplace: edit the project directly")
    sp.add_argument("--commit", action="store_true", help="with --isolation inplace: commit after each write step")
    sp.add_argument("--runs-dir", help=f"where run directories go (default {default_runs_dir()})")
    sp.add_argument("--concurrency", type=int, help="parallel read-only steps")
    sp.add_argument("--yes", "-y", action="store_true", help="approve approval steps automatically")
    sp.add_argument("--input", "-i", action="append", metavar="NAME=VALUE", help="workflow input (value or @file)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agency-orchestrate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--agents-dir", help="agency-agents checkout to take agents from (default: this repo)")
    ap.add_argument("--rules", help="routing rules (default: orchestrator/routing.json)")
    sub = ap.add_subparsers(dest="command_name", required=True)

    sp = sub.add_parser("agents", help="list or search agents")
    sp.add_argument("--search", "-s")
    sp.add_argument("--division")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("analyze", help="analyze a project")
    sp.add_argument("project")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--graphify", help="path to a Graphify graph.json")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("route", help="show which agents a task goes to")
    sp.add_argument("text")
    sp.add_argument("--project", "-p")
    sp.add_argument("--graphify")
    sp.add_argument("--limit", type=int, default=5)
    sp.set_defaults(func=cmd_route)

    sp = sub.add_parser("plan", help="write a workflow for a task on a project")
    _add_plan_flags(sp)
    sp.add_argument("--output", "-o", help="file to write (.yaml or .json); default stdout")
    sp.add_argument("--json", action="store_true", help="print JSON instead of YAML")
    sp.add_argument("--provider", choices=sorted(providers.PROVIDERS), help="record a provider in the workflow")
    sp.add_argument("--model")
    sp.add_argument("--quiet", "-q", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("validate", help="check a workflow")
    sp.add_argument("workflow")
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("graph", help="show a workflow's execution graph")
    sp.add_argument("workflow")
    sp.add_argument("--format", "-f", choices=["text", "mermaid", "dot"], default="text")
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("run", help="run a workflow")
    sp.add_argument("workflow", nargs="?")
    sp.add_argument("--project", "-p", help="project path (default: the workflow's project.path)")
    sp.add_argument("--resume", metavar="RUN_DIR", help="continue a run that stopped")
    sp.add_argument("--from", dest="from_step", metavar="STEP", help="with --resume: redo this step and everything after it")
    _add_run_flags(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("do", help="plan and run a task on a project")
    _add_plan_flags(sp)
    _add_run_flags(sp)
    sp.add_argument("--save-plan", metavar="FILE", help="also write the generated workflow here")
    sp.set_defaults(func=cmd_do)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (wf.WorkflowError, UnknownAgentError, providers.ProviderError, FileNotFoundError,
            ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted — resume with: agency-orchestrate run --resume <run dir>", file=sys.stderr)
        return 130
