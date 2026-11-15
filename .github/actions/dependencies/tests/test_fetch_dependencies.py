"""Offline unit tests for fetch_dependencies.py (fixtures model real 4D observations)."""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
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

    def test_gitlab_on_a_21x_runtime_and_missing_repo(self):
        # 4D 21.x (including tool4d 21.0) does not load GitLab components; no request is made for them.
        self.declare({"g": {"gitlab": "x/y"}, "m": {"github": "o/missing"}})
        report = self.run_manager(FakeGitHub(self.repos()), "--runtime-version", "21.2")
        reasons = {i["name"]: i["reason"] for i in report["incomplete"]}
        self.assertIn("does not load GitLab components", reasons["g"])
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

    # -- local components (miyako/4D#6; real 4D 21.2 build 100653, macOS: cases C6ctl, R1-R8)

    def local_component(self, folder, deps=None, name="L"):
        (folder / "Project" / "Sources").mkdir(parents=True, exist_ok=True)
        (folder / "Project" / f"{name}.4DProject").write_text("{}")
        if deps is not None:
            (folder / "Project/Sources/dependencies.json").write_text(json.dumps({"dependencies": deps}))
        return folder

    def lock(self):
        return json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())

    def test_github_children_of_a_local_component_are_resolved(self):
        # C6ctl: 4D reads the local component's dependencies.json and fetches its GitHub dependencies.
        local = self.local_component(self.tmp / "elsewhere/L.4dbase", {"b": {"github": "o/b", "tag": "21.0"}})
        self.declare({"L": {"path": str(local)}})
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(report["incomplete"], [])
        self.assertEqual(report["resolved"], {"b": "21.0"})
        self.assertEqual(report["dependencies"]["L"]["source"], "local")
        self.assertEqual(Path(report["dependencies"]["L"]["path"]), local)
        self.assertFalse(report["dependencies"]["b"]["primary"])
        lock = self.lock()["dependencies"]
        self.assertEqual(lock["L"], {"path": fd.native_path(local), "dependencies": {"b": {"github": "o/b", "tag": "21.0"}},
                                     "loaded": None, "found": True})
        self.assertEqual(lock["b"]["tag"], "21.0")

    def test_relative_local_paths_resolve_from_the_package_parent_folder(self):
        # R1/R6: a relative path in dependencies.json resolves from the folder that contains the project package,
        # not from the package, its Project or Sources folder, or the working directory.
        self.local_component(self.tmp / "libs/L.4dbase", {"b": {"github": "o/b", "tag": "21.0"}})
        for decoy in (self.pkg, self.pkg / "Project", self.pkg / "Project/Sources"):
            self.local_component(decoy / "libs/L.4dbase", {"a": {"github": "o/a", "tag": "1.0.0"}})
        self.declare({"L": {"path": "libs/L.4dbase"}})
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(report["resolved"], {"b": "21.0"})
        self.assertEqual(Path(report["dependencies"]["L"]["path"]).resolve(), (self.tmp / "libs/L.4dbase").resolve())

    def test_paths_declared_by_a_local_component_resolve_from_the_host_package_parent(self):
        # R8/R9: also for a local component's own declarations, not from the component's location, and
        # recursively (local -> local -> GitHub).
        local = self.local_component(self.tmp / "deep/L.4dbase", {"M": {"path": "M.4dbase"}, "N": {"path": "N.4dbase"},
                                                                  "Z": {"path": "Z.4dbase"}})
        self.local_component(self.tmp / "M.4dbase", {"b": {"github": "o/b", "tag": "21.0"}}, "M")
        self.local_component(self.tmp / "deep/M.4dbase", {"a": {"github": "o/a", "tag": "1.0.0"}}, "M")
        self.local_component(self.tmp / "N.4dbase", None, "N")
        self.declare({"L": {"path": str(local)}})
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(report["resolved"], {"b": "21.0"})
        self.assertEqual(report["incomplete"], [])
        lock = self.lock()["dependencies"]
        # 4D's nested form: a local child that declares dependencies keeps its declared path and gains them (R9);
        # a leaf, or a missing one, is written with its resolved state (R8, R3).
        self.assertEqual(lock["L"]["dependencies"], {
            "M": {"path": "M.4dbase", "dependencies": {"b": {"github": "o/b", "tag": "21.0"}}},
            "N": {"path": fd.native_path((self.tmp / "N.4dbase").resolve()), "loaded": None, "found": True},
            "Z": {"path": "", "loaded": None, "found": False}})
        self.assertEqual(lock["M"]["dependencies"], {"b": {"github": "o/b", "tag": "21.0"}})

    def test_environment4d_relative_paths_resolve_from_its_folder(self):
        # R2 (parent folder) and R7 (package folder).
        for where in (self.pkg, self.tmp):
            with self.subTest(where=where):
                env = where / "environment4d.json"
                self.local_component(where / "env/L.4dbase", {"b": {"github": "o/b", "tag": "21.0"}})
                decoy = (self.tmp if where == self.pkg else self.pkg) / "env/L.4dbase"
                self.local_component(decoy, {"a": {"github": "o/a", "tag": "1.0.0"}})
                self.declare({"L": {"path": "/nonexistent/L.4dbase"}})
                env.write_text(json.dumps({"dependencies": {"L": "env/L.4dbase"}}))
                report = self.run_manager(FakeGitHub(self.repos()))
                self.assertEqual(report["resolved"], {"b": "21.0"})
                env.unlink()
                __import__("shutil").rmtree(where / "env")
                __import__("shutil").rmtree(decoy.parent)

    def test_dependency_cycles_load_every_component_once(self):
        # Y1-Y4: 4D resolves cycles (X -> Y -> X, X -> X, X -> Y -> Z -> X) without error, loads each component
        # once, and still resolves a GitHub child declared inside the cycle (Y3).
        cycles = {
            "Y1": {"X": {"Y": {}}, "Y": {"X": {}}},
            "Y2": {"X": {"X": {}}},
            "Y3": {"X": {"Y": {}}, "Y": {"X": {}, "b": {"github": "o/b", "tag": "21.0"}}},
            "Y4": {"X": {"Y": {}}, "Y": {"Z": {}}, "Z": {"X": {}}},
        }
        for case, graph in cycles.items():
            with self.subTest(case=case):
                shutil.rmtree(self.tmp / "libs", ignore_errors=True)
                (self.pkg / "userPreferences.u/dependencies-lock.json").unlink(missing_ok=True)
                for name, deps in graph.items():
                    self.local_component(self.tmp / f"libs/{name}.4dbase", {
                        child: spec or {"path": f"libs/{child}.4dbase"} for child, spec in deps.items()}, name)
                self.declare({"X": {"path": "libs/X.4dbase"}})
                report = self.run_manager(FakeGitHub(self.repos()))
                self.assertEqual((report["incomplete"], report["warnings"]), ([], []))
                self.assertEqual(report["resolved"], {"b": "21.0"} if case == "Y3" else {})
                lock = self.lock()["dependencies"]
                self.assertEqual(sorted(lock), sorted(graph) + (["b"] if case == "Y3" else []))
                self.assertTrue(all(lock[name]["found"] for name in graph))

    def test_missing_local_component_is_not_found_but_left_to_the_caller(self):
        # R5: 4D writes path "" and found false. Local components stay the caller's responsibility.
        self.declare({"L": {"path": "missing/L.4dbase"}})
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual(report["incomplete"], [])
        self.assertIsNone(report["dependencies"]["L"].get("path"))
        self.assertEqual(self.lock()["dependencies"]["L"], {"path": "", "loaded": None, "found": False})
        self.assertTrue(any("L: local component not found" in w for w in report["warnings"]))

    def test_unreadable_local_manifest_makes_the_local_component_incomplete(self):
        local = self.local_component(self.tmp / "L.4dbase")
        (local / "Project/Sources/dependencies.json").write_text("{")
        self.declare({"L": {"path": "L.4dbase"}})
        report = self.run_manager(FakeGitHub(self.repos()))
        self.assertEqual([i["name"] for i in report["incomplete"]], ["L"])
        self.assertIn("cannot parse", report["incomplete"][0]["reason"])

    def test_local_path_declared_by_a_github_component_is_not_resolved(self):
        # Not observed in 4D: left unresolved, as before, with a warning.
        self.local_component(self.tmp / "c.4dbase", {"b": {"github": "o/b", "tag": "21.0"}}, "c")
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        client = FakeGitHub(self.repos(), {("o/a", "1.0.0"): {"c": {"path": "c.4dbase"}}})
        report = self.run_manager(client)
        self.assertEqual(report["resolved"], {"a": "1.0.0"})
        self.assertNotIn("path", report["dependencies"]["c"])
        self.assertTrue(any("c: local path declared by o/a@1.0.0" in w for w in report["warnings"]))

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

    # Real 4D 21.2 cases, mapped onto o/h (20.0.1, 20.0.3; runtime 20.2, so "4d" is 4D:20 and latest is
    # 20.0.3): (first, second, tag, lock version, conflict). C6c, T3, T4, V1, V2 and A1 conflict: the dependency
    # is still loaded as first declared, but the components that declare it are not.
    COMBINATIONS = {
        "C6c": ({"tag": "20.0.1"}, {"tag": "20.0.3"}, "20.0.1", None, True),
        "L1": ({"version": "latest"}, {"version": "^20.0.0"}, "20.0.3", "20.0.3", False),
        "L2": ({"version": "latest"}, {"tag": "20.0.1"}, "20.0.3", "latest", False),
        "L3": ({"tag": "20.0.1"}, {"version": "latest"}, "20.0.1", None, False),
        "L4": ({"version": "latest"}, {"version": "<20.0.3"}, "20.0.3", "latest", False),
        "L5": ({"version": "<20.0.3"}, {}, "20.0.1", "<20.0.3", False),
        "F1": ({"version": "4d"}, {"version": "^20.0.0"}, "20.0.3", "20.0.3", False),
        "F2": ({"version": "4d"}, {"tag": "20.0.1"}, "20.0.3", "4D:20", False),
        "F3": ({"version": "4d"}, {"version": "latest"}, "20.0.3", "20.0.3", False),
        "F4": ({"tag": "20.0.1"}, {"version": "4d"}, "20.0.1", None, False),
        "F5": ({"version": "4d"}, {"version": "<20.0.3"}, "20.0.1", "20.0.1", False),
        "F6": ({"version": "<20.0.3"}, {"version": "4d"}, "20.0.1", "20.0.1", False),
        "T1": ({"tag": "20.0.1"}, {"version": "^20.0.0"}, "20.0.1", None, False),
        "T2": ({"version": "^20.0.0"}, {"tag": "20.0.1"}, "20.0.3", "^20.0.0", False),
        "T3": ({"tag": "20.0.1"}, {"version": ">=20.0.3"}, "20.0.1", None, True),
        "T4": ({"version": ">=20.0.3"}, {"tag": "20.0.1"}, "20.0.3", ">=20.0.3", True),
        "V1": ({"version": ">=20.0.3"}, {"version": "<20.0.3"}, "20.0.3", ">=20.0.3", True),
        "V2": ({"version": "<20.0.3"}, {"version": ">=20.0.3"}, "20.0.1", "<20.0.3", True),
        "E1": ({"version": "^20.0.0"}, {"version": "20.0.1"}, "20.0.1", "20.0.1", False),
        "A1": ({"version": "4d"}, {"version": ">=21.0.0"}, "20.0.3", "4D:20", True),
        "A2": ({"version": "4d"}, {"tag": "19.0"}, "20.0.3", "4D:20", False),
    }

    def test_differing_secondary_declarations_follow_4d(self):
        for case, (first, second, tag, version, conflict) in self.COMBINATIONS.items():
            with self.subTest(case=case):
                for state in (self.pkg / "userPreferences.u", self.tmp / "home", self.tmp / "store"):
                    shutil.rmtree(state, ignore_errors=True)  # child manifests are stored with each archive
                self.declare_two_parents()
                report = self.run_manager(self.conflict_client(first, second), "--runtime-version", "20.2")
                h = report["dependencies"]["h"]
                self.assertEqual((h["tag"], h.get("version")), (tag, version))
                self.assertNotIn("error", h)
                self.assertEqual([i["name"] for i in report["incomplete"]], ["x", "y"] if conflict else [])
                self.assertEqual(report["warnings"], [])
                lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())
                self.assertEqual((lock["dependencies"]["h"]["tag"], lock["dependencies"]["h"].get("version")),
                                 (tag, version))
                self.assertTrue(lock["dependencies"]["h"]["found"])
                self.assertEqual("errors" in lock["dependencies"]["h"], conflict)

    def test_version_conflict_disables_the_declaring_components_as_4d_writes_it(self):
        # C6c/T3: the dependency is found and loaded with the conflict in `errors`; each local component that
        # declares it is found but not loaded, with errCode 21.
        for comp in ("X", "Y"):
            folder = self.tmp / f"libs/Comp{comp}.4dbase"
            (folder / "Project/Sources").mkdir(parents=True)
            (folder / f"Project/Comp{comp}.4DProject").write_text("{}")
            tag = "20.0.1" if comp == "X" else "20.0.3"
            (folder / "Project/Sources/dependencies.json").write_text(
                json.dumps({"dependencies": {"h": {"github": "o/h", "tag": tag}}}))
        self.declare({"CompX": {"path": "libs/CompX.4dbase"}, "CompY": {"path": "libs/CompY.4dbase"}})
        report = self.run_manager(self.conflict_client({}, {}))
        self.assertEqual(report["resolved"], {"h": "20.0.1"})
        self.assertEqual([i["name"] for i in report["incomplete"]], ["CompX", "CompY"])
        self.assertIn("disabled by a version conflict", report["incomplete"][0]["reason"])
        lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())["dependencies"]
        self.assertTrue(lock["h"]["found"])
        self.assertIn("incompatible", lock["h"]["errors"][0]["message"])
        for comp in ("CompX", "CompY"):
            self.assertEqual((lock[comp]["loaded"], lock[comp]["found"], lock[comp]["errors"][0]["errCode"]),
                             (False, True, 21))

    # Real 4D 21.2 cases G1-G3 and M1-M5, mapped onto o/h as above: local components CompX, CompY (and CompZ)
    # declare h in this order. R overlaps ^20.0.0 in theory, but no release is in it. 4D settles them one at a
    # time against the current state: (declarations, tag, lock version, conflict).
    R = ">=20.0.4 <20.1.0"
    SEQUENCES = {
        "G1": ([{"version": "^20.0.0"}, {"version": R}], "20.0.3", "^20.0.0", True),
        "G2": ([{"version": "4d"}, {"version": R}], "20.0.3", "4D:20", True),
        "G3": ([{"tag": "20.0.1"}, {"version": R}], "20.0.1", None, True),
        "M1": ([{"version": "^20.0.0"}, {"version": "<20.0.3"}, {"version": "latest"}], "20.0.1", "20.0.1", False),
        "M2": ([{"tag": "20.0.1"}, {"version": "^20.0.0"}, {"version": ">=20.0.3"}], "20.0.1", None, True),
        "M3": ([{"version": "latest"}, {"version": "^20.0.0"}, {"version": "<20.0.3"}], "20.0.3", None, True),
        "M4": ([{"version": "^20.0.0"}, {"version": ">=20.0.3"}, {"version": "<20.0.3"}], "20.0.3", None, True),
        "M5": ([{"version": "^20.0.0"}, {"tag": "20.0.1"}, {"version": ">=20.0.3"}], "20.0.3", "20.0.3", False),
    }

    def test_secondary_declarations_are_settled_one_at_a_time(self):
        for case, (specs, tag, version, conflict) in self.SEQUENCES.items():
            with self.subTest(case=case):
                for state in (self.pkg / "userPreferences.u", self.tmp / "home", self.tmp / "store", self.tmp / "libs"):
                    shutil.rmtree(state, ignore_errors=True)
                comps = [f"Comp{c}" for c in "XYZ"[:len(specs)]]
                for comp, spec in zip(comps, specs):
                    folder = self.tmp / f"libs/{comp}.4dbase"
                    (folder / "Project/Sources").mkdir(parents=True)
                    (folder / f"Project/{comp}.4DProject").write_text("{}")
                    (folder / "Project/Sources/dependencies.json").write_text(
                        json.dumps({"dependencies": {"h": {**spec, "github": "o/h"}}}))
                self.declare({comp: {"path": f"libs/{comp}.4dbase"} for comp in comps})
                report = self.run_manager(self.conflict_client({}, {}), "--runtime-version", "20.2")
                h = report["dependencies"]["h"]
                self.assertEqual((h["tag"], h.get("version")), (tag, version))
                self.assertEqual(sorted(i["name"] for i in report["incomplete"]), comps if conflict else [])
                self.assertEqual(report["warnings"], [])
                lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())["dependencies"]
                self.assertEqual((lock["h"]["tag"], lock["h"].get("version")), (tag, version))
                self.assertEqual([lock[c]["loaded"] for c in comps], [False if conflict else None] * len(comps))

    def test_identical_secondary_declarations_are_not_combined(self):
        self.declare_two_parents()
        report = self.run_manager(self.conflict_client({"version": "^20.0.0"}, {"version": "^20.0.0"}))
        self.assertEqual((report["resolved"]["h"], report["incomplete"]), ("20.0.3", []))
        self.assertNotIn("declarations", report["dependencies"]["h"])
        lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())
        self.assertEqual(lock["dependencies"]["h"]["version"], "^20.0.0")

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

    def test_a_long_retry_after_fails_at_once(self):
        client = fd.GitLab("https://gitlab.com")
        client.attempts = 4

        def fake_open(request, timeout):
            raise fd.urllib.error.HTTPError(request.full_url, 429, "x", {"Retry-After": "911"}, None)
        client.opener = mock.Mock(open=fake_open)
        with mock.patch.object(fd.time, "sleep") as sleep:
            with self.assertRaises(fd.Failure) as raised:
                client.json("https://gitlab.com/api/v4/projects/x/releases")
        sleep.assert_not_called()
        self.assertIn("rate limit (HTTP 429)", str(raised.exception))
        self.assertIn("token", str(raised.exception))

    def test_rejected_token_without_stored_archive_is_incomplete(self):
        self.declare({"a": {"github": "o/a", "tag": "1.0.0"}})
        report = self.run_manager(self.http_error_client(401), "--no-write-lock")
        self.assertEqual([i["name"] for i in report["incomplete"]], ["a"])


