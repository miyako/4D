#!/usr/bin/env python3
"""Emulate the 4D dependency manager for GitHub-hosted components.

Resolves `Project/Sources/dependencies.json` (+ `environment4d.json`), downloads
release archives and installs them in the cache layout 4D uses, so that tool4d
(which never fetches) or 4D/4D Server can load them. Stdlib only, Python >= 3.10.
See README.md beside this file for the observed 4D behaviour each rule follows.
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://api.github.com"
USER_AGENT = "miyako-4d-dependencies"
RUNTIMES = {"tool4d": "tool4d", "4d": "4D", "4d server": "4D Server"}
TOKEN_ENV = "DEPENDENCIES_GITHUB_TOKEN"
UNVERIFIED_REUSE = "unverified (4D-equivalent)"
MAX_ARCHIVE = 1 << 30
MAX_EXTRACTED = 2 << 30
MAX_ENTRIES = 50000
TAG = re.compile(r"^v?(\d+)(?:R(\d+))?(?:\.(\d+))?(?:\.(\d+))?$")


class Failure(Exception):
    """A dependency-level failure, reported in `incomplete`."""


class AuthFailure(Failure):
    """GitHub rejected the request (401, or 403 other than rate limiting): never worked around."""


class NotFound(Failure):
    """The release or repository does not exist (or is invisible without a token)."""


class UsageError(Exception):
    """Invalid invocation or policy violation (exit 2)."""


# ---------------------------------------------------------------- versions

def tag_key(tag: str):
    m = TAG.match(tag or "")
    if not m:
        return None
    major, r, a, b = m.groups()
    return (int(major), int(r or 0), int(a or 0), int(b or 0))


def runtime_anchor(version: str):
    """'21R5'/'21R5.1' -> ('21R5', 2150); '21.2'/'21' -> ('21', 2102/2100)."""
    m = re.match(r"^\s*(\d+)(?:\s*R(\d+))?(?:\.(\d+))?", version or "")
    if not m:
        raise UsageError(f"unrecognised runtime version {version!r}")
    major, r, minor = int(m.group(1)), m.group(2), int(m.group(3) or 0)
    if r is not None:
        return f"{major}R{int(r)}", major * 100 + int(r) * 10
    return str(major), major * 100 + min(minor, 99)


def anchor_pick(tags, anchor: str):
    """4D:N -> newest N.x (no R tags), else newest below N; 4D:NRm -> newest N.x/NRk with k<=m."""
    m = re.match(r"^(\d+)(?:R(\d+))?$", anchor)
    if not m:
        raise Failure(f"invalid 4D anchor {anchor!r}")
    major, r = int(m.group(1)), m.group(2)
    keyed = [(tag_key(t), t) for t in tags if tag_key(t)]
    if r is None:
        pool = [x for x in keyed if x[0][0] == major and x[0][1] == 0]
    else:
        pool = [x for x in keyed if x[0][0] == major and x[0][1] <= int(r)]
    pool = pool or [x for x in keyed if x[0][0] < major]
    return max(pool)[1] if pool else None


_PARTIAL = re.compile(r"^v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?$")


def _semver(tag: str):
    """Numeric key for semver ranges; R tags are not semver and never satisfy a range."""
    m = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", tag or "")
    return tuple(int(g or 0) for g in m.groups()) if m else None


def _partial(text: str):
    m = _PARTIAL.match(text.strip())
    if not m:
        raise Failure(f"invalid version {text!r}")
    parts = []
    for g in m.groups():
        if g is None or g in "xX*":
            break
        parts.append(int(g))
    return parts


def _bump(parts):
    """Exclusive upper bound of an X-range such as 1 or 1.2."""
    if not parts:
        return None
    up = parts[:-1] + [parts[-1] + 1]
    return tuple(up + [0] * (3 - len(up)))


def _low(parts):
    return tuple(parts + [0] * (3 - len(parts)))


def _comparator(token: str):
    m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$", token)
    op, text = m.group(1) or "", m.group(2)
    parts = _partial(text)
    if op in ("", "="):
        if len(parts) == 3:
            return [("=", _low(parts))]
        return [(">=", _low(parts))] + ([("<", _bump(parts))] if parts else [])
    if op == "^":
        p = parts + [0] * (3 - len(parts))
        if p[0] > 0 or len(parts) == 1:
            up = (p[0] + 1, 0, 0)
        elif p[1] > 0 or len(parts) == 2:
            up = (0, p[1] + 1, 0)
        else:
            up = (0, 0, p[2] + 1)
        return [(">=", _low(parts)), ("<", up)]
    if op == "~":
        up = _bump(parts[:2])
        return [(">=", _low(parts))] + ([("<", up)] if up else [])
    if op == ">" and len(parts) < 3:
        return [(">=", _bump(parts))] if parts else [("<", (0, 0, 0))]
    if op == "<=" and len(parts) < 3:
        return [("<", _bump(parts))] if parts else []
    return [(op, _low(parts))]


def parse_range(spec: str):
    """Docs syntax: *, 1.*, 1.2.*, >= > <= < =, ^, ~, `a - b`, space-separated AND, || OR."""
    sets = []
    for alternative in spec.split("||"):
        alternative = alternative.strip().replace("\u2013", "-")
        hyphen = re.match(r"^(\S+)\s+-\s+(\S+)$", alternative)
        constraints = []
        if hyphen:
            a, b = _partial(hyphen.group(1)), _partial(hyphen.group(2))
            constraints.append((">=", _low(a)))
            constraints.append(("<=", _low(b)) if len(b) == 3 else ("<", _bump(b)))
        elif alternative not in ("", "*", "x", "X"):
            for token in re.findall(r"(?:>=|<=|>|<|=|\^|~)?\s*[^\s<>=^~]+", alternative):
                constraints.extend(_comparator(token.strip()))
        sets.append(constraints)
    return sets


def satisfies(tag: str, sets) -> bool:
    v = _semver(tag)
    if v is None:
        return False
    ops = {">=": v.__ge__, "<=": v.__le__, ">": v.__gt__, "<": v.__lt__, "=": v.__eq__}
    return any(all(ops[op](bound) for op, bound in constraints) for constraints in sets)


def classify(spec: dict, anchor):
    """Return (kind, value, lockVersion); kind is tag | latest | anchor | range."""
    if spec.get("tag"):
        return "tag", str(spec["tag"]), None
    version = spec.get("version")
    if version is None or str(version).strip() == "":
        return "latest", None, "latest"
    version = str(version).strip()
    if version.lower() == "latest":
        return "latest", None, version
    if version.lower() == "4d":
        if not anchor:
            raise Failure('"version": "4d" needs the runtime version (--runtime-version)')
        return "anchor", anchor, f"4D:{anchor}"
    if version[:3].upper() == "4D:":
        return "anchor", version[3:], version
    if TAG.match(version):
        return "tag", version, version
    return "range", parse_range(version), version


# ---------------------------------------------------------------- GitHub

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    """Minimal REST client. The token goes to api.github.com only and is never logged.
    401/403 (bad token, SAML) are hard failures with no anonymous retry, as in 4D.
    429/5xx/network errors get bounded backoff."""

    def __init__(self, token=None, offline=False, attempts=4):
        self.token = token or None
        self.offline = offline
        self.attempts = attempts
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, url: str, accept: str) -> bytes:
        if self.offline:
            raise Failure("offline: network access disabled")
        last = None
        for attempt in range(self.attempts):
            current = url
            delay = 0.0
            try:
                for _ in range(6):
                    headers = {"Accept": accept, "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
                    if self.token and urllib.parse.urlsplit(current).hostname == "api.github.com":
                        headers["Authorization"] = "Bearer " + self.token
                    try:
                        with self.opener.open(urllib.request.Request(current, headers=headers), timeout=60) as r:
                            if int(r.headers.get("Content-Length") or 0) > MAX_ARCHIVE:
                                raise Failure("response too large")
                            data = r.read(MAX_ARCHIVE + 1)
                            if len(data) > MAX_ARCHIVE:
                                raise Failure("response too large")
                            return data
                    except urllib.error.HTTPError as error:
                        if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                            current = urllib.parse.urljoin(current, error.headers["Location"])
                            continue
                        raise
                raise Failure("too many redirects")
            except urllib.error.HTTPError as error:
                last = f"HTTP {error.code}"
                if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
                    wait = float(error.headers.get("X-RateLimit-Reset") or 0) - time.time()
                    if wait > 60:
                        raise Failure(f"GitHub API rate limit exhausted (HTTP {error.code})"
                                      + ("" if self.token else "; a token raises the limit")) from None
                    delay = max(1.0, wait)
                elif error.code in (429, 500, 502, 503, 504):
                    delay = float(error.headers.get("Retry-After") or 0) or min(2 ** attempt + random.random(), 20)
                elif error.code in (401, 403):
                    raise AuthFailure(f"HTTP {error.code} from GitHub: " + (
                        "token rejected or not authorised for this resource (no anonymous retry, as 4D)"
                        if self.token else "access denied")) from None
                elif error.code == 404:
                    raise LookupError(url) from None
                else:
                    raise Failure(f"HTTP {error.code} for {url}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last = str(getattr(error, "reason", error))
                delay = min(2 ** attempt + random.random(), 20)
            if attempt + 1 < self.attempts:
                time.sleep(delay)
        raise Failure(f"network error for {url}: {last}")

    def json(self, path: str):
        return json.loads(self._request(API + path, "application/vnd.github+json"))

    def _hint(self):
        return "" if self.token else " (a token may be required for private repositories)"

    def release(self, repo: str, kind: str, value=None):
        try:
            if kind == "latest":
                return self.json(f"/repos/{repo}/releases/latest")
            if kind == "tag":
                return self.json(f"/repos/{repo}/releases/tags/{urllib.parse.quote(value, safe='')}")
        except LookupError:
            what = "latest release" if kind == "latest" else f"release {value!r}"
            raise NotFound(f"{what} not found in {repo}{self._hint()}") from None
        releases = self.releases(repo)
        tags = [r["tag_name"] for r in releases]
        if kind == "anchor":
            chosen = anchor_pick(tags, value)
        else:
            matching = [t for t in tags if satisfies(t, value)]
            chosen = max(matching, key=_semver) if matching else None
        if not chosen:
            raise Failure(f"no release of {repo} matches " + (f"4D:{value}" if kind == "anchor" else "the range"))
        return next(r for r in releases if r["tag_name"] == chosen)

    def releases(self, repo: str):
        out = []
        for page in range(1, 11):
            try:
                batch = self.json(f"/repos/{repo}/releases?per_page=100&page={page}")
            except LookupError:
                raise NotFound(f"repository {repo} not found{self._hint()}") from None
            out += [r for r in batch if not r.get("draft") and not r.get("prerelease")]
            if len(batch) < 100:
                break
        return out

    def child_dependencies(self, repo: str, tag: str):
        """Packages do not contain dependencies.json; 4D reads it from the repository at the tag."""
        path = f"/repos/{repo}/contents/Project/Sources/dependencies.json?ref={urllib.parse.quote(tag, safe='')}"
        try:
            data = self._request(API + path, "application/vnd.github.raw+json")
        except LookupError:
            return None
        where = f"Project/Sources/dependencies.json of {repo}@{tag}"
        try:
            value = json.loads(data.decode("utf-8-sig"))
        except ValueError as error:
            raise Failure(f"cannot parse {where}: {error}") from None
        try:
            return manifest_dependencies(value) or None
        except ValueError as error:
            raise Failure(f"invalid {where}: {error}") from None

    def download(self, asset: dict) -> bytes:
        if self.token:
            return self._request(asset["url"], "application/octet-stream")
        return self._request(asset["browser_download_url"], "application/octet-stream")


# ---------------------------------------------------------------- filesystem

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value, indent="\t") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 600, stale: float = 1800):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()
                    continue
            if time.time() > deadline:
                raise Failure(f"timed out waiting for lock {path}") from None
            time.sleep(0.2 + random.random() * 0.3)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def package_form(root: Path):
    if list((root / "Contents").glob("*.4DZ")) or list(root.glob("*.4DZ")):
        return "compiled"
    if (root / "Project").is_dir() and list((root / "Project").glob("*.4DProject")):
        return "interpreted"
    return None


def safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a component zip, rejecting traversal, absolute, drive and symlink entries;
    strip one `<name>.4dbase` wrapper (4D stores unwrapped content)."""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXTRACTED:
            raise Failure("archive exceeds extraction limits")
        for info in infos:
            name = info.filename
            if name.startswith("__MACOSX/") or name.rsplit("/", 1)[-1] == ".DS_Store":
                continue
            if (name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/")
                    or re.match(r"^[A-Za-z]:", name) or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise Failure(f"unsafe archive entry {name!r}")
            target = dest / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            if mode and os.name != "nt":
                os.chmod(target, mode | 0o600)
    kids = [c for c in dest.iterdir() if c.name not in ("__MACOSX", ".DS_Store")]
    root = kids[0] if len(kids) == 1 and kids[0].is_dir() and kids[0].suffix.lower() == ".4dbase" else dest
    if not package_form(root):
        raise Failure("archive is not a recognised 4D component package")
    return root


def install_tree(source: Path, target: Path, replace: bool) -> bool:
    """Atomically move `source` to `target`. Returns False if `target` already existed."""
    stage = target.with_name(f".{target.name}.{os.getpid()}.partial")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.move(str(source), str(stage))
    if replace and target.exists():
        trash = target.with_name(f".{target.name}.{os.getpid()}.old")
        os.replace(target, trash)
        shutil.rmtree(trash, ignore_errors=True)
    try:
        os.replace(stage, target)
        return True
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)
        if target.is_dir():
            return False
        raise


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    return "windows" if os.name == "nt" else "linux"


