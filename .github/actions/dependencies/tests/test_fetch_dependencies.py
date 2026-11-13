"""Offline unit tests for fetch_dependencies.py (fixtures model real 4D observations)."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_dependencies as fd  # noqa: E402


def component_zip(name, wrapped=True, extra=None):
    buf = io.BytesIO()
    prefix = f"{name}.4dbase/" if wrapped else ""
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(prefix + f"Project/{name}.4DProject", "{}")
        z.writestr(prefix + "Project/Sources/Methods/m.4dm", "//")
        for entry, data in (extra or {}).items():
            z.writestr(entry, data)
    return buf.getvalue()


class FakeGitHub(fd.GitHub):
    """Serves releases from memory; records calls."""

    def __init__(self, repos, children=None, token=None, offline=False):
        super().__init__(token, offline)
        self.repos = repos  # repo -> list of (tag, bytes)
        self.children = children or {}
        self.calls = []
        self.latest = {}

    def _release(self, repo, tag, data):
        name = repo.split("/")[1] + ".zip"
        return {"tag_name": tag, "html_url": f"https://github.com/{repo}/releases/tag/{tag}", "assets": [{
            "name": name, "id": 1, "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "url": f"https://api.github.com/repos/{repo}/releases/assets/1",
            "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{name}", "_data": data}]}

    def json(self, path):
        if self.offline:
            raise fd.Failure("offline: network access disabled")
        self.calls.append(path)
        parts = path.split("/")
        repo = "/".join(parts[2:4])
        if repo not in self.repos:
            raise LookupError(path)
        releases = [self._release(repo, t, d) for t, d in self.repos[repo]]
        if path.endswith("/releases/latest"):
            tag = self.latest.get(repo) or self.repos[repo][-1][0]
            return next(r for r in releases if r["tag_name"] == tag)
        if "/releases/tags/" in path:
            tag = path.rsplit("/", 1)[1]
            for r in releases:
                if r["tag_name"] == tag:
                    return r
            raise LookupError(path)
        return releases if "page=1" in path else []

    def child_dependencies(self, repo, tag):
        self.calls.append(f"contents {repo}@{tag}")
        return self.children.get((repo, tag))

    def download(self, asset):
        if self.offline:
            raise fd.Failure("offline")
        self.calls.append("download " + asset["name"])
        return asset["_data"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.pkg = self.tmp / "P"
        (self.pkg / "Project" / "Sources").mkdir(parents=True)
        (self.pkg / "Project" / "P.4DProject").write_text("{}")
        env = mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "", "GITHUB_OUTPUT": ""})
        env.start()
        self.addCleanup(env.stop)

    def declare(self, deps, file="Project/Sources/dependencies.json"):
        (self.pkg / file).write_text(json.dumps({"dependencies": deps}))

    def run_manager(self, client, *extra):
        argv = ["--project", str(self.pkg), "--user", "u", "--home", str(self.tmp / "home"),
                "--archive-store", str(self.tmp / "store"), *extra]
        args = fd.parser().parse_args(argv)
        args.runtime = fd.RUNTIMES[args.runtime.lower()]
        return fd.Manager(args, client).run()


class Versions(unittest.TestCase):
    TAGS = ["20.0.1", "20.0.3", "20.0.23", "20.1.0", "21.0", "21.5", "21R2", "21R5", "21R5.1", "21R6", "22.0"]

    def pick(self, spec):
        sets = fd.parse_range(spec)
        hits = [t for t in self.TAGS if fd.satisfies(t, sets)]
        return max(hits, key=fd._semver) if hits else None

    def test_ranges(self):
        self.assertEqual(self.pick("^20.0.0"), "20.1.0")
        self.assertEqual(self.pick("~20.0.0"), "20.0.23")
        self.assertEqual(self.pick("20.0.*"), "20.0.23")
        self.assertEqual(self.pick("20.*"), "20.1.0")
        self.assertEqual(self.pick(">=20.0.3 <20.0.23"), "20.0.3")
        self.assertEqual(self.pick("20.0.1 - 20.0.3"), "20.0.3")
        self.assertEqual(self.pick("20.0.1 \u2013 20.0.3"), "20.0.3")
        self.assertEqual(self.pick("<20.0.3 || >=22"), "22.0")
        self.assertEqual(self.pick("*"), "22.0")
        self.assertEqual(self.pick(">21.5"), "22.0")
        self.assertIsNone(self.pick("^23.0.0"))

    def test_r_tags_never_match_ranges(self):
        self.assertFalse(fd.satisfies("21R5", fd.parse_range(">=21")))

    def test_anchor(self):
        self.assertEqual(fd.anchor_pick(self.TAGS, "21"), "21.5")
        self.assertEqual(fd.anchor_pick(self.TAGS, "21R5"), "21R5.1")
        self.assertEqual(fd.anchor_pick(self.TAGS, "21R3"), "21R2")
        self.assertEqual(fd.anchor_pick(["20.0.3", "21R2"], "21"), "20.0.3")

    def test_runtime_anchor(self):
        self.assertEqual(fd.runtime_anchor("21R5"), ("21R5", 2150))
        self.assertEqual(fd.runtime_anchor("21.2"), ("21", 2102))
        self.assertEqual(fd.runtime_anchor("21.0"), ("21", 2100))

    def test_classify_lock_versions(self):
        self.assertEqual(fd.classify({}, "21")[2], "latest")
        self.assertEqual(fd.classify({"version": "4d"}, "21R5")[:3], ("anchor", "21R5", "4D:21R5"))
        self.assertEqual(fd.classify({"version": "20.0.1"}, None), ("tag", "20.0.1", "20.0.1"))
        self.assertEqual(fd.classify({"tag": "21.3"}, None), ("tag", "21.3", None))
        with self.assertRaises(fd.Failure):
            fd.classify({"version": "4d"}, None)


class Extraction(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def extract(self, data):
        (self.tmp / "a.zip").write_bytes(data)
        return fd.safe_extract(self.tmp / "a.zip", self.tmp / "x")

    def test_unwraps_4dbase(self):
        root = self.extract(component_zip("c", extra={"__MACOSX/._c": "x"}))
        self.assertEqual(root.name, "c.4dbase")
        self.assertTrue((root / "Project" / "c.4DProject").is_file())

    def test_rejects_traversal_and_symlink(self):
        with self.assertRaises(fd.Failure):
            self.extract(component_zip("c", extra={"../evil": "x"}))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            info = zipfile.ZipInfo("c.4dbase/link")
            info.external_attr = 0o120777 << 16
            z.writestr(info, "/etc/passwd")
        with self.assertRaises(fd.Failure):
            self.extract(buf.getvalue())

    def test_rejects_non_component(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "x")
        with self.assertRaises(fd.Failure):
            self.extract(buf.getvalue())


class Manager(Base):
    def repos(self):
        return {"o/a": [("1.0.0", component_zip("a")), ("1.2.0", component_zip("a"))],
                "o/b": [("21.0", component_zip("b")), ("21R5.1", component_zip("b")), ("21.5", component_zip("b"))]}

    def test_resolution_recursion_and_cache_layout(self):
        self.declare({"a": {"github": "o/a", "version": "^1.0.0"}})
        client = FakeGitHub(self.repos(), {("o/a", "1.2.0"): {"b": {"github": "o/b", "version": "4d"}}})
        report = self.run_manager(client, "--runtime-version", "21R5")
        self.assertEqual(report["resolved"], {"a": "1.2.0", "b": "21R5.1"})
        self.assertEqual(report["incomplete"], [])
        dep = Path(report["dependenciesFolder"])
        self.assertTrue(str(dep).endswith(os.path.join("tool4d", "Dependencies")))
        self.assertTrue((dep / ".github/o/a/1.2.0/Project/a.4DProject").is_file())
        meta = json.loads((dep / ".github/o/a/1.2.0.json").read_text())
        self.assertEqual(meta["dependencies"], {"b": {"github": "o/b", "version": "4d"}})
        lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())
        self.assertEqual(lock["version"], 2150)
        self.assertEqual(lock["dependencies"]["b"]["version"], "4D:21R5")
        self.assertFalse(report["dependencies"]["b"]["primary"])

    def test_lock_pin_and_update(self):
        self.declare({"a": {"github": "o/a"}})
        lock = self.pkg / "userPreferences.u/dependencies-lock.json"
        lock.parent.mkdir()
        lock.write_text(json.dumps({"dependencies": {"a": {"github": "o/a", "version": "latest", "tag": "1.0.0"}}}))
        self.assertEqual(self.run_manager(FakeGitHub(self.repos()))["resolved"], {"a": "1.0.0"})
        self.assertEqual(self.run_manager(FakeGitHub(self.repos()), "--update")["resolved"], {"a": "1.2.0"})

    def test_primary_wins_over_secondary(self):
        self.declare({"a": {"github": "o/a", "tag": "1.2.0"}, "b": {"github": "o/b", "tag": "21.0"}})
        client = FakeGitHub(self.repos(), {("o/a", "1.2.0"): {"b": {"github": "o/b", "tag": "21.5"}}})
        report = self.run_manager(client)
        self.assertEqual(report["resolved"]["b"], "21.0")
        self.assertEqual(report["warnings"], [])

    def test_environment4d_overrides_and_components_precedence(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        (self.pkg / "environment4d.json").write_text(json.dumps({"dependencies": {"a": {"github": "o/a", "tag": "1.2.0"}}}))
        (self.pkg / "Components" / "a.4dbase").mkdir(parents=True)
        report = self.run_manager(FakeGitHub(self.repos()))
        entry = report["dependencies"]["a"]
        self.assertEqual(entry["tag"], "1.2.0")
        self.assertEqual(entry["overloadedBy"], "Components/a.4dbase")
        self.assertEqual(entry["errors"][0]["errCode"], 19)
        self.assertEqual(report["incomplete"], [])

    def test_offline_uses_reverified_archive_only(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}, "c": {"github": "o/a"}})
        self.run_manager(FakeGitHub(self.repos()), "--no-write-lock")
        report = self.run_manager(FakeGitHub(self.repos(), offline=True), "--offline", "--no-write-lock")
        self.assertEqual(report["dependencies"]["a"]["source"], "archive")
        self.assertEqual([i["name"] for i in report["incomplete"]], ["c"])

    def test_tampered_archive_is_discarded(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        self.run_manager(FakeGitHub(self.repos()))
        stored = self.tmp / "store/github/o/a/1.0.0/a.zip"
        stored.write_bytes(component_zip("evil"))
        offline = self.run_manager(FakeGitHub(self.repos(), offline=True), "--offline")
        self.assertEqual(len(offline["incomplete"]), 1)
        client = FakeGitHub(self.repos())
        online = self.run_manager(client)
        self.assertEqual(online["dependencies"]["a"]["source"], "download")
        self.assertIn("download a.zip", client.calls)

    def test_shared_mode_reuse_is_unverified(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        args = ["--archive-store", "none"]
        self.run_manager(FakeGitHub(self.repos()), *args)
        client = FakeGitHub(self.repos())
        report = self.run_manager(client, *args)
        self.assertEqual(report["dependencies"]["a"]["reuse"], fd.UNVERIFIED_REUSE)
        self.assertEqual(client.calls, [])

    def test_digest_mismatch_fails(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        client = FakeGitHub(self.repos())
        client.download = lambda asset: component_zip("other")
        report = self.run_manager(client)
        self.assertIn("digest", report["incomplete"][0]["reason"])

    def test_rewrite_exact_tags_flat(self):
        self.declare({"a": {"github": "o/a", "version": "^1.0.0"}})
        copy = self.tmp / "copy"
        __import__("shutil").copytree(self.pkg, copy)
        client = FakeGitHub(self.repos(), {("o/a", "1.2.0"): {"b": {"github": "o/b", "version": "21.0"}}})
        self.run_manager(client, "--rewrite", str(copy))
        deps = json.loads((copy / "Project/Sources/dependencies.json").read_text())["dependencies"]
        self.assertEqual(deps, {"a": {"github": "o/a", "tag": "1.2.0"}, "b": {"github": "o/b", "tag": "21.0"}})
        original = json.loads((self.pkg / "Project/Sources/dependencies.json").read_text())
        self.assertEqual(original["dependencies"]["a"]["version"], "^1.0.0")

    def test_gitlab_and_missing_repo(self):
        self.declare({"g": {"gitlab": "x/y"}, "m": {"github": "o/missing"}})
        report = self.run_manager(FakeGitHub(self.repos()))
        reasons = {i["name"]: i["reason"] for i in report["incomplete"]}
        self.assertIn("GitLab", reasons["g"])
        self.assertIn("token may be required", reasons["m"])


class Policy(unittest.TestCase):
    def test_hosted_runner_requires_tool4d(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted"}):
            with self.assertRaises(fd.UsageError):
                fd.enforce_runner_policy("4D")
            fd.enforce_runner_policy("tool4d")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
            fd.enforce_runner_policy("4D Server")

    def test_token_only_sent_to_api_and_no_anonymous_retry(self):
        client = fd.GitHub("secret", attempts=1)
        seen = []

        def fake_open(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            raise fd.urllib.error.HTTPError(request.full_url, 401, "x", {}, None)
        client.opener = mock.Mock(open=fake_open)
        with self.assertRaises(fd.Failure) as ctx:
            client.json("/repos/o/a/releases/latest")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertEqual(seen[0][1], "Bearer secret")


if __name__ == "__main__":
    unittest.main()
