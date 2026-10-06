#!/usr/bin/env bash
# A checkout that ran convert.sh before Windsurf moved to one rule per agent
# still has the old integrations/windsurf/.windsurfrules. The installer must
# treat that as missing output and convert, not fail "rules missing".
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/repo/scripts" "$scratch/repo/engineering" "$scratch/repo/integrations/windsurf" "$scratch/home"
cp "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/convert.sh" "$SCRIPT_DIR/lib.sh" "$scratch/repo/scripts/"
cp "$SCRIPT_DIR/../divisions.json" "$scratch/repo/"
printf '%s\n' '---' 'name: Example Agent' 'description: Example agent' 'color: blue' '---' \
  '# Example Agent' '' '## 🧠 Your Identity & Memory' 'Example.' > "$scratch/repo/engineering/example.md"
printf '# Windsurf\n' > "$scratch/repo/integrations/windsurf/README.md"
printf '# The Agency (old single-file roster)\n' > "$scratch/repo/integrations/windsurf/.windsurfrules"

HOME="$scratch/home" bash "$scratch/repo/scripts/install.sh" --no-interactive --tool windsurf \
  --path "$scratch/dest" > "$scratch/install.log" 2>&1 || {
  cat "$scratch/install.log"; echo 'FAIL: install with a stale .windsurfrules failed' >&2; exit 1;
}
[[ -f "$scratch/dest/example-agent.md" ]] || {
  cat "$scratch/install.log"; echo 'FAIL: the per-agent Windsurf rule was not installed' >&2; exit 1;
}
[[ ! -e "$scratch/repo/integrations/windsurf/.windsurfrules" ]] || {
  echo 'FAIL: conversion left the stale .windsurfrules behind' >&2; exit 1;
}
echo 'PASS: a stale .windsurfrules triggers conversion to per-agent rules'