def user_cache(home=None) -> Path:
    plat = platform_name()
    if plat == "macos":
        return (home or Path.home()) / "Library" / "Caches"
    if plat == "windows":
        if home:
            return home / "AppData" / "Local"
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if home:
        return home / ".cache"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def isolation_env(home: Path) -> dict:
    plat = platform_name()
    if plat == "macos":
        return {"HOME": str(home), "CFFIXED_USER_HOME": str(home)}
    if plat == "windows":
        # tool4d needs USERPROFILE as well as LOCALAPPDATA/APPDATA, and quits silently without
        # loading the project when either AppData folder is missing (dependencies-debug run 43464410687).
        local, roaming = home / "AppData" / "Local", home / "AppData" / "Roaming"
        local.mkdir(parents=True, exist_ok=True)
        roaming.mkdir(parents=True, exist_ok=True)
        return {"USERPROFILE": str(home), "HOME": str(home), "LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    return {"HOME": str(home), "XDG_CACHE_HOME": str(home / ".cache")}


def native_path(path: Path) -> str:
    """Folder path in the form 4D writes in the lock file (HFS on macOS)."""
    if platform_name() != "macos":
        return str(path) + os.sep
    real = os.path.realpath(path)
    volume, rest = None, real
    if real.startswith("/Volumes/"):
        volume, _, rest = real[len("/Volumes/"):].partition("/")
    if volume is None:
        with contextlib.suppress(OSError):
            volume = next((v for v in os.listdir("/Volumes") if os.path.realpath("/Volumes/" + v) == "/"), None)
    return ":".join([volume or "Macintosh HD"] + [p for p in rest.split("/") if p]) + ":"


# ---------------------------------------------------------------- project

def locate_project(path: Path):
    path = path.resolve()
    if path.is_file() and path.suffix.lower() == ".4dproject":
        return path.parent.parent
    if path.is_dir():
        found = list((path / "Project").glob("*.4DProject"))
        if found:
            return path
    raise UsageError(f"no 4D project at {path}")


def manifest_dependencies(value) -> dict:
    """The `dependencies` object of a dependencies.json/environment4d.json document."""
    if not isinstance(value, dict):
        raise ValueError("the top level must be a JSON object")
    dependencies = value.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ValueError('"dependencies" must be a JSON object')
    return dependencies


def load_manifest(path: Path) -> dict:
    try:
        return manifest_dependencies(load_json(path))
    except ValueError as error:
        raise UsageError(f"invalid {path}: {error}") from None


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise UsageError(f"cannot read {path}: {error}") from None


def find_environment(package: Path):
    """environment4d.json in the package folder or the nearest parent folder."""
    for folder in [package, *package.parents]:
        candidate = folder / "environment4d.json"
        if candidate.is_file():
            return candidate
    return None


def component_override(package: Path, name: str):
    components = package / "Components"
    for candidate in (f"{name}.4dbase", f"{name}.4DZ", name):
        if (components / candidate).exists():
            return f"Components/{candidate}"
    return None


# ---------------------------------------------------------------- manager

class Manager:
    def __init__(self, args, client: GitHub):
        self.args = args
        self.client = client
        self.runtime = args.runtime
        self.anchor, self.version_code = runtime_anchor(args.runtime_version) if args.runtime_version else (None, None)
        self.home = Path(args.home).resolve() if args.home else None
        self.dependencies_folder = Path(args.dependencies_folder).resolve() if args.dependencies_folder else \
            user_cache(self.home) / self.runtime / "Dependencies"
        self.archive_store = None
        if args.archive_store != "none" and (args.archive_store or self.home):
            self.archive_store = Path(args.archive_store).resolve() if args.archive_store else \
                user_cache() / "miyako-4d-dependencies" / "archives"
        self.package = locate_project(Path(args.project))
        self.lock_path = self.package / f"userPreferences.{args.user or getpass.getuser()}" / "dependencies-lock.json"
        self.results = {}
        self.incomplete = []
        self.warnings = []
        self.combined = {}  # name -> conflicting secondary declarations, resolved together

    def declarations(self):
        declared = {}
        sources = self.package / "Project" / "Sources" / "dependencies.json"
        if sources.is_file():
            declared = dict(load_manifest(sources))
        environment = find_environment(self.package)
        env_entries = {}
        if environment:
            env_entries = {}
            for name, entry in load_manifest(environment).items():
                if name not in declared:
                    # 4D 21.2 and 21R5 neither fetch nor load a name declared only here (M9 C4a-C4c).
                    self.warnings.append(f"{name}: declared only in {environment}; 4D ignores it")
                    continue
                # environment4d.json may map a name straight to a path.
                env_entries[name] = entry
                declared[name] = {"path": entry} if isinstance(entry, str) else entry
        return declared, environment, env_entries

    def lock_pins(self):
        if self.args.update or not self.lock_path.is_file():
            return {}
        try:
            entries = json.loads(self.lock_path.read_text("utf-8-sig")).get("dependencies") or {}
            return {n: e for n, e in entries.items() if isinstance(e, dict) and e.get("tag") and e.get("github")}
        except (OSError, ValueError, AttributeError):
            self.warnings.append(f"ignored unreadable lock file {self.lock_path}")
            return {}

    def run(self):
        declared, environment, env_entries = self.declarations()
        pins = self.lock_pins()
        base_warnings = list(self.warnings)
        # A secondary declaration that the first resolution of its name does not satisfy is resolved together
        # with the earlier ones, which can change the tag and so the children: resolve again from the start.
        for _ in range(64):
            self.results, self.incomplete, self.warnings = {}, [], list(base_warnings)
            if not self.resolve_all(declared, pins):
                break
        else:
            raise UsageError("secondary dependency conflicts did not settle")
        if not self.args.no_write_lock:
            self.write_lock()
        if self.args.rewrite:
            self.rewrite(Path(self.args.rewrite).resolve(), environment, env_entries)
        return self.report(environment)

    def resolve_all(self, declared, pins) -> bool:
        """Breadth-first resolution; returns True when a new secondary conflict requires another pass."""
        queue = [(name, spec, True) for name, spec in declared.items()]
        specs = {}
        while queue:
            name, spec, primary = queue.pop(0)
            if name in self.results:
                previous = self.results[name]
                if not isinstance(spec, dict) or primary or previous["primary"] or name in self.combined:
                    if name in self.combined and isinstance(spec, dict) and spec not in self.combined[name]:
                        self.combined[name].append(spec)
                        return True
                    continue
                if spec == specs[name]:
                    continue
                if self._floating(specs[name], spec) and self._compatible(previous, spec):
                    self.warnings.append(f"{name}: secondary declaration {json.dumps(spec)} is satisfied by "
                                         f"{previous.get('github')}@{previous.get('tag')}; how 4D combines latest "
                                         "or 4d anchors with other declarations has not been observed")
                    continue
                # 4D resolves differing secondary declarations together and records the chosen tag (C6a/C6b).
                self.combined[name] = [specs[name], spec]
                return True
            if not primary and name in self.combined:
                entry = self.resolve_one(name, None, False, pins, combined=self.combined[name])
            else:
                entry = self.resolve_one(name, spec, primary, pins)
                specs[name] = spec
            self.results[name] = entry
            for child, child_spec in (entry.get("dependencies") or {}).items():
                queue.append((child, child_spec, False))
        return False

    def _floating(self, *specs) -> bool:
        try:
            return any(classify(spec, self.anchor)[0] in ("latest", "anchor") for spec in specs)
        except Failure:
            return False

    def combined_release(self, name, repo, specs, pins):
        """Resolve conflicting secondary declarations together, as 4D does (M9 C6a-C6c): the highest release
        that satisfies every range, or the single exact tag if it satisfies them; otherwise a conflict.
        Returns (tag, release, lockVersion); 4D records the chosen tag as the lock `version`."""
        described = ", ".join(json.dumps(spec) for spec in specs)
        if any(str(spec.get("github", "")).strip("/") != repo for spec in specs):
            raise Failure(f"secondary declarations of {name} name different repositories: {described}")
        kinds = [classify(spec, self.anchor) for spec in specs]
        if any(kind in ("latest", "anchor") for kind, _, _ in kinds):
            raise Failure(f"secondary declarations of {name} conflict ({described}); combining latest or "
                          "4d anchors with other constraints has not been observed in 4D")
        tags = {value for kind, value, _ in kinds if kind == "tag"}
        ranges = [value for kind, value, _ in kinds if kind == "range"]
        if len(tags) > 1:
            raise Failure(f"secondary dependency {name} is disabled by conflicting tags "
                          f"{', '.join(sorted(tags))}; 4D does not load the components that need it")
        if tags:
            tag = tags.pop()
            if not all(satisfies(tag, value) for value in ranges):
                raise Failure(f"secondary dependency {name} is disabled by a version conflict: {described}")
            return tag, None, tag
        pin = pins.get(name)
        if pin and pin["github"] == repo and all(satisfies(pin["tag"], value) for value in ranges):
            return pin["tag"], None, pin["tag"]
        releases = self.client.releases(repo)
        matching = [r for r in releases if all(satisfies(r["tag_name"], value) for value in ranges)]
        if not matching:
            raise Failure(f"secondary dependency {name} is disabled by a version conflict: no release of "
                          f"{repo} satisfies {described}")
        release = max(matching, key=lambda r: _semver(r["tag_name"]))
        return release["tag_name"], release, release["tag_name"]

    def _compatible(self, previous, spec):
        if spec.get("github") and spec.get("github") != previous.get("github"):
            return False
        if not previous.get("tag"):
            return True
        try:
            kind, value, _ = classify(spec, self.anchor)
        except Failure:
            return False
        if kind == "tag":
            return value == previous["tag"]
        return satisfies(previous["tag"], value) if kind == "range" else True

    def fail(self, entry, reason):
        entry["error"] = reason
        self.incomplete.append({"name": entry["name"], "reason": reason})
        return entry

    def resolve_one(self, name, spec, primary, pins, combined=None):
        entry = {"name": name, "primary": primary}
        if combined:
            spec = combined[0]
            entry["declarations"] = combined
        if not isinstance(spec, dict):
            return self.fail(entry, f"invalid declaration {json.dumps(spec)}: expected a JSON object")
        if spec.get("gitlab"):
            entry["gitlab"] = spec["gitlab"]
            return self.fail(entry, "GitLab dependencies are not supported by this emulator")
        if not spec.get("github"):
            entry.update(source="local", local=spec.get("path", ""))
            return entry
        repo = str(spec["github"]).strip("/")
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            return self.fail(entry, f"invalid github repository {repo!r}")
        entry["github"] = repo
        try:
            if combined:
                tag, release, entry["version"] = self.combined_release(name, repo, combined, pins)
                entry["tag"] = tag
                self.materialise(entry, repo, tag, release)
                return self.finish(entry, name)
            kind, value, lock_version = classify(spec, self.anchor)
            if lock_version:
                entry["version"] = lock_version
            pin = pins.get(name)
            release = None
            if (kind != "tag" and pin and pin["github"] == repo and (pin.get("version") or None) == lock_version
                    and (kind != "range" or satisfies(pin["tag"], value))):
                tag, entry["pinned"] = pin["tag"], True
            elif kind == "tag":
                tag = value
            else:
                release = self.client.release(repo, kind, value)
                tag = release["tag_name"]
            entry["tag"] = tag
            self.materialise(entry, repo, tag, release)
        except Failure as error:
            return self.fail(entry, str(error))
        return self.finish(entry, name)

    def finish(self, entry, name):
        override = component_override(self.package, name)
        if override:
            entry["overloadedBy"] = override
            entry["errors"] = [{"errCode": 19, "message":
                                f"A component with the same name ({name}) is already loaded from {override}"}]
        return entry

    def materialise(self, entry, repo, tag, release):
        repo_folder = self.dependencies_folder / ".github" / repo
        target = repo_folder / tag
        meta_path = repo_folder / f"{tag}.json"
        if self.archive_store is None and target.is_dir() and meta_path.is_file():
            meta = load_json(meta_path)
            entry.update({k: meta.get(k) for k in ("htmlURL", "archiveURL", "archiveSize", "dependencies")})
            if not package_form(target):
                # W5: 4D keeps the metadata but neither loads nor repairs a damaged cache folder.
                raise Failure(f"cached {repo}@{tag} is not a loadable package and 4D does not repair it; "
                              f"delete {target} to re-fetch")
            entry.update(source="cache", reuse=UNVERIFIED_REUSE, path=str(target))
            return
        repo_folder.mkdir(parents=True, exist_ok=True)
        source = "download"
        archive = meta = None
        if self.archive_store is not None:
            archive, meta, source = self.archived(repo, tag, release)
        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=repo_folder) as tmp:
            tmp = Path(tmp)
            if archive is None:
                release = release or self.client.release(repo, "tag", tag)
                asset = self.asset(release, repo)
                data = self.client.download(asset)
                self.check_digest(data, asset)
                archive = tmp / asset["name"]
                archive.write_bytes(data)
                meta = self.meta(release, asset, repo)
                meta["dependencies"] = self.client.child_dependencies(repo, tag)
            root = safe_extract(archive, tmp / "x")
            with file_lock(repo_folder / f".{tag}.lock"):
                installed = install_tree(root, target, replace=self.archive_store is not None)
                if installed or not meta_path.exists():
                    write_json(meta_path, {k: meta.get(k) for k in
                                           ("github", "tag", "htmlURL", "archiveURL", "archiveSize", "dependencies")})
        if self.archive_store is not None:
            reuse = "verified archive"
        else:
            reuse = "downloaded" if installed else UNVERIFIED_REUSE
            source = source if installed else "cache"
        entry.update(source=source, reuse=reuse, path=str(target))
        entry.update({k: meta.get(k) for k in ("htmlURL", "archiveURL", "archiveSize", "dependencies")})

    @staticmethod
    def asset(release, repo):
        name = repo.split("/")[1] + ".zip"
        asset = next((a for a in release.get("assets") or [] if a.get("name") == name), None)
        if not asset:
            raise Failure(f"release {release.get('tag_name')} of {repo} has no {name} asset")
        return asset

    @staticmethod
    def check_digest(data: bytes, asset) -> str:
        actual = hashlib.sha256(data).hexdigest()
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:") and digest[7:].lower() != actual:
            raise Failure(f"{asset.get('name')}: sha256 does not match the release asset digest")
        if asset.get("size") is not None and asset["size"] != len(data):
            raise Failure(f"{asset.get('name')}: size does not match the release asset")
        return actual

    def meta(self, release, asset, repo):
        return {"github": repo, "tag": release["tag_name"], "htmlURL": release.get("html_url"),
                "archiveURL": asset["url"] if self.client.token else asset["browser_download_url"],
                "archiveSize": asset.get("size"), "asset": asset.get("name"), "assetId": asset.get("id"),
                "digest": asset.get("digest")}

    def archived(self, repo, tag, release):
        """Return (archive, meta, source) from the verified archive store, downloading on a miss.
        A stored archive is re-hashed on every use and checked against the current release
        digest (online) or the digest recorded when it was stored (offline)."""
        folder = self.archive_store / "github" / repo / tag
        meta_path = folder / "meta.json"
        with file_lock(self.archive_store / "github" / repo / f".{tag}.lock"):
            stored = None
            if meta_path.is_file():
                with contextlib.suppress(OSError, ValueError):
                    stored = json.loads(meta_path.read_text("utf-8"))
            current, lookup_error = None, None
            if not self.client.offline:
                try:
                    release = release or self.client.release(repo, "tag", tag)
                    current = self.asset(release, repo)
                except AuthFailure:
                    raise  # a rejected token is not an outage: no fallback, as 4D does not retry
                except Failure as error:
                    lookup_error = error
            if stored and stored.get("asset"):
                archive = folder / stored["asset"]
                expected = (current or {}).get("digest") or stored.get("digest")
                if archive.is_file():
                    actual = sha256_file(archive)
                    if actual == stored.get("sha256") and (not expected or expected == f"sha256:{actual}"):
                        if lookup_error:
                            self.warnings.append(f"{repo}@{tag}: release lookup failed ({lookup_error}); "
                                                 "using the archive verified against its recorded digest")
                        return archive, stored, "archive"
                    self.warnings.append(f"{repo}@{tag}: stored archive failed verification; discarded")
            if current is None:
                raise lookup_error or Failure(f"{repo}@{tag}: offline and no verified archive is stored")
            data = self.client.download(current)
            meta = self.meta(release, current, repo)
            meta["sha256"] = self.check_digest(data, current)
            meta["dependencies"] = self.client.child_dependencies(repo, tag)
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".archive.", dir=folder)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, folder / current["name"])
            write_json(meta_path, meta)
            return folder / current["name"], meta, "download"

    def write_lock(self):
        lock = {"version": self.version_code, "dependencies": {}} if self.version_code else {"dependencies": {}}
        for name, entry in sorted(self.results.items()):
            if not entry.get("github"):
                continue
            item = {"github": entry["github"]}
            for key in ("version", "tag", "htmlURL", "archiveURL", "archiveSize"):
                if entry.get(key) is not None:
                    item[key] = entry[key]
            if entry.get("error") and not entry.get("htmlURL"):
                # W15: without release metadata 4D writes no tag and empty URLs.
                item.pop("tag", None)
                item.update(htmlURL="", archiveURL="")
            item["path"] = native_path(Path(entry["path"])) if entry.get("path") else ""
            item["dependencies"] = entry.get("dependencies")
            item["found"] = bool(entry.get("path")) and not entry.get("error")
            item["loaded"] = None
            if entry.get("errors"):
                item["errors"] = entry["errors"]
            elif entry.get("error"):
                item["errors"] = [{"message": entry["error"]}]
            lock["dependencies"][name] = item
        write_json(self.lock_path, lock)

    def rewrite(self, package: Path, environment, env_entries):
        """tool4d loads exact `tag` entries only and never resolves children: write a flat
        exact-tag dependencies.json into a *workspace copy* of the package."""
        target = package / "Project" / "Sources" / "dependencies.json"
        original = load_json(target) if target.is_file() else {}
        deps = dict(original.get("dependencies") or {})
        for name, entry in self.results.items():
            if entry.get("github") and entry.get("tag") and not entry.get("error") and name not in env_entries:
                kept = {k: v for k, v in (deps.get(name) or {}).items() if k not in ("version", "tag", "github")}
                deps[name] = {"github": entry["github"], "tag": entry["tag"], **kept}
        original["dependencies"] = deps
        write_json(target, original, indent=2)
        github_env = [n for n, s in env_entries.items() if isinstance(s, dict) and s.get("github")]
        if not github_env:
            return
        copy_env = package / "environment4d.json"
        if not copy_env.is_file():
            self.warnings.append(f"environment4d.json GitHub entries {github_env} are outside {package}; "
                                 "tool4d may not load them unless they use exact tags")
            return
        value = load_json(copy_env)
        for name in github_env:
            entry = self.results.get(name) or {}
            if entry.get("tag") and not entry.get("error"):
                value["dependencies"][name] = {"github": entry["github"], "tag": entry["tag"]}
        write_json(copy_env, value, indent=2)

    def report(self, environment):
        return {
            "runtime": self.runtime, "runtimeVersion": self.args.runtime_version or None,
            "platform": platform_name(), "package": str(self.package),
            "dependenciesFolder": str(self.dependencies_folder),
            "home": str(self.home) if self.home else None,
            "env": isolation_env(self.home) if self.home else {},
            "archiveStore": str(self.archive_store) if self.archive_store else None,
            "lockFile": None if self.args.no_write_lock else str(self.lock_path),
            "environment4d": str(environment) if environment else None,
            "token": bool(self.client.token), "offline": self.client.offline,
            "resolved": {n: e["tag"] for n, e in sorted(self.results.items()) if e.get("tag") and not e.get("error")},
            "dependencies": self.results, "incomplete": self.incomplete, "warnings": self.warnings,
        }


