"""Tests for the orchestrator. Run: python3 -m unittest discover -s orchestrator/tests"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from agency_orchestrator import dag, planner, project as proj, providers, workflow as wf  # noqa: E402
from agency_orchestrator.cli import main as cli_main  # noqa: E402
from agency_orchestrator.roster import Roster, UnknownAgentError, parse_agent_file, slugify  # noqa: E402
from agency_orchestrator.routing import Rules  # noqa: E402
from agency_orchestrator.runner import Runner  # noqa: E402

FIXTURE = HERE / "fixtures" / "shop"
FAKE = [sys.executable, str(HERE / "fake_agent.py")]
TASK = "Add a discount column to orders and show the discounted price in the web app"

ROSTER = Roster.load()
RULES = Rules.load()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def git_project(tmp: Path) -> Path:
    dest = tmp / "shop"
    shutil.copytree(FIXTURE, dest)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return dest


class RosterTest(unittest.TestCase):
    def test_ids_follow_lib_sh(self):
        self.assertEqual(slugify("C++ & Rust: Systems / Engineer"), "c-rust-systems-engineer")
        a = ROSTER.resolve("frontend-developer")
        for ref in ("Frontend Developer", "engineering-frontend-developer",
                    "engineering/engineering-frontend-developer", "engineering/engineering-frontend-developer.md"):
            self.assertIs(ROSTER.resolve(ref), a)

    def test_unknown_agent_suggests(self):
        with self.assertRaises(UnknownAgentError) as cm:
            ROSTER.resolve("frontend-develper")
        self.assertIn("frontend-developer", str(cm.exception))

    def test_frontmatter_folds_and_unquotes(self):
        meta, body = parse_agent_file("---\nname: \"A: B\"\ndescription: one\n  two\ncolor: red\n---\n\n# Body\n")
        self.assertEqual(meta["name"], "A: B")
        self.assertEqual(meta["description"], "one two")
        self.assertEqual(body, "# Body")
        self.assertIsNone(parse_agent_file("# no frontmatter"))

    def test_search_prefers_whole_words(self):
        top = [a.slug for _, a in ROSTER.search("postgres query index", limit=3)]
        self.assertIn("database-optimizer", top)


class RoutingRulesTest(unittest.TestCase):
    def test_rules_name_real_agents(self):
        self.assertEqual(RULES.problems(ROSTER), [])


class WorkflowTest(unittest.TestCase):
    def wf(self, steps, **top):
        return wf.from_dict({"name": "t", "steps": steps, **top})

    def test_validation_finds_every_problem(self):
        w = self.wf([
            {"id": "a", "agent": "code-reviewer", "task": "x {{missing}}", "output": "out"},
            {"id": "b", "agent": "nope-agent", "task": "y {{out}}"},
            {"id": "c", "agent": "code-reviewer", "task": "z", "depends_on": ["ghost"], "output": "out"},
        ])
        problems = "\n".join(wf.validate(w, ROSTER))
        self.assertIn("unknown agent 'nope-agent'", problems)
        self.assertIn("unknown step 'ghost'", problems)
        self.assertIn("written by both", problems)

    def test_template_needs_an_ancestor(self):
        w = self.wf([
            {"id": "a", "agent": "code-reviewer", "task": "x", "output": "out"},
            {"id": "b", "agent": "code-reviewer", "task": "{{out}}"},
            {"id": "c", "agent": "code-reviewer", "task": "{{nothing}}", "depends_on": ["a"]},
        ])
        problems = "\n".join(wf.validate(w, ROSTER))
        self.assertIn("step 'b' reads {{out}} from 'a' but does not depend on it", problems)
        self.assertIn("{{nothing}}, which no input or step defines", problems)

    def test_cycle_is_named(self):
        w = self.wf([
            {"id": "a", "agent": "code-reviewer", "task": "x", "depends_on": ["c"]},
            {"id": "b", "agent": "code-reviewer", "task": "x", "depends_on": ["a"]},
            {"id": "c", "agent": "code-reviewer", "task": "x", "depends_on": ["b"]},
        ])
        self.assertTrue(any("dependency cycle: " in p for p in wf.validate(w, ROSTER)))

    def test_ao_workflows_load(self):
        w = self.wf([
            {"id": "r", "role": "engineering/engineering-code-reviewer", "task": "x", "emoji": "🔍", "type": "normal"},
        ], agents_dir="agency-agents", llm={"provider": "deepseek", "model": "deepseek-chat"})
        self.assertEqual(w.steps[0].agent, "engineering/engineering-code-reviewer")
        self.assertEqual(w.steps[0].mode, "read", "nothing writes to a project unless it says so")
        self.assertTrue(w.warnings)
        self.assertEqual(wf.validate(w, ROSTER), [])
        with self.assertRaises(wf.WorkflowError):
            self.wf([{"id": "v", "type": "video", "task": "x"}])

    def test_conditions(self):
        ctx = {"x": "Verdict: contains APPROVE"}
        self.assertTrue(wf.evaluate_condition("{{x}} contains approve", ctx))
        self.assertFalse(wf.evaluate_condition("{{x}} equals approve", ctx))
        with self.assertRaises(wf.WorkflowError):
            wf.parse_condition("{{x}} not contains y")

    def test_yaml_round_trip(self):
        w = self.wf([{"id": "a", "agent": "code-reviewer", "task": "line 1\nline 2", "mode": "write"}])
        again = wf.from_dict(wf.parse_text(wf.dump(w)))
        self.assertEqual(again.steps[0].task, "line 1\nline 2")
        self.assertEqual(again.steps[0].mode, "write")


class DagTest(unittest.TestCase):
    def test_diamond_levels(self):
        w = wf.from_dict({"name": "d", "steps": [
            {"id": "a", "agent": "x", "task": "t"},
            {"id": "b", "agent": "x", "task": "t", "depends_on": ["a"]},
            {"id": "c", "agent": "x", "task": "t", "depends_on": ["a"]},
            {"id": "d", "agent": "x", "task": "t", "depends_on": ["b", "c"]},
        ]})
        g = dag.build(w)
        self.assertEqual(g.levels, [["a"], ["b", "c"], ["d"]])
        self.assertEqual(g.ancestors("d"), {"a", "b", "c"})
        self.assertEqual(g.descendants("a"), {"b", "c", "d"})
        self.assertIn("a --> b", dag.render_mermaid(g))


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.p = proj.analyze(FIXTURE, rules=RULES)
        self.by = {c.name: c for c in self.p.components}

    def test_components_and_stacks(self):
        self.assertEqual(set(self.by), {"shop-core", "shop-api", "ui", "shop-web", "infra"})
        self.assertIn("api", self.by["shop-api"].tags)
        self.assertIn("database", self.by["shop-api"].tags)
        self.assertIn("frontend", self.by["shop-web"].tags)
        self.assertEqual(self.by["infra"].tags, ["infra"])
        self.assertEqual(self.by["shop-api"].test_command, "pytest")

    def test_dependency_graph_from_three_sources(self):
        edges = {(e.source, e.target, e.kind) for e in self.p.edges}
        self.assertIn(("shop-api", "shop-core", "import"), edges)
        self.assertIn(("shop-api", "shop-core", "manifest"), edges)
        self.assertIn(("shop-web", "ui", "manifest"), edges)
        self.assertIn(("shop-web", "ui", "import"), edges)
        self.assertIn(("shop-web", "shop-api", "compose"), edges)

    def test_owners_follow_the_stack(self):
        owners = {a.component: a.agent for a in RULES.assign(self.p, TASK)}
        self.assertEqual(owners["shop-api"], "backend-architect")
        self.assertEqual(owners["shop-web"], "frontend-developer")
        self.assertEqual(owners["infra"], "devops-automator")
        self.assertEqual(owners["shop-core"], "senior-developer")
        concerns = {a.component: a.concerns for a in RULES.assign(self.p, TASK)}
        self.assertEqual(concerns["shop-api"], [("database", "database-optimizer")])

    def test_graphify_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shop"
            shutil.copytree(FIXTURE, root)
            (root / "graphify-out").mkdir()
            graph = {"directed": False, "multigraph": False, "graph": {}, "nodes": [
                {"id": "ui_index", "label": "index.tsx", "source_file": "packages/ui/src/index.tsx"},
                {"id": "core_pricing_total", "label": "total()", "source_file": "libs/core/shop_core/pricing.py"},
                {"id": "ref_left_pad", "label": "left-pad", "external": True},
            ], "links": [
                {"source": "ui_index", "target": "core_pricing_total", "relation": "calls", "confidence": "INFERRED"},
                {"source": "ui_index", "target": "ref_left_pad", "relation": "imports", "confidence": "EXTRACTED"},
            ]}
            (root / "graphify-out" / "graph.json").write_text(json.dumps(graph))
            (root / ".agency").mkdir()
            (root / ".agency" / "project.json").write_text(json.dumps({"components": [
                {"path": "infra", "name": "platform", "agent": "sre-site-reliability-engineer", "depends_on": ["shop-api"]},
            ]}))
            p = proj.analyze(root, rules=RULES)
            edges = {(e.source, e.target, e.kind) for e in p.edges}
            self.assertIn(("ui", "shop-core", "graphify"), edges)
            self.assertIn(("platform", "shop-api", "override"), edges)
            self.assertIn("left-pad", p.component("ui").dependencies)
            self.assertEqual(RULES.implementer(p.component("platform"))[0], "sre-site-reliability-engineer")


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.p = proj.analyze(FIXTURE, rules=RULES)

    def test_plan_follows_the_dependency_graph(self):
        w, notes = planner.plan(TASK, self.p, RULES, ROSTER)
        self.assertEqual(wf.validate(w, ROSTER), [])
        g = dag.build(w)
        ids = [s.id for s in w.steps]
        self.assertEqual(ids[0], "brief")
        anc = g.ancestors
        self.assertIn("impl-shop-core", anc("impl-shop-api"))
        self.assertIn("database-shop-api", anc("impl-shop-api"))
        self.assertIn("impl-ui", anc("impl-shop-web"))
        self.assertIn("impl-shop-api", anc("impl-shop-web"))
        self.assertTrue(all(w.step(f"impl-{c}").condition for c in ("shop-core", "shop-api", "ui", "shop-web", "infra")))
        self.assertEqual(w.step("impl-shop-api").agent, "backend-architect")
        self.assertEqual(w.step("database-shop-api").agent, "database-optimizer")
        self.assertEqual(w.step("test").agent, "api-tester")
        self.assertEqual(w.step("review").mode, "read")
        self.assertIn("Bash(pytest:*)", w.step("impl-shop-api").llm["allowed_tools"])
        # the web agent reads the API agent's report, its real dependency
        self.assertIn("{{impl_shop_api}}", w.step("impl-shop-web").task)

    def test_named_components_skip_the_brief(self):
        w, _ = planner.plan("Rename the Price component", self.p, RULES, ROSTER,
                            planner.PlanOptions(components=["ui"]))
        self.assertEqual([s.id for s in w.steps], ["impl-ui", "test", "review"])
        self.assertIsNone(w.step("impl-ui").condition)

    def test_impact_adds_dependents(self):
        w, _ = planner.plan("Change the Price props", self.p, RULES, ROSTER,
                            planner.PlanOptions(components=["ui"], impact=True, tests=False, review=False))
        self.assertEqual([s.id for s in w.steps], ["impl-ui", "impl-shop-web"])
        self.assertEqual(w.step("impl-shop-web").depends_on, ["impl-ui"])

    def test_intents_add_reviewers(self):
        w, _ = planner.plan("Fix the XSS vulnerability in the order list", self.p, RULES, ROSTER,
                            planner.PlanOptions(components=["shop-web"]))
        self.assertEqual(w.step("security-review").agent, "application-security-engineer")
        self.assertEqual(w.step("security-review").mode, "read")


class ProviderTest(unittest.TestCase):
    def test_claude_code_flags_by_mode(self):
        p = providers.ClaudeCodeProvider({"model": "sonnet", "allowed_tools": ["Bash(pytest:*)", "Write"]})
        req = providers.Request("s", "a", "A", "sys", "prompt", Path("."), "write", p.settings)
        argv = p.argv(req, Path("/tmp/sys.md"))
        self.assertIn("acceptEdits", argv)
        self.assertIn("Bash(pytest:*)", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        req.mode = "read"
        argv = p.argv(req, Path("/tmp/sys.md"))
        self.assertNotIn("acceptEdits", argv)
        allowed = argv[argv.index("--allowedTools") + 1:]
        self.assertNotIn("Write", allowed, "a read step never gets an edit tool, even if the workflow lists one")
        self.assertIn("--disallowedTools", argv)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = git_project(self.tmp)
        self.log = self.tmp / "calls.log"
        self.env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.env)))
        os.environ["FAKE_LOG"] = str(self.log)
        os.environ["FAKE_AFFECTED"] = "shop-api,shop-web"
        self.out = io.StringIO()

    def runner(self, w, **kw):
        kw.setdefault("provider", "command")
        kw.setdefault("settings", {"command": FAKE})
        return Runner(w, ROSTER, project=self.project, runs_dir=self.tmp / "runs", echo=self.out.write, **kw)

    def plan(self):
        w, _ = planner.plan(TASK, proj.analyze(self.project, rules=RULES), RULES, ROSTER)
        return w

    def calls(self) -> list[str]:
        return self.log.read_text().split("\n")[:-1] if self.log.exists() else []

    def test_brief_decides_scope_graph_decides_order(self):
        res = self.runner(self.plan()).run()
        self.assertTrue(res.ok, self.out.getvalue())
        st = res.states
        for skipped in ("impl-shop-core", "impl-infra", "impl-ui"):
            self.assertEqual(st[skipped].status, "skipped")
        for done in ("brief", "database-shop-api", "impl-shop-api", "impl-shop-web", "test", "review"):
            self.assertEqual(st[done].status, "done")
        order = [c.split()[0] for c in self.calls()]
        self.assertEqual(order, ["brief", "database-shop-api", "impl-shop-api", "impl-shop-web", "test", "review"])
        # one commit per write step, on the run's own branch
        log = git(self.project, "log", "--format=%s", f"main..{res.branch}").splitlines()
        self.assertEqual(log, ["agency(test): API Tester", "agency(impl-shop-web): Frontend Developer",
                               "agency(impl-shop-api): Backend Architect",
                               "agency(database-shop-api): Database Optimizer"])
        self.assertFalse((self.project / "agency-fake").exists(), "the user's working tree is never touched")
        self.assertEqual(git(self.project, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        # the persona reached the agent, and the brief reached the implementers
        out = (res.run_dir / st["impl-shop-api"].output_file).read_text()
        self.assertIn("persona: # Backend Architect", out)
        self.assertIn("saw the brief", out)

    def test_failure_blocks_dependents_and_resume_finishes(self):
        os.environ["FAKE_FAIL"] = "impl-shop-web"
        res = self.runner(self.plan()).run()
        self.assertFalse(res.ok)
        self.assertEqual(res.states["impl-shop-web"].status, "failed")
        self.assertEqual(res.states["test"].status, "blocked")
        self.assertIn("stashed", res.states["impl-shop-web"].note)
        del os.environ["FAKE_FAIL"]
        self.log.unlink()
        res2 = self.runner(self.plan(), resume=res.run_dir).run()
        self.assertTrue(res2.ok, self.out.getvalue())
        self.assertEqual([c.split()[0] for c in self.calls()], ["impl-shop-web", "test", "review"])

    def test_resume_from_rolls_back_later_commits(self):
        res = self.runner(self.plan()).run()
        self.assertTrue(res.ok)
        self.log.unlink()
        res2 = self.runner(self.plan(), resume=res.run_dir, from_step="impl-shop-api").run()
        self.assertTrue(res2.ok, self.out.getvalue())
        # everything after impl-shop-api in the graph is redone; database-shop-api (before it) is not
        self.assertEqual([c.split()[0] for c in self.calls()], ["impl-shop-api", "impl-shop-web", "test", "review"])
        log = git(self.project, "log", "--format=%s", f"main..{res.branch}").splitlines()
        self.assertEqual(len(log), 4, "redone steps replace their commits instead of stacking new ones")

    def test_read_only_step_cannot_change_the_tree(self):
        os.environ["FAKE_READ_WRITES"] = "1"
        w = wf.from_dict({"name": "r", "steps": [{"id": "look", "agent": "code-reviewer", "task": "look", "mode": "read"}]})
        res = self.runner(w).run()
        self.assertIn("discarded", res.states["look"].note)
        self.assertEqual(git(res.worktree, "status", "--porcelain"), "")

    def test_dry_run_leaves_no_trace(self):
        res = self.runner(self.plan(), provider="dry-run", settings={}).run()
        self.assertTrue(res.ok)
        self.assertIsNone(res.branch)
        self.assertEqual(git(self.project, "worktree", "list").count("\n"), 0)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_approval_without_terminal_fails_unless_yes(self):
        w = wf.from_dict({"name": "a", "steps": [
            {"id": "ok", "type": "approval", "prompt": "Go?"},
            {"id": "after", "agent": "code-reviewer", "task": "x", "depends_on": ["ok"]},
        ]})
        with unittest.mock.patch("sys.stdin", io.StringIO("")):
            res = self.runner(w).run()
        self.assertEqual(res.states["ok"].status, "failed")
        self.assertEqual(res.states["after"].status, "blocked")
        res = self.runner(w, yes=True).run()
        self.assertTrue(res.ok)


class CliTest(unittest.TestCase):
    def run_cli(self, *args) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_analyze_plan_validate_graph(self):
        code, out = self.run_cli("analyze", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertIn("shop-web → shop-api  [compose", out)
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "plan.yaml"
            code, out = self.run_cli("plan", TASK, "-p", str(FIXTURE), "-o", str(plan_file))
            self.assertEqual(code, 0, out)
            self.assertEqual(self.run_cli("validate", str(plan_file))[0], 0)
            code, out = self.run_cli("graph", str(plan_file), "-f", "mermaid")
            self.assertIn("brief -->", out)

    def test_errors_are_reported_not_raised(self):
        code, out = self.run_cli("plan", TASK, "-p", str(FIXTURE), "-c", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no component 'nope'", out)


if __name__ == "__main__":
    unittest.main()
