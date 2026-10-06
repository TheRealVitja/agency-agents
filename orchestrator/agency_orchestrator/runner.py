"""Run a workflow against a project.

Isolation (default `worktree`): the run gets its own git worktree on a new
branch `agency/<run-id>`, cut from the workflow's base (HEAD by default). The
project's working tree is never touched; uncommitted changes there are not part
of the run. After every write step the orchestrator commits what the agent
changed, so the branch reads as one commit per agent and a later step — or a
human — can see exactly who did what. `inplace` runs in the project directory
itself (no branch, commits only with --commit).

Scheduling: a step starts once its own dependencies are done — no waiting for
a whole level. Write steps run alone (two agents editing one tree at once
would trample each other); read steps run in parallel up to `concurrency`.

Statuses: done, failed, skipped (condition false: dependents still run and
read an empty output), blocked (a dependency failed: the step never ran).
Everything lands in the run directory, so `--resume` can pick up where it
stopped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import providers, workflow as wf
from .dag import build
from .roster import Roster, slugify

TERMINAL = ("done", "failed", "skipped", "blocked")


def default_runs_dir() -> Path:
    return Path(os.environ.get("AGENCY_RUNS_DIR") or Path.home() / ".agency-orchestrator" / "runs")


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _git_identity(cwd: Path) -> list[str]:
    """`-c` flags for a commit identity when the user has none configured."""
    email = _git(cwd, "config", "user.email", check=False)
    name = _git(cwd, "config", "user.name", check=False)
    flags = []
    if not email:
        flags += ["-c", "user.email=agency-orchestrator@localhost"]
    if not name:
        flags += ["-c", "user.name=Agency Orchestrator"]
    return flags


ORCHESTRATION_RULES = """
---

## Working inside the Agency orchestrator

You are one step of a multi-agent run on an existing project. Other agents
work before and after you; the orchestrator passes your final answer on to
them verbatim.

- Run: {run_id}. Your step: `{step_id}` ({mode_text}).
- Working directory: the project root{branch_text}.
{scope_text}{mode_rules}
- Finish with exactly the answer the task asks for. Keep it self-contained:
  the next agent sees only what you write, not this conversation.
"""

WRITE_RULES = """- Edit the files your part of the task needs, and nothing unrelated.
- Do not commit, push, rebase, merge, switch branches or change git config:
  the orchestrator commits your changes when you finish.
- Run the checks you are allowed to (tests, type checks, builds) before you finish.
"""
READ_RULES = """- Read only. Do not create, edit or delete any file. Inspect code, run
  read-only commands, and report.
