"""Analysis of a target project: its components, what each is built with, and
which components depend on which.

A component is a directory with a build manifest (package.json, pyproject.toml,
go.mod, Cargo.toml, *.uproject, ...). Every file belongs to the deepest
component containing it. A project with no manifest at all is one component.

Edges mean "A depends on B" (A imports, calls or deploys after B). They come
from, strongest first:
  override   <project>/.agency/project.(json|yaml) says so
  manifest   a declared dependency names another component (workspaces, path deps)
  graphify   graphify-out/graph.json has imports/calls between their files
  import     this module's own scan of Python, JS/TS and Go imports
  compose    docker-compose `depends_on` between services built from them
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "dist", "build", "out",
    "target", ".venv", "venv", "env", ".env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache", "coverage",
    "htmlcov", "graphify-out", ".idea", ".vscode", "Pods", "DerivedData", ".gradle", ".terraform",
    "obj", "bin", "Library", "Temp", "Logs", "Binaries", "Intermediate", "Saved", "DerivedDataCache",
    "fixtures", "__fixtures__", "testdata", ".agency", ".dart_tool", ".expo",
}
MANIFESTS = {
    "package.json": "npm", "pyproject.toml": "python", "setup.py": "python", "setup.cfg": "python",
    "requirements.txt": "python", "Pipfile": "python", "go.mod": "go", "Cargo.toml": "cargo",
    "pom.xml": "maven", "build.gradle": "gradle", "build.gradle.kts": "gradle", "composer.json": "composer",
    "Gemfile": "ruby", "pubspec.yaml": "dart", "mix.exs": "elixir", "project.godot": "godot",
    "platformio.ini": "platformio", "foundry.toml": "solidity", "Chart.yaml": "helm",
    "deno.json": "deno", "oh-package.json5": "ohpm", "package.xml": "ros",
}
MANIFEST_GLOBS = {"*.uproject": "unreal", "*.csproj": "dotnet", "*.fsproj": "dotnet", "hardhat.config.*": "solidity"}
LANG_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".vue": "javascript",
    ".svelte": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala", ".cs": "csharp", ".fs": "fsharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".m": "objective-c", ".dart": "dart", ".ex": "elixir", ".exs": "elixir", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".sol": "solidity", ".gd": "gdscript", ".lua": "lua",
    ".tf": "terraform", ".sql": "sql", ".sh": "shell", ".ets": "arkts", ".md": "markdown",
}
CODE_LANGS = set(LANG_BY_EXT.values()) - {"markdown", "sql", "shell", "terraform"}
MAX_FILES = 40000
MAX_SCAN_BYTES = 512 * 1024


@dataclass
class Component:
    name: str
    path: str                                  # relative to the project root, "." for the root
    manifests: list[str] = field(default_factory=list)
    ecosystems: list[str] = field(default_factory=list)
    package_names: list[str] = field(default_factory=list)
    dependencies: set[str] = field(default_factory=set)
    files: list[str] = field(default_factory=list, repr=False)   # relative to the project root
    languages: Counter = field(default_factory=Counter)
    tags: list[str] = field(default_factory=list)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    agent: str | None = None                   # set by an override; routing fills the rest
    test_command: str | None = None

    @property
    def main_languages(self) -> list[str]:
        code = [(n, c) for n, c in self.languages.most_common() if n in CODE_LANGS]
        if not code:
            return []
        top = code[0][1]
        return [n for n, c in code if c >= max(1, top * 0.2)]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name, "path": self.path, "manifests": self.manifests,
            "ecosystems": self.ecosystems, "package_names": self.package_names,
            "languages": dict(self.languages.most_common(6)), "tags": self.tags,
            "evidence": self.evidence, "dependencies": sorted(self.dependencies)[:60],
            "files": len(self.files), "agent": self.agent, "test_command": self.test_command,
        }


@dataclass
class Edge:
    source: str        # depends on ...
    target: str        # ... this component
    kind: str
    weight: int = 1
    examples: list[str] = field(default_factory=list)


@dataclass
class Project:
    root: Path
    name: str
    components: list[Component]
    edges: list[Edge]
    is_git: bool
    graphify: str | None = None
    notes: list[str] = field(default_factory=list)

    def component(self, name: str) -> Component:
        for c in self.components:
            if c.name == name:
                return c
        raise KeyError(name)

    def deps_of(self, name: str) -> set[str]:
        return {e.target for e in self.edges if e.source == name}

    def dependents_of(self, name: str) -> set[str]:
        return {e.source for e in self.edges if e.target == name}

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.name, "root": str(self.root), "git": self.is_git, "graphify": self.graphify,
            "components": [c.summary() for c in self.components],
            "edges": [{"from": e.source, "to": e.target, "kind": e.kind, "weight": e.weight,
                       "examples": e.examples[:3]} for e in self.edges],
            "notes": self.notes,
        }


# --- manifest parsing ----------------------------------------------------------

def _dep_name(spec: str) -> str:
    """Bare package name of a requirement line: 'psycopg[binary]>=3' -> 'psycopg'."""
    spec = spec.strip().split(";")[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)", spec)
    return m.group(1).lower().replace("_", "-") if m else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_manifest(path: Path) -> tuple[list[str], set[str], dict[str, Any]]:
    """(package names it declares, dependency names, extra facts)."""
    name = path.name
    text = _read(path)
    names: list[str] = []
    deps: set[str] = set()
    extra: dict[str, Any] = {}
    try:
        if name in ("package.json", "deno.json"):
            data = json.loads(text or "{}")
            if data.get("name"):
                names.append(str(data["name"]))
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for dep, ver in (data.get(key) or {}).items():
                    deps.add(dep.lower())
                    if isinstance(ver, str) and ver.startswith(("file:", "link:", "workspace:")):
                        extra.setdefault("local_deps", []).append(dep)
            ws = data.get("workspaces")
            if isinstance(ws, dict):
                ws = ws.get("packages")
            if ws:
                extra["workspaces"] = list(ws)
            scripts = data.get("scripts") or {}
            if "test" in scripts and "no test specified" not in str(scripts["test"]):
                extra["test_command"] = "npm test"
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            proj = data.get("project") or {}
            if proj.get("name"):
                names.append(str(proj["name"]))
            for spec in proj.get("dependencies") or []:
                deps.add(_dep_name(spec))
            for group in (proj.get("optional-dependencies") or {}).values():
                deps.update(_dep_name(s) for s in group)
            poetry = (data.get("tool") or {}).get("poetry") or {}
            if poetry.get("name"):
                names.append(str(poetry["name"]))
            for key in ("dependencies", "dev-dependencies"):
                for dep, spec in (poetry.get(key) or {}).items():
                    if dep.lower() != "python":
                        deps.add(_dep_name(dep))
                    if isinstance(spec, dict) and "path" in spec:
                        extra.setdefault("local_paths", []).append(spec["path"])
            for group in ((poetry.get("group") or {}).values()):
                deps.update(_dep_name(d) for d in (group.get("dependencies") or {}) if d.lower() != "python")
            if "pytest" in deps or (data.get("tool") or {}).get("pytest"):
                extra["test_command"] = "pytest"
        elif name == "requirements.txt":
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if line and not line.startswith("-"):
                    deps.add(_dep_name(line))
            if "pytest" in deps:
                extra["test_command"] = "pytest"
        elif name == "Pipfile":
            data = tomllib.loads(text)
            for key in ("packages", "dev-packages"):
                deps.update(_dep_name(d) for d in (data.get(key) or {}))
            if "pytest" in deps:
                extra["test_command"] = "pytest"
        elif name in ("setup.py", "setup.cfg"):
            m = re.search(r"""name\s*[=:]\s*['"]?([A-Za-z0-9._\-]+)""", text)
            if m:
                names.append(m.group(1))
            for spec in re.findall(r"""['"]([A-Za-z0-9._\-]+)\s*(?:[<>=!~\[][^'"]*)?['"]""",
                                   text.split("install_requires", 1)[1] if "install_requires" in text else ""):
                deps.add(_dep_name(spec))
        elif name == "go.mod":
            m = re.search(r"^module\s+(\S+)", text, re.M)
            if m:
                names.append(m.group(1))
            for mod in re.findall(r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]{2,}/\S+)\s+v\S+", text, re.M):
                deps.add(mod.lower())
            for target in re.findall(r"^\s*(?:replace\s+)?\S+(?:\s+\S+)?\s*=>\s*(\.{1,2}/\S*)", text, re.M):
                extra.setdefault("local_paths", []).append(target)
            extra["test_command"] = "go test ./..."
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            pkg = data.get("package") or {}
            if pkg.get("name"):
                names.append(str(pkg["name"]))
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                for dep, spec in (data.get(key) or {}).items():
                    deps.add(dep.lower())
                    if isinstance(spec, dict) and "path" in spec:
                        extra.setdefault("local_paths", []).append(spec["path"])
            members = ((data.get("workspace") or {}).get("members")) or []
            if members:
                extra["workspaces"] = list(members)
            extra["test_command"] = "cargo test"
        elif name == "composer.json":
            data = json.loads(text or "{}")
            if data.get("name"):
                names.append(str(data["name"]))
            for key in ("require", "require-dev"):
                deps.update(d.lower() for d in (data.get(key) or {}))
        elif name == "Gemfile":
            deps.update(g.lower() for g in re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M))
        elif name == "pubspec.yaml":
            m = re.search(r"^name:\s*(\S+)", text, re.M)
            if m:
                names.append(m.group(1))
            if re.search(r"sdk:\s*flutter", text):
                deps.add("flutter")
            block = re.search(r"^dependencies:\s*\n((?:[ \t]+.*\n?)*)", text, re.M)
            if block:
                deps.update(d.lower() for d in re.findall(r"^[ \t]{2}([A-Za-z0-9_]+):", block.group(1), re.M))
        elif name == "pom.xml":
            m = re.search(r"<artifactId>([^<]+)</artifactId>", text)
            if m:
                names.append(m.group(1))
            for group, art in re.findall(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>", text):
                deps.update({art.lower(), f"{group}:{art}".lower(), group.lower()})
        elif name in ("build.gradle", "build.gradle.kts"):
            for coord in re.findall(r"""(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*\(?\s*['"]([^'"]+)['"]""", text):
                parts = coord.split(":")
                deps.update(p.lower() for p in parts[:2] if p)
        elif name.endswith((".csproj", ".fsproj")):
            deps.update(p.lower() for p in re.findall(r'<PackageReference\s+Include="([^"]+)"', text))
            extra["local_paths"] = re.findall(r'<ProjectReference\s+Include="([^"]+)"', text)
        elif name == "mix.exs":
            deps.update(d.lower() for d in re.findall(r"\{:(\w+),", text))
        elif name.endswith(".uproject"):
            data = json.loads(text or "{}")
            deps.update(str(p.get("Name", "")).lower() for p in data.get("Plugins") or [] if p.get("Enabled"))
    except (ValueError, tomllib.TOMLDecodeError, IndexError) as e:
        extra["error"] = f"{name}: {e}"
    deps.discard("")
    return names, deps, extra


# --- discovery -----------------------------------------------------------------

def _walk(root: Path) -> list[str]:
    """Relative paths of the project's files, minus build output and vendored code."""
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                             or d in (".github", ".circleci"))
        rel_dir = os.path.relpath(dirpath, root)
        for f in sorted(filenames):
            rel = f if rel_dir == "." else f"{PurePosixPath(Path(rel_dir).as_posix())}/{f}"
            files.append(rel)
            if len(files) >= MAX_FILES:
                return files
    return files


