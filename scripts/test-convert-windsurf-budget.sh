#!/usr/bin/env bash
# Windsurf's rule limit is 12,000 characters, and the trim must measure it the
# same way on every awk and in every locale. length() is bytes in mawk and
# macOS awk but characters in gawk under a UTF-8 locale, so a non-ASCII agent
# used to come out shorter on one machine than another (CI's gawk disagreed
# with the committed manifest). Korean is 3 bytes a character: counted as
# bytes, the rule keeps about a third of what Windsurf would read.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required." >&2; exit 2; }
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/repo/scripts" "$scratch/repo/engineering"
cp "$SCRIPT_DIR/convert.sh" "$SCRIPT_DIR/lib.sh" "$scratch/repo/scripts/"
cp "$SCRIPT_DIR/../divisions.json" "$scratch/repo/"

agent="$scratch/repo/engineering/korean.md"
{
  printf '%s\n' '---' 'name: Korean Fixture' 'description: 한국어 본문으로 문자 수를 확인하는 에이전트' 'color: blue' '---' '' '# Korean Fixture' ''
  for section in 1 2 3 4 5 6 7 8 9 10 11 12; do
    printf '## 섹션 %s\n\n' "$section"
    for para in 1 2 3 4 5 6 7 8; do
      for line in 1 2 3; do
        printf '이 문장은 윈드서프 규칙 길이를 바이트가 아니라 문자로 세는지 확인하기 위한 한국어 예문입니다.\n'
      done
      printf '\n'
    done
  done
} > "$agent"

for locale in C C.UTF-8; do
  LC_ALL="$locale" "$scratch/repo/scripts/convert.sh" --tool windsurf --out "$scratch/out-$locale" > /dev/null 2>&1 || {
    echo "FAIL: convert.sh --tool windsurf failed under LC_ALL=$locale" >&2; exit 1;
  }
done

cmp -s "$scratch/out-C/windsurf/rules/korean-fixture.md" "$scratch/out-C.UTF-8/windsurf/rules/korean-fixture.md" || {
  echo "FAIL: the Windsurf rule depends on the locale convert.sh runs in" >&2; exit 1;
}

python3 - "$scratch/out-C/windsurf/rules/korean-fixture.md" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
limit = 12000
if "Trimmed to fit Windsurf" not in text:
    sys.exit("FAIL: the fixture should have needed trimming")
if len(text) > limit:
    sys.exit(f"FAIL: rule is {len(text)} characters, over the {limit}-character limit")
# A clean break is accepted from 70% of the budget on; bytes-as-characters
# lands near a third of it.
if len(text) < limit * 0.7:
    sys.exit(f"FAIL: rule is only {len(text)} characters — the trim measured bytes, not characters")
print(f"PASS: Windsurf trim counts characters ({len(text)} of {limit}) and is locale-independent")
PY
