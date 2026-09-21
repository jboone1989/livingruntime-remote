from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


class UserAuthorizationCode(AuthorizationCode):
    subject: str


class UserRefreshToken(RefreshToken):
    subject: str
    resource: str | None = None


class UserAccessToken(AccessToken):
    pass


class EmbeddedAuthStore:
    """OAuth/account state stored beside relay state without storing raw bearer tokens."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init(self) -> None:
        with self.db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_users(
                  subject TEXT PRIMARY KEY,
                  email TEXT NOT NULL UNIQUE,
                  password_salt TEXT NOT NULL,
                  password_hash TEXT NOT NULL,
                  email_verified INTEGER NOT NULL DEFAULT 0,
                  disabled INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_clients(
                  client_id TEXT PRIMARY KEY,
                  payload_json TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending_auth(
                  request_id TEXT PRIMARY KEY,
                  client_id TEXT NOT NULL,
                  params_json TEXT NOT NULL,
                  expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes(
                  code_hash TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  client_id TEXT NOT NULL,
                  scopes_json TEXT NOT NULL,
                  expires_at INTEGER NOT NULL,
                  code_challenge TEXT NOT NULL,
                  redirect_uri TEXT NOT NULL,
                  redirect_uri_explicit INTEGER NOT NULL,
                  resource TEXT
                );
                CREATE TABLE IF NOT EXISTS oauth_access_tokens(
                  token_hash TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  client_id TEXT NOT NULL,
                  scopes_json TEXT NOT NULL,
                  expires_at INTEGER NOT NULL,
                  resource TEXT
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens(
                  token_hash TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  client_id TEXT NOT NULL,
                  scopes_json TEXT NOT NULL,
                  expires_at INTEGER NOT NULL,
                  resource TEXT
                );
                CREATE INDEX IF NOT EXISTS oauth_access_subject_idx
                  ON oauth_access_tokens(subject,expires_at);
                """
            )

    @staticmethod
    def _password_digest(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )

    def create_user(self, email: str, password: str, *, email_verified: bool = False) -> str:
        normalized = email.strip().lower()
        if "@" not in normalized or len(normalized) > 254:
            raise ValueError("valid email is required")
        if len(password) < 12:
            raise ValueError("password must be at least 12 characters")
        salt = secrets.token_bytes(16)
        subject = "usr_" + uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute(
                    "INSERT INTO auth_users(subject,email,password_salt,password_hash,email_verified,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        subject,
                        normalized,
                        salt.hex(),
                        self._password_digest(password, salt).hex(),
                        1 if email_verified else 0,
                        _now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("account already exists") from exc
        return subject

    def authenticate_user(self, email: str, password: str) -> str | None:
        normalized = email.strip().lower()
        with self.db() as db:
            row = db.execute(
                "SELECT subject,password_salt,password_hash,disabled FROM auth_users WHERE email=?",
                (normalized,),
            ).fetchone()
        if not row or row["disabled"]:
            return None
        salt = bytes.fromhex(str(row["password_salt"]))
        expected = bytes.fromhex(str(row["password_hash"]))
        actual = self._password_digest(password, salt)
        return str(row["subject"]) if secrets.compare_digest(actual, expected) else None

    def userinfo(self, subject: str) -> dict[str, object] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT email,email_verified,disabled FROM auth_users WHERE subject=?", (subject,)
            ).fetchone()
        if not row or row["disabled"]:
            return None
        return {
            "sub": subject,
            "email": str(row["email"]),
            "email_verified": bool(row["email_verified"]),
        }

    def cleanup(self) -> None:
        now = _now()
        with self.db() as db:
            db.execute("DELETE FROM oauth_pending_auth WHERE expires_at < ?", (now,))
            db.execute("DELETE FROM oauth_codes WHERE expires_at < ?", (now,))
            db.execute("DELETE FROM oauth_access_tokens WHERE expires_at < ?", (now,))
            db.execute("DELETE FROM oauth_refresh_tokens WHERE expires_at < ?", (now,))

    def save_client(self, client: OAuthClientInformationFull) -> None:
        if not client.client_id:
            raise ValueError("OAuth client_id is required")
        if client.token_endpoint_auth_method not in {None, "none"}:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="LivingRuntime Remote only accepts public PKCE clients",
            )
        client.token_endpoint_auth_method = "none"
        client.client_secret = None
        with self.db() as db:
            db.execute(
                "INSERT INTO oauth_clients(client_id,payload_json,created_at) VALUES(?,?,?) "
                "ON CONFLICT(client_id) DO UPDATE SET payload_json=excluded.payload_json",
                (client.client_id, client.model_dump_json(), _now()),
            )

    def load_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.db() as db:
            row = db.execute(
                "SELECT payload_json FROM oauth_clients WHERE client_id=?", (client_id,)
            ).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["payload_json"]) if row else None

    def create_pending_auth(self, client_id: str, params: AuthorizationParams) -> str:
        self.cleanup()
        request_id = "auth_" + secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute(
                "INSERT INTO oauth_pending_auth(request_id,client_id,params_json,expires_at) VALUES(?,?,?,?)",
                (request_id, client_id, params.model_dump_json(), _now() + 600),
            )
        return request_id

    def load_pending_auth(self, request_id: str) -> tuple[str, AuthorizationParams] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT client_id,params_json,expires_at FROM oauth_pending_auth WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if not row or int(row["expires_at"]) < _now():
            return None
        return str(row["client_id"]), AuthorizationParams.model_validate_json(row["params_json"])

    def consume_pending_auth(
        self, request_id: str, subject: str
    ) -> tuple[UserAuthorizationCode, str | None]:
        pending = self.load_pending_auth(request_id)
        if not pending:
            raise ValueError("authorization request expired")
        client_id, params = pending
        code = secrets.token_urlsafe(32)
        scopes = list(params.scopes or ["remote:read"])
        model = UserAuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=time.time() + 300,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=subject,
        )
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM oauth_pending_auth WHERE request_id=? AND expires_at>=?",
                (request_id, _now()),
            ).rowcount
            if deleted != 1:
                raise ValueError("authorization request expired")
            db.execute(
                "INSERT INTO oauth_codes("
                "code_hash,subject,client_id,scopes_json,expires_at,code_challenge,"
                "redirect_uri,redirect_uri_explicit,resource) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    _digest(code),
                    subject,
                    client_id,
                    json.dumps(scopes),
                    int(model.expires_at),
                    model.code_challenge,
                    str(model.redirect_uri),
                    1 if model.redirect_uri_provided_explicitly else 0,
                    model.resource,
                ),
            )
        return model, params.state

    def load_code(self, client_id: str, code: str) -> UserAuthorizationCode | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM oauth_codes WHERE code_hash=? AND client_id=?",
                (_digest(code), client_id),
            ).fetchone()
        if not row or int(row["expires_at"]) < _now():
            return None
        return UserAuthorizationCode(
            code=code,
            scopes=json.loads(row["scopes_json"]),
            expires_at=float(row["expires_at"]),
            client_id=str(row["client_id"]),
            code_challenge=str(row["code_challenge"]),
            redirect_uri=str(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"],
            subject=str(row["subject"]),
        )

    def issue_tokens(
        self,
        *,
        subject: str,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        consume_code: str | None = None,
        consume_refresh: str | None = None,
    ) -> OAuthToken:
        access = secrets.token_urlsafe(40)
        refresh = secrets.token_urlsafe(48)
        access_exp = _now() + 3600
        refresh_exp = _now() + 30 * 86400
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if consume_code:
                if db.execute(
                    "DELETE FROM oauth_codes WHERE code_hash=? AND client_id=?",
                    (_digest(consume_code), client_id),
                ).rowcount != 1:
                    raise ValueError("authorization code was already consumed")
            if consume_refresh:
                if db.execute(
                    "DELETE FROM oauth_refresh_tokens WHERE token_hash=? AND client_id=?",
                    (_digest(consume_refresh), client_id),
                ).rowcount != 1:
                    raise ValueError("refresh token was already consumed")
            db.execute(
                "INSERT INTO oauth_access_tokens VALUES(?,?,?,?,?,?)",
                (_digest(access), subject, client_id, json.dumps(scopes), access_exp, resource),
            )
            db.execute(
                "INSERT INTO oauth_refresh_tokens VALUES(?,?,?,?,?,?)",
                (_digest(refresh), subject, client_id, json.dumps(scopes), refresh_exp, resource),
            )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
        )

    def load_access_token(self, token: str) -> UserAccessToken | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM oauth_access_tokens WHERE token_hash=?", (_digest(token),)
            ).fetchone()
        if not row or int(row["expires_at"]) < _now():
            return None
        return UserAccessToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=str(row["subject"]),
        )

    def load_refresh_token(self, client_id: str, token: str) -> UserRefreshToken | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM oauth_refresh_tokens WHERE token_hash=? AND client_id=?",
                (_digest(token), client_id),
            ).fetchone()
        if not row or int(row["expires_at"]) < _now():
            return None
        return UserRefreshToken(
            token=token,
            client_id=str(row["client_id"]),
            scopes=json.loads(row["scopes_json"]),
            expires_at=int(row["expires_at"]),
            subject=str(row["subject"]),
            resource=row["resource"],
        )

    def revoke_token(self, token: str) -> None:
        token_hash = _digest(token)
        with self.db() as db:
            db.execute("DELETE FROM oauth_access_tokens WHERE token_hash=?", (token_hash,))
            db.execute("DELETE FROM oauth_refresh_tokens WHERE token_hash=?", (token_hash,))