def _manifest_kind(basename: str) -> str | None:
    if basename in MANIFESTS:
        return MANIFESTS[basename]
    for pattern, kind in MANIFEST_GLOBS.items():
        if fnmatch.fnmatch(basename, pattern):
            return kind
    return None


def _component_roots(files: list[str]) -> dict[str, list[str]]:
    roots: dict[str, list[str]] = {}
    tf_dirs: set[str] = set()
    for rel in files:
        p = PurePosixPath(rel)
        parent = str(p.parent)
        if _manifest_kind(p.name):
            roots.setdefault(parent, []).append(rel)
        elif p.suffix == ".tf":
            tf_dirs.add(parent)
        elif rel.endswith("ProjectSettings/ProjectVersion.txt"):  # Unity: the marker sits one level down
            roots.setdefault(str(p.parent.parent), []).append(rel)
    # Terraform: a directory of .tf files is a component unless an ancestor
    # already is one (modules/ under an environment root stay with it).
    for d in sorted(tf_dirs, key=lambda x: x.count("/")):
        if not any(d == r or d.startswith(r + "/") for r in roots if r != "."):
            roots.setdefault(d, []).append(f"{d}/*.tf" if d != "." else "*.tf")
    return roots


def _owner(rel: str, roots: list[str]) -> str:
    best = "."
    for r in roots:
        if r == ".":
            continue
        if rel == r or rel.startswith(r + "/"):
            if best == "." or len(r) > len(best):
                best = r
    return best


