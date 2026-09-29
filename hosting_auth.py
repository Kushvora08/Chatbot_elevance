"""Persistent accounts and revocable browser sessions for the hosted application."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from werkzeug.security import check_password_hash, generate_password_hash

from .errors import AccessDenied
from .security import AccessController


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AccountStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(32))
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS upload_owners (
                    file_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_user(self, username, password, role="customer"):
        username = str(username).strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._@+-]{2,119}", username):
            raise ValueError("Use a username of 3-120 letters, numbers or email characters.")
        if not isinstance(password, str) or not 14 <= len(password) <= 128:
            raise ValueError("Use a password of 14-128 characters.")
        if role not in {"admin", "customer"}:
            raise ValueError("Unknown account role.")
        user_id = uuid.uuid4().hex
        with self.connect() as db:
            try:
                db.execute("INSERT INTO users VALUES (?,?,?,?,1)",
                           (user_id, username, generate_password_hash(password), role))
            except sqlite3.IntegrityError:
                raise ValueError("That username already exists.") from None
        return {"id": user_id, "username": username, "role": role, "enabled": True}

    def bootstrap(self, username, password):
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1").fetchone()
        if not exists:
            if not username or not password:
                raise ValueError("Set ADMIN_USERNAME and ADMIN_PASSWORD before the first hosted startup.")
            self.create_user(username, password, "admin")

    def authenticate(self, username, password):
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (str(username).strip().casefold(),)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else self.dummy_hash, str(password)[:129])
        if not row or not valid or not row["enabled"]:
            return None
        return {key: row[key] for key in ("id", "username", "role")}

    def issue_session(self, user_id, now=None):
        now = time.time() if now is None else now
        opaque = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO browser_sessions VALUES (?,?,?)", (digest(opaque), user_id, now + 12 * 3600))
        return opaque

    def resolve_session(self, opaque, now=None):
        if not isinstance(opaque, str):
            return None
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("""SELECT u.id,u.username,u.role FROM browser_sessions s JOIN users u ON u.id=s.user_id
                                WHERE s.id_hash=? AND s.expires>? AND u.enabled=1""", (digest(opaque), now)).fetchone()
        return dict(row) if row else None

    def revoke(self, opaque):
        if opaque:
            with self.connect() as db:
                db.execute("DELETE FROM browser_sessions WHERE id_hash=?", (digest(opaque),))

    def users(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,username,role,enabled FROM users ORDER BY username")]

    def disable_user(self, user_id, acting_user):
        if user_id == acting_user:
            raise ValueError("You cannot disable your own account.")
        with self.connect() as db:
            db.execute("UPDATE users SET enabled=0 WHERE id=?", (user_id,))
            db.execute("DELETE FROM browser_sessions WHERE user_id=?", (user_id,))

    def reset_password(self, user_id, password):
        if not isinstance(password, str) or not 14 <= len(password) <= 128:
            raise ValueError("Use a password of 14-128 characters.")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise ValueError("Unknown account.")
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(password), user_id))
            db.execute("DELETE FROM browser_sessions WHERE user_id=?", (user_id,))

    def own_upload(self, file_id, user_id, expires):
        with self.connect() as db:
            db.execute("INSERT INTO upload_owners VALUES (?,?,?)", (file_id, user_id, expires))

    def check_uploads(self, refs, user_id):
        if not isinstance(refs, list) or len(refs) > 4:
            raise ValueError("Attach up to four files.")
        with self.connect() as db:
            for ref in refs:
                if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", ref):
                    raise AccessDenied("Invalid evidence reference.")
                row = db.execute("SELECT user_id FROM upload_owners WHERE file_id=?", (ref,)).fetchone()
                if not row or row["user_id"] != user_id:
                    raise AccessDenied("This file does not belong to your account.")

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("DELETE FROM browser_sessions WHERE expires<=?", (now,))
            # Keep ownership tombstones briefly so expired-file errors remain distinguishable.
            db.execute("DELETE FROM upload_owners WHERE expires<?", (now - 86400,))


class HostedAccessController(AccessController):
    """Capabilities exist only in a trusted request context, never in the browser."""

    def __init__(self, config, secret):
        super().__init__(config)
        self.secret = secret.encode()
        self.actor = ContextVar("hosted_actor", default=None)

    @contextmanager
    def scope(self, user_id, role):
        token = hmac.new(self.secret, ("actor:" + user_id).encode(), hashlib.sha256).hexdigest()
        reset = self.actor.set((token, role))
        try:
            yield token
        finally:
            self.actor.reset(reset)

    def _role_for_token(self, token):
        actor = self.actor.get()
        return actor[1] if actor and hmac.compare_digest(actor[0], token) else None