class FakeGitLab(fd.GitLab):
    """Serves GitLab releases from memory: (tag, released order, [(link name, url)]); downloads by URL."""

    def __init__(self, host="https://gitlab.com", releases=None, files=None, children=None):
        super().__init__(host)
        self.data = releases or []  # newest first, as the API lists them
        self.files = files or {}  # url -> bytes
        self.children = children or {}
        self.calls = []

    def _release(self, repo, tag, links):
        return {"tag_name": tag, "upcoming_release": False, "assets": {"links": [
            {"id": i, "name": name, "url": url, "direct_asset_url": url} for i, (name, url) in enumerate(links)]}}

    def json(self, url):
        self.calls.append(url)
        repo = urllib.parse.unquote(url.split("/projects/")[1].split("/")[0])
        if "/releases/permalink/latest" in url:
            tag, links = self.data[0]
            return self._release(repo, tag, links)
        if "/releases/" in url:
            tag = urllib.parse.unquote(url.rsplit("/", 1)[1])
            for t, links in self.data:
                if t == tag:
                    return self._release(repo, t, links)
            raise LookupError(url)
        return [self._release(repo, t, links) for t, links in self.data] if "page=1" in url else []

    def child_dependencies(self, repo, tag):
        return self.children.get((repo, tag))

    def download(self, asset):
        self.calls.append("download " + asset["url"])
        return self.files[asset["url"]]