def _matches_file(pattern: str, comp_rel_paths: Iterable[str]) -> str | None:
    for rel in comp_rel_paths:
        if pattern.endswith("/"):
            d = pattern.rstrip("/")
            parts = PurePosixPath(rel).parts[:-1]
            if any(fnmatch.fnmatch(part, d) for part in parts) or rel.startswith(pattern):
                return rel
        elif "/" in pattern:
            if fnmatch.fnmatch(rel, pattern) or rel.endswith("/" + pattern) or rel == pattern:
                return rel
        elif fnmatch.fnmatch(PurePosixPath(rel).name, pattern):
            return rel
    return None


# --- import scanning -------------------------------------------------------------

_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*))", re.M)
_JS_IMPORT = re.compile(r"""(?:\bimport\s[^'"]*?from\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|\bexport\s[^'"]*?from\s*)['"]([^'"]+)['"]""")
_GO_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"', re.M)
_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")


def _python_modules(comp: Component) -> set[str]:
    """Top-level names other code would import this component's code by."""
    mods: set[str] = set()
    base = "" if comp.path == "." else comp.path + "/"
    for rel in comp.files:
        if not rel.endswith(".py"):
            continue
        sub = rel[len(base):]
        parts = PurePosixPath(sub).parts
        if parts and parts[0] in ("src", "lib") and len(parts) > 1:
            parts = parts[1:]
        if parts[0] in ("tests", "test", "docs", "scripts", "examples"):
            continue
        mods.add(parts[0][:-3] if len(parts) == 1 else parts[0])
    mods.discard("setup")
    mods.discard("conftest")
    mods.discard("__init__")
    return mods