"""


@dataclass
class StepState:
    status: str = "pending"
    agent: str = ""
    agent_name: str = ""
    started: float | None = None
    ended: float | None = None
    output_file: str | None = None
    commit: str | None = None
    diffstat: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class RunResult:
    ok: bool
    run_dir: Path
    branch: str | None
    worktree: Path | None
    states: dict[str, StepState]
    base: str | None = None


class Runner:
    def __init__(self, workflow: wf.Workflow, roster: Roster, *, provider: str | None = None,
                 project: Path | str | None = None, isolation: str = "worktree",
                 runs_dir: Path | str | None = None, resume: Path | str | None = None,
                 from_step: str | None = None, yes: bool = False, commit: bool | None = None,
                 inputs: dict[str, str] | None = None, settings: dict[str, Any] | None = None,
                 echo: Callable[[str], None] | None = None, concurrency: int | None = None):
        self.workflow = workflow
        self.roster = roster
        self.provider_name = provider or str(workflow.llm.get("provider") or "claude-code")
        self.settings = {**workflow.llm, **(settings or {})}
        self.settings.pop("provider", None)
        self.isolation = isolation
        self.yes = yes
        self.inputs = dict(inputs or {})
        self.echo = echo or (lambda s: print(s, flush=True))
        self.concurrency = max(1, int(concurrency or workflow.concurrency or 1))
        self.from_step = from_step
        self.resume = Path(resume).expanduser().resolve() if resume else None
        proj = project or workflow.project.get("path") or os.getcwd()
        self.project = Path(proj).expanduser().resolve()
        self.runs_dir = Path(runs_dir).expanduser().resolve() if runs_dir else default_runs_dir()
        self.commit = (isolation == "worktree") if commit is None else commit
        self.lock = threading.Lock()
        self.states: dict[str, StepState] = {}
        self.context: dict[str, str] = {}
        self.meta: dict[str, Any] = {}

    # --- setup -------------------------------------------------------------------

    def _prepare(self) -> None:
        wf.check(self.workflow, self.roster)
        if not self.project.is_dir():
            raise FileNotFoundError(f"project {self.project} does not exist")
        self.dag = build(self.workflow)
        self.provider = providers.get(self.provider_name, self.settings)
        self.provider.check()
        if self.isolation not in ("worktree", "inplace"):
            raise ValueError("isolation must be 'worktree' or 'inplace'")
        if not self.provider.edits_files:
            # A preview changes nothing, so it needs no branch or worktree.
            self.isolation, self.commit = "inplace", False

        missing = [i["name"] for i in self.workflow.inputs
                   if i.get("required") and i["name"] not in self.inputs and "default" not in i]
        if missing:
            raise ValueError(f"missing input(s): {', '.join(missing)} — pass them with -i name=value")

        if self.resume:
            self.run_dir = self.resume
            meta_file = self.run_dir / "metadata.json"
            if not meta_file.is_file():
                raise FileNotFoundError(f"{meta_file} not found — not a run directory")
            self.meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.run_id = self.meta["run_id"]
            self.isolation = self.meta.get("isolation", self.isolation)
            self.commit = self.meta.get("commit", self.commit)
            self.project = Path(self.meta["project"])
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base_id = f"{stamp}-{slugify(self.workflow.name)[:40].strip('-') or 'run'}"
            parent = self.runs_dir / slugify(self.project.name)
            # Two runs in the same second must not share a directory or a branch.
            n = 1
            self.run_id = base_id
            while (parent / self.run_id).exists() or _git(
                    self.project, "rev-parse", "--verify", "--quiet", f"refs/heads/agency/{self.run_id}", check=False):
                n += 1
                self.run_id = f"{base_id}-{n}"
            self.run_dir = parent / self.run_id
            self.run_dir.mkdir(parents=True, exist_ok=False)
        for sub in ("steps", "prompts"):
            (self.run_dir / sub).mkdir(exist_ok=True)
        wf.dump(self.workflow, self.run_dir / "workflow.json", fmt="json")

        self.branch = None
        self.base_sha = None
        is_git = _git(self.project, "rev-parse", "--is-inside-work-tree", check=False) == "true"
        if self.isolation == "worktree":
            if not is_git:
                raise RuntimeError(f"{self.project} is not a git repository — use --isolation inplace, "
                                   "or `git init` and commit first")
            top = Path(_git(self.project, "rev-parse", "--show-toplevel"))
            sub = self.project.relative_to(top) if self.project != top else Path(".")
            if self.resume and self.meta.get("worktree"):
                wt = Path(self.meta["worktree"])
                if not wt.is_dir():
                    raise RuntimeError(f"worktree {wt} of this run is gone — cannot resume in place")
                self.branch, self.base_sha = self.meta["branch"], self.meta["base"]
            else:
                base = str(self.workflow.project.get("base") or "HEAD")
                self.base_sha = _git(top, "rev-parse", "--verify", f"{base}^{{commit}}")
                if _git(top, "status", "--porcelain", check=False):
                    self.echo("  ! the project has uncommitted changes; the run starts from "
                              f"{base} ({self.base_sha[:8]}) without them")
                self.branch = f"agency/{self.run_id}"
                wt = self.run_dir / "worktree"
                _git(top, "worktree", "add", "-q", "-b", self.branch, str(wt), self.base_sha)
            self.worktree = wt
            self.cwd = (wt / sub).resolve()
        else:
            self.worktree = None
            self.cwd = self.project
            if is_git:
                self.base_sha = _git(self.project, "rev-parse", "HEAD", check=False) or None
            if self.provider.edits_files:
                self.echo("  ! isolation 'inplace': agents edit the project directory directly")

        self.context = {i["name"]: str(i["default"]) for i in self.workflow.inputs if "default" in i}
        self.context.update(self.inputs)
        self.context.update({
            "project_path": str(self.cwd), "project_name": self.project.name, "run_id": self.run_id,
            "base_ref": self.base_sha or "HEAD", "branch": self.branch or "",
        })
        self.meta.update({
            "run_id": self.run_id, "workflow": self.workflow.name, "project": str(self.project),
            "isolation": self.isolation, "commit": self.commit, "provider": self.provider_name,
            "branch": self.branch, "base": self.base_sha,
            "worktree": str(self.worktree) if self.worktree else None,
            "cwd": str(self.cwd),
        })
        self._restore()

    def _restore(self) -> None:
        old = self.meta.get("steps", {}) if self.resume else {}
        for s in self.workflow.steps:
            st = StepState(**{k: v for k, v in old.get(s.id, {}).items() if k in StepState.__dataclass_fields__})
            if st.status not in ("done", "skipped"):
                st = StepState()
            self.states[s.id] = st
        if not self.resume:
            return
        reset: set[str] = set()
        if self.from_step:
            if self.from_step not in self.states:
                raise ValueError(f"--from: no step '{self.from_step}'")
            reset = {self.from_step} | self.dag.descendants(self.from_step)
        # Rolling back a commit also rolls back every commit made after it.
        commits = [(sid, st) for sid, st in self.states.items() if st.commit]
        if self.worktree and commits and reset & {sid for sid, _ in commits}:
            order = _git(self.worktree, "rev-list", "--reverse", f"{self.base_sha}..HEAD").split()
            pos = {c: i for i, c in enumerate(order)}
            first = min(pos.get(st.commit, 10**9) for sid, st in commits if sid in reset)
            if first < 10**9:
                reset |= {sid for sid, st in commits if pos.get(st.commit, -1) >= first}
                target = order[first - 1] if first > 0 else self.base_sha
                _git(self.worktree, "reset", "-q", "--hard", target)
                self.echo(f"  ↺ worktree reset to {target[:8]} (before {first and order[first][:8] or 'the first step'})")
        for sid in reset:
            self.states[sid] = StepState()
        for s in self.workflow.steps:
            st = self.states[s.id]
            if s.output and st.status == "done" and st.output_file:
                self.context[s.output] = Path(self.run_dir, st.output_file).read_text(encoding="utf-8")
            elif s.output and st.status == "skipped":
                self.context[s.output] = ""

    def _save(self) -> None:
        with self.lock:
            self.meta["steps"] = {sid: st.to_dict() for sid, st in self.states.items()}
            self.meta["updated"] = time.time()
            tmp = self.run_dir / "metadata.json.tmp"
            tmp.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")
            tmp.replace(self.run_dir / "metadata.json")

    # --- one step -------------------------------------------------------------------

    def _system_prompt(self, step: wf.Step, agent) -> str:
        scope = f"- Your part of the project: `{step.workdir}/`.\n" if step.workdir and step.workdir != "." else ""
        rules = ORCHESTRATION_RULES.format(
            run_id=self.run_id, step_id=step.id,
            mode_text="you may edit files" if step.mode == "write" else "read-only",
            branch_text=f" (an isolated git worktree on branch `{self.branch}`)" if self.branch else "",
            scope_text=scope, mode_rules=WRITE_RULES if step.mode == "write" else READ_RULES)
        header = f"# {agent.name}\n\n{agent.description}\n\n" if agent.description else f"# {agent.name}\n\n"
        return header + agent.body.strip() + "\n" + rules

    def _prompt(self, step: wf.Step) -> str:
        text = wf.render(step.task, self.context)
        if step.acceptance:
            text += "\n\nACCEPTANCE CRITERIA (your answer must meet them):\n" + wf.render(step.acceptance, self.context)
        return text

    def _approve(self, step: wf.Step) -> tuple[bool, str]:
        text = wf.render(step.prompt or step.task or f"Continue after {', '.join(step.depends_on)}?", self.context)
        if self.yes:
            return True, "approved (--yes)"
        if not sys.stdin.isatty():
            return False, "approval needed but stdin is not a terminal — rerun with --yes or --resume interactively"
        with self.lock:
            self.echo(f"\n  ? {step.id}: {text}")
            answer = input("    approve? [y/N] ").strip().lower()
        return (answer in ("y", "yes", "j", "ja")), f"answered '{answer}'"

    def _commit(self, step: wf.Step, agent) -> tuple[str | None, str | None]:
        root = self.worktree or self.project
        _git(root, "add", "-A")
        if not _git(root, "diff", "--cached", "--name-only"):
            return None, None
        first = next((l.strip() for l in self.workflow.description.splitlines() if l.strip()), self.workflow.name)
        msg = (f"agency({step.id}): {agent.name}\n\n{first[:200]}\n\n"
               f"Agency-Run: {self.run_id}\nAgency-Step: {step.id}\nAgency-Agent: {agent.slug}\n")
        _git(root, *_git_identity(root), "commit", "-q", "--no-verify", "-m", msg)
        sha = _git(root, "rev-parse", "HEAD")
        stat = _git(root, "show", "--shortstat", "--format=", "HEAD").strip()
        return sha, stat

    def _guard_read_only(self, step: wf.Step) -> str | None:
        """A read step that changed files gets its changes discarded. Only in
        the run's own worktree: in place, the user's own uncommitted work would
        look the same, and nothing of theirs is ever thrown away."""
        if not self.worktree or not _git(self.worktree, "status", "--porcelain", check=False):
            return None
        _git(self.worktree, "reset", "-q", "--hard")
        _git(self.worktree, "clean", "-q", "-fd")
        return "read-only step modified files; changes were discarded"

    def _run_step(self, step: wf.Step, index: int) -> StepState:
        st = self.states[step.id]
        st.started = time.time()
        if step.type == "approval":
            ok, note = self._approve(step)
            st.status, st.note = ("done", note) if ok else ("failed", note)
            if not ok:
                st.error = note
            st.ended = time.time()
            return st
        agent = self.roster.resolve(step.agent)
        st.agent, st.agent_name = agent.slug, agent.name
        settings = {**self.settings, **step.llm}
        prompt = self._prompt(step)
        req = providers.Request(step_id=step.id, agent=agent.slug, agent_name=agent.name,
                                system_prompt=self._system_prompt(step, agent), prompt=prompt,
                                cwd=self.cwd, mode=step.mode, settings=settings,
                                log_dir=self.run_dir / "prompts")
        retries = int(settings.get("retry", 0))
        result = None
        for attempt in range(retries + 1):
            result = self.provider.run(req)
            if result.ok:
                break
            if attempt < retries:
                self.echo(f"  ↻ {step.id}: {result.error[:160]} — retry {attempt + 1}/{retries}")
                time.sleep(min(30, 2 ** attempt * 3))
        assert result is not None
        st.cost_usd = result.cost_usd
        out_name = f"steps/{index:02d}-{step.id}.md"
        (self.run_dir / out_name).write_text(result.output or "", encoding="utf-8")
        st.output_file = out_name
        if step.mode == "read" and self.provider.edits_files:
            warn = self._guard_read_only(step)
            if warn:
                st.note = warn
        if not result.ok:
            st.status, st.error = "failed", result.error
            if step.mode == "write" and self.worktree and self.provider.edits_files:
                # Keep the half-done edits for inspection, out of the next step's way.
                if _git(self.worktree, "status", "--porcelain", check=False):
                    _git(self.worktree, "stash", "push", "-u", "-q", "-m", f"agency {self.run_id} {step.id} (failed)")
                    st.note = "partial changes stashed in the worktree (git stash list)"
        else:
            st.status = "done"
            if step.mode == "write" and self.commit and self.provider.edits_files:
                st.commit, st.diffstat = self._commit(step, agent)
        st.ended = time.time()
        if step.output:
            self.context[step.output] = result.output if result.ok else ""
        return st

    # --- scheduling -----------------------------------------------------------------------

    def _decide(self, sid: str) -> str | None:
        """'run', 'skip', 'block', or None (not ready yet)."""
        step = self.dag.steps[sid]
        deps = [self.states[d].status for d in self.dag.deps[sid]]
        if any(d not in TERMINAL for d in deps):
            return None
        if step.depends_on_mode == "all" and any(d in ("failed", "blocked") for d in deps):
            return "block"
        if step.depends_on_mode == "any_completed" and deps and not any(d == "done" for d in deps):
            return "block"
        return "run"

    def _fmt(self, sid: str, st: StepState) -> str:
        dur = f"{(st.ended or time.time()) - (st.started or time.time()):.0f}s" if st.started else ""
        icon = {"done": "✓", "failed": "✗", "skipped": "–", "blocked": "⊘"}.get(st.status, "?")
        extra = []
        if st.commit:
            extra.append(f"commit {st.commit[:8]}" + (f" ({st.diffstat})" if st.diffstat else ""))
        elif st.status == "done" and self.dag.steps[sid].mode == "write" and self.provider.edits_files:
            extra.append("no changes")
        if st.cost_usd:
            extra.append(f"${st.cost_usd:.2f}")
        if st.note:
            extra.append(st.note)
        if st.error:
            extra.append(st.error.splitlines()[0][:160])
        return f"  {icon} {sid:<26} {st.agent_name or self.dag.steps[sid].type:<28} {dur:>6}  " + "; ".join(extra)

    def run(self) -> RunResult:
        self._prepare()
        order = self.dag.order()
        index = {sid: i + 1 for i, sid in enumerate(order)}
        self.echo(f"Run {self.run_id}  ·  provider {self.provider_name}  ·  {len(order)} steps")
        if self.branch:
            self.echo(f"  branch {self.branch} from {self.base_sha[:8]}  ·  worktree {self.worktree}")
        self.echo(f"  outputs {self.run_dir}")
        self._save()

        running: dict[Future, tuple[str, bool]] = {}
        writer_running = False
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while True:
                progressed = False
                for sid in order:
                    st = self.states[sid]
                    if st.status != "pending":
                        continue
                    decision = self._decide(sid)
                    if decision is None:
                        continue
                    step = self.dag.steps[sid]
                    if decision == "block":
                        st.status = "blocked"
                        st.error = "a dependency failed"
                        if step.output:
                            self.context[step.output] = ""
                        self.echo(self._fmt(sid, st))
                        progressed = True
                        continue
                    if step.condition:
                        try:
                            run_it = wf.evaluate_condition(step.condition, self.context)
                        except wf.WorkflowError as e:
                            st.status, st.error = "failed", str(e)
                            self.echo(self._fmt(sid, st))
                            progressed = True
                            continue
                        if not run_it:
                            st.status, st.note = "skipped", "condition not met"
                            if step.output:
                                self.context[step.output] = ""
                            self.echo(self._fmt(sid, st))
                            progressed = True
                            continue
                    exclusive = step.mode == "write" or step.type == "approval"
                    if writer_running or (exclusive and running) or len(running) >= self.concurrency:
                        continue
                    st.status = "running"
                    who = self.roster.resolve(step.agent).name if step.type == "agent" else "approval"
                    self.echo(f"  ▶ {sid:<26} {who:<28} {'writes' if step.mode == 'write' else 'reads'}"
                              + (f" @{step.workdir}" if step.workdir and step.workdir != "." else ""))
                    fut = pool.submit(self._run_step, step, index[sid])
                    running[fut] = (sid, exclusive)
                    writer_running = writer_running or exclusive
                    progressed = True
                    if exclusive:
                        break
                self._save()
                if not running:
                    if progressed:
                        continue
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    sid, exclusive = running.pop(fut)
                    try:
                        fut.result()
                    except Exception as e:  # a bug or git failure: record it, keep the run consistent
                        st = self.states[sid]
                        st.status, st.error, st.ended = "failed", f"{type(e).__name__}: {e}", time.time()
                        if self.dag.steps[sid].output:
                            self.context[self.dag.steps[sid].output] = ""
                    if exclusive:
                        writer_running = False
                    self.echo(self._fmt(sid, self.states[sid]))
                self._save()

        ok = all(st.status in ("done", "skipped") for st in self.states.values())
        self.meta["ok"] = ok
        self._save()
        self._summary(ok)
        return RunResult(ok=ok, run_dir=self.run_dir, branch=self.branch, worktree=self.worktree,
                         states=self.states, base=self.base_sha)

    def _summary(self, ok: bool) -> None:
        lines = [f"# {self.workflow.name}", "", f"- Run: `{self.run_id}` — {'succeeded' if ok else 'did not finish'}",
                 f"- Project: `{self.project}`", f"- Provider: {self.provider_name}"]
        if self.branch:
            lines.append(f"- Branch: `{self.branch}` (from `{self.base_sha[:8]}`), worktree `{self.worktree}`")
        lines += ["", "| Step | Agent | Status | Commit | Note |", "|---|---|---|---|---|"]
        for sid in self.dag.order():
            st = self.states[sid]
            lines.append(f"| {sid} | {st.agent_name or self.dag.steps[sid].type} | {st.status} | "
                         f"{(st.commit or '')[:8]} | {(st.error or st.note or st.diffstat or '').splitlines()[0] if (st.error or st.note or st.diffstat) else ''} |")
        last = next((sid for sid in reversed(self.dag.order()) if self.states[sid].status == "done"
                     and self.states[sid].output_file), None)
        if last:
            lines += ["", f"## Final output ({last})", "",
                      Path(self.run_dir, self.states[last].output_file).read_text(encoding="utf-8")]
        (self.run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.echo("")
        self.echo(f"{'Done' if ok else 'Stopped'}: " + ", ".join(
            f"{sum(1 for s in self.states.values() if s.status == k)} {k}" for k in TERMINAL
            if any(s.status == k for s in self.states.values())))
        self.echo(f"  summary  {self.run_dir / 'summary.md'}")
        if self.branch:
            top = _git(self.project, "rev-parse", "--show-toplevel", check=False) or str(self.project)
            self.echo(f"  review   git -C {top} log --stat {self.base_sha[:8]}..{self.branch}")
            self.echo(f"  merge    git -C {top} merge {self.branch}")
            self.echo(f"  cleanup  git -C {top} worktree remove {self.worktree} && git -C {top} branch -D {self.branch}")
        if not ok:
            self.echo(f"  resume   agency-orchestrate run --resume {self.run_dir}")
