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

    def test_windows_isolation_env_creates_profile_folders(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(fd, "platform_name", return_value="windows"):
            home = Path(directory)
            env = fd.isolation_env(home)
            self.assertEqual(env["USERPROFILE"], str(home))
            for key in ("LOCALAPPDATA", "APPDATA"):
                self.assertTrue(Path(env[key]).is_dir())

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

    def test_damaged_shared_cache_is_not_found_or_repaired(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        args = ["--archive-store", "none"]
        self.run_manager(FakeGitHub(self.repos()), *args)
        project = next((self.tmp / "home").rglob(".github/o/a/1.0.0/Project/*.4DProject"))
        project.unlink()
        client = FakeGitHub(self.repos())
        report = self.run_manager(client, *args)
        self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])
        self.assertEqual(client.calls, [])
        self.assertFalse(project.exists())
        item = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())["dependencies"]["a"]
        self.assertEqual((item["tag"], item["path"], item["found"]), ("1.0.0", "", False))
        self.assertTrue(item["htmlURL"])

    def test_failed_lookup_lock_has_no_tag(self):
        self.declare({"a": {"github": "o/missing", "tag": "1.0.0"}})
        self.run_manager(FakeGitHub(self.repos()))
        item = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())["dependencies"]["a"]
        self.assertNotIn("tag", item)
        self.assertEqual((item["htmlURL"], item["archiveURL"], item["path"], item["found"]), ("", "", "", False))

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

    # -- manifests (M8 gaps 1 and 2)

    def child_manifest_client(self, body):
        """Serve `body` as o/a's Project/Sources/dependencies.json through the real parser."""
        client = FakeGitHub(self.repos())
        client.child_dependencies = lambda repo, tag: fd.GitHub.child_dependencies(client, repo, tag)
        client._request = lambda url, accept: body
        return client

    def test_unparsable_child_manifest_marks_parent_incomplete(self):
        for body in (b"", b"{", b"\xff\xfe{"):
            with self.subTest(body=body):
                self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
                report = self.run_manager(self.child_manifest_client(body), "--no-write-lock")
                self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])
                self.assertIn("dependencies.json", report["incomplete"][0]["reason"])
                self.assertEqual(report["resolved"], {})

    def test_wrongly_shaped_child_manifest_marks_parent_incomplete(self):
        for body in (b"[]", b'{"dependencies": []}', b'{"dependencies": "x"}'):
            with self.subTest(body=body):
                self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
                report = self.run_manager(self.child_manifest_client(body), "--no-write-lock")
                self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])

    def test_child_manifest_without_dependencies_is_valid(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        report = self.run_manager(self.child_manifest_client(b"{}"), "--no-write-lock")
        self.assertEqual((report["resolved"], report["incomplete"]), ({"a": "1.0.0"}, []))

    def test_wrongly_shaped_manifest_is_a_usage_error(self):
        for text in ("[]", "[1]", '"x"', '{"dependencies": []}', '{"dependencies": null}', '{"dependencies": "o/a"}'):
            with self.subTest(text=text):
                (self.pkg / "Project/Sources/dependencies.json").write_text(text)
                with self.assertRaises(fd.UsageError):
                    self.run_manager(FakeGitHub(self.repos()))

    def test_wrongly_shaped_environment4d_is_a_usage_error(self):
        self.declare({})
        (self.pkg / "environment4d.json").write_text('{"dependencies": []}')
        with self.assertRaises(fd.UsageError):
            self.run_manager(FakeGitHub(self.repos()))

    def test_manifest_without_dependencies_is_valid(self):
        (self.pkg / "Project/Sources/dependencies.json").write_text("{}")
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual((report["dependencies"], report["incomplete"]), ({}, []))

    def test_non_object_declarations_are_incomplete_not_local(self):
        (self.pkg / "Project/Sources/dependencies.json").write_text(
            json.dumps({"dependencies": {"s": "o/a", "n": None, "l": [], "a": {"github": "o/a", "tag": "1.0.0"}}}))
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(sorted(i["name"] for i in report["incomplete"]), ["l", "n", "s"])
        self.assertEqual(report["resolved"], {"a": "1.0.0"})
        for name in ("s", "n", "l"):
            self.assertNotIn("source", report["dependencies"][name])

    def test_non_object_child_declaration_is_incomplete(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        client = FakeGitHub(self.repos(), {("o/a", "1.0.0"): {"b": "o/b"}})
        report = self.run_manager(client)
        self.assertEqual([i["name"] for i in report["incomplete"]], ["b"])
        self.assertNotIn("source", report["dependencies"]["b"])

    def test_environment4d_string_entry_is_still_a_path(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        (self.pkg / "environment4d.json").write_text(json.dumps({"dependencies": {"a": "../a"}}))
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual((report["dependencies"]["a"]["source"], report["dependencies"]["a"]["local"]), ("local", "../a"))
        self.assertEqual(report["incomplete"], [])

    # -- real-4D observations from the M9 probes (4D 21.2 build 100653, 4D 21R5 build 100156, macOS)

    def test_environment4d_only_declaration_is_ignored(self):
        # C4a-C4c: 4D neither fetches nor loads a name that only environment4d.json declares.
        for manifest in (None, {}):
            with self.subTest(manifest=manifest):
                path = self.pkg / "Project/Sources/dependencies.json"
                path.unlink(missing_ok=True)
                if manifest is not None:
                    self.declare(manifest)
                (self.tmp / "environment4d.json").write_text(
                    json.dumps({"dependencies": {"a": {"github": "o/a", "tag": "1.0.0"}}}))
                client = FakeGitHub(self.repos())
                report = self.run_manager(client)
                self.assertEqual((report["dependencies"], report["incomplete"]), ({}, []))
                self.assertIn("4D ignores it", report["warnings"][0])
                self.assertEqual(client.calls, [])

    def test_environment4d_overrides_only_declared_names(self):
        # C4d: the same file overrides a name that dependencies.json declares.
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        (self.pkg / "environment4d.json").write_text(json.dumps({"dependencies": {
            "a": {"github": "o/a", "tag": "1.2.0"}, "b": {"github": "o/b", "tag": "21.0"}}}))
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(report["resolved"], {"a": "1.2.0"})

    def test_children_of_components_overloaded_dependency_are_resolved(self):
        # C5: the overloaded dependency is installed (errCode 19) and its child is fetched and loaded.
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        (self.pkg / "Components/a.4dbase").mkdir(parents=True)
        client = FakeGitHub(self.repos(), {("o/a", "1.0.0"): {"b": {"github": "o/b", "tag": "21.0"}}})
        report = self.run_manager(client)
        self.assertEqual(report["resolved"], {"a": "1.0.0", "b": "21.0"})
        self.assertEqual(report["dependencies"]["a"]["errors"][0]["errCode"], 19)
        self.assertNotIn("overloadedBy", report["dependencies"]["b"])

    def conflict_client(self, first, second):
        repos = self.repos()
        repos["o/h"] = [("20.0.1", component_zip("h")), ("20.0.3", component_zip("h"))]
        return FakeGitHub(repos, {("o/a", "1.0.0"): {"h": dict(first, github="o/h")},
                                  ("o/a", "1.2.0"): {"h": dict(second, github="o/h")}})

    def declare_two_parents(self, reverse=False):
        names = [("x", "1.0.0"), ("y", "1.2.0")]
        self.declare({n: {"github": "o/a", "tag": t} for n, t in (reversed(names) if reverse else names)})

    def test_overlapping_secondary_ranges_resolve_to_their_intersection(self):
        # C6a/C6b: ^20.0.0 and <20.0.3 give 20.0.1 in either order; the lock version is the chosen tag.
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.declare_two_parents(reverse)
                report = self.run_manager(self.conflict_client({"version": "^20.0.0"}, {"version": "<20.0.3"}))
                self.assertEqual(report["resolved"]["h"], "20.0.1")
                self.assertEqual((report["incomplete"], report["warnings"]), ([], []))
                lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())
                self.assertEqual(lock["dependencies"]["h"]["version"], "20.0.1")

    def test_combined_secondary_ranges_reuse_a_satisfying_pin(self):
        self.declare_two_parents()
        self.run_manager(self.conflict_client({"version": "^20.0.0"}, {"version": "<20.0.3"}))
        client = self.conflict_client({"version": "^20.0.0"}, {"version": "<20.0.3"})
        client.offline = True
        report = self.run_manager(client, "--offline")
        self.assertEqual((report["resolved"]["h"], report["incomplete"]), ("20.0.1", []))

    def test_conflicting_secondary_tags_are_incomplete(self):
        # C6c: 4D reports a version conflict and loads neither component that needs the dependency.
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({"tag": "20.0.1"}, {"tag": "20.0.3"}))
        self.assertEqual([i["name"] for i in report["incomplete"]], ["h"])
        self.assertIn("conflicting tags", report["incomplete"][0]["reason"])

    def test_disjoint_secondary_ranges_are_incomplete(self):
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({"version": ">20.0.1"}, {"version": "<20.0.3"}))
        self.assertEqual([i["name"] for i in report["incomplete"]], ["h"])
        self.assertIn("version conflict", report["incomplete"][0]["reason"])

    def test_secondary_conflict_with_latest_is_explicitly_unverified(self):
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({}, {"version": "<20.0.3"}))
        self.assertEqual([i["name"] for i in report["incomplete"]], ["h"])
        self.assertIn("not been observed", report["incomplete"][0]["reason"])

    def test_identical_secondary_declarations_are_not_combined(self):
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({"version": "^20.0.0"}, {"version": "^20.0.0"}))
        self.assertEqual((report["resolved"]["h"], report["incomplete"]), ("20.0.3", []))
        self.assertNotIn("declarations", report["dependencies"]["h"])
        lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())
        self.assertEqual(lock["dependencies"]["h"]["version"], "^20.0.0")

    def test_compatible_latest_secondary_keeps_first_resolution_with_warning(self):
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({"version": "<20.0.3"}, {}))
        self.assertEqual((report["resolved"]["h"], report["incomplete"]), ("20.0.1", []))
        self.assertIn("not been observed", report["warnings"][0])

    def test_secondary_conflict_restarts_with_the_combined_tags_children(self):
        # Re-resolving a combined dependency can change its children: they come from the final tag only.
        self.declare_two_parents()
        client = self.conflict_client({"version": "^20.0.0"}, {"version": "<20.0.3"})
        client.repos["o/c"] = [("1.0.0", component_zip("c")), ("2.0.0", component_zip("c"))]
        client.children[("o/h", "20.0.3")] = {"c": {"github": "o/c", "tag": "2.0.0"}}
        client.children[("o/h", "20.0.1")] = {"c": {"github": "o/c", "tag": "1.0.0"}}
        report = self.run_manager(client)
        self.assertEqual((report["resolved"]["h"], report["resolved"]["c"]), ("20.0.1", "1.0.0"))

    # -- archive-store fallback after an online lookup failure (M8 gap 3)

    def store_archive(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        report = self.run_manager(FakeGitHub(self.repos()), "--no-write-lock")
        self.assertEqual(report["incomplete"], [])
        self.assertTrue((self.tmp / "store/github/o/a/1.0.0/a.zip").is_file())

    def http_error_client(self, code):
        client = FakeGitHub(self.repos(), token="secret")
        client.attempts = 1
        client.json = lambda path: fd.GitHub.json(client, path)

        def fake_open(request, timeout):
            raise fd.urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        client.opener = mock.Mock(open=fake_open)
        return client

    def test_rejected_token_does_not_fall_back_to_stored_archive(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.store_archive()
                report = self.run_manager(self.http_error_client(code), "--no-write-lock")
                self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])
                self.assertIn(f"HTTP {code}", report["incomplete"][0]["reason"])
                self.assertNotIn("secret", json.dumps(report))

    def test_deleted_release_falls_back_to_stored_archive_with_warning(self):
        self.store_archive()
        repos = self.repos()
        repos["o/a"] = [x for x in repos["o/a"] if x[0] != "1.0.0"]
        report = self.run_manager(FakeGitHub(repos), "--no-write-lock")
        self.assertEqual((report["resolved"], report["incomplete"]), ({"a": "1.0.0"}, []))
        self.assertEqual(report["dependencies"]["a"]["source"], "archive")
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("o/a@1.0.0", report["warnings"][0])
        self.assertIn("not found", report["warnings"][0])

    def test_outage_falls_back_to_stored_archive_with_warning(self):
        self.store_archive()
        client = self.http_error_client(503)
        with mock.patch.object(fd.time, "sleep"):
            report = self.run_manager(client, "--no-write-lock")
        self.assertEqual((report["resolved"], report["incomplete"]), ({"a": "1.0.0"}, []))
        self.assertEqual(report["dependencies"]["a"]["source"], "archive")
        self.assertIn("HTTP 503", report["warnings"][0])

    def test_rejected_token_without_stored_archive_is_incomplete(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        report = self.run_manager(self.http_error_client(401), "--no-write-lock")
        self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])


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
