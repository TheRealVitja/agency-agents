"""Agency orchestrator: hand a task to an existing project and let the agents
whose expertise matches each part of it do the work, in dependency order.

Modules:
  roster    — the repo's agent files, resolvable by slug, name, stem or path
  workflow  — workflow files (YAML/JSON), validation, templates, conditions
  dag       — dependency graph of a workflow: cycles, levels, rendering
  project   — analysis of a target project: components, tags, dependency graph
  routing   — which agent takes which component or intent (routing.json)
  planner   — task + project analysis -> workflow
  providers — how a step reaches a model: claude-code, command, dry-run
  runner    — executes a workflow inside an isolated git worktree
"""

__version__ = "0.1.0"
