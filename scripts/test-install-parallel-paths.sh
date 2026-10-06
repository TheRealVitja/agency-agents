#!/usr/bin/env bash
# Parallel workers must receive --agents-file / --path values intact, even when
# they contain spaces or glob characters. Workers used to get the parent's flags
# as one command-shaped string that the child shell split and globbed again.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/repo/scripts" "$scratch/repo/engineering" "$scratch/repo/integrations" "$scratch/home"
cp "$SCRIPT_DIR/install.sh" "$SCRIPT_DIR/lib.sh" "$scratch/repo/scripts/"
cp "$SCRIPT_DIR/../divisions.json" "$scratch/repo/"
export HOME="$scratch/home"
export CLAUDE_CONFIG_DIR="$HOME/.claude" COPILOT_AGENT_DIR="$HOME/.github/agents"
for agent in first second; do
  printf '%s\n' '---' "name: $agent" 'description: Example agent' 'color: blue' '---' '# Example agent' > "$scratch/repo/engineering/$agent.md"
done

odd="$scratch/agency selection [x]"
mkdir -p "$odd"
printf 'first\n' > "$odd/agents list.txt"

bash "$scratch/repo/scripts/install.sh" --tool claude-code,copilot --agents-file "$odd/agents list.txt" \
  --no-convert --parallel --jobs 2 > "$scratch/agents-file.log" 2>&1 || {
  cat "$scratch/agents-file.log"; echo 'FAIL: parallel install with a spaced --agents-file path failed' >&2; exit 1;
}
for dir in "$HOME/.claude/agents" "$HOME/.github/agents"; do
  [[ -f "$dir/first.md" ]] || { cat "$scratch/agents-file.log"; echo "FAIL: selected agent missing from $dir" >&2; exit 1; }
  [[ ! -e "$dir/second.md" ]] || { echo "FAIL: unselected agent installed into $dir" >&2; exit 1; }
done

echo 'PASS: parallel workers keep spaced and bracketed paths as single arguments'