def _scan_imports(project_root: Path, comps: list[Component], owner_of) -> dict[tuple[str, str], list[str]]:
    hits: dict[tuple[str, str], list[str]] = {}
    py_owner: dict[str, str] = {}
    for c in comps:
        for m in _python_modules(c):
            py_owner.setdefault(m, c.name)
    pkg_owner = {n: c.name for c in comps for n in c.package_names}
    go_mods = sorted(((n, c.name) for c in comps if "go" in c.ecosystems for n in c.package_names),
                     key=lambda x: -len(x[0]))

    def add(src: str, dst: str | None, where: str) -> None:
        if dst and dst != src:
            hits.setdefault((src, dst), []).append(where)

    for c in comps:
        scanned = 0
        for rel in c.files:
            if scanned > 4000:
                break
            path = project_root / rel
            if rel.endswith(".py") or rel.endswith(_JS_EXTS) or rel.endswith(".go"):
                try:
                    if path.stat().st_size > MAX_SCAN_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                text = _read(path)
            else:
                continue
            if rel.endswith(".py"):
                for m in _PY_IMPORT.finditer(text):
                    names = [m.group(1)] if m.group(1) else [x.strip() for x in m.group(2).split(",")]
                    for name in names:
                        add(c.name, py_owner.get(name.split(".")[0]), rel)
            elif rel.endswith(".go"):
                for spec in _GO_IMPORT.findall(text):
                    for mod, owner in go_mods:
                        if spec == mod or spec.startswith(mod + "/"):
                            add(c.name, owner, rel)
                            break
            else:
                for spec in _JS_IMPORT.findall(text):
                    if spec.startswith("."):
                        target = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
                        if target.startswith(".."):
                            continue
                        add(c.name, owner_of(target), rel)
                    else:
                        parts = spec.split("/")
                        pkg = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
                        add(c.name, pkg_owner.get(pkg), rel)
    return hits


def _graphify_edges(graph_path: Path, owner_of) -> tuple[dict[tuple[str, str], list[str]], dict[str, set[str]]]:
    """Cross-component edges and external dependency names from a Graphify
    graph.json (NetworkX node-link data: nodes carry source_file, links carry
    relation and confidence; the link source is the importing/calling side)."""
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in data.get("nodes", [])}
    links = data.get("links", data.get("edges", []))
    edges: dict[tuple[str, str], list[str]] = {}
    external: dict[str, set[str]] = {}
    for link in links:
        if link.get("relation") not in ("imports", "imports_from", "calls", "inherits", "references", "uses", "mixes_in", "implements"):
            continue
        src, dst = nodes.get(link.get("source")), nodes.get(link.get("target"))
        if not src or not src.get("source_file"):
            continue
        a = owner_of(src["source_file"])
        if dst and dst.get("source_file") and not dst.get("external"):
            b = owner_of(dst["source_file"])
            if a != b:
                edges.setdefault((a, b), []).append(
                    f"{src['source_file']} {link.get('relation')} {dst['source_file']} [{link.get('confidence', '?')}]")
        elif dst is not None and link.get("relation") in ("imports", "imports_from"):
            label = str(dst.get("label") or dst.get("id") or "")
            label = re.sub(r"^ref_", "", label).strip().lower()
            if label:
                external.setdefault(a, set()).add(label)
    return edges, external


