#!/usr/bin/env python3
"""A stand-in for a coding agent, driven by the `command` provider in tests.

Reads the prompt on stdin and acts like an agent would, without a model:
  write steps   create agency-fake/<step>.txt in the working directory
  read steps    change nothing (unless FAKE_READ_WRITES is set)
  brief         answers with an AFFECTED line for each name in FAKE_AFFECTED
Knobs: FAKE_FAIL=<step> fails that step after a partial edit; FAKE_LOG=<file>
gets one line per call, so tests can count who ran.
"""

import os
import sys
from pathlib import Path

step = os.environ["AGENCY_STEP"]
mode = os.environ["AGENCY_MODE"]
agent = os.environ["AGENCY_AGENT"]
prompt = sys.stdin.read()
persona = Path(os.environ["AGENCY_SYSTEM_FILE"]).read_text(encoding="utf-8").splitlines()[0]

if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(f"{step} {agent} {mode}\n")

if mode == "write" or os.environ.get("FAKE_READ_WRITES"):
    out = Path("agency-fake")
    out.mkdir(exist_ok=True)
    (out / f"{step}.txt").write_text(f"{agent}\n", encoding="utf-8")

if os.environ.get("FAKE_FAIL") == step:
    print("partial work, then a crash", file=sys.stderr)
    sys.exit(3)

lines = [f"step {step} by {agent} ({mode})", f"persona: {persona}"]
if "BRIEF-FROM-ARCHITECT" in prompt:
    lines.append("saw the brief")
if step == "brief":
    lines.append("BRIEF-FROM-ARCHITECT")
    for name in filter(None, os.environ.get("FAKE_AFFECTED", "").split(",")):
        lines.append(f"AFFECTED: component:{name};")
if step == "test":
    lines.append("RESULT: PASS")
print("\n".join(lines))
