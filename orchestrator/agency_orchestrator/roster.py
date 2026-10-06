"""The agent roster: every agent file under a division in divisions.json.

Agent ids follow scripts/lib.sh exactly, so a workflow, install.sh and the
runbook rosters all name an agent the same way: the slug derived from the
`name:` frontmatter ("Frontend Developer" -> "frontend-developer"). A reference
may also be the display name, the file stem, or the division path AO-style
workflows use ("engineering/engineering-frontend-developer").
"""

from __future__ import annotations

import bisect
import difflib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def slugify(text: str) -> str:
    """Mirror of lib.sh slugify: lowercase, non-[a-z0-9] runs to one '-', trimmed."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", text.lower())).strip("-")


def _unquote(value: str) -> str:
    # lib.sh get_field: a quoted scalar carries its quotes as delimiters only.
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_agent_file(text: str) -> tuple[dict[str, str], str] | None:
    """Frontmatter fields and body of an agent file, or None if it has no
    frontmatter (strategy playbooks, READMEs). Indented continuation lines of
    a plain scalar fold into one line, as YAML and lib.sh do."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        return None
    fields: dict[str, str] = {}
    key = None
    end = None
    for i, raw in enumerate(lines[1:], start=1):
        line = raw.rstrip("\r")
        if line == "---":
            end = i
            break
        m = re.match(r"^([A-Za-z_][\w-]*):(?:\s(.*))?$", line)
        if m:
            key = m.group(1)
            if key not in fields:
                fields[key] = (m.group(2) or "").strip()
            else:
                key = None  # first match wins, like get_field
            continue
        if key and re.match(r"^[ \t]+\S", line):
            fields[key] = (fields[key] + " " + line.strip()).strip()
        else:
            key = None
    if end is None:
        return None
    for k in list(fields):
        fields[k] = _unquote(fields[k])
    body = "\n".join(lines[end + 1:]).strip("\n")
    return fields, body


@dataclass(frozen=True)
class Agent:
    slug: str
    name: str
    description: str
    division: str
    path: Path
    emoji: str = ""
    vibe: str = ""
    body: str = field(default="", repr=False)

    @property
    def stem(self) -> str:
        return self.path.stem

    def rel_path(self, root: Path) -> str:
        return self.path.relative_to(root).as_posix()

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}".strip()


class UnknownAgentError(LookupError):
    pass


_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
_STOP = frozenset("""
a about all also an and any are as at be been but by can could do does for
from get has have help how i if in into is it its just like make me my need
of on one or our out please should so some than that the their them then
there these they this those to up us use using want was way we what when
where which who why will with would you your
""".split())


def tokens(text: str) -> list[str]:
    return [t.rstrip(".") for t in _WORD.findall(text.lower()) if t.rstrip(".") not in _STOP]