class GitLabSupport(Base):
    # Mirrors the real-4D cases GL1-GL8 and H1-H7 (4D 21 R5, miyako-group/wavplay).
    A, B = "https://example.org/a/wavplay.zip", "https://example.org/b/wavplay.zip"

    def gitlab(self, host="https://gitlab.com", releases=None):
        files = {self.A: component_zip("wavplay"), self.B: component_zip("wavplay")}
        # newest first: 20.1.0 was released last, 20.2.0 is the highest and has two links
        releases = releases or [("20.1.0", [("wavplay.zip", self.A)]),
                                ("20.2.0", [("wavplay.zip", self.A), ("component.zip", self.B)]),
                                ("20.0.0", [("wavplay-21.0.3.zip", self.A)])]
        made = {}

        def factory(base):
            made[base] = FakeGitLab(base, releases, files)
            return made[base]
        return factory, made

    def run_gitlab(self, spec, version="21R5", releases=None, *extra):
        self.declare({"wavplay": {"gitlab": "miyako-group/wavplay", **spec}})
        factory, made = self.gitlab(releases=releases)
        argv = ["--project", str(self.pkg), "--user", "u", "--home", str(self.tmp / "home"),
                "--archive-store", str(self.tmp / "store"), "--runtime", "4D", *extra]
        if version:
            argv += ["--runtime-version", version]
        args = fd.parser().parse_args(argv)
        args.runtime = fd.RUNTIMES[args.runtime.lower()]
        report = fd.Manager(args, FakeGitHub(Manager.repos(self)), gitlab=factory).run()
        lock = json.loads((self.pkg / "userPreferences.u/dependencies-lock.json").read_text())["dependencies"]
        return report, lock["wavplay"], made

    def test_versions_resolve_as_in_4d(self):
        cases = {  # H1-H7
            "H1": ({}, "20.2.0", "highest"), "H2": ({"version": "highest"}, "20.2.0", "highest"),
            "H3": ({"version": "latest"}, "20.1.0", "latest"), "H4": ({"version": "^20.0.0"}, "20.2.0", "^20.0.0"),
            "H5": ({"tag": "20.2.0"}, "20.2.0", None), "H6": ({"version": "4d"}, "20.2.0", "4D:21R5"),
            "H7": ({"version": "20.1.0"}, "20.1.0", "20.1.0"),
        }
        for case, (spec, tag, version) in cases.items():
            with self.subTest(case=case):
                for state in (self.pkg / "userPreferences.u", self.tmp / "home", self.tmp / "store"):
                    shutil.rmtree(state, ignore_errors=True)
                report, item, _ = self.run_gitlab(spec)
                self.assertEqual(report["incomplete"], [])
                self.assertEqual((item["tag"], item.get("version")), (tag, version))
                self.assertEqual(item["archiveURL"], self.A)

    def test_cache_metadata_and_lock_are_written_as_4d_writes_them(self):
        report, item, _ = self.run_gitlab({"tag": "20.0.0"})  # GL1
        folder = Path(report["dependenciesFolder"]) / ".gitlab/miyako-group/wavplay"
        self.assertTrue((folder / "20.0.0/Project/wavplay.4DProject").is_file())
        meta = json.loads((folder / "20.0.0.json").read_text())
        self.assertEqual(list(meta), ["tag", "htmlURL", "archiveURL", "archiveSize", "dependencies", "gitlab", "host"])
        self.assertEqual((meta["htmlURL"], meta["host"], meta["dependencies"]),
                         ("/miyako-group/wavplay/-/tags/20.0.0", None, None))
        self.assertEqual(set(item), {"gitlab", "tag", "htmlURL", "archiveURL", "path", "found", "loaded"})
        self.assertTrue(item["found"])

    def test_a_declared_host_is_kept_and_named_in_the_cache_path(self):
        report, item, made = self.run_gitlab({"host": "https://gitlab.com", "tag": "20.0.0"})  # GL8
        self.assertEqual(item["host"], "https://gitlab.com")
        self.assertTrue((Path(report["dependenciesFolder"]) / ".gitlab/gitlab.com/miyako-group/wavplay/20.0.0").is_dir())
        self.assertEqual(list(made), ["https://gitlab.com"])

    def test_a_missing_release_links_to_the_releases_page(self):
        report, item, _ = self.run_gitlab({"tag": "9.9.9"})  # GL7
        self.assertEqual([i["name"] for i in report["incomplete"]], ["wavplay"])
        self.assertEqual((item["htmlURL"], item["archiveURL"], item["found"]),
                         ("https://gitlab.com/miyako-group/wavplay/-/releases/", "", False))
        self.assertNotIn("tag", item)

    def test_a_gitlab_components_own_dependencies_are_not_followed(self):
        # M1t, M2t: 4D 21 R5 installs neither child, whether the manifest is at the root or in a subfolder.
        client = fd.GitLab("https://gitlab.com")
        client._request = mock.Mock(side_effect=AssertionError("no request expected"))
        self.assertIsNone(client.child_dependencies("miyako-group/wavplay", "20.0.7"))

    def test_with_several_links_the_first_listed_is_used(self):
        # K1t: 20.0.5 lists component.zip (added last) before wavplay.zip; 4D used component.zip.
        releases = [("20.0.5", [("component.zip", self.B), ("wavplay.zip", self.A)])]
        report, item, _ = self.run_gitlab({"tag": "20.0.5"}, "21R5", releases)
        self.assertEqual((report["incomplete"], item["archiveURL"]), ([], self.B))

    def test_the_gitlab_token_goes_to_the_gitlab_host_only(self):
        client = fd.GitLab("https://gitlab.com", token="secret")
        self.assertEqual(client.headers("https://gitlab.com/api/v4/projects/x", "application/json")["PRIVATE-TOKEN"],
                         "secret")
        self.assertNotIn("PRIVATE-TOKEN", client.headers("https://github.com/o/r/releases/download/1/r.zip", "*/*"))
        self.assertNotIn("PRIVATE-TOKEN", client.headers("https://gitlab.com.evil.example/x", "*/*"))
        # gitlab.json holds one token and no host: a self-hosted host does not receive it.
        args = fd.parser().parse_args(["--project", str(self.pkg), "--user", "u"])
        args.runtime = fd.RUNTIMES[args.runtime.lower()]
        manager = fd.Manager(args, FakeGitHub(Manager.repos(self)), gitlab_token="secret")
        self.assertEqual(manager.gitlab_factory("https://gitlab.com").token, "secret")
        self.assertIsNone(manager.gitlab_factory("https://gitlab.example.com").token)

    def test_the_gitlab_token_file_is_read_like_4ds_gitlab_json(self):
        token_file = self.tmp / "gitlab.json"
        token_file.write_text(json.dumps({"token": "secret"}))
        args = fd.parser().parse_args(["--project", str(self.pkg), "--gitlab-token-file", str(token_file)])
        self.assertEqual(fd.read_gitlab_token(args), "secret")
        with mock.patch.dict(os.environ, {"DEPENDENCIES_GITLAB_TOKEN": "from-env"}):
            args = fd.parser().parse_args(["--project", str(self.pkg)])
            self.assertEqual(fd.read_gitlab_token(args), "from-env")

    def test_gitlab_needs_4d_21_r3_or_later(self):
        for version, ok in (("21.2", False), ("21.1", False), ("21R3", True), ("21R5", True), ("20.8", False)):
            with self.subTest(version=version):
                for state in (self.pkg / "userPreferences.u", self.tmp / "home", self.tmp / "store"):
                    shutil.rmtree(state, ignore_errors=True)
                report, item, made = self.run_gitlab({"tag": "20.0.0"}, version)
                self.assertEqual(report["incomplete"] == [], ok)
                if not ok:
                    self.assertEqual(made, {})  # no request is made
                    self.assertFalse(item["found"])
        report, _, _ = self.run_gitlab({"tag": "20.0.0"}, None)
        self.assertEqual(report["incomplete"], [])
        self.assertIn("21 R3", report["warnings"][0])

    def test_pins_and_offline_runs_use_the_archive_store(self):
        self.run_gitlab({})
        self.declare({"wavplay": {"gitlab": "miyako-group/wavplay"}})
        args = fd.parser().parse_args(["--project", str(self.pkg), "--user", "u", "--home", str(self.tmp / "home2"),
                                       "--archive-store", str(self.tmp / "store"), "--runtime", "4D",
                                       "--runtime-version", "21R5", "--offline"])
        args.runtime = fd.RUNTIMES[args.runtime.lower()]
        offline = FakeGitLab()
        offline.offline = True
        report = fd.Manager(args, FakeGitHub(Manager.repos(self), offline=True), gitlab=lambda base: offline).run()
        self.assertEqual((report["resolved"], report["incomplete"]), ({"wavplay": "20.2.0"}, []))
        self.assertEqual(report["dependencies"]["wavplay"]["source"], "archive")
        self.assertFalse([c for c in offline.calls if c.startswith("download")])


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
