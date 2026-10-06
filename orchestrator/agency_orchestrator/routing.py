"""Who takes which part of a project, driven by orchestrator/routing.json.

Three kinds of tags (see routing.json `_note`):
  platform  decides a component's implementer — the first one that matches wins
  concern   a cross-cutting layer (database, auth, payments, ...) whose agent
            works on the component first when the task is about that layer
  language  the fallback when no platform matched
Intents add read-only specialists (security, performance, ...) to the whole task.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project import Component, Project, files_relative_to, _matches_file
from .roster import Roster

DEFAULT_RULES = Path(__file__).resolve().parents[1] / "routing.json"


@dataclass
class TagRule:
    tag: str
    kind: str
    agents: list[str]
    deps: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)          # component/package name patterns
    requires_deps: list[str] = field(default_factory=list)  # and the component must have one of these


@dataclass
class Assignment:
    """A component and the agent routed to implement it, with the reasons."""
    component: str
    agent: str
    reason: str
    concerns: list[tuple[str, str]] = field(default_factory=list)   # (tag, agent) that hit the task


def _keyword_hit(keywords: list[str], text: str) -> str | None:
    low = text.lower()
    for kw in keywords:
        # Whole words: "ci" must not fire on "decide", "db" not on "feedback".
        if re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", low):
            return kw
    return None


class Rules:
    def __init__(self, data: dict[str, Any], source: Path | None = None):
        self.source = source
        self.tags = [TagRule(tag=t["tag"], kind=t.get("kind", "platform"), agents=list(t["agents"]),
                             deps=[d.lower() for d in t.get("deps", [])], files=list(t.get("files", [])),
                             languages=list(t.get("languages", [])), keywords=list(t.get("keywords", [])),
                             names=[n.lower() for n in t.get("names", [])],
                             requires_deps=[d.lower() for d in t.get("requires_deps", [])])
                     for t in data.get("tags", [])]
        self.by_tag = {t.tag: t for t in self.tags}
        self.phases: dict[str, Any] = data.get("phases", {})
        self.layer_order: list[str] = list(data.get("layer_order", []))
        self.intents: list[dict[str, Any]] = list(data.get("intents", []))

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Rules":
        path = Path(path or DEFAULT_RULES)
        return cls(json.loads(path.read_text(encoding="utf-8")), source=path)

    def agent_slugs(self) -> set[str]:
        slugs: set[str] = set()
        for t in self.tags:
            slugs.update(t.agents)
        for v in self.phases.values():
            if isinstance(v, dict):
                for agents in v.values():
                    slugs.update(agents)
            else:
                slugs.update(v)
        for i in self.intents:
            slugs.update(i.get("agents", []))
        return slugs

    def problems(self, roster: Roster) -> list[str]:
        out = [f"routing names unknown agent '{s}'" for s in sorted(self.agent_slugs()) if roster.get(s) is None]
        seen: set[str] = set()
        for t in self.tags:
            if t.tag in seen:
                out.append(f"routing tag '{t.tag}' is defined twice")
            seen.add(t.tag)
            if t.kind not in ("platform", "concern", "language"):
                out.append(f"routing tag '{t.tag}' has unknown kind '{t.kind}'")
            if t.kind == "concern" and not t.keywords:
                out.append(f"concern tag '{t.tag}' needs keywords, or it can never act")
        for tag in self.layer_order:
            if tag not in self.by_tag:
                out.append(f"layer_order names unknown tag '{tag}'")
            elif self.by_tag[tag].kind != "platform":
                out.append(f"layer_order tag '{tag}' is not a platform tag, so it never ranks a component")
        return out

    # --- tagging -------------------------------------------------------------

    def tag_component(self, comp: Component) -> None:
        rel_files = files_relative_to(comp)
        langs = set(comp.main_languages)
        tags: list[str] = []
        evidence: dict[str, list[str]] = {}
        names = [comp.name.lower(), *(n.lower() for n in comp.package_names)]
        for t in self.tags:
            why: list[str] = []
            # `names` and `requires_deps` are conditions: "a Unity assembly
            # called *Networking*" must not match a Go package of that name.
            if t.requires_deps and not any(fnmatch.fnmatch(d, p) for p in t.requires_deps for d in comp.dependencies):
                continue
            if t.names:
                hit_name = next((n for n in names if any(fnmatch.fnmatch(n, p) for p in t.names)), None)
                if not hit_name:
                    continue
                why.append(f"name {hit_name}")
            for pattern in t.deps:
                hit = next((d for d in sorted(comp.dependencies) if fnmatch.fnmatch(d, pattern)), None)
                if hit:
                    why.append(f"dependency {hit}")
                    break
            for pattern in t.files:
                hit = _matches_file(pattern, rel_files)
                if hit:
                    why.append(f"file {hit}")
                    break
            hit_lang = next((l for l in t.languages if l in langs), None)
            if hit_lang:
                why.append(f"language {hit_lang}")
            if why:
                tags.append(t.tag)
                evidence[t.tag] = why
        for t in comp.evidence.get("_override_tags", []):
            if t not in tags:
                tags.insert(0, t)
            evidence.setdefault(t, []).insert(0, "declared in .agency/project")
        comp.tags = tags
        comp.evidence = {k: v for k, v in comp.evidence.items() if not k.startswith("_")} | evidence

    def tag_project(self, project: Project) -> None:
        for c in project.components:
            self.tag_component(c)

    # --- routing ---------------------------------------------------------------

    def _first(self, comp: Component, kind: str) -> TagRule | None:
        for t in self.tags:
            if t.kind == kind and t.tag in comp.tags:
                return t
        return None

    def implementer(self, comp: Component) -> tuple[str, str]:
        """(agent slug, reason) for whoever builds this component."""
        if comp.agent:
            return comp.agent, "declared in .agency/project"
        for kind in ("platform", "language"):
            rule = self._first(comp, kind)
            if rule:
                why = "; ".join(comp.evidence.get(rule.tag, [])[:2])
                return rule.agents[0], f"{kind} tag '{rule.tag}' ({why})"
        return self.phases.get("fallback", ["senior-developer"])[0], "no stack recognised: fallback"

    def concerns(self, comp: Component, task: str) -> list[tuple[str, str, str]]:
        """(tag, agent, keyword) for each concern of the component the task is about."""
        out = []
        for t in self.tags:
            if t.kind == "concern" and t.tag in comp.tags:
                kw = _keyword_hit(t.keywords, task)
                if kw:
                    out.append((t.tag, t.agents[0], kw))
        return out

    def intents_for(self, task: str) -> list[tuple[str, str, str]]:
        out = []
        for i in self.intents:
            kw = _keyword_hit(i.get("keywords", []), task)
            if kw:
                out.append((i["intent"], i["agents"][0], kw))
        return out

    def layer_rank(self, comp: Component) -> int:
        """Lower runs first when the graph says nothing: infrastructure before
        the API before the UI. Ranked by the tag that picked the implementer,
        so an API that happens to use a database is still an API. A component
        known only by its language (a shared library, usually) comes first."""
        platform = self._first(comp, "platform")
        if platform is None:
            return -1 if self._first(comp, "language") else len(self.layer_order)
        return self.layer_order.index(platform.tag) if platform.tag in self.layer_order else len(self.layer_order)

    def tester(self, comps: list[Component]) -> str:
        by_tag = self.phases.get("test_by_tag", {})
        for c in comps:
            for tag, agents in by_tag.items():
                if tag in c.tags:
                    return agents[0]
        return self.phases.get("test", ["test-automation-engineer"])[0]

    def lead(self) -> str:
        return self.phases.get("lead", ["software-architect"])[0]

    def reviewer(self) -> str:
        return self.phases.get("review", ["code-reviewer"])[0]

    def assign(self, project: Project, task: str = "") -> list[Assignment]:
        out = []
        for c in project.components:
            agent, reason = self.implementer(c)
            out.append(Assignment(c.name, agent, reason, [(t, a) for t, a, _ in self.concerns(c, task)]))
        return out