class Roster:
    """All agents of one repo checkout (or any directory laid out like it)."""

    def __init__(self, agents: list[Agent], root: Path):
        self.root = root
        self.agents = agents
        self._by_slug = {a.slug: a for a in agents}
        self._by_stem: dict[str, Agent] = {}
        for a in agents:
            self._by_stem.setdefault(a.stem, a)
        self._index: dict[str, dict[str, set[str]]] | None = None
        self._idf: dict[str, float] = {}
        self._vocab: list[str] = []

    @classmethod
    def load(cls, root: Path | str | None = None) -> "Roster":
        root = Path(root or REPO_ROOT).resolve()
        divisions_file = root / "divisions.json"
        if not divisions_file.is_file():
            raise FileNotFoundError(f"{divisions_file} not found — is {root} an agency-agents checkout?")
        divisions = json.loads(divisions_file.read_text(encoding="utf-8"))["divisions"]
        agents: list[Agent] = []
        seen: dict[str, Path] = {}
        for division in divisions:
            base = root / division
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.md")):
                parsed = parse_agent_file(path.read_text(encoding="utf-8"))
                if parsed is None:
                    continue
                meta, body = parsed
                name = meta.get("name", "")
                slug = slugify(name)
                if not slug:
                    continue
                if slug in seen:
                    # convert.sh refuses duplicate slugs; keep the first and move on.
                    continue
                seen[slug] = path
                agents.append(Agent(
                    slug=slug, name=name, description=meta.get("description", ""),
                    division=division, path=path, emoji=meta.get("emoji", ""),
                    vibe=meta.get("vibe", ""), body=body,
                ))
        return cls(agents, root)

    def __len__(self) -> int:
        return len(self.agents)

    def __contains__(self, ref: str) -> bool:
        try:
            self.resolve(ref)
            return True
        except UnknownAgentError:
            return False

    def get(self, slug: str) -> Agent | None:
        return self._by_slug.get(slug)

    def resolve(self, ref: str) -> Agent:
        """An agent by slug, display name, file stem, or division path."""
        ref = (ref or "").strip()
        if not ref:
            raise UnknownAgentError("empty agent reference")
        if ref in self._by_slug:
            return self._by_slug[ref]
        path_like = ref[:-3] if ref.endswith(".md") else ref
        if "/" in path_like:
            candidate = (self.root / f"{path_like}.md").resolve()
            for a in self.agents:
                if a.path == candidate:
                    return a
            path_like = path_like.rsplit("/", 1)[-1]
        if path_like in self._by_stem:
            return self._by_stem[path_like]
        slug = slugify(ref)
        if slug in self._by_slug:
            return self._by_slug[slug]
        hints = difflib.get_close_matches(slug, list(self._by_slug), n=3, cutoff=0.6)
        hint = f" — did you mean: {', '.join(hints)}?" if hints else ""
        raise UnknownAgentError(f"unknown agent '{ref}'{hint}")

    # --- search ---------------------------------------------------------------
    # Whole-word matching with per-field weights and IDF, the same idea as the
    # Hermes router (#869): a substring match ("ai" in "Email") is noise.
    _FIELDS = {"name": 8.0, "description": 4.5, "division": 2.0, "vibe": 1.5}

    def _build_index(self) -> None:
        if self._index is not None:
            return
        index: dict[str, dict[str, set[str]]] = {}
        df: dict[str, int] = {}
        for a in self.agents:
            entry = {
                "name": set(tokens(a.name)),
                "description": set(tokens(a.description)),
                "division": set(tokens(a.division)),
                "vibe": set(tokens(a.vibe)),
                "body": set(tokens(a.body[:6000])),
            }
            index[a.slug] = entry
            for term in set().union(*entry.values()):
                df[term] = df.get(term, 0) + 1
        n = max(len(self.agents), 1)
        self._idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self._vocab = sorted(df)
        self._index = index

    def _forms(self, term: str) -> set[str]:
        """Index terms a query term may match: itself, a plain singular, and —
        for terms of 5+ characters — every term it prefixes, so "postgres"
        reaches "postgresql" and "kubernet" reaches "kubernetes"."""
        forms = {term}
        if len(term) > 3 and term.endswith("s"):
            forms.add(term[:-1])
        if len(term) >= 5:
            for cand in self._vocab[bisect.bisect_left(self._vocab, term):]:
                if not cand.startswith(term):
                    break
                forms.add(cand)
        return forms

    def score(self, agent: Agent, query_terms: list[str]) -> float:
        self._build_index()
        entry = self._index[agent.slug]  # type: ignore[index]
        score, matched = 0.0, 0
        for term in set(query_terms):
            forms = self._forms(term)
            weight = 0.0
            for f, w in self._FIELDS.items():
                if entry[f] & forms:
                    weight = max(weight, w)
            if not weight and entry["body"] & forms:
                weight = 1.0
            if weight:
                matched += 1
                score += weight * max(self._idf.get(t, 1.0) for t in forms)
        if not matched:
            return 0.0
        return score * (0.6 + 0.4 * matched / max(len(set(query_terms)), 1))

    def search(self, query: str, limit: int = 8, division: str | None = None) -> list[tuple[float, Agent]]:
        terms = tokens(query)
        if not terms:
            return []
        hits = [(self.score(a, terms), a) for a in self.agents if not division or a.division == division]
        hits = [h for h in hits if h[0] > 0]
        hits.sort(key=lambda h: (-h[0], h[1].slug))
        return hits[:limit]