# ---------------------------------------------------------------- CLI

def detect_version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UsageError(f"cannot query {binary} --version: {error}") from None
    match = re.search(r"\b(\d+(?:\s*R\d+)?(?:\.\d+)?)\s+build\b", out.stdout + out.stderr)
    if not match:
        raise UsageError(f"cannot parse the version printed by {binary}")
    return match.group(1).replace(" ", "")


def enforce_runner_policy(runtime: str) -> None:
    if (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and runtime != "tool4d"):
        raise UsageError(f"runtime {runtime!r} is only allowed on self-hosted runners; "
                         "GitHub-hosted runners must use tool4d")


def read_token(args):
    if args.token_file:
        return (load_json(Path(args.token_file)) or {}).get("token") or None
    return os.environ.get(TOKEN_ENV) or None


def github_output(report, report_file=None) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    values = {"dependencies-folder": report["dependenciesFolder"], "home": report["home"] or "", "lock-file": report["lockFile"] or "",
              "resolved": json.dumps(report["resolved"]), "incomplete": json.dumps(report["incomplete"]),
              "env": json.dumps(report["env"]), "complete": "false" if report["incomplete"] else "true",
              "report-file": report_file or ""}
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            delimiter = f"EOF_{os.urandom(8).hex()}"
            f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def parser():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True, help="package folder or .4DProject file")
    p.add_argument("--runtime", default="tool4d", help="tool4d | 4D | 4D Server (cache folder name)")
    p.add_argument("--runtime-version", help='e.g. 21R5 or 21.2; anchors "version": "4d"')
    p.add_argument("--runtime-path", help="runtime executable, queried with --version when --runtime-version is absent")
    p.add_argument("--home", help="isolated profile root: the cache goes inside it; `env` says how to point the runtime at it")
    p.add_argument("--dependencies-folder", help="explicit Dependencies folder (overrides the --home location)")
    p.add_argument("--archive-store", help="verified archive store; 'none' disables it (default: on with --home)")
    p.add_argument("--token-file", help=f"JSON {{\"token\": ...}} like 4D's github.json; otherwise ${TOKEN_ENV}")
    p.add_argument("--update", action="store_true", help="ignore tags pinned in dependencies-lock.json")
    p.add_argument("--offline", action="store_true", help="no network: cached or archived tags only")
    p.add_argument("--no-write-lock", action="store_true", help="do not write dependencies-lock.json")
    p.add_argument("--user", help="name for userPreferences.<user> (default: current user)")
    p.add_argument("--rewrite", help="workspace package copy that receives an exact-tag dependencies.json (tool4d)")
    p.add_argument("--output", help="write the JSON report here (default: stdout)")
    return p


