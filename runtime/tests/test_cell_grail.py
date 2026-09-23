"""A1/A7 unit specs: the captured Grail genome is pinned and verified byte for byte."""

import hashlib
import io
import os
import shutil
import tarfile
import unittest
from pathlib import Path

from acceptance_support import (HAVE_SEED, KERNEL_SHA256, NO_SEED, PINNED_COMMIT, SEED, criteria,
                                find_seed, private_dir)
from brainstem_agent import grail
from brainstem_agent.grail import GrailSourceError



def copy_seed(destination: Path) -> Path:
    target = destination / "rapp_brainstem"
    shutil.copytree(SEED, target)
    for root, _dirs, files in os.walk(target):
        for name in files:
            os.chmod(os.path.join(root, name), 0o644)
    return target


def tarball(members: list[tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member in members:
            if isinstance(member, tuple):
                info, data = member
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(member)
    return buffer.getvalue()


def seed_members(prefix=f"rapp-installer-{PINNED_COMMIT}/rapp_brainstem/"):
    members = []
    for relative in sorted(grail.load_inventory()):
        data = (SEED / relative).read_bytes()
        info = tarfile.TarInfo(prefix + relative)
        info.size = len(data)
        info.mode = 0o644
        members.append((info, data))
    return members


class InventoryTests(unittest.TestCase):
    @criteria("A1", "A7")
    def test_inventory_pins_thirty_files_and_the_kernel(self):
        inventory = grail.load_inventory()
        self.assertEqual(len(inventory), 30)
        self.assertEqual(inventory["brainstem.py"], KERNEL_SHA256)
        self.assertEqual(grail.KERNEL_SHA256, KERNEL_SHA256)
        self.assertEqual(grail.PINNED_COMMIT, PINNED_COMMIT)
        self.assertEqual(grail.VERSION, "0.6.16")
        self.assertIn("agents/basic_agent.py", inventory)
        self.assertTrue(all(len(value) == 64 for value in inventory.values()))

    @criteria("A7")
    def test_repository_never_vendors_grail_source(self):
        runtime = Path(grail.__file__).resolve().parent
        self.assertFalse(list(runtime.rglob("brainstem.py")))
        self.assertFalse(list(runtime.rglob("local_storage.py")))


class SeedDiscoveryTests(unittest.TestCase):
    """Tests find the verified seed from the environment or the cell's own cache, never
    from a path of one machine."""

    def layout(self, cache: Path) -> Path:
        root = cache / "grail" / PINNED_COMMIT / "rapp_brainstem"
        root.mkdir(parents=True)
        (root / "brainstem.py").write_text("# stand-in\n")
        return root

    @criteria("A11")
    def test_the_seed_comes_from_the_environment_or_the_cells_verified_cache(self):
        scratch = private_dir(self)
        empty = {"BRAINSTEM_AGENT_HOME": str(scratch / "empty-home")}
        self.assertIsNone(find_seed(empty))
        self.assertEqual(find_seed({**empty, "BRAINSTEM_AGENT_GRAIL_SEED": str(scratch / "s")}),
                         scratch / "s")
        from_home = self.layout(scratch / "home" / "cache")
        self.assertEqual(find_seed({"BRAINSTEM_AGENT_HOME": str(scratch / "home")}), from_home)
        from_cache = self.layout(scratch / "cache")
        self.assertEqual(find_seed({"BRAINSTEM_AGENT_HOME": str(scratch / "home"),
                                    "BRAINSTEM_AGENT_CACHE": str(scratch / "cache")}), from_cache)
        from_tests = self.layout(scratch / "test-cache")
        self.assertEqual(find_seed({"BRAINSTEM_AGENT_CACHE": str(scratch / "cache"),
                                    "BRAINSTEM_AGENT_TEST_CACHE": str(scratch / "test-cache")}),
                         from_tests)


@unittest.skipUnless(HAVE_SEED, NO_SEED)
class TreeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.inventory = grail.load_inventory()

    @criteria("A7")
    def test_verify_tree_lists_untracked_and_refuses_changes(self):
        root = copy_seed(self.dir)
        self.assertEqual(grail.verify_tree(root, self.inventory, allow_extra=True), [])
        (root / ".env").write_text("")
        (root / ".brainstem_data").mkdir()
        (root / ".brainstem_data" / "x.json").write_text("{}")
        self.assertEqual(
            grail.verify_tree(root, self.inventory, allow_extra=True),
            [".brainstem_data/x.json", ".env"],
        )
        with self.assertRaises(GrailSourceError):
            grail.verify_tree(root, self.inventory, allow_extra=False)
        (root / "brainstem.py").write_bytes((root / "brainstem.py").read_bytes() + b"\n#")
        with self.assertRaises(GrailSourceError) as caught:
            grail.verify_tree(root, self.inventory, allow_extra=True)
        self.assertIn("brainstem.py", str(caught.exception))

    @criteria("A7")
    def test_missing_or_replaced_tracked_file_is_refused(self):
        root = copy_seed(self.dir)
        (root / "soul.md").unlink()
        with self.assertRaises(GrailSourceError):
            grail.verify_tree(root, self.inventory, allow_extra=True)
        root2 = copy_seed(self.dir / "second")
        (root2 / "VERSION").unlink()
        (root2 / "VERSION").symlink_to(root2 / "requirements.txt")
        with self.assertRaises(GrailSourceError):
            grail.verify_tree(root2, self.inventory, allow_extra=True)

    @criteria("A1", "A7")
    def test_cache_from_seed_is_verified_and_read_only(self):
        cache = self.dir / "cache"
        source = grail.ensure_grail_source(cache, seed_dir=SEED, fetch=False)
        self.assertEqual(source.commit, PINNED_COMMIT)
        self.assertTrue(str(source.root).startswith(str(cache)))
        self.assertEqual(dict(source.inventory), self.inventory)
        kernel = source.root / "brainstem.py"
        self.assertEqual(hashlib.sha256(kernel.read_bytes()).hexdigest(), KERNEL_SHA256)
        self.assertFalse(kernel.stat().st_mode & 0o222)
        again = grail.ensure_grail_source(cache, seed_dir=None, fetch=False)
        self.assertEqual(again.root, source.root)

    @criteria("A7")
    def test_tampered_seed_is_refused_without_partial_cache(self):
        seed = copy_seed(self.dir / "seed")
        (seed / "soul.md").write_text("You are someone else.")
        cache = self.dir / "cache"
        with self.assertRaises(GrailSourceError):
            grail.ensure_grail_source(cache, seed_dir=seed, fetch=False)
        with self.assertRaises(GrailSourceError):
            grail.ensure_grail_source(cache, seed_dir=None, fetch=False)

    @criteria("A1", "A7")
    def test_tampered_cache_is_detected(self):
        cache = self.dir / "cache"
        source = grail.ensure_grail_source(cache, seed_dir=SEED, fetch=False)
        target = source.root / "local_storage.py"
        os.chmod(target, 0o644)
        target.write_text("# replaced\n")
        with self.assertRaises(GrailSourceError):
            grail.ensure_grail_source(cache, seed_dir=None, fetch=False)


@unittest.skipUnless(HAVE_SEED, NO_SEED)
class TarballExtractionTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.inventory = grail.load_inventory()

    @criteria("A7")
    def test_codeload_layout_extracts_to_a_verified_tree(self):
        other = tarfile.TarInfo(f"rapp-installer-{PINNED_COMMIT}/README.md")
        other.size = 2
        data = tarball(seed_members() + [(other, b"hi")])
        root = grail.extract_tarball(data, self.dir / "out", self.inventory)
        self.assertEqual(grail.verify_tree(root, self.inventory, allow_extra=False), [])

    @criteria("A6", "A7")
    def test_hostile_archives_are_refused(self):
        prefix = f"rapp-installer-{PINNED_COMMIT}/rapp_brainstem/"
        link = tarfile.TarInfo(prefix + "agents/evil_agent.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        hard = tarfile.TarInfo(prefix + "hard.py")
        hard.type = tarfile.LNKTYPE
        hard.linkname = prefix + "brainstem.py"
        device = tarfile.TarInfo(prefix + "dev")
        device.type = tarfile.CHRTYPE
        traversal = tarfile.TarInfo(prefix + "../../escape.txt")
        traversal.size = 1
        absolute = tarfile.TarInfo("/tmp/ba-absolute-escape.txt")
        absolute.size = 1
        duplicate_info, duplicate_data = seed_members()[0]
        cases = {
            "symlink": [link],
            "hardlink": [hard],
            "device": [device],
            "traversal": [(traversal, b"x")],
            "absolute": [(absolute, b"x")],
            "duplicate": [(duplicate_info, duplicate_data)],
        }
        for label, extra in cases.items():
            with self.subTest(label):
                out = self.dir / label
                with self.assertRaises(GrailSourceError):
                    grail.extract_tarball(tarball(seed_members() + extra), out, self.inventory)
                self.assertFalse((self.dir / "escape.txt").exists())
        self.assertFalse(Path("/tmp/ba-absolute-escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
