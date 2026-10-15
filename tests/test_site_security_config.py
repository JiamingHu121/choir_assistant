"""Private credential provisioning: secrets never become CLI arguments/output."""
from contextlib import redirect_stdout, redirect_stderr
from hashlib import pbkdf2_hmac
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from choir_assistant.deployment import security


class SiteSecurityConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_generated_credentials_are_salted_unique_and_password_is_absent(self):
        password = "synthetic private fixture"
        first = security.create_site_config(password, "Example Choir", "https://github.com/example/project")
        second = security.create_site_config(password)
        self.assertNotIn(password, json.dumps(first))
        self.assertNotEqual(first["auth"]["salt"], second["auth"]["salt"])
        self.assertNotEqual(first["auth"]["cookieSigningKey"], second["auth"]["cookieSigningKey"])
        self.assertNotEqual(first["internalHealthToken"], second["internalHealthToken"])
        auth = first["auth"]
        self.assertEqual(pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(auth["salt"]), auth["iterations"]).hex(), auth["passwordHash"])
        self.assertNotEqual(pbkdf2_hmac("sha256", b"wrong", bytes.fromhex(auth["salt"]), auth["iterations"]).hex(), auth["passwordHash"])

    def test_config_create_is_private_and_refuses_file_or_symlink_overwrite(self):
        path = self.root / "private/site-config.json"
        first = {"secret": "fixture"}
        security.write_site_config(path, first)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            security.write_site_config(path, {"secret": "changed"})
        self.assertEqual(json.loads(path.read_text()), first)
        link = self.root / "link.json"
        link.symlink_to(path)
        with self.assertRaises(FileExistsError):
            security.write_site_config(link, {})
        self.assertEqual(json.loads(path.read_text()), first)

    def test_cli_password_file_does_not_print_secrets(self):
        password = "synthetic-cli-fixture-secret"
        source = self.root / "password.txt"
        source.write_text(password + "\n")
        source.chmod(0o600)
        output = io.StringIO()
        path = self.root / "config.json"
        with redirect_stdout(output):
            security.main(["configure", "--output", str(path), "--password-file", str(source), "--site-title", "Example Library"])
        config = json.loads(path.read_text())
        rendered = output.getvalue()
        for secret in (password, config["auth"]["passwordHash"], config["auth"]["salt"], config["auth"]["cookieSigningKey"], config["internalHealthToken"]):
            self.assertNotIn(secret, rendered)
        self.assertTrue(json.loads(rendered)["authenticationEnabled"])
        source.chmod(0o644)
        with self.assertRaises(ValueError):
            security.read_password_file(source)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            security.main(["configure", "--output", str(path), "--password", password])

    def test_rejects_multiline_password_or_executable_project_urls(self):
        for password in ("", "line\nbreak", "x" * 257):
            with self.assertRaises(ValueError):
                security.create_site_config(password)
        for url in ("javascript:alert(1)", "http://github.com/example/project", "https://user:pass@example.org"):
            with self.assertRaises(ValueError):
                security.create_site_config("synthetic", github_url=url)
