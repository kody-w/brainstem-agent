"""H1/H2 (and H11/H12 bookkeeping) unit specs: the release manifest, ``version --json``, the
zipapp and the offline install into a fresh virtual environment.

The install spec builds a real venv and installs ``runtime/`` with pip offline
(``--no-index --no-build-isolation``, the venv's own setuptools); nothing is downloaded. It
skips, naming the setuptools it found, when that venv cannot build a wheel offline (setuptools
older than 70.1 and no ``wheel`` package, as with python.org's 3.11 installers): there the
zipapp is the offline path.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path

from acceptance_support import (CANARY_TOKEN, RUNTIME, cli_json, criteria, isolated_env,
                                leaks, private_dir, record_metric, run_cli, write_token_file)
from brainstem_agent import __version__, grail, release, state
from brainstem_agent.host import ALL_CAPABILITIES
from ops_support import copy_package, make_candidate, offline_wheel_problem

# A second interpreter for the zipapp spec when one is on PATH (the offline path for 3.12+).
OTHER_PYTHON = next((found for name in ("python3.13", "python3.12", "python3.11")
                     if (found := shutil.which(name)) and
                     os.path.realpath(found) != os.path.realpath(sys.executable)), None)


class ManifestTests(unittest.TestCase):
    @criteria("H2")
    def test_the_committed_release_manifest_is_produced_from_this_tree(self):
        committed = release.MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertEqual(committed, release.render_manifest(release.build_manifest()),
                         "run: PYTHONPATH=runtime python3.11 -m brainstem_agent.release --write")
        manifest = json.loads(committed)
        self.assertEqual(manifest["version"], __version__)
        self.assertEqual(manifest["grail"]["commit"], grail.PINNED_COMMIT)
        self.assertEqual(manifest["grail"]["kernel_sha256"], grail.KERNEL_SHA256)
        self.assertEqual(manifest["grail"]["inventory_files"], 30)
        self.assertEqual(manifest["store"]["reads"][0]["schema_sha256"], state.SCHEMA_DIGEST)
        self.assertEqual(sorted(manifest["capabilities"]), sorted(ALL_CAPABILITIES))
        self.assertIn("brainstem_agent/release.py", manifest["files"])
        self.assertIn("brainstem_agent/bridge/rapp_bridge_agent.py", manifest["files"])
        self.assertNotIn("brainstem_agent/data/release-manifest.json", manifest["files"])
        self.assertEqual(release.tree_sha256(manifest["files"]), manifest["tree_sha256"])
        self.assertTrue(release.verify_tree()["ok"], release.verify_tree()["problems"])

    @criteria("H2")
    def test_the_manifest_ships_every_package_data_file_the_wheel_ships(self):
        """pyproject's package data (bridge, data and the companion's ui assets) is what a
        release ships: a zipapp or an upgraded version without the page could not serve it."""
        manifest = release.build_manifest()
        text = (RUNTIME / "pyproject.toml").read_text()
        package = RUNTIME / "brainstem_agent"
        patterns = re.findall(r'"([^"]+)"', re.search(r"brainstem_agent = \[([^\]]*)\]",
                                                      text).group(1))
        self.assertIn("ui/*", patterns)
        shipped = [path for pattern in patterns for path in sorted(package.glob(pattern))
                   if path.is_file() and not path.name.startswith(".")
                   and path.name != release.MANIFEST_NAME]
        self.assertTrue(any(path.parent.name == "ui" for path in shipped))
        for path in shipped:
            self.assertIn(f"brainstem_agent/{path.relative_to(package).as_posix()}",
                          manifest["files"])

    @criteria("H2")
    def test_pyproject_and_package_agree_on_the_version(self):
        text = (RUNTIME / "pyproject.toml").read_text()
        self.assertIn(f'version = "{__version__}"', text)
        self.assertIn('dependencies = []', text)

    @criteria("H2")
    def test_verify_tree_names_changed_missing_and_unlisted_files(self):
        root = private_dir(self)
        package = copy_package(root)
        self.assertTrue(release.verify_tree(package)["ok"])
        (package / "policy.py").write_text("tampered = True\n")
        (package / "organs" / "memory.py").unlink()
        (package / "injected.py").write_text("print('hi')\n")
        (package / "__pycache__").mkdir(exist_ok=True)
        (package / "__pycache__" / "x.pyc").write_bytes(b"cache files are ignored")
        check = release.verify_tree(package)
        self.assertFalse(check["ok"])
        text = " ".join(check["problems"])
        self.assertIn("brainstem_agent/policy.py (sha256 differs)", text)
        self.assertIn("brainstem_agent/organs/memory.py (missing)", text)
        self.assertIn("brainstem_agent/injected.py (not in the manifest)", text)
        self.assertNotIn("pyc", text)

    @criteria("H2")
    def test_the_check_command_fails_on_a_stale_manifest(self):
        root = private_dir(self)
        package = copy_package(root)
        (package / "policy.py").write_text((package / "policy.py").read_text() + "\n# edit\n")
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(root), "HOME": str(root)}
        stale = subprocess.run([sys.executable, "-m", "brainstem_agent.release", "--check"],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(stale.returncode, 1)
        self.assertIn("--write", stale.stderr)
        subprocess.run([sys.executable, "-m", "brainstem_agent.release", "--write"], env=env,
                       check=True, capture_output=True, timeout=60)
        fresh = subprocess.run([sys.executable, "-m", "brainstem_agent.release", "--check"],
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(fresh.returncode, 0, fresh.stderr)


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.home = private_dir(self)
        self.scratch = private_dir(self)

    @criteria("H2")
    def test_version_json_names_every_identity_the_brief_asks_for(self):
        result = run_cli(["version", "--json"], isolated_env(self.home, self.scratch))
        self.assertEqual(result.returncode, 0, result.stderr)
        document = cli_json(result)
        self.assertEqual(document["product"], "Brainstem Agent")
        self.assertEqual(document["version"], __version__)
        self.assertRegex(document["version_id"], rf"^{re.escape(__version__)}-[0-9a-f]{{12}}$")
        self.assertEqual(document["grail"]["commit"], grail.PINNED_COMMIT)
        self.assertEqual(document["grail"]["version"], grail.VERSION)
        self.assertEqual(document["grail"]["kernel_sha256"], grail.KERNEL_SHA256)
        self.assertRegex(document["grail"]["inventory_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(document["store"]["schema_version"], 2)
        self.assertEqual(document["store"]["schema_sha256"], state.SCHEMA_DIGEST)
        self.assertEqual(len(document["store"]["migrates_from"]), 4)
        self.assertRegex(document["bridge_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(document["worker_lock_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("files.write", document["capabilities"])
        self.assertTrue(document["release_manifest"]["verified"], document["release_manifest"])
        self.assertEqual(document["release_manifest"]["tree_sha256"],
                         release.load_manifest()["tree_sha256"])
        self.assertEqual(document["store"]["home_store"], {"exists": False})
        self.assertFalse((self.home / "state").exists(), "version must not create a store")


class ZipappTests(unittest.TestCase):
    def setUp(self):
        self.root = private_dir(self)
        self.home = self.root / "home"
        self.scratch = private_dir(self)

    def run_app(self, app, *arguments, python=sys.executable, home=None):
        env = isolated_env(home or self.home, self.scratch)
        env.pop("PYTHONPATH")
        return subprocess.run([python, str(app), *arguments], capture_output=True, text=True,
                              env=env, timeout=120, cwd=str(self.scratch))

    @criteria("H1", "H2")
    def test_the_zipapp_is_deterministic_verifies_and_unpacks_itself(self):
        first = release.build_zipapp(self.root / "one.pyz")
        second = release.build_zipapp(self.root / "two.pyz")
        self.assertEqual(first["sha256"], second["sha256"], "the zipapp build is reproducible")
        pythons = [sys.executable] + ([OTHER_PYTHON] if OTHER_PYTHON else [])
        for python in pythons:
            with self.subTest(python=python):
                home = self.root / f"home-{Path(python).name}"
                result = self.run_app(first["path"], "version", "--json", python=python,
                                      home=home)
                self.assertEqual(result.returncode, 0, result.stderr[-800:])
                document = json.loads(result.stdout)
                self.assertEqual(document["version_id"], first["version_id"])
                self.assertTrue(document["release_manifest"]["verified"])
                self.assertEqual(document["install"]["kind"], "home-version")
                unpacked = home / "versions" / first["version_id"]
                self.assertTrue((unpacked / ".verified.json").is_file())
                self.assertTrue((unpacked / "brainstem_agent" / "cli.py").is_file())
                doctor = self.run_app(first["path"], "doctor", "--json", python=python,
                                      home=home)
                self.assertIn(doctor.returncode, (0, 1))
                self.assertIn("checks", json.loads(doctor.stdout))

    @criteria("H1")
    def test_a_damaged_zipapp_refuses_to_unpack(self):
        built = release.build_zipapp(self.root / "app.pyz")
        damaged = self.root / "damaged.pyz"
        with zipfile.ZipFile(built["path"]) as source, zipfile.ZipFile(damaged, "w") as target:
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "brainstem_agent/policy.py":
                    data += b"\n# changed\n"
                target.writestr(info, data)
        result = self.run_app(damaged, "version", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the release manifest", result.stderr + result.stdout)
        self.assertFalse(any((self.home / "versions").glob("0.*")))

    @criteria("H2")
    def test_a_stale_tree_cannot_be_packaged(self):
        root = private_dir(self)
        package = copy_package(root)
        (package / "policy.py").write_text("changed = 1\n")
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            release.build_zipapp(root / "app.pyz", root=package)
        self.assertFalse((root / "app.pyz").exists())


class OfflineInstallTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info >= (3, 12), "Python 3.12+ venvs carry no setuptools, so "
                     "the offline path there is the zipapp (ZipappTests)")
    @criteria("H1", "H12")
    def test_offline_pip_install_into_a_fresh_venv_gives_a_working_console_script(self):
        work = private_dir(self)
        source = work / "runtime"
        source.mkdir()
        shutil.copy(RUNTIME / "pyproject.toml", source)
        shutil.copy(RUNTIME / "README.md", source)
        copy_package(source)
        venv = work / "venv"
        transcript, started = [], time.monotonic()

        def step(*command, env=None, ok=(0,)):
            began = time.monotonic()
            result = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                    env=env, cwd=str(work))
            transcript.append({"command": " ".join(Path(part).name if "/" in part else part
                                                   for part in command),
                               "exit": result.returncode,
                               "seconds": round(time.monotonic() - began, 2)})
            self.assertIn(result.returncode, ok, result.stderr[-1500:] + result.stdout[-500:])
            return result

        step(sys.executable, "-m", "venv", str(venv))
        problem = offline_wheel_problem(venv / "bin" / "python")
        if problem:
            self.skipTest(problem)
        pip_env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "PIP_NO_INPUT": "1",
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        step(str(venv / "bin" / "pip"), "install", "--quiet", "--no-index",
             "--no-build-isolation", str(source), env=pip_env)
        install_seconds = round(time.monotonic() - started, 2)
        command = venv / "bin" / "brainstem-agent"
        self.assertTrue(command.is_file())
        home, scratch = private_dir(self), private_dir(self)
        token = write_token_file(scratch)
        env = {**isolated_env(home, scratch), "BRAINSTEM_AGENT_GITHUB_TOKEN_FILE": str(token)}
        env.pop("PYTHONPATH")
        version = json.loads(step(str(command), "version", "--json", env=env).stdout)
        self.assertEqual(version["install"]["kind"], "pip")
        self.assertTrue(version["release_manifest"]["verified"], version["release_manifest"])
        self.assertIn("site-packages", version["install"]["package"])
        doctor = json.loads(step(str(command), "doctor", "--json", env=env, ok=(0, 1)).stdout)
        self.assertTrue(doctor["checks"]["credential"]["ok"])
        self.assertTrue(doctor["checks"]["disk_space"]["ok"])
        self.assertEqual(doctor["checks"]["grail_source"]["fix"], "Run: brainstem-agent setup")
        self.assertEqual(leaks({"canary": CANARY_TOKEN}, roots=[home],
                               texts=[json.dumps(doctor)]), [])
        record_metric("h1_offline_install", {"python": sys.version.split()[0],
                                             "install_seconds": install_seconds,
                                             "steps": transcript})


class BookkeepingTests(unittest.TestCase):
    @criteria("H11")
    def test_the_earlier_milestones_suites_are_still_part_of_discovery(self):
        import run_acceptance

        loader = unittest.TestLoader()
        names = {type(test).__module__ for suite in loader.discover(str(RUNTIME / "tests"))
                 for group in suite for test in getattr(group, "_tests", [group])}
        for module in ("test_cell_host", "test_cell_daemon", "test_cell_learning",
                       "test_cell_longturn", "test_cell_reach", "test_state", "test_real_core",
                       "test_live"):
            self.assertIn(module, names)
        for module in run_acceptance.OPERABLE:
            self.assertTrue((RUNTIME / "tests" / f"{module}.py").is_file(), module)

    @criteria("H12")
    def test_the_evidence_runner_knows_the_operable_criteria(self):
        import run_acceptance

        for number in range(1, 13):
            title, required = run_acceptance.CRITERIA[f"H{number}"]
            self.assertTrue(title)
            self.assertIn(required, ("unit", "real-core", "live"))
        self.assertEqual(run_acceptance.CRITERIA["H12"][1], "live")
        needles = [CANARY_TOKEN]
        text = run_acceptance.redact(f"token {CANARY_TOKEN} and ghp_{'x' * 30}", needles)
        self.assertNotIn(CANARY_TOKEN, text)
        self.assertNotIn("x" * 30, text)