class EmbeddedOAuthProvider(
    OAuthAuthorizationServerProvider[UserAuthorizationCode, UserRefreshToken, UserAccessToken]
):
    def __init__(self, store: EmbeddedAuthStore, public_base: str) -> None:
        self.store = store
        self.public_base = public_base.rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.load_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            client_info.client_id = "client_" + secrets.token_urlsafe(24)
        self.store.save_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not client.client_id:
            raise ValueError("OAuth client is missing client_id")
        request_id = self.store.create_pending_auth(client.client_id, params)
        return f"{self.public_base}/oauth/login?{urlencode({'request_id': request_id})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> UserAuthorizationCode | None:
        if not client.client_id:
            return None
        return self.store.load_code(client.client_id, authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: UserAuthorizationCode
    ) -> OAuthToken:
        return self.store.issue_tokens(
            subject=authorization_code.subject,
            client_id=authorization_code.client_id,
            scopes=list(authorization_code.scopes),
            resource=authorization_code.resource,
            consume_code=authorization_code.code,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> UserRefreshToken | None:
        if not client.client_id:
            return None
        return self.store.load_refresh_token(client.client_id, refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: UserRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self.store.issue_tokens(
            subject=refresh_token.subject,
            client_id=refresh_token.client_id,
            scopes=scopes or list(refresh_token.scopes),
            resource=refresh_token.resource,
            consume_refresh=refresh_token.token,
        )

    async def load_access_token(self, token: str) -> UserAccessToken | None:
        return self.store.load_access_token(token)

    async def revoke_token(self, token: UserAccessToken | UserRefreshToken) -> None:
        self.store.revoke_token(token.token)
