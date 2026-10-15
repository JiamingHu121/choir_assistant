"""Create private deployment credentials without adding them to a site export."""
from __future__ import annotations

import argparse
import getpass
from hashlib import pbkdf2_hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import urlsplit
from contextlib import closing


PASSWORD_ITERATIONS = 600000


def create_site_config(password, site_title="合唱曲库", github_url=""):
    """Return deployment-only credentials; no default password or account exists."""
    if not isinstance(password, str) or not 1 <= len(password) <= 256 or any(c in password for c in "\r\n\0"):
        raise ValueError("Password must contain 1–256 characters on one line")
    if not isinstance(site_title, str) or not 1 <= len(site_title) <= 80:
        raise ValueError("Site title must contain 1–80 characters")
    parsed = urlsplit(github_url)
    if github_url and (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password):
        raise ValueError("Project URL must be an HTTPS address without credentials")
    salt = secrets.token_bytes(32)
    return {"schemaVersion": 1, "siteTitle": site_title, "githubUrl": github_url,
            "internalHealthToken": secrets.token_hex(32),
            "auth": {"salt": salt.hex(),
                     "passwordHash": pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS).hex(),
                     "iterations": PASSWORD_ITERATIONS, "cookieSigningKey": secrets.token_hex(32), "sessionSeconds": 43200},
            "feedback": {"maxBytes": 10 * 1024 * 1024, "maxEntries": 2000}}


def write_site_config(path, data):
    """Create a 0600 file exclusively. Existing files/symlinks are never replaced."""
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    return path


def read_password_file(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Password file must be a private regular file with mode 0600")
    if path.stat().st_size > 2048:
        raise ValueError("Password file is too large")
    value = path.read_text(encoding="utf-8")
    if value.endswith("\n"):
        value = value[:-1]
    if value.endswith("\r"):
        value = value[:-1]
    return value


def read_feedback(path, limit=50):
    """Read the local inbox as JSON data, never render user-provided HTML."""
    path = Path(path).expanduser().resolve()
    if not path.is_file() or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("An existing feedback database and limit 1–500 are required")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id, created, payload FROM feedback ORDER BY created DESC, id LIMIT ?", (limit,)).fetchall()
    return [{"id": ident, "created": created, **json.loads(payload)} for ident, created, payload in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    configure = sub.add_parser("configure", help="Generate a private website configuration")
    configure.add_argument("--output", required=True)
    configure.add_argument("--site-title", default="合唱曲库")
    configure.add_argument("--github-url", default="")
    configure.add_argument("--password-file", help="Private mode-0600 file; otherwise prompt without echo")
    feedback = sub.add_parser("feedback", help="Read the private local feedback inbox")
    feedback.add_argument("--database", required=True)
    feedback.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.action == "feedback":
            print(json.dumps({"feedback": read_feedback(args.database, args.limit)}, ensure_ascii=False, indent=2))
            return
        if args.password_file:
            password = read_password_file(args.password_file)
        else:
            password = getpass.getpass("Site password: ")
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords do not match")
        value = create_site_config(password, args.site_title, args.github_url)
        password = None
        path = write_site_config(args.output, value)
    except (ValueError, OSError, UnicodeError, sqlite3.Error):
        parser.error("Cannot create configuration or read inbox; check private paths, permissions and input. Existing configuration is never overwritten.")
    print(json.dumps({"path": str(path), "authenticationEnabled": True, "secretsPrinted": False}))


if __name__ == "__main__":
    main()
