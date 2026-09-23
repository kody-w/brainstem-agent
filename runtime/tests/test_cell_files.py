"""A2/A6 unit specs for the files organ: workspace-relative, no escapes of any kind."""

import hashlib
import os
import threading
import time
import unittest

from acceptance_support import criteria, private_dir
from brainstem_agent.organs import InvocationContext, OrganError, validate_arguments
from brainstem_agent.organs.files import FilesOrgan


def context(root, **changes):
    values = dict(
        owner="local", workspace="ws", namespace="ns", session_id="s", turn_id="t",
        call_id="c", workspace_root=root, capabilities=("files.read", "files.write"),
        deadline=time.monotonic() + 30,
    )
    values.update(changes)
    return InvocationContext(**values)


class FilesOrganTests(unittest.TestCase):
    def setUp(self):
        self.organ = FilesOrgan()
        self.workspace = private_dir(self)
        self.outside = private_dir(self)
        (self.outside / "secret.txt").write_text("OUTSIDE-SECRET")
        self.specs = {spec.name: spec for spec in self.organ.tools()}

    def call(self, tool, arguments, root=None):
        arguments = validate_arguments(self.specs[tool].parameters, arguments)
        return self.organ.invoke(context(root or self.workspace), tool, arguments)

    @criteria("A2")
    def test_tools_and_capabilities(self):
        self.assertEqual(set(self.specs), {"write_file", "read_file", "list_files"})
        self.assertEqual(self.specs["write_file"].capability, "files.write")
        self.assertEqual(self.specs["read_file"].capability, "files.read")
        self.assertEqual(self.specs["list_files"].capability, "files.read")
        self.assertEqual(self.specs["write_file"].effect, "write")

    @criteria("A2")
    def test_write_is_exact_creates_parents_and_reports_bytes(self):
        result = self.call("write_file", {"path": "notes/hello.txt", "content": "hi from the cell."})
        self.assertTrue(result.ok)
        target = self.workspace / "notes" / "hello.txt"
        self.assertEqual(target.read_bytes(), b"hi from the cell.")
        self.assertIn("17 bytes", result.content)
        self.assertEqual(result.evidence["bytes"], 17)
        self.assertEqual(result.evidence["sha256"], hashlib.sha256(b"hi from the cell.").hexdigest())
        self.call("write_file", {"path": "notes/hello.txt", "content": "replaced"})
        self.assertEqual(target.read_text(), "replaced")

    @criteria("A2")
    def test_read_and_list(self):
        self.call("write_file", {"path": "a/b.txt", "content": "bee"})
        self.assertIn("bee", self.call("read_file", {"path": "a/b.txt"}).content)
        listing = self.call("list_files", {"path": "."}).content
        self.assertIn("a/", listing)
        self.assertIn("b.txt", self.call("list_files", {"path": "a"}).content)
        with self.assertRaises(OrganError):
            self.call("read_file", {"path": "missing.txt"})

    @criteria("A6")
    def test_absolute_parent_and_malformed_paths_are_refused(self):
        for path in ("/etc/passwd", str(self.outside / "x.txt"), "../x.txt", "notes/../../x.txt",
                     "a/./../../b", "", ".", "..", "a\x00b", "notes/\x01"):
            with self.subTest(path=path):
                with self.assertRaises(OrganError):
                    self.call("write_file", {"path": path, "content": "escape"})
                with self.assertRaises(OrganError):
                    self.call("read_file", {"path": path or "/"})
        self.assertFalse((self.workspace.parent / "x.txt").exists())
        self.assertFalse((self.outside / "x.txt").exists())

    @criteria("A6")
    def test_symlink_escapes_are_refused(self):
        (self.workspace / "linkdir").symlink_to(self.outside, target_is_directory=True)
        (self.workspace / "leaf.txt").symlink_to(self.outside / "secret.txt")
        (self.workspace / "dangling.txt").symlink_to(self.outside / "new.txt")
        attempts = [
            ("write_file", {"path": "linkdir/x.txt", "content": "escape"}),
            ("read_file", {"path": "linkdir/secret.txt"}),
            ("list_files", {"path": "linkdir"}),
            ("write_file", {"path": "leaf.txt", "content": "escape"}),
            ("read_file", {"path": "leaf.txt"}),
            ("write_file", {"path": "dangling.txt", "content": "escape"}),
        ]
        for tool, arguments in attempts:
            with self.subTest(tool=tool, arguments=arguments):
                with self.assertRaises(OrganError):
                    self.call(tool, arguments)
        self.assertEqual((self.outside / "secret.txt").read_text(), "OUTSIDE-SECRET")
        self.assertFalse((self.outside / "x.txt").exists())
        self.assertFalse((self.outside / "new.txt").exists())

    @criteria("A6")
    def test_hard_links_to_outside_files_are_refused(self):
        os.link(self.outside / "secret.txt", self.workspace / "hard.txt")
        with self.assertRaises(OrganError):
            self.call("read_file", {"path": "hard.txt"})
        with self.assertRaises(OrganError):
            self.call("write_file", {"path": "hard.txt", "content": "escape"})
        self.assertEqual((self.outside / "secret.txt").read_text(), "OUTSIDE-SECRET")

    @criteria("A6")
    def test_any_file_with_more_than_one_link_is_refused(self):
        (self.workspace / "a.txt").write_text("inside")
        os.link(self.workspace / "a.txt", self.workspace / "b.txt")
        for name in ("a.txt", "b.txt"):
            with self.subTest(name=name):
                with self.assertRaises(OrganError):
                    self.call("read_file", {"path": name})
                with self.assertRaises(OrganError):
                    self.call("write_file", {"path": name, "content": "replaced"})
        self.assertEqual((self.workspace / "a.txt").read_text(), "inside")
        self.assertEqual((self.workspace / "a.txt").stat().st_nlink, 2)

    @criteria("A6")
    def test_refused_write_leaves_the_link_and_the_outside_file_untouched(self):
        secret = self.outside / "secret.txt"
        os.link(secret, self.workspace / "hard.txt")
        before = os.stat(secret)
        with self.assertRaises(OrganError):
            self.call("write_file", {"path": "hard.txt", "content": "escape"})
        after = os.stat(secret)
        self.assertEqual((after.st_ino, after.st_nlink, after.st_size),
                         (before.st_ino, 2, before.st_size))
        self.assertEqual(os.stat(self.workspace / "hard.txt").st_ino, before.st_ino)
        self.assertEqual(secret.read_text(), "OUTSIDE-SECRET")

    @criteria("A6")
    def test_listing_never_reports_a_hard_linked_files_metadata(self):
        os.link(self.outside / "secret.txt", self.workspace / "hard.txt")
        (self.workspace / "plain.txt").write_text("abc")
        listing = self.call("list_files", {"path": "."}).content
        self.assertIn("plain.txt (3 bytes)", listing)
        line = next(item for item in listing.splitlines() if item.startswith("hard.txt"))
        self.assertIn("hard link", line)
        self.assertNotIn("14 bytes", line, "the outside file's size leaked through the listing")

    @criteria("A6", "A8")
    def test_reading_a_fifo_is_refused_without_blocking(self):
        fifo = self.workspace / "pipe"
        os.mkfifo(fifo)
        outcome = {}

        def read():
            try:
                self.call("read_file", {"path": "pipe"})
                outcome["result"] = "read"
            except OrganError as error:
                outcome["result"] = str(error)

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(3)
        if thread.is_alive():
            descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(descriptor)
            thread.join(3)
            self.fail("read_file blocked on a FIFO")
        self.assertIn("not a regular file", outcome["result"])

    @criteria("A6")
    def test_two_workspaces_do_not_share_files(self):
        other = private_dir(self)
        self.call("write_file", {"path": "shared.txt", "content": "only in A"})
        with self.assertRaises(OrganError):
            self.call("read_file", {"path": "shared.txt"}, root=other)
        self.assertFalse((other / "shared.txt").exists())

    @criteria("A8")
    def test_cancelled_invocation_does_no_work(self):
        ctx = context(self.workspace)
        ctx.cancelled.set()
        arguments = validate_arguments(
            self.specs["write_file"].parameters, {"path": "late.txt", "content": "x"})
        with self.assertRaises(OrganError):
            self.organ.invoke(ctx, "write_file", arguments)
        self.assertFalse((self.workspace / "late.txt").exists())


if __name__ == "__main__":
    unittest.main()