def action_argv():
    """Build argv from the composite action's INPUT_* environment."""
    get = lambda name: os.environ.get("FD_" + name.upper().replace("-", "_"), "").strip()
    argv = ["--project", get("project") or "."]
    for name in ("runtime", "runtime-version", "runtime-path", "home", "dependencies-folder",
                 "archive-store", "rewrite", "output"):
        if get(name):
            argv += [f"--{name}", get(name)]
    for name, flag in (("update", "--update"), ("offline", "--offline")):
        if get(name).lower() == "true":
            argv.append(flag)
    if get("write-lock").lower() == "false":
        argv.append("--no-write-lock")
    return argv


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    action = argv == ["--action"]
    args = parser().parse_args(action_argv() if action else argv)
    if action and not args.output:
        fd, args.output = tempfile.mkstemp(prefix="dependencies-report-", suffix=".json",
                                           dir=os.environ.get("RUNNER_TEMP") or None)
        os.close(fd)
    try:
        runtime = RUNTIMES.get(args.runtime.strip().lower())
        if not runtime:
            raise UsageError(f"unknown runtime {args.runtime!r}; use tool4d, 4D or 4D Server")
        args.runtime = runtime
        enforce_runner_policy(runtime)
        if args.home and runtime != "tool4d" and platform_name() == "windows":
            raise UsageError("4D and 4D Server on Windows ignore LOCALAPPDATA/APPDATA overrides, so --home cannot "
                             "isolate them; run the self-hosted runner as a dedicated user instead")
        if not args.runtime_version and args.runtime_path:
            args.runtime_version = detect_version(args.runtime_path)
        report = Manager(args, GitHub(read_token(args), offline=args.offline)).run()
    except UsageError as error:
        print(f"fetch_dependencies: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    github_output(report, args.output)
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else ""
    for item in report["incomplete"]:
        print(f"{prefix}dependency {item['name']} incomplete: {item['reason']}", file=sys.stderr)
    for warning in report["warnings"]:
        print(f"{prefix}{warning}", file=sys.stderr)
    if action and os.environ.get("FD_FAIL_ON_INCOMPLETE", "true").lower() == "false":
        return 0
    return 3 if report["incomplete"] else 0


if __name__ == "__main__":
    sys.exit(main())
