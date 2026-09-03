import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import urlparse

ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]
WEB_OUTLOOK_APP_PATH = ROOT_DIR / "web_outlook_app.py"
ISOLATED_SECRET_KEY = "test-secret-key"


@contextlib.contextmanager
def temporary_environment(**updates):
    previous = {key: os.environ.get(key) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_isolated_web_outlook_app(*, role="primary"):
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="recipient-mail-links-tests-"))
    database_path = temp_dir / "test.db"
    module_name = f"test_recipient_mail_links_app_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, WEB_OUTLOOK_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with temporary_environment(
        DATABASE_PATH=str(database_path),
        SECRET_KEY=ISOLATED_SECRET_KEY,
        NODE_ROLE=role,
        MASTER_URL="https://primary.example" if role == "replica" else None,
    ):
        spec.loader.exec_module(module)

    module.resolve_secret_key = lambda: ISOLATED_SECRET_KEY
    module.secret_key = ISOLATED_SECRET_KEY
    module.app.secret_key = ISOLATED_SECRET_KEY
    module._cipher_suite = None
    return module, temp_dir, module_name


def cleanup_isolated_web_outlook_app(module, temp_dir, module_name):
    try:
        with module.app.app_context():
            module.close_connection(None)
    except Exception:
        pass
    sys.modules.pop(module_name, None)
    shutil.rmtree(temp_dir, ignore_errors=True)


