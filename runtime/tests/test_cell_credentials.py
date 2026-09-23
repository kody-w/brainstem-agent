"""A1/A8/A11 unit specs: credential discovery reports a source label, never the value."""

import os
import unittest
from pathlib import Path

from acceptance_support import CANARY_TOKEN, criteria, private_dir, write_token_file
from brainstem_agent.credentials import (
    CredentialUnavailable,
    GitHubCredential,
    resolve_github_credential,
)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.dir = private_dir(self)
        self.brainstem = self.dir / "brainstem"

    def env(self, **values):
        return {"HOME": str(self.dir), "BRAINSTEM_HOME": str(self.brainstem), **values}

    def installed(self, value=CANARY_TOKEN, **options):
        directory = self.brainstem / "src" / "rapp_brainstem"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return write_token_file(directory, value, name=".copilot_token", **options)

    @criteria("A1")
    def test_installed_brainstem_token_is_the_default_source(self):
        self.installed()
        credential = resolve_github_credential(environ=self.env())
        self.assertIsInstance(credential, GitHubCredential)
        self.assertEqual(credential.value, CANARY_TOKEN)
        self.assertEqual(credential.source, "brainstem")
        self.assertEqual(credential.kind, "ghu")

    @criteria("A1")
    def test_explicit_token_file_plain_text(self):
        path = write_token_file(self.dir, CANARY_TOKEN, name="plain", as_json=False)
        credential = resolve_github_credential(
            environ=self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(path)))
        self.assertEqual(credential.source, "file")
        self.assertEqual(credential.value, CANARY_TOKEN)

    @criteria("A1", "A11")
    def test_value_never_appears_in_repr_str_or_description(self):
        self.installed()
        credential = resolve_github_credential(environ=self.env())
        for text in (repr(credential), str(credential), repr(credential.describe())):
            self.assertNotIn(CANARY_TOKEN, text)
            self.assertNotIn(CANARY_TOKEN[:8], text)
        self.assertEqual(credential.describe(), {"source": "brainstem", "kind": "ghu"})

    @criteria("A1", "A8")
    def test_missing_credential_is_explicit(self):
        with self.assertRaises(CredentialUnavailable) as caught:
            resolve_github_credential(environ=self.env())
        self.assertIn("credential", str(caught.exception).lower())

    @criteria("A8")
    def test_explicit_file_that_is_missing_never_falls_back(self):
        self.installed()
        with self.assertRaises(CredentialUnavailable):
            resolve_github_credential(environ=self.env(
                BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(self.dir / "absent.json")))

    @criteria("A6", "A8")
    def test_unsafe_or_malformed_token_files_are_refused(self):
        cases = {
            "group-readable": dict(mode=0o640),
            "world-readable": dict(mode=0o604),
        }
        for label, options in cases.items():
            with self.subTest(label):
                path = write_token_file(self.dir, name=label, **options)
                with self.assertRaises(CredentialUnavailable) as caught:
                    resolve_github_credential(
                        environ=self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(path)))
                self.assertNotIn(CANARY_TOKEN, str(caught.exception))
        contents = {
            "empty": "",
            "json-without-token": '{"refresh_token": "x"}',
            "oversized": "x" * 5000,
            "not-a-token": '{"access_token": "has spaces and\\nnewlines"}',
        }
        for label, text in contents.items():
            with self.subTest(label):
                path = self.dir / label
                path.write_text(text)
                os.chmod(path, 0o600)
                with self.assertRaises(CredentialUnavailable):
                    resolve_github_credential(
                        environ=self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(path)))
        target = write_token_file(self.dir, name="real-target")
        link = self.dir / "link"
        link.symlink_to(target)
        with self.assertRaises(CredentialUnavailable):
            resolve_github_credential(
                environ=self.env(BRAINSTEM_AGENT_GITHUB_TOKEN_FILE=str(link)))

    @criteria("A6")
    def test_resolution_is_read_only(self):
        path = self.installed()
        before = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        resolve_github_credential(environ=self.env())
        after = (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [".copilot_token"])


if __name__ == "__main__":
    unittest.main()
