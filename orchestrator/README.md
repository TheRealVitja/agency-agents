# Agency Orchestrator

Hand a task to an **existing project** and let the agents whose expertise
matches each part of it do the work — in the order the project's own
dependency graph dictates.

```bash
./orchestrator/agency-orchestrate do \
  "Add a discount column to orders and show the discounted price in the web app" \
  --project ~/dev/shop
```

What happens:

1. **Analyze** the project: every directory with a build manifest is a
   component (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`,
   `*.uproject`, Terraform, …). Its dependencies say what it is built with,
   and imports, manifests, `docker-compose` and (optionally) a
   [Graphify](https://github.com/safishamsi/graphify) graph say which
   components depend on which.
2. **Route** each component to the agent for its stack — React →
   Frontend Developer, FastAPI → Backend Architect, `*.uproject` → Unreal
   Systems Engineer, Terraform → DevOps Automator — from the rules in
   [`routing.json`](routing.json). Cross-cutting layers get their specialist
   first when the task is about them: “add a *column*” sends the Database
   Optimizer into the API service before the Backend Architect builds on it.
3. **Plan** a workflow: an architect's brief (read-only) decides which
   components the task touches; implementation steps follow the dependency
   graph (a library before the service that imports it, the API before the UI
   that calls it); then tests, a code review, and reviews for intents the task
   mentions (security, performance, accessibility, privacy).
4. **Run** it in an isolated git worktree on a new branch `agency/<run-id>`,
   one commit per agent. Your working tree is never touched; you review the
   branch and merge it — or don't.

```
  L1 → [brief] Software Architect 👁
  L2 → [impl-shop-core] Senior Developer ✎ @libs/core
  L3 → [impl-infra] DevOps Automator ✎ @infra
  L4 → [database-shop-api] Database Optimizer ✎ @services/api
  L5 → [impl-shop-api] Backend Architect ✎ @services/api
  L6 → [impl-ui] Frontend Developer ✎ @packages/ui
  L7 → [impl-shop-web] Frontend Developer ✎ @web
  L8 → [test] API Tester ✎
  L9 → [review] Code Reviewer 👁
```

Requirements: Python 3.11+, git, and for real runs the
[Claude Code](https://claude.com/claude-code) CLI logged in (no API key
needed). PyYAML is optional — without it, workflows are JSON.

## Commands

| Command | What it does |
|---|---|
| `analyze PROJECT` | Components, stacks, dependency graph, and who would own each part (`--json` for tooling) |
| `route "TEXT" [-p PROJECT]` | Which agents a task would go to, and why |
| `plan "TASK" -p PROJECT [-o plan.yaml]` | Write the workflow without running it — edit it, then `run` it |
| `validate WORKFLOW` | Check agents, dependencies, cycles, template variables |
| `graph WORKFLOW [-f mermaid\|dot]` | Show the execution graph |
| `run WORKFLOW` | Execute a workflow (`--resume RUN_DIR [--from STEP]` to continue) |
| `do "TASK" -p PROJECT` | `plan` + `run` in one go |
| `agents [--search Q]` | List or search the roster |

Useful flags for `plan`/`do`:

- `-c/--component NAME` — limit the work to these components (no brief; the
  task names the scope). Repeatable.
- `--impact` — also update the components that depend on the selected ones.
- `--lead/--no-lead`, `--no-tests`, `--no-review`, `--no-intents`.
- `--base REF` — branch the run from another ref than `HEAD`.

And for `run`/`do`:

- `--provider claude-code|command|dry-run` — `dry-run` previews every prompt
  without running anything (no branch, no worktree).
- `--model M` — e.g. `sonnet`, `opus`, `haiku` for Claude Code.
- `--allow 'Bash(make test:*)'` — pre-approve more commands (see Safety).
- `--isolation inplace [--commit]` — work directly in the project directory.
- `-y/--yes` — approve `type: approval` steps automatically.

## Where things go

Each run gets a directory under `~/.agency-orchestrator/runs/<project>/<run-id>/`
(override with `--runs-dir` or `AGENCY_RUNS_DIR`):

```
workflow.json      the exact workflow that ran
metadata.json      status, agent, commit, cost and timing per step (used by --resume)
steps/NN-<id>.md   each agent's final answer
prompts/<id>.*     the persona (system prompt) and task each agent received
summary.md         table of steps + the final output
worktree/          the run's git worktree on branch agency/<run-id>
```

When the run ends it prints how to review, merge and clean up:

```bash
git -C ~/dev/shop log --stat <base>..agency/<run-id>
git -C ~/dev/shop merge agency/<run-id>
git -C ~/dev/shop worktree remove <run dir>/worktree && git -C ~/dev/shop branch -D agency/<run-id>
```

If a step fails, everything that depends on it is blocked, the step's partial
edits are stashed in the worktree for inspection, and
`run --resume <run dir>` continues from there. `--from STEP` redoes a step and
everything after it, rolling the branch back to before its commit.

## Workflow format

Plans are ordinary workflow files you can edit before running. The format is
a superset of [agency-orchestrator](https://github.com/jnMetaCode/agency-orchestrator)'s
YAML, so its workflows load as they are (`role:` works as `agent:`; media
steps are not supported).

```yaml
name: "shop: add a discount column"
project: {path: /home/me/dev/shop, base: HEAD}
llm: {provider: claude-code, model: sonnet}
concurrency: 2
steps:
  - id: brief
    agent: software-architect        # slug, display name, file stem or division path
    mode: read                       # read (default) or write
    task: "Plan this change ... {{project_name}}"
    output: brief
  - id: impl-shop-api
    agent: backend-architect
    mode: write
    workdir: services/api            # the part of the project this step owns
    depends_on: [brief]
    condition: "{{brief}} contains AFFECTED: component:shop-api;"
    task: "TASK: ... {{brief}}"
    output: impl_shop_api
    llm: {allowed_tools: ["Bash(pytest:*)"]}
  - id: ship-it
    type: approval
    prompt: "Merge-ready?"
    depends_on: [impl-shop-api]
```

- `{{var}}` reads an input (`-i name=value`, `-i name=@file`), a built-in
  (`project_path`, `project_name`, `run_id`, `base_ref`, `branch`), or the
  `output` of a step it depends on — `validate` rejects anything else.
- `condition: "<text> contains|equals <word>"` — false means **skipped**: the
  step does not run, its output is empty, and steps after it still run.
- A **failed** step blocks everything that depends on it
  (`depends_on_mode: any_completed` relaxes that for merge steps).
- `mode: write` steps run one at a time (two agents editing one tree would
  trample each other) and are committed after each; `mode: read` steps run in
  parallel up to `concurrency` and may not change files.

## Routing rules

[`routing.json`](routing.json) is data, not code — extend it for your stacks:

- `tags` are checked in order against each component's dependencies
  (`deps`, fnmatch, any ecosystem), files (`files`) and languages. The first
  matching **platform** tag picks the implementer; **language** tags are the
  fallback; **concern** tags (database, auth, payments, realtime, i18n,
  search, container, CI, e2e tests, netcode, game audio, …) put their
  specialist on a component before its owner when the task hits one of their
  `keywords` — and, when there is a brief, only where the architect marks it
  (`SPECIALIST: <tag> for component:<name>;`).
- `names` (component name patterns) and `requires_deps` are conditions, not
  evidence: `{"names": ["*network*"], "requires_deps": ["unity-asmdef"]}`
  matches a Unity assembly called `*.Networking`, never a Go package.
- `layer_order` breaks ties when the graph says nothing: infrastructure, data
  and ML before APIs, APIs before desktop/mobile/web clients, docs last.
- `phases` names the architect, tester (per tag), reviewer and fallback.
- `intents` add read-only reviews when the task mentions them.

Every agent named there must exist in the roster — the tests check it, so a
renamed agent fails CI instead of failing a run.

## Unity projects

A Unity project (`ProjectSettings/ProjectVersion.txt`) is one component, its
packages read from `Packages/manifest.json`; every assembly definition
(`.asmdef`) inside it is a component of its own, and its `references` — by
name or `GUID:` — are the dependency graph. File listing follows
`.gitignore`, so `Library/`, `Builds/` and generated `.csproj` files never
count.

Routing: gameplay assemblies → Unity Architect, an assembly named
`*Network*`/`*Netcode*`/`*Multiplayer*` → Unity Multiplayer Engineer, editor-only
assemblies → Unity Editor Tool Developer, test assemblies → Unity Architect.
Referencing Netcode does not make a system a networking job: the Multiplayer
Engineer works on it first only when the task is about sync, RPCs, co-op and
the like. Shader, art-pipeline and audio work get the Shader Graph Artist,
Technical Artist and Game Audio Engineer the same way; balancing and
gameplay-loop tasks add an Economy Designer or Game Designer review.

Agents cannot open the Unity editor from the run's worktree, so nothing
compiles or runs tests there: review the branch in Unity (it also creates the
`.meta` files for new scripts) before you merge. Project MCP servers such as
a Unity bridge stay off in runs — they would act on the editor that has your
main checkout open, not on the run's branch.

## Per-project overrides

Put `.agency/project.json` (or `.yaml`) in the target project when the
analysis needs help:

```json
{
  "ignore": ["legacy/"],
  "components": [
    {"path": "services/api", "name": "api", "tags": ["api"], "test_command": "make test"},
    {"path": "infra", "agent": "sre-site-reliability-engineer", "depends_on": ["api"]}
  ]
}
```

`agent` pins the implementer, `tags` add stack tags, `depends_on` adds edges
the analyzer cannot see (a frontend talking to an API over HTTP, for
example — `docker-compose` `depends_on` is picked up automatically).

## Graphify

If the project has a [Graphify](https://github.com/safishamsi/graphify) graph
(`graphify-out/graph.json`, or `--graphify PATH`), its tree-sitter
`imports`/`calls`/`inherits` edges between files are added to the component
graph, and its external imports to each component's dependencies. Graphify's
code pass is local and needs no model:

```bash
uv tool install graphifyy && graphify update ~/dev/shop
```

Without it, the analyzer's own scan covers Python, JS/TS and Go imports,
manifest dependencies and `docker-compose`.

## Safety

- Agents run headless, so nothing can ask for permission mid-run: anything
  not pre-approved is denied. Write steps get file editing plus read-only git
  and the build/test commands of their component's stack (`pytest`,
  `npm test`, `go test`, `cargo test`, …). Installing packages, deploying and
  `terraform apply` are deliberately **not** pre-approved — add them per
  workflow (`llm.allowed_tools`) or per run (`--allow`) when you mean it.
- A step with a `workdir` starts in that directory (the rest of the project
  stays readable), so `pytest` or `npm test` match their pre-approval as
  typed. Every refused call is listed in the step's status line and under
  “Refused tool calls” in `summary.md` — if an agent's answer says tests
  passed but its `pytest` call was refused, believe the list. Install the
  project's dev dependencies before a run if you want agents to run tests;
  the orchestrator does not install anything.
- Read steps cannot edit: edit tools are disallowed, and if a read step
  changes the worktree anyway, the change is discarded and noted.
- Agents are told not to commit, push or switch branches; the orchestrator
  makes one commit per write step on the run branch and never touches your
  branches or working tree (unless you choose `--isolation inplace`).

## Other providers

`--provider command --command '<template>'` runs any CLI with the prompt on
stdin and takes stdout as the answer. Placeholders: `{system_file}`,
`{prompt_file}`, `{cwd}`, `{mode}`, `{agent}`, `{step}`, `{model}`; the same
values are in `AGENCY_*` environment variables. The test suite drives the
whole engine this way with [`tests/fake_agent.py`](tests/fake_agent.py).

## Tests

```bash
python3 -m unittest discover -s orchestrator/tests -v
```

They cover the roster (ids match `scripts/lib.sh`), validation, the graph,
project analysis (manifest, import, compose, Graphify and override edges),
routing, planning, and full runs with a fake agent: scoping by the brief,
commit-per-step, failure + resume, `--from` rollback, the read-only guard,
dry runs and approvals. CI runs them in `test-orchestrator.yml`.