class RecipientMailLinkInsertProxy:
    def __init__(
        self,
        conn,
        *,
        first_error_message: str,
        after_rollback_insert_row: dict[str, object] | None = None,
    ):
        self._conn = conn
        self._first_error_message = first_error_message
        self._after_rollback_insert_row = after_rollback_insert_row
        self._failed_once = False
        self.insert_attempts = 0

    def _execute_real(self, sql, params=()):
        if params:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if normalized.startswith("INSERT INTO recipient_mail_links"):
            self.insert_attempts += 1
            if not self._failed_once:
                self._failed_once = True
                raise sqlite3.IntegrityError(self._first_error_message)
        if (
            normalized == "ROLLBACK TO SAVEPOINT recipient_mail_link_insert"
            and self._after_rollback_insert_row is not None
        ):
            result = self._execute_real(sql)
            self._execute_real(
                """
                INSERT INTO recipient_mail_links (
                    account_id,
                    main_email_display,
                    recipient_email_display,
                    recipient_email_normalized,
                    token_hash,
                    token_encrypted,
                    expires_at,
                    primary_access_count,
                    last_accessed_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._after_rollback_insert_row["account_id"],
                    self._after_rollback_insert_row["main_email_display"],
                    self._after_rollback_insert_row["recipient_email_display"],
                    self._after_rollback_insert_row["recipient_email_normalized"],
                    self._after_rollback_insert_row["token_hash"],
                    self._after_rollback_insert_row["token_encrypted"],
                    self._after_rollback_insert_row["expires_at"],
                    self._after_rollback_insert_row["primary_access_count"],
                    self._after_rollback_insert_row["last_accessed_at"],
                    self._after_rollback_insert_row["created_at"],
                    self._after_rollback_insert_row["updated_at"],
                ),
            )
            self._after_rollback_insert_row = None
            return result
        return self._execute_real(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class CommitFailingProxy:
    def __init__(self, conn, message: str = "commit failed") -> None:
        self._conn = conn
        self._message = message
        self.rollback_calls = 0

    def execute(self, sql, params=()):
        if params:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def commit(self):
        raise sqlite3.OperationalError(self._message)

    def rollback(self):
        self.rollback_calls += 1
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RecordingReadStream:
    def __init__(self, content: bytes) -> None:
        self._content = bytes(content)
        self._offset = 0
        self.read_sizes: list[int] = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size is None or size < 0:
            raise AssertionError("unbounded read")
        chunk = self._content[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class DummyUpload:
    def __init__(self, filename: str, stream) -> None:
        self.filename = filename
        self.stream = stream


class SavepointFailureProxy:
    def __init__(self, conn, *, fail_sql: str, exc: Exception) -> None:
        self._conn = conn
        self._fail_sql = fail_sql
        self._exc = exc
        self._failed = False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        if not self._failed and normalized == self._fail_sql:
            self._failed = True
            raise self._exc
        if params:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def rollback(self):
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TransactionStateProxy:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.executed_sql: list[str] = []
        self.rollback_calls = 0

    @property
    def in_transaction(self):
        return self._conn.in_transaction

    def execute(self, sql, params=()):
        self.executed_sql.append(" ".join(str(sql).split()))
        if params:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def rollback(self):
        self.rollback_calls += 1
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class RecipientMailLinkRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module, cls._temp_dir, cls._module_name = load_isolated_web_outlook_app()
        cls.app = cls.module.app
        cls.app.config["TESTING"] = True
        cls.app.config["WTF_CSRF_ENABLED"] = False
        cls.client = cls.app.test_client()
        with cls.client.session_transaction() as sess:
            sess["logged_in"] = True

    @classmethod
    def tearDownClass(cls):
        cleanup_isolated_web_outlook_app(cls.module, cls._temp_dir, cls._module_name)

    def setUp(self):
        with self.app.app_context():
            self.module.init_db()
            db = self.module.get_db()
            tables = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "recipient_mail_links" in tables:
                db.execute("DELETE FROM recipient_mail_links")
            db.execute("DELETE FROM accounts")
            db.commit()

    def insert_account(self, email_addr="owner@example.com") -> int:
        with self.app.app_context():
            db = self.module.get_db()
            cursor = db.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', '', '', 1, '', 'active', 'outlook', 'outlook', '', 993, '', 0)
                """,
                (email_addr,),
            )
            db.commit()
            return int(cursor.lastrowid)

    def seed_recipient_link(
        self,
        *,
        account_email: str = "owner@example.com",
        account_id: int | None = None,
        main_email: str = "Owner@Example.com",
        recipient_email: str = "Recipient@Example.com",
        expires_at: str | None = None,
    ) -> dict[str, object]:
        if account_id is None:
            cache = getattr(self, "_seed_account_ids", None)
            if cache is None:
                cache = self._seed_account_ids = {}
            if account_email not in cache:
                cache[account_email] = self.insert_account(account_email)
            account_id = cache[account_email]
        with self.app_context():
            db = self.module.get_db()
            recipient = self.module.normalize_recipient_email(recipient_email)
            record = self.module.upsert_recipient_mail_link(
                db,
                account_id,
                main_email,
                recipient.display,
                recipient.normalized,
                expires_at,
            )
            db.commit()
            return record

    def seed_recipient_links(
        self, entries: list[tuple[str, str, str, str | None]]
    ) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        account_ids: dict[str, int] = {}
        for account_email, main_email, recipient_email, expires_at in entries:
            if account_email not in account_ids:
                account_ids[account_email] = self.insert_account(account_email)
            records.append(
                self.seed_recipient_link(
                    account_email=account_email,
                    account_id=account_ids[account_email],
                    main_email=main_email,
                    recipient_email=recipient_email,
                    expires_at=expires_at,
                )
            )
        return records

    def list_recipient_link_rows(self) -> list[dict[str, object]]:
        with self.app_context():
            rows = self.module.get_db().execute(
                """
                SELECT *
                FROM recipient_mail_links
                ORDER BY id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    @contextlib.contextmanager
    def app_context(self):
        with self.app.app_context():
            self.module.init_db()
            yield

    def set_aliases(self, account_id: int, primary_email: str, aliases) -> list[str]:
        with self.app_context():
            db = self.module.get_db()
            success, cleaned_aliases, errors = self.module.replace_account_aliases(
                account_id,
                primary_email,
                list(aliases),
                db,
            )
            self.assertTrue(success, errors)
            db.commit()
            return cleaned_aliases

    def import_links(
        self,
        *,
        mode,
        files,
        mainemail=None,
        expiry=None,
        expires_at=None,
        auto_export=None,
    ):
        data = {"mode": mode}
        if mainemail is not None:
            data["mainemail"] = mainemail
        if expiry is not None:
            data["expiry"] = expiry
        if expires_at is not None:
            data["expires_at"] = expires_at
        if auto_export is not None:
            data["auto_export"] = auto_export
        data["files"] = [(io.BytesIO(content), filename) for filename, content in files]
        return self.client.post(
            "/api/verification-links/import",
            data=data,
            content_type="multipart/form-data",
        )

    def unauthenticated_import_links(
        self, *, mode, files, mainemail=None, expiry=None, expires_at=None
    ):
        client = self.app.test_client()
        data = {"mode": mode}
        if mainemail is not None:
            data["mainemail"] = mainemail
        if expiry is not None:
            data["expiry"] = expiry
        if expires_at is not None:
            data["expires_at"] = expires_at
        data["files"] = [(io.BytesIO(content), filename) for filename, content in files]
        return client.post(
            "/api/verification-links/import",
            data=data,
            content_type="multipart/form-data",
        )

    def count_recipient_links(self) -> int:
        with self.app_context():
            row = self.module.get_db().execute(
                "SELECT COUNT(*) AS count FROM recipient_mail_links"
            ).fetchone()
            return int(row["count"] or 0)

    def list_recipient_links(self) -> list[dict[str, object]]:
        with self.app_context():
            rows = self.module.get_db().execute(
                """
                SELECT account_id, main_email_display, recipient_email_display,
                       recipient_email_normalized, expires_at
                FROM recipient_mail_links
                ORDER BY account_id, recipient_email_normalized
                """
            ).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _public_link_parts(url: str) -> list[str]:
        return urlparse(url).path.strip("/").split("/")

    def test_schema_has_recipient_link_constraints(self):
        with self.app_context():
            db = self.module.get_db()
            table_info = db.execute("PRAGMA table_info(recipient_mail_links)").fetchall()
            account_table_info = db.execute("PRAGMA table_info(accounts)").fetchall()
            indexes = db.execute("PRAGMA index_list(recipient_mail_links)").fetchall()

        columns = {row["name"]: row for row in table_info}
        account_columns = {row["name"]: row for row in account_table_info}
        index_names = {row["name"] for row in indexes}
        self.assertIn("token_encrypted", columns)
        self.assertEqual(columns["token_encrypted"]["notnull"], 1)
        self.assertIn("primary_access_count", columns)
        self.assertEqual(columns["primary_access_count"]["dflt_value"], "0")
        self.assertIn("idx_recipient_mail_links_binding", index_names)
        self.assertIn("idx_recipient_mail_links_token_hash", index_names)
        self.assertIn("idx_recipient_mail_links_recipient_lookup", index_names)
        self.assertIn("recipient_share_segment", account_columns)
        self.assertEqual(account_columns["recipient_share_segment"]["notnull"], 1)
        self.assertEqual(account_columns["recipient_share_segment"]["dflt_value"], "''")

    def test_recipient_links_reuse_persisted_account_share_segment(self):
        generator = getattr(self.module, "generate_recipient_mail_share_segment", None)
        self.assertIsNotNone(generator)
        account_id = self.insert_account("owner@example.com")
        expected_segment = "S" * 43

        with patch.object(
            self.module,
            "generate_recipient_mail_share_segment",
            return_value=expected_segment,
        ) as generate_mock:
            self.seed_recipient_link(
                account_id=account_id,
                recipient_email="first@example.com",
            )
            self.seed_recipient_link(
                account_id=account_id,
                recipient_email="second@example.com",
            )

        with self.app_context():
            row = self.module.get_db().execute(
                "SELECT recipient_share_segment FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()

        self.assertEqual(row["recipient_share_segment"], expected_segment)
        self.assertRegex(row["recipient_share_segment"], r"^[A-Za-z0-9_-]{43}$")
        generate_mock.assert_called_once_with()

    def test_backfill_preserves_legacy_primary_share_segment(self):
        backfill = getattr(self.module, "backfill_recipient_share_segments", None)
        self.assertIsNotNone(backfill)
        account_id = self.insert_account("legacy-owner@example.com")
        self.seed_recipient_link(
            account_id=account_id,
            main_email="legacy-owner@example.com",
            recipient_email="legacy-recipient@example.com",
        )

        with self.app_context():
            db = self.module.get_db()
            db.execute(
                "UPDATE accounts SET recipient_share_segment = '' WHERE id = ?",
                (account_id,),
            )
            updated = backfill(db)
            row = db.execute(
                "SELECT recipient_share_segment FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            db.commit()

        self.assertEqual(updated, 1)
        self.assertEqual(
            row["recipient_share_segment"],
            self.module.build_recipient_link_share_segment(account_id),
        )

    def test_recipient_link_create_and_repeat_import_reuses_token_and_first_display(self):
        upsert = getattr(self.module, "upsert_recipient_mail_link", None)
        self.assertIsNotNone(upsert)
        account_id = self.insert_account("Owner@Example.com")

        with self.app_context():
            db = self.module.get_db()
            first = upsert(
                db,
                account_id,
                "Owner@Example.com",
                "Recipient01@iCloud.com",
                "recipient01@icloud.com",
                None,
            )
            second = upsert(
                db,
                account_id,
                "owner@example.com",
                "recipient01@icloud.com",
                "recipient01@icloud.com",
                "2026-09-01T00:00:00Z",
            )
            db.commit()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(second["main_email_display"], "Owner@Example.com")
        self.assertEqual(second["recipient_email_display"], "Recipient01@iCloud.com")
        self.assertEqual(second["expires_at"], "2026-09-01T00:00:00Z")

    def test_recipient_link_token_is_encrypted_and_only_digest_is_searchable(self):
        upsert = getattr(self.module, "upsert_recipient_mail_link", None)
        digest_token = getattr(self.module, "digest_recipient_mail_token", None)
        self.assertIsNotNone(upsert)
        self.assertIsNotNone(digest_token)
        account_id = self.insert_account("owner@example.com")

        with self.app_context():
            db = self.module.get_db()
            created = upsert(
                db,
                account_id,
                "owner@example.com",
                "target@example.com",
                "target@example.com",
                None,
            )
            row = db.execute(
                """
                SELECT token_hash, token_encrypted
                FROM recipient_mail_links
                WHERE id = ?
                """,
                (created["id"],),
            ).fetchone()

        self.assertNotIn(created["token"], row["token_encrypted"])
        self.assertEqual(row["token_hash"], digest_token(created["token"]))
        self.assertGreaterEqual(len(created["token"]), 40)

    def test_recipient_link_binding_race_reuses_existing_record_after_unique_error(self):
        upsert = getattr(self.module, "upsert_recipient_mail_link", None)
        self.assertIsNotNone(upsert)
        account_id = self.insert_account("owner@example.com")
        original_updated_at = "2026-08-01T00:00:00Z"
        race_token = "race-existing-token"

        with self.app_context():
            db = self.module.get_db()
            proxy = RecipientMailLinkInsertProxy(
                db,
                first_error_message=(
                    "UNIQUE constraint failed: "
                    "recipient_mail_links.account_id, recipient_mail_links.recipient_email_normalized"
                ),
                after_rollback_insert_row={
                    "account_id": account_id,
                    "main_email_display": "Owner First@Example.com",
                    "recipient_email_display": "Recipient First@Example.com",
                    "recipient_email_normalized": "recipient@example.com",
                    "token_hash": self.module.digest_recipient_mail_token(race_token),
                    "token_encrypted": self.module.encrypt_data(race_token),
                    "expires_at": None,
                    "primary_access_count": 0,
                    "last_accessed_at": None,
                    "created_at": "2026-08-01T00:00:00Z",
                    "updated_at": original_updated_at,
                },
            )

            result = upsert(
                proxy,
                account_id,
                "Owner New@Example.com",
                "Recipient New@Example.com",
                "recipient@example.com",
                "2026-09-02T00:00:00Z",
            )
            db.commit()

        self.assertFalse(result["created"])
        self.assertEqual(result["token"], race_token)
        self.assertEqual(result["main_email_display"], "Owner First@Example.com")
        self.assertEqual(result["recipient_email_display"], "Recipient First@Example.com")
        self.assertEqual(result["expires_at"], "2026-09-02T00:00:00Z")
        self.assertNotEqual(result["updated_at"], original_updated_at)
        self.assertEqual(proxy.insert_attempts, 1)

    def test_recipient_link_token_hash_collision_retries_and_succeeds(self):
        upsert = getattr(self.module, "upsert_recipient_mail_link", None)
        self.assertIsNotNone(upsert)
        account_id = self.insert_account("owner@example.com")

        with self.app_context():
            db = self.module.get_db()
            proxy = RecipientMailLinkInsertProxy(
                db,
                first_error_message="UNIQUE constraint failed: recipient_mail_links.token_hash",
            )
            with patch.object(
                self.module,
                "generate_recipient_mail_token",
                side_effect=["colliding-token", "fresh-token"],
            ):
                result = upsert(
                    proxy,
                    account_id,
                    "Owner@Example.com",
                    "Recipient@Example.com",
                    "recipient@example.com",
                    None,
                )
            db.commit()

        self.assertTrue(result["created"])
        self.assertEqual(result["token"], "fresh-token")
        self.assertEqual(proxy.insert_attempts, 2)

    def test_recipient_link_unexpected_integrity_error_is_reraised(self):
        upsert = getattr(self.module, "upsert_recipient_mail_link", None)
        self.assertIsNotNone(upsert)
        account_id = self.insert_account("owner@example.com")

        with self.app_context():
            db = self.module.get_db()
            proxy = RecipientMailLinkInsertProxy(
                db,
                first_error_message="FOREIGN KEY constraint failed",
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
                upsert(
                    proxy,
                    account_id,
                    "Owner@Example.com",
                    "Recipient@Example.com",
                    "recipient@example.com",
                    None,
                )

        self.assertEqual(proxy.insert_attempts, 1)

    def test_import_single_file_uses_first_line_as_mainemail(self):
        account_id = self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[
                (
                    "wrong-file-name.txt",
                    b"Owner@Example.com\nRecipient01@iCloud.com----https://mail.example/link\nbad address\n",
                )
            ],
            mainemail="   ",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(
            payload["summary"],
            {
                "successful_files": 1,
                "failed_files": 0,
                "created_records": 1,
                "reused_records": 0,
                "invalid_lines": 1,
            },
        )
        self.assertNotIn("created_links", payload["summary"])
        self.assertNotIn("reused_links", payload["summary"])
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(payload["failed_files"], [])
        group = payload["groups"][0]
        self.assertEqual(group["source_file"], "wrong-file-name.txt")
        self.assertEqual(group["main_email"], "Owner@Example.com")
        self.assertEqual(group["account_id"], account_id)
        self.assertEqual(group["created_count"], 1)
        self.assertEqual(group["reused_count"], 0)
        self.assertEqual(len(group["record_ids"]), 1)
        self.assertEqual(group["errors"][0]["error_code"], "invalid_email")
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_single_file_extracts_main_email_from_first_line_segments(self):
        account_id = self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[
                (
                    "wrong-file-name.txt",
                    b"Outlook full info----Owner@Example.com\nRecipient01@iCloud.com----https://mail.example/link\n",
                )
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertEqual(group["main_email"], "Owner@Example.com")
        self.assertEqual(group["account_id"], account_id)
        self.assertEqual(group["created_count"], 1)
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_single_file_auto_creates_missing_main_mailbox(self):
        response = self.import_links(
            mode="single",
            files=[
                (
                    "missing-main.txt",
                    b"newowner@outlook.com----secret-pass----11111111-2222-3333-4444-555555555555----refresh-token\nrecipient01@example.com\nrecipient02@example.com\n",
                )
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["successful_files"], 1)
        self.assertEqual(payload["summary"]["failed_files"], 0)
        self.assertEqual(payload["summary"]["created_records"], 2)
        self.assertEqual(len(payload["groups"]), 1)
        group = payload["groups"][0]
        self.assertEqual(group["main_email"], "newowner@outlook.com")
        self.assertEqual(group["created_count"], 2)
        self.assertEqual(group["reused_count"], 0)
        self.assertEqual(group["account_created"], True)
        with self.app_context():
            account = self.module.get_account_by_email("newowner@outlook.com")
            self.assertIsNotNone(account)
            self.assertEqual(account["password"], "secret-pass")
            self.assertEqual(account["client_id"], "11111111-2222-3333-4444-555555555555")
            self.assertEqual(account["refresh_token"], "refresh-token")
        self.assertEqual(self.count_recipient_links(), 2)

    def test_import_single_file_supports_icloud_imap_main_line(self):
        response = self.import_links(
            mode="single",
            files=[
                (
                    "icloud-owner.txt",
                    b"owner@icloud.com----app-password\nrecipient01@example.com\n",
                )
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["successful_files"], 1)
        self.assertEqual(payload["summary"]["created_records"], 1)
        group = payload["groups"][0]
        self.assertEqual(group["main_email"], "owner@icloud.com")
        self.assertEqual(group["account_created"], True)

        with self.app_context():
            account = self.module.get_account_by_email("owner@icloud.com")
            self.assertIsNotNone(account)
            self.assertEqual(account["account_type"], "imap")
            self.assertEqual(account["provider"], "icloud")
            self.assertEqual(account["imap_host"], "imap.mail.me.com")
            self.assertEqual(account["imap_port"], 993)
            self.assertEqual(account["imap_password"], "app-password")
            self.assertEqual(account["password"], "")
            self.assertEqual(account["client_id"], "")
            self.assertEqual(account["refresh_token"], "")
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_single_file_prefers_outlook_email_among_multiple_candidates(self):
        account_id = self.insert_account("owner@outlook.com")

        response = self.import_links(
            mode="single",
            files=[
                (
                    "mixed.txt",
                    b"Primary Gmail----Owner@Gmail.com----Owner@Outlook.com\nRecipient01@iCloud.com\n",
                )
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        group = payload["groups"][0]
        self.assertEqual(group["main_email"], "Owner@Outlook.com")
        self.assertEqual(group["account_id"], account_id)
        self.assertEqual(group["created_count"], 1)
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_requires_login_with_no_store_header(self):
        self.insert_account("owner@example.com")

        response = self.unauthenticated_import_links(
            mode="single",
            files=[("owner@example.com.txt", b"recipient@example.com\n")],
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertTrue(response.get_json()["need_login"])
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_single_file_prefers_explicit_mainemail_and_alias_resolution(self):
        account_id = self.insert_account("actual@example.com")
        self.set_aliases(account_id, "actual@example.com", ["alias@example.com"])

        with patch.object(
            self.module,
            "recipient_link_now",
            return_value=self.module.datetime(2026, 8, 29, tzinfo=self.module.timezone.utc),
        ):
            response = self.import_links(
                mode="single",
                files=[("wrong@example.com.txt", b"Recipient@Example.com\n")],
                mainemail=" Alias@Example.com ",
                expiry="1d",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        group = payload["groups"][0]
        self.assertEqual(group["main_email"], "Alias@Example.com")
        self.assertEqual(group["account_id"], account_id)
        link = self.list_recipient_links()[0]
        self.assertEqual(link["account_id"], account_id)
        self.assertEqual(link["main_email_display"], "Alias@Example.com")
        self.assertEqual(link["expires_at"], "2026-08-30T00:00:00Z")

    def test_import_batch_auto_creates_missing_mailboxes(self):
        account_id = self.insert_account("valid@example.com")

        response = self.import_links(
            mode="batch",
            files=[
                ("first.txt", b"valid@example.com\nrecipient1@example.com\n"),
                ("second.txt", b"missing@example.com\nrecipient2@example.com\n"),
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["successful_files"], 2)
        self.assertEqual(payload["summary"]["failed_files"], 0)
        self.assertEqual(payload["summary"]["created_records"], 2)
        self.assertEqual(payload["summary"]["reused_records"], 0)
        self.assertNotIn("created_links", payload["summary"])
        self.assertNotIn("reused_links", payload["summary"])
        self.assertEqual(payload["summary"]["invalid_lines"], 0)
        self.assertEqual(len(payload["groups"]), 2)
        self.assertEqual(payload["groups"][0]["account_id"], account_id)
        self.assertEqual(len(payload["groups"][0]["record_ids"]), 1)
        self.assertEqual(payload["groups"][1]["main_email"], "missing@example.com")
        self.assertEqual(payload["groups"][1]["account_created"], True)
        with self.app_context():
            self.assertIsNotNone(self.module.get_account_by_email("missing@example.com"))
        self.assertEqual(payload["failed_files"], [])
        self.assertEqual(self.count_recipient_links(), 2)

    def test_import_batch_treats_invalid_utf8_as_per_file_failure(self):
        self.insert_account("valid@example.com")

        response = self.import_links(
            mode="batch",
            files=[
                ("valid@example.com.txt", b"valid@example.com\nrecipient1@example.com\n"),
                ("valid@example.com-second.txt", b"\xff\xfe"),
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["successful_files"], 1)
        self.assertEqual(payload["summary"]["failed_files"], 1)
        self.assertEqual(payload["summary"]["created_records"], 1)
        self.assertEqual(payload["summary"]["reused_records"], 0)
        self.assertNotIn("created_links", payload["summary"])
        self.assertNotIn("reused_links", payload["summary"])
        self.assertEqual(payload["failed_files"][0]["error_code"], "invalid_utf8")
        self.assertEqual(payload["groups"][0]["created_count"], 1)
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_auto_export_single_file_uses_api_source_filename(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[
                (
                    "customers.txt",
                    b"owner@example.com----password----client-id----token\nrecipient@example.com\n",
                )
            ],
            auto_export="1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        self.assertIn("api-customers.txt", response.headers.get("Content-Disposition", ""))
        lines = response.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "owner@example.com----password----client-id----token")
        self.assertTrue(lines[1].startswith("recipient@example.com----http://localhost/show/"))
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_auto_export_single_file_rejects_existing_main_mailbox_data(self):
        account_id = self.insert_account("owner@example.com")
        self.seed_recipient_link(
            account_id=account_id,
            main_email="owner@example.com",
            recipient_email="existing@example.com",
        )

        response = self.import_links(
            mode="single",
            files=[(
                "customers.txt",
                b"owner@example.com----secret-pass----11111111-2222-3333-4444-555555555555----refresh-token\nrecipient@example.com\n",
            )],
            auto_export="1",
        )

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["error_code"], "main_mailbox_data_exists")
        self.assertIn("owner@example.com", payload["error"])
        with self.app_context():
            account = self.module.get_account_by_email("owner@example.com")
            self.assertIsNotNone(account)
            self.assertEqual(account["password"], "secret-pass")
            self.assertEqual(account["client_id"], "11111111-2222-3333-4444-555555555555")
            self.assertEqual(account["refresh_token"], "refresh-token")
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_auto_export_batch_always_returns_zip_with_api_source_filenames(self):
        self.insert_account("alpha@example.com")
        self.insert_account("beta@example.com")

        response = self.import_links(
            mode="batch",
            files=[
                ("alpha-list.txt", b"alpha@example.com----alpha-pass----alpha-token\none@example.com\n"),
                ("beta-list.txt", b"beta@example.com----beta-pass----beta-token\ntwo@example.com\n"),
            ],
            auto_export="1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        self.assertCountEqual(archive.namelist(), ["api-alpha-list.txt", "api-beta-list.txt"])
        alpha_lines = archive.read("api-alpha-list.txt").decode("utf-8").splitlines()
        self.assertEqual(alpha_lines[0], "alpha@example.com----alpha-pass----alpha-token")
        self.assertTrue(alpha_lines[1].startswith("one@example.com----http://localhost/show/"))
        self.assertEqual(self.count_recipient_links(), 2)

        response = self.import_links(
            mode="batch",
            files=[("single-batch.txt", b"alpha@example.com\nthree@example.com\n")],
            auto_export="1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        self.assertEqual(archive.namelist(), ["api-single-batch.txt"])

    def test_import_auto_export_batch_includes_failed_files(self):
        self.insert_account("valid@example.com")

        response = self.import_links(
            mode="batch",
            files=[
                ("valid.txt", b"valid@example.com\nok@example.com\n"),
                ("missing.txt", b"missing@example.com\nbad@example.com\n"),
            ],
            auto_export="1",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        self.assertCountEqual(archive.namelist(), ["api-valid.txt", "api-missing.txt"])
        success_lines = archive.read("api-valid.txt").decode("utf-8").splitlines()
        missing_lines = archive.read("api-missing.txt").decode("utf-8").splitlines()
        self.assertEqual(success_lines[0], "valid@example.com")
        self.assertEqual(missing_lines[0], "missing@example.com")
        self.assertTrue(any(line.startswith("bad@example.com----") for line in missing_lines[1:]))

    def test_import_rejects_request_wide_binding_limit_before_writes(self):
        self.insert_account("owner@example.com")

        with patch.object(self.module, "RECIPIENT_LINK_MAX_BINDINGS", 1):
            response = self.import_links(
                mode="single",
                files=[
                    (
                        "owner@example.com.txt",
                        b"owner@example.com\nfirst@example.com\nsecond@example.com\n",
                    )
                ],
            )

        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["error_code"], "recipient_limit_exceeded")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_returns_422_when_all_files_are_invalid_or_unknown(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="batch",
            files=[
                ("missing@example.com.txt", b"missing@example.com\nrecipient@example.com\n"),
                ("owner@example.com.txt", b"owner@example.com\nbad address\n"),
            ],
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(
            payload["summary"],
            {
                "successful_files": 1,
                "failed_files": 1,
                "created_records": 1,
                "reused_records": 0,
                "invalid_lines": 1,
            },
        )
        self.assertNotIn("created_links", payload["summary"])
        self.assertNotIn("reused_links", payload["summary"])
        self.assertEqual(len(payload["groups"]), 1)
        self.assertEqual(len(payload["failed_files"]), 1)
        self.assertEqual(payload["failed_files"][0]["error_code"], "no_valid_recipients")
        self.assertEqual(self.count_recipient_links(), 1)

    def test_import_rejects_invalid_mode_before_writes(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="invalid",
            files=[("owner@example.com.txt", b"recipient@example.com\n")],
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "invalid_mode")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rejects_single_mode_multiple_files_before_writes(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[
                ("owner@example.com.txt", b"recipient1@example.com\n"),
                ("owner@example.com-2.txt", b"recipient2@example.com\n"),
            ],
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "invalid_file_count")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rejects_too_many_files_before_writes(self):
        self.insert_account("owner@example.com")

        with patch.object(self.module, "RECIPIENT_LINK_MAX_FILES", 1):
            response = self.import_links(
                mode="batch",
                files=[
                    ("owner@example.com.txt", b"recipient1@example.com\n"),
                    ("owner@example.com.txt", b"recipient2@example.com\n"),
                ],
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "invalid_file_count")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rejects_non_txt_file_before_writes(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[("owner@example.com.csv", b"recipient@example.com\n")],
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "invalid_file_type")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rejects_total_size_limit_before_writes(self):
        self.insert_account("owner@example.com")

        with patch.object(self.module, "RECIPIENT_LINK_MAX_TOTAL_BYTES", 4):
            response = self.import_links(
                mode="single",
                files=[("owner@example.com.txt", b"abcde")],
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "total_size_exceeded")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_read_recipient_import_upload_reads_remaining_plus_one(self):
        reader = getattr(self.module, "_read_recipient_import_upload", None)
        self.assertIsNotNone(reader)
        stream = RecordingReadStream(b"abcde")
        item = DummyUpload("owner@example.com.txt", stream)

        with self.assertRaisesRegex(self.module.RecipientLinkInputError, "total_size_exceeded"):
            reader(item, 4)

        self.assertEqual(stream.read_sizes, [5])

    def test_import_rejects_custom_expiry_without_timezone_before_writes(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[("owner@example.com.txt", b"owner@example.com\nrecipient@example.com\n")],
            expiry="custom",
            expires_at="2026-08-30T12:30:00",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(
            response.get_json()["error_code"], "expires_at_timezone_required"
        )
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rejects_invalid_custom_expiry_before_writes(self):
        self.insert_account("owner@example.com")

        response = self.import_links(
            mode="single",
            files=[("owner@example.com.txt", b"owner@example.com\nrecipient@example.com\n")],
            expiry="custom",
            expires_at="not-a-timestamp",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.get_json()["error_code"], "invalid_expires_at")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_main_email_from_filename_uses_basename_only(self):
        self.assertEqual(
            self.module._main_email_from_filename("../../Owner@Example.com.txt"),
            "Owner@Example.com",
        )

    def test_import_counts_deduplicated_recipients_toward_limit(self):
        self.insert_account("owner@example.com")

        with patch.object(self.module, "RECIPIENT_LINK_MAX_BINDINGS", 1):
            response = self.import_links(
                mode="single",
                files=[
                    (
                        "owner@example.com.txt",
                        b"owner@example.com\ndup@example.com\nDUP@example.com\nunique@example.com\n",
                    )
                ],
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "recipient_limit_exceeded")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_counts_same_recipient_for_different_main_mailboxes(self):
        self.insert_account("alpha@example.com")
        self.insert_account("beta@example.com")

        with patch.object(self.module, "RECIPIENT_LINK_MAX_BINDINGS", 1):
            response = self.import_links(
                mode="batch",
                files=[
                    ("alpha.txt", b"alpha@example.com\nshared@example.com\n"),
                    ("beta.txt", b"beta@example.com\nshared@example.com\n"),
                ],
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "recipient_limit_exceeded")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rolls_back_only_failed_file_and_commits_other_valid_files(self):
        self.insert_account("owner@example.com")

        original_upsert = self.module.upsert_recipient_mail_link

        def flaky_upsert(db, account_id, main_email, recipient_display, recipient_normalized, expires_at):
            if recipient_normalized == "fail@example.com":
                raise sqlite3.OperationalError("database is locked")
            return original_upsert(
                db,
                account_id,
                main_email,
                recipient_display,
                recipient_normalized,
                expires_at,
            )

        with patch.object(self.module, "upsert_recipient_mail_link", side_effect=flaky_upsert):
            response = self.import_links(
                mode="batch",
                files=[
                    ("one.txt", b"owner@example.com\nok@example.com\n"),
                    ("two.txt", b"owner@example.com\nfail@example.com\n"),
                ],
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["summary"]["successful_files"], 1)
        self.assertEqual(payload["summary"]["failed_files"], 1)
        self.assertEqual(payload["summary"]["created_records"], 1)
        self.assertEqual(payload["summary"]["reused_records"], 0)
        self.assertNotIn("created_links", payload["summary"])
        self.assertNotIn("reused_links", payload["summary"])
        self.assertEqual(payload["failed_files"][0]["error_code"], "file_persistence_failed")
        links = self.list_recipient_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["recipient_email_normalized"], "ok@example.com")

    def test_import_unknown_upsert_error_returns_500_and_rolls_back_all_files(self):
        self.insert_account("owner@example.com")

        original_upsert = self.module.upsert_recipient_mail_link

        def flaky_upsert(db, account_id, main_email, recipient_display, recipient_normalized, expires_at):
            if recipient_normalized == "boom@example.com":
                raise RuntimeError("boom")
            return original_upsert(
                db,
                account_id,
                main_email,
                recipient_display,
                recipient_normalized,
                expires_at,
            )

        with patch.object(self.module, "upsert_recipient_mail_link", side_effect=flaky_upsert):
            response = self.import_links(
                mode="batch",
                files=[
                    ("one.txt", b"owner@example.com\nok@example.com\n"),
                    ("two.txt", b"owner@example.com\nboom@example.com\n"),
                ],
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.get_json()["error_code"], "import_failed")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_rolls_back_entire_batch_when_commit_fails(self):
        self.insert_account("owner@example.com")

        with self.app_context():
            real_db = self.module.get_db()
            proxy = CommitFailingProxy(real_db)

            with patch.object(self.module, "get_db", return_value=proxy):
                response = self.import_links(
                    mode="single",
                    files=[("owner@example.com.txt", b"owner@example.com\nrecipient@example.com\n")],
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error_code"], "import_failed")
        self.assertGreaterEqual(proxy.rollback_calls, 1)
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_savepoint_cleanup_failure_returns_500_and_rolls_back_batch(self):
        self.insert_account("owner@example.com")
        original_upsert = self.module.upsert_recipient_mail_link

        def flaky_upsert(db, account_id, main_email, recipient_display, recipient_normalized, expires_at):
            if recipient_normalized == "fail@example.com":
                raise sqlite3.OperationalError("database is locked")
            return original_upsert(
                db,
                account_id,
                main_email,
                recipient_display,
                recipient_normalized,
                expires_at,
            )

        with self.app_context():
            real_db = self.module.get_db()
            proxy = SavepointFailureProxy(
                real_db,
                fail_sql="ROLLBACK TO SAVEPOINT recipient_import_2",
                exc=sqlite3.OperationalError("savepoint cleanup failed"),
            )

            with patch.object(self.module, "get_db", return_value=proxy), patch.object(
                self.module, "upsert_recipient_mail_link", side_effect=flaky_upsert
            ):
                response = self.import_links(
                    mode="batch",
                    files=[
                        ("one.txt", b"owner@example.com\nok@example.com\n"),
                        ("two.txt", b"owner@example.com\nfail@example.com\n"),
                    ],
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.get_json()["error_code"], "import_failed")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_import_replica_returns_read_only_error_without_writes(self):
        self.insert_account("owner@example.com")

        with patch.object(
            self.module,
            "CLUSTER_CONFIG",
            replace(self.module.CLUSTER_CONFIG, role="replica"),
        ):
            response = self.import_links(
                mode="single",
                files=[("owner@example.com.txt", b"owner@example.com\nrecipient@example.com\n")],
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.get_json()["error_code"], "replica_read_only")
        self.assertEqual(self.count_recipient_links(), 0)

    def test_begin_recipient_import_transaction_refuses_existing_transaction(self):
        starter = getattr(self.module, "_begin_recipient_import_transaction", None)
        self.assertIsNotNone(starter)

        with self.app_context():
            real_db = self.module.get_db()
            real_db.execute("BEGIN")
            real_db.execute(
                """
                INSERT INTO accounts (
                    email, password, client_id, refresh_token,
                    group_id, remark, status, account_type, provider,
                    imap_host, imap_port, imap_password, forward_enabled
                )
                VALUES (?, '', '', '', 1, '', 'active', 'outlook', 'outlook', '', 993, '', 0)
                """,
                ("inside-transaction@example.com",),
            )
            proxy = TransactionStateProxy(real_db)

            try:
                self.assertFalse(starter(proxy))
                self.assertEqual(proxy.rollback_calls, 0)
                self.assertFalse(any(sql == "BEGIN" for sql in proxy.executed_sql))
                self.assertTrue(real_db.in_transaction)
                row = real_db.execute(
                    "SELECT COUNT(*) AS count FROM accounts WHERE email = ?",
                    ("inside-transaction@example.com",),
                ).fetchone()
                self.assertEqual(int(row["count"] or 0), 1)
            finally:
                real_db.rollback()

    def test_cannot_manage_routes_requires_login_and_primary(self):
        anonymous = self.app.test_client()

        response = anonymous.get("/api/verification-links")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        response = anonymous.get("/verification-links/manage")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        with patch.object(
            self.module,
            "CLUSTER_CONFIG",
            replace(self.module.CLUSTER_CONFIG, role="replica"),
        ):
            response = self.client.get("/api/verification-links")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

            response = self.client.get("/verification-links")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

            response = self.client.get("/verification-links/manage")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_management_page_has_navigation_back_to_mailboxes(self):
        response = self.client.get("/verification-links")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('aria-label="主导航"', html)
        self.assertIn('href="/"', html)
        self.assertIn('返回邮箱主页', html)
        self.assertNotIn('href="/verification-links/manage"', html)
        self.assertNotIn('id="manage"', html)
        self.assertNotIn('id="rows"', html)

    def test_mail_homepage_layout_links_to_imported_mailboxes_management(self):
        html = pathlib.Path(ROOT_DIR, "templates", "partials", "index", "layout.html").read_text(encoding="utf-8")

        self.assertIn('id="importedMailboxesManagementBtn"', html)
        self.assertIn('id="desktopImportedMailboxesManagementBtn"', html)
        self.assertIn('href="/verification-links/manage"', html)
        self.assertIn('管理已导入邮箱', html)

    def test_imported_mailboxes_page_is_separate_and_has_main_email_dropdown(self):
        response = self.client.get("/verification-links/manage")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        html = response.get_data(as_text=True)
        self.assertIn("<title>管理已导入邮箱</title>", html)
        self.assertIn('id="mainEmailFilter"', html)
        self.assertIn('role="combobox"', html)
        self.assertIn('aria-controls="mainEmailFilterOptions"', html)
        self.assertIn('id="query"', html)
        self.assertIn('id="deleteMainEmail"', html)
        self.assertIn('href="/verification-links"', html)

    def test_import_forms_offer_clearable_main_mailbox_selectors(self):
        response = self.client.get("/verification-links")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="singleFilePick"', html)
        self.assertIn('id="singleFileSelection"', html)
        self.assertIn('id="batchFilePick"', html)
        self.assertIn('id="batchFileSelection"', html)
        self.assertIn('id="textMainemail"', html)
        self.assertIn('role="combobox"', html)
        self.assertIn('class="combobox-clear"', html)
        self.assertIn('aria-label="清空主邮箱"', html)
        self.assertIn("第一行主邮箱，后续每行一个收件人", html)
        self.assertIn("每个 TXT 第一行是主邮箱或别名", html)
        self.assertNotIn("文件名识别主邮箱", html)

    def test_import_script_loads_mailboxes_and_handles_text_import(self):
        script = pathlib.Path(ROOT_DIR, "static", "js", "verification-links.js").read_text(encoding="utf-8")

        self.assertIn("/api/verification-links/main-mailboxes", script)
        self.assertIn("limit: 10", script)
        self.assertIn("page_size: 20", script)
        self.assertNotIn("page_size: 50", script)
        self.assertNotIn("/api/accounts?limit=10000", script)
        self.assertIn("textMainemail", script)
        self.assertIn("mainEmailFilter", script)
        self.assertIn("selectedFilesText", script)
        self.assertIn("updateFileSelections", script)
        self.assertIn("pickImportFiles", script)
        self.assertIn("auto_export", script)
        self.assertIn("downloadResponse(response", script)
        self.assertNotIn("window.verificationImportMode === 'batch') startImport", script)
        self.assertIn("deleteMainEmail", script)
        self.assertIn("/api/verification-links/main-mailbox", script)
        self.assertIn("main_email", script)
        self.assertIn("new File([", script)
        self.assertIn("q: input.value.trim()", script)
        self.assertIn("combobox-clear", script)

    def test_list_recipient_links_supports_pagination_search_and_status(self):
        self.seed_recipient_links(
            [
                ("owner@example.com", "TargetOwner@Example.com", "TargetAlpha@Example.com", None),
                ("owner@example.com", "TargetOwner@Example.com", "TargetBeta@Example.com", None),
                ("owner@example.com", "TargetOwner@Example.com", "TargetGamma@Example.com", None),
            ]
        )

        response = self.client.get(
            "/api/verification-links?page=1&page_size=2&query=TARGET&status=active"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        self.assertEqual(
            payload["pagination"],
            {"page": 1, "page_size": 2, "total": 3, "pages": 2},
        )
        self.assertEqual(len(payload["items"]), 2)
        for item in payload["items"]:
            self.assertIn("share_url", item)
            self.assertIn("query_url", item)
            self.assertTrue(item["share_url"].startswith("http://localhost/show/"))
            self.assertTrue(item["query_url"].startswith("http://localhost/query/"))
            self.assertNotIn("token_encrypted", item)
            self.assertNotIn("token_hash", item)

        share_segments = {
            self._public_link_parts(item["share_url"])[1]
            for item in payload["items"]
        }
        query_segments = {
            self._public_link_parts(item["query_url"])[1]
            for item in payload["items"]
        }
        self.assertEqual(len(share_segments), 1)
        self.assertEqual(share_segments, query_segments)

        active_all = self.client.get(
            "/api/verification-links?page=1&page_size=50&query=TARGET&status=all"
        )
        self.assertEqual(active_all.status_code, 200)
        self.assertEqual(active_all.get_json()["pagination"]["total"], 3)

        expired = self.client.get(
            "/api/verification-links?page=1&page_size=50&query=TARGET&status=expired"
        )
        self.assertEqual(expired.status_code, 200)
        self.assertEqual(expired.get_json()["pagination"]["total"], 0)

    def test_main_mailbox_options_searches_fuzzily_and_limits_results(self):
        matching_account_ids = [
            self.insert_account(f"team-{index:02d}@example.com")
            for index in range(12)
        ]
        self.insert_account("other@example.com")
        self.set_aliases(matching_account_ids[0], "team-00@example.com", ["blue-team-alias@example.com"])

        response = self.client.get("/api/verification-links/main-mailboxes?q=team&limit=10")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(len(payload["items"]), 10)
        self.assertTrue(all("team" in item.lower() for item in payload["items"]))

        response = self.client.get("/api/verification-links/main-mailboxes?q=blue&limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], ["blue-team-alias@example.com"])

        response = self.client.get("/api/verification-links/main-mailboxes?limit=11")
        self.assertEqual(response.status_code, 400)

    def test_list_recipient_links_filters_by_main_email_or_alias(self):
        owner_id = self.insert_account("owner@example.com")
        other_id = self.insert_account("other@example.com")
        self.set_aliases(owner_id, "owner@example.com", ["alias@example.com"])
        with self.app_context():
            db = self.module.get_db()
            self.module.upsert_recipient_mail_link(
                db,
                owner_id,
                "Owner@Example.com",
                "owner-recipient@example.com",
                "owner-recipient@example.com",
                None,
            )
            self.module.upsert_recipient_mail_link(
                db,
                other_id,
                "Other@Example.com",
                "other-recipient@example.com",
                "other-recipient@example.com",
                None,
            )
            db.commit()

        response = self.client.get("/api/verification-links?main_email=alias@example.com")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["pagination"]["total"], 1)
        self.assertEqual(payload["items"][0]["recipient_email_display"], "owner-recipient@example.com")

        response = self.client.get("/api/verification-links?main_email=missing@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pagination"]["total"], 0)

    def test_delete_main_mailbox_links_resolves_alias_and_keeps_account(self):
        owner_id = self.insert_account("owner@example.com")
        other_id = self.insert_account("other@example.com")
        self.set_aliases(owner_id, "owner@example.com", ["alias@example.com"])
        with self.app_context():
            db = self.module.get_db()
            self.module.upsert_recipient_mail_link(
                db,
                owner_id,
                "Owner@Example.com",
                "owner-one@example.com",
                "owner-one@example.com",
                None,
            )
            self.module.upsert_recipient_mail_link(
                db,
                owner_id,
                "Alias@Example.com",
                "owner-two@example.com",
                "owner-two@example.com",
                None,
            )
            self.module.upsert_recipient_mail_link(
                db,
                other_id,
                "Other@Example.com",
                "other-one@example.com",
                "other-one@example.com",
                None,
            )
            db.commit()

        response = self.client.delete(
            "/api/verification-links/main-mailbox",
            json={"main_email": "alias@example.com"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["account_id"], owner_id)
        self.assertEqual(payload["deleted_count"], 3)
        with self.app_context():
            db = self.module.get_db()
            owner_account = db.execute(
                "SELECT id FROM accounts WHERE id = ?",
                (owner_id,),
            ).fetchone()
            owner_aliases = db.execute(
                "SELECT id FROM account_aliases WHERE account_id = ?",
                (owner_id,),
            ).fetchall()
            remaining = db.execute(
                """
                SELECT account_id, recipient_email_normalized
                FROM recipient_mail_links
                ORDER BY account_id
                """
            ).fetchall()
        self.assertIsNone(owner_account)
        self.assertEqual(owner_aliases, [])
        self.assertEqual(len(remaining), 1)
        self.assertEqual(int(remaining[0]["account_id"]), other_id)
        self.assertEqual(remaining[0]["recipient_email_normalized"], "other-one@example.com")

    def test_delete_main_mailbox_links_validates_input(self):
        response = self.client.delete(
            "/api/verification-links/main-mailbox",
            json={"main_email": ""},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "main_mailbox_required")

        response = self.client.delete(
            "/api/verification-links/main-mailbox",
            json={"main_email": "missing@example.com"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error_code"], "main_mailbox_not_found")

    def test_list_recipient_links_validates_pagination_and_search_escaping(self):
        self.seed_recipient_links(
            [
                ("owner@example.com", "Owner@Example.com", "under_score@example.com", None),
                ("owner@example.com", "Owner@Example.com", "underXscore@example.com", None),
            ]
        )

        response = self.client.get("/api/verification-links?page=0")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        response = self.client.get("/api/verification-links?page_size=0")
        self.assertEqual(response.status_code, 400)

        response = self.client.get("/api/verification-links?page_size=101")
        self.assertEqual(response.status_code, 400)

        response = self.client.get("/api/verification-links?status=unknown")
        self.assertEqual(response.status_code, 400)

        response = self.client.get("/api/verification-links?query=under_score")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pagination"]["total"], 1)

        response = self.client.get("/api/verification-links?query=%25")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pagination"]["total"], 0)

    def test_setting_routes_normalize_and_fallback(self):
        response = self.client.get("/api/verification-links/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(response.get_json()["configured_base_url"], "")

        response = self.client.put(
            "/api/verification-links/settings",
            json={"base_url": "https://mail.example.com/prefix/"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["effective_base_url"], "https://mail.example.com/prefix")

        response = self.client.get("/api/verification-links/settings")
        self.assertEqual(response.get_json()["effective_base_url"], "https://mail.example.com/prefix")

        response = self.client.put(
            "/api/verification-links/settings",
            json={"base_url": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["configured_base_url"], "")
        self.assertEqual(response.get_json()["effective_base_url"], "http://localhost")

        response = self.client.get("/api/verification-links/settings")
        self.assertEqual(response.get_json()["effective_base_url"], "http://localhost")

    def test_patch_delete_and_batch_mutations_require_existing_records(self):
        link = self.seed_recipient_link(
            expires_at=None,
            recipient_email="Recipient01@iCloud.com",
        )
        record_id = int(link["id"])

        response = self.client.patch(
            f"/api/verification-links/{record_id}",
            json={"expires_at": "2026-09-01T00:00:00+08:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

        with self.app_context():
            row = self.module.get_db().execute(
                "SELECT expires_at, updated_at FROM recipient_mail_links WHERE id = ?",
                (record_id,),
            ).fetchone()
        self.assertEqual(row["expires_at"], "2026-08-31T16:00:00Z")
        self.assertIsNotNone(row["updated_at"])

        response = self.client.patch(
            f"/api/verification-links/{record_id}",
            json={"recipient_email_display": "hijack@example.com"},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.patch(
            "/api/verification-links/999999",
            json={"expires_at": None},
        )
        self.assertEqual(response.status_code, 404)

        response = self.client.delete("/api/verification-links/999999")
        self.assertEqual(response.status_code, 404)

        response = self.client.delete(f"/api/verification-links/{record_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.count_recipient_links(), 0)

    def test_batch_expiry_is_atomic_for_missing_ids(self):
        link = self.seed_recipient_link(recipient_email="Recipient02@iCloud.com")
        record_id = int(link["id"])

        response = self.client.post(
            "/api/verification-links/batch-expiry",
            json={"ids": [record_id, 999999], "expires_at": "2026-09-01T00:00:00Z"},
        )

        self.assertEqual(response.status_code, 404)
        with self.app_context():
            row = self.module.get_db().execute(
                "SELECT expires_at FROM recipient_mail_links WHERE id = ?",
                (record_id,),
            ).fetchone()
        self.assertIsNone(row["expires_at"])

    def test_batch_delete_is_atomic_and_returns_affected_ids(self):
        link_one = self.seed_recipient_link(recipient_email="Recipient03@iCloud.com")
        link_two = self.seed_recipient_link(recipient_email="Recipient04@iCloud.com")

        response = self.client.post(
            "/api/verification-links/batch-delete",
            json={"ids": [int(link_one["id"]), int(link_two["id"])]},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertCountEqual(payload["affected_ids"], [int(link_one["id"]), int(link_two["id"])])
        self.assertEqual(self.count_recipient_links(), 0)

        response = self.client.post(
            "/api/verification-links/batch-delete",
            json={"ids": [int(link_one["id"]), 999999]},
        )
        self.assertEqual(response.status_code, 404)

    def test_export_single_and_multi_group_formats(self):
        link_one = self.seed_recipient_link(
            main_email="Recipient01@iCloud.com",
            recipient_email="Recipient01@iCloud.com",
        )
        response = self.client.post(
            "/api/verification-links/export",
            json={"ids": [int(link_one["id"])]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        body = response.get_data()
        lines = body.decode("utf-8").splitlines()
        self.assertEqual(lines[0], "Recipient01@iCloud.com")
        self.assertTrue(lines[1].startswith("Recipient01@iCloud.com----http://localhost/show/"))
        self.assertTrue(body.endswith(b"\n"))

        group_one = self.seed_recipient_link(
            main_email="Owner:Team/West@example.com",
            recipient_email="Alpha@One.com",
        )
        group_two = self.seed_recipient_link(
            main_email="Owner?Team\\West@example.com",
            recipient_email="Beta@One.com",
        )
        response = self.client.post(
            "/api/verification-links/export",
            json={
                "groups": [
                    {
                        "main_email": "Owner:Team/West@example.com",
                        "account_id": int(group_one["account_id"]),
                        "record_ids": [int(group_one["id"])],
                    },
                    {
                        "main_email": "Owner?Team\\West@example.com",
                        "account_id": int(group_two["account_id"]),
                        "record_ids": [int(group_two["id"])],
                    },
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        self.assertCountEqual(archive.namelist(), [
            "Owner_Team_West@example.com.txt",
            "Owner_Team_West@example.com-2.txt",
        ])
