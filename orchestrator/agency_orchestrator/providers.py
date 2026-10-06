"""How a step reaches a model.

  claude-code  the Claude Code CLI, run headless in the project worktree with
               tools: write steps may edit files (acceptEdits), read steps may
               not (edit tools disallowed). Uses the logged-in subscription.
  command      any CLI: a command template, prompt on stdin, answer on stdout.
               Placeholders: {system_file} {prompt_file} {cwd} {mode} {agent}
               {step} {model}. Covers other coding agents and the test suite.
  dry-run      runs nothing; reports what each step would get. Answers the
               architect's brief with every AFFECTED line, so a preview shows
               the whole plan.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Tools a step may use without asking. Headless runs cannot answer a
# permission prompt, so anything not listed is denied. Projects add their own
# build/test commands per step (`llm.allowed_tools`) — the planner does this
# for each component's test command.
READ_TOOLS = ["Read", "Glob", "Grep", "LS", "Bash(git status:*)", "Bash(git diff:*)",
              "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)", "Bash(cat:*)"]
WRITE_TOOLS = READ_TOOLS + ["Edit", "MultiEdit", "Write", "NotebookEdit", "TodoWrite"]
EDIT_TOOLS = ["Edit", "MultiEdit", "Write", "NotebookEdit"]


@dataclass
class Request:
    step_id: str
    agent: str              # slug
    agent_name: str
    system_prompt: str
    prompt: str
    cwd: Path
    mode: str               # write | read
    settings: dict[str, Any] = field(default_factory=dict)
    log_dir: Path | None = None
    project_root: Path | None = None   # when cwd is a component inside it


@dataclass
class Result:
    ok: bool
    output: str
    error: str = ""
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    denied: list[str] = field(default_factory=list)   # tool calls refused for lack of permission


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"
    edits_files = True

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = dict(settings or {})

    def check(self) -> None:
        """Raise ProviderError if the provider cannot run here (missing binary)."""

    def run(self, req: Request) -> Result:  # pragma: no cover - interface
        raise NotImplementedError

    def _write_files(self, req: Request) -> tuple[Path, Path]:
        base = req.log_dir or Path(os.environ.get("TMPDIR", "/tmp"))
        base.mkdir(parents=True, exist_ok=True)
        sys_file = base / f"{req.step_id}.system.md"
        prompt_file = base / f"{req.step_id}.prompt.md"
        sys_file.write_text(req.system_prompt, encoding="utf-8")
        prompt_file.write_text(req.prompt, encoding="utf-8")
        return sys_file, prompt_file


def _timeout(req: Request) -> int:
    default = 1800 if req.mode == "write" else 900
    return int(req.settings.get("timeout", default))


class DryRunProvider(Provider):
    name = "dry-run"
    edits_files = False

    def run(self, req: Request) -> Result:
        self._write_files(req)
        marks = re.findall(r"^(?:AFFECTED: component:|SPECIALIST: )[^\n;]+;$", req.prompt, re.M)
        out = [f"[dry-run] {req.agent_name} ({req.agent}) would {req.mode} in {req.cwd}.",
               f"Prompt: {len(req.prompt)} characters, persona: {len(req.system_prompt)} characters."]
        if marks:
            out += ["", *marks]
        if "RESULT: PASS" in req.prompt:
            out.append("RESULT: PASS")
        return Result(ok=True, output="\n".join(out))


class CommandProvider(Provider):
    name = "command"

    def _argv(self, req: Request, sys_file: Path, prompt_file: Path) -> list[str]:
        cmd = self.settings.get("command")
        if not cmd:
            raise ProviderError("provider 'command' needs llm.command (a list or a string)")
        argv = shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]
        values = {"system_file": str(sys_file), "prompt_file": str(prompt_file), "cwd": str(req.cwd),
                  "mode": req.mode, "agent": req.agent, "step": req.step_id,
                  "model": str(req.settings.get("model") or "")}
        return [a.format(**values) for a in argv]

    def check(self) -> None:
        cmd = self.settings.get("command")
        if not cmd:
            raise ProviderError("provider 'command' needs llm.command (a list or a string)")
        exe = shlex.split(cmd)[0] if isinstance(cmd, str) else str(cmd[0])
        if not (Path(exe).exists() or shutil.which(exe)):
            raise ProviderError(f"command provider: '{exe}' not found")

    def run(self, req: Request) -> Result:
        sys_file, prompt_file = self._write_files(req)
        argv = self._argv(req, sys_file, prompt_file)
        env = dict(os.environ, AGENCY_STEP=req.step_id, AGENCY_AGENT=req.agent, AGENCY_MODE=req.mode,
                   AGENCY_SYSTEM_FILE=str(sys_file), AGENCY_PROMPT_FILE=str(prompt_file))
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, input=req.prompt, capture_output=True, text=True, cwd=req.cwd,
                                  env=env, timeout=_timeout(req))
        except subprocess.TimeoutExpired:
            return Result(False, "", f"timed out after {_timeout(req)}s", duration_s=time.monotonic() - start)
        except OSError as e:
            return Result(False, "", f"could not start {argv[0]}: {e}", duration_s=time.monotonic() - start)
        dur = time.monotonic() - start
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-1500:]
            return Result(False, proc.stdout, f"exit {proc.returncode}: {tail}", duration_s=dur)
        return Result(True, proc.stdout.strip(), duration_s=dur)


class ClaudeCodeProvider(Provider):
    name = "claude-code"

    def binary(self) -> str:
        return str(self.settings.get("command") or os.environ.get("AGENCY_CLAUDE_BIN") or "claude")

    def check(self) -> None:
        if not shutil.which(self.binary()):
            raise ProviderError(f"'{self.binary()}' not found — install Claude Code or set AGENCY_CLAUDE_BIN")

    def argv(self, req: Request, sys_file: Path) -> list[str]:
        s = req.settings
        args = [self.binary(), "-p", "--output-format", "json", "--no-session-persistence",
                "--append-system-prompt-file", str(sys_file)]
        if req.project_root and req.project_root != req.cwd:
            # The step starts in its component, so `pytest` / `npm test` match
            # their pre-approved rules without a `cd`; the rest of the project
            # stays readable.
            args += ["--add-dir", str(req.project_root)]
        extra_tools = [str(t) for t in (s.get("allowed_tools") or [])]
        if req.mode == "write":
            args += ["--permission-mode", str(s.get("permission_mode", "acceptEdits"))]
            tools = WRITE_TOOLS + extra_tools
        else:
            tools = READ_TOOLS + [t for t in extra_tools if t.split("(")[0] not in EDIT_TOOLS]
            args += ["--disallowedTools", *EDIT_TOOLS]
        args += ["--allowedTools", *dict.fromkeys(tools)]
        if s.get("model"):
            args += ["--model", str(s["model"])]
        if s.get("effort"):
            args += ["--effort", str(s["effort"])]
        if s.get("max_budget_usd"):
            args += ["--max-budget-usd", str(s["max_budget_usd"])]
        args += [str(a) for a in (s.get("extra_args") or [])]
        return args

    def run(self, req: Request) -> Result:
        sys_file, _ = self._write_files(req)
        argv = self.argv(req, sys_file)
        if req.log_dir:
            (req.log_dir / f"{req.step_id}.argv.json").write_text(json.dumps(argv, indent=2), encoding="utf-8")
        start = time.monotonic()
        try:
            # The prompt goes on stdin: the agent body plus upstream outputs can
            # be far longer than one command-line argument may be.
            proc = subprocess.run(argv, input=req.prompt, capture_output=True, text=True, cwd=req.cwd,
                                  timeout=_timeout(req))
        except subprocess.TimeoutExpired:
            return Result(False, "", f"claude timed out after {_timeout(req)}s", duration_s=time.monotonic() - start)
        except OSError as e:
            return Result(False, "", f"could not start claude: {e}", duration_s=time.monotonic() - start)
        dur = time.monotonic() - start
        if req.log_dir:
            (req.log_dir / f"{req.step_id}.raw.json").write_text(proc.stdout, encoding="utf-8")
        data = _last_json(proc.stdout)
        if data is None:
            tail = (proc.stderr or proc.stdout).strip()[-1500:]
            return Result(False, "", f"claude exit {proc.returncode}, no JSON result: {tail}", duration_s=dur)
        output = str(data.get("result") or "")
        cost = data.get("total_cost_usd")
        usage = data.get("usage") or {}
        denied = [_describe_tool_call(d) for d in data.get("permission_denials") or []]
        if data.get("is_error") or proc.returncode != 0 or data.get("subtype") not in (None, "success"):
            reason = data.get("subtype") or "error"
            return Result(False, output, f"claude reported {reason}: {output[-800:]}", cost, usage, dur, denied)
        return Result(True, output, cost_usd=cost, usage=usage, duration_s=dur, denied=denied)


def _describe_tool_call(denial: dict[str, Any]) -> str:
    tool = str(denial.get("tool_name") or "?")
    inp = denial.get("tool_input") or {}
    detail = inp.get("command") or inp.get("file_path") or inp.get("path") or ""
    detail = " ".join(str(detail).split())
    return f"{tool}({detail[:120]}{'…' if len(detail) > 120 else ''})" if detail else tool


def _last_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


PROVIDERS = {"claude-code": ClaudeCodeProvider, "command": CommandProvider, "dry-run": DryRunProvider}


def get(name: str, settings: dict[str, Any] | None = None) -> Provider:
    try:
        return PROVIDERS[name](settings)
    except KeyError:
        raise ProviderError(f"unknown provider '{name}' — one of {', '.join(PROVIDERS)}") from None