def _compose_edges(project_root: Path, files: list[str], owner_of) -> dict[tuple[str, str], list[str]]:
    edges: dict[tuple[str, str], list[str]] = {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return edges
    for rel in files:
        if PurePosixPath(rel).name not in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml"):
            continue
        try:
            data = yaml.safe_load(_read(project_root / rel)) or {}
        except yaml.YAMLError:
            continue
        services = data.get("services") or {}
        base = PurePosixPath(rel).parent
        svc_comp: dict[str, str] = {}
        for name, svc in services.items():
            build = (svc or {}).get("build")
            ctx = build.get("context") if isinstance(build, dict) else build
            if ctx:
                target = os.path.normpath(str(base / ctx)).replace(os.sep, "/")
                svc_comp[name] = owner_of(target + "/__compose__")
        for name, svc in services.items():
            deps = (svc or {}).get("depends_on") or []
            deps = list(deps) if isinstance(deps, (list, dict)) else []
            for dep in deps:
                a, b = svc_comp.get(name), svc_comp.get(dep)
                if a and b and a != b:
                    edges.setdefault((a, b), []).append(f"{rel}: {name} depends_on {dep}")
    return edges


def _load_override(root: Path) -> dict[str, Any]:
    for name in ("project.json", "project.yaml", "project.yml"):
        p = root / ".agency" / name
        if p.is_file():
            text = _read(p)
            if name.endswith(".json"):
                return json.loads(text)
            import yaml  # type: ignore
            return yaml.safe_load(text) or {}
    return {}


# --- analysis ------------------------------------------------------------------

def analyze(root: Path | str, graphify: Path | str | None = None, rules=None) -> Project:
    """Analyze the project at `root`. `rules` (routing.Rules) tags components;
    pass None to get the raw structure only."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    override = _load_override(root)
    files = _walk(root)
    notes: list[str] = []
    if len(files) >= MAX_FILES:
        notes.append(f"stopped listing files at {MAX_FILES}; large trees are only partly analyzed")
    ignore = [str(p).rstrip("/") for p in override.get("ignore") or []]
    if ignore:
        files = [f for f in files if not any(f == i or f.startswith(i + "/") for i in ignore)]

    roots = _component_roots(files)
    for oc in override.get("components") or []:
        roots.setdefault(str(oc.get("path", ".")).strip("/") or ".", [])
    # The root always exists as a catch-all owner of files no component
    # claims; it is dropped again below if it holds no code.
    roots.setdefault(".", [])
    root_list = sorted(roots, key=lambda r: (r != ".", r))

    # A root with no manifest still owns the files no component claims.
    by_root: dict[str, Component] = {}
    used_names: set[str] = set()
    for r in root_list:
        names: list[str] = []
        deps: set[str] = set()
        ecos: list[str] = []
        extras: dict[str, Any] = {}
        for m in roots[r]:
            kind = _manifest_kind(PurePosixPath(m).name)
            if kind and kind not in ecos:
                ecos.append(kind)
            if not m.endswith("*.tf"):
                n, d, x = parse_manifest(root / m)
                names.extend(n)
                deps |= d
                for k, v in x.items():
                    if k == "error":
                        notes.append(v)
                    elif isinstance(v, list):
                        extras.setdefault(k, []).extend(v)
                    else:
                        extras.setdefault(k, v)
            elif "terraform" not in ecos:
                ecos.append("terraform")
        base = root.name if r == "." else PurePosixPath(r).name
        name = base
        if names and r != ".":
            pretty = names[0].split("/")[-1]
            name = pretty if pretty not in used_names else base
        while name in used_names:
            name = f"{name}-{PurePosixPath(r).parent.name or 'root'}"
        used_names.add(name)
        comp = Component(name=name, path=r, manifests=list(roots[r]), ecosystems=ecos,
                         package_names=list(dict.fromkeys(names)), dependencies=deps,
                         test_command=extras.get("test_command"))
        comp.evidence["_extras"] = [json.dumps(extras, sort_keys=True)] if extras else []
        by_root[r] = comp

    def owner_of(rel: str) -> str:
        return by_root[_owner(rel, list(by_root))].name

    for rel in files:
        comp = by_root[_owner(rel, list(by_root))]
        comp.files.append(rel)
        lang = LANG_BY_EXT.get(PurePosixPath(rel).suffix.lower())
        if lang:
            comp.languages[lang] += 1

    # Overrides first: every edge below is keyed by the final component name.
    for oc in override.get("components") or []:
        comp = by_root.get(str(oc.get("path", ".")).strip("/") or ".")
        if not comp:
            notes.append(f".agency/project: no component at path '{oc.get('path')}'")
            continue
        new_name = str(oc.get("name") or comp.name)
        if new_name != comp.name and new_name not in {c.name for c in by_root.values()}:
            comp.name = new_name
        if oc.get("agent"):
            comp.agent = str(oc["agent"])
        if oc.get("tags"):
            comp.evidence["_override_tags"] = [str(t) for t in oc["tags"]]
        if oc.get("test_command"):
            comp.test_command = str(oc["test_command"])

    comps = list(by_root.values())
    # Drop a manifest-less root that ended up owning nothing worth working on.
    if len(comps) > 1 and by_root.get(".") and not by_root["."].manifests:
        rc = by_root["."]
        if not any(l in CODE_LANGS for l in rc.languages):
            comps.remove(rc)

    names_of = {c.name for c in comps}
    edge_map: dict[tuple[str, str, str], Edge] = {}

    def add_edges(found: dict[tuple[str, str], list[str]], kind: str) -> None:
        for (a, b), examples in found.items():
            if a not in names_of or b not in names_of or a == b:
                continue
            key = (a, b, kind)
            e = edge_map.setdefault(key, Edge(a, b, kind, 0, []))
            e.weight += len(examples)
            e.examples.extend(examples[: max(0, 5 - len(e.examples))])

    # manifest edges: declared deps that name another component
    pkg_owner = {n.lower(): c.name for c in comps for n in c.package_names}
    manifest_hits: dict[tuple[str, str], list[str]] = {}
    for c in comps:
        for dep in c.dependencies:
            owner = pkg_owner.get(dep)
            if owner and owner != c.name:
                manifest_hits.setdefault((c.name, owner), []).append(f"declares dependency {dep}")
        extras = json.loads(c.evidence["_extras"][0]) if c.evidence.get("_extras") else {}
        for lp in extras.get("local_paths", []):
            base = "" if c.path == "." else c.path + "/"
            target = os.path.normpath(base + str(lp).replace("\\", "/")).replace(os.sep, "/")
            owner = owner_of(target + "/__path__")
            if owner != c.name:
                manifest_hits.setdefault((c.name, owner), []).append(f"path dependency {lp}")
    add_edges(manifest_hits, "manifest")

    graph_file = Path(graphify) if graphify else root / "graphify-out" / "graph.json"
    used_graphify = None
    if graph_file.is_file():
        try:
            g_edges, g_external = _graphify_edges(graph_file, owner_of)
            add_edges(g_edges, "graphify")
            for cname, labels in g_external.items():
                for c in comps:
                    if c.name == cname:
                        c.dependencies |= labels
            used_graphify = str(graph_file)
        except (ValueError, KeyError, OSError) as e:
            notes.append(f"could not read Graphify graph {graph_file}: {e}")
    elif graphify:
        notes.append(f"Graphify graph {graph_file} not found")

    add_edges(_scan_imports(root, comps, owner_of), "import")
    add_edges(_compose_edges(root, files, owner_of), "compose")

    for oc in override.get("components") or []:
        comp = by_root.get(str(oc.get("path", ".")).strip("/") or ".")
        for dep in oc.get("depends_on") or []:
            if comp and comp.name in names_of and dep in names_of:
                add_edges({(comp.name, str(dep)): ["declared in .agency/project"]}, "override")

    for c in comps:
        c.evidence.pop("_extras", None)
    project = Project(root=root, name=str(override.get("name") or root.name), components=comps,
                      edges=sorted(edge_map.values(), key=lambda e: (e.source, e.target, e.kind)),
                      is_git=(root / ".git").exists(), graphify=used_graphify, notes=notes)
    if rules is not None:
        rules.tag_project(project)
    return project


def files_relative_to(comp: Component) -> list[str]:
    base = "" if comp.path == "." else comp.path + "/"
    return [f[len(base):] for f in comp.files]


def matches_file(pattern: str, comp: Component) -> str | None:
    return _matches_file(pattern, files_relative_to(comp))
