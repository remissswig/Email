from __future__ import annotations

import hashlib
import io
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
import zipfile

from outlook_web.recipient_links import (
    RecipientLinkInputError,
    decode_recipient_txt,
    decode_recipient_txt_with_main_email,
    normalize_public_base_url,
    normalize_recipient_email,
    parse_expiry,
    safe_export_stem,
)

if TYPE_CHECKING:
    from web_outlook_app import *  # noqa: F403


RECIPIENT_LINK_TOKEN_BYTES = 32
RECIPIENT_LINK_SETTING = "recipient_link_public_base_url"
RECIPIENT_LINK_MAX_FILES = 20
RECIPIENT_LINK_MAX_TOTAL_BYTES = 5 * 1024 * 1024
RECIPIENT_LINK_MAX_BINDINGS = 10_000
RECIPIENT_LINK_MAX_PAGE_SIZE = 100
RECIPIENT_LINK_MAX_MAILBOX_OPTIONS = 10

RECIPIENT_LINK_ERROR_MESSAGES = {
    "invalid_mode": "导入模式无效",
    "invalid_file_count": "上传文件数量无效",
    "invalid_file_type": "仅支持导入 TXT 文件",
    "total_size_exceeded": "导入文件总大小超过限制",
    "recipient_limit_exceeded": "导入收件人数量超过限制",
    "invalid_expiry": "过期策略无效",
    "invalid_expires_at": "自定义过期时间无效",
    "expires_at_timezone_required": "自定义过期时间必须包含时区",
    "invalid_utf8": "TXT 文件编码无效，请使用 UTF-8",
    "main_mailbox_not_found": "主邮箱不存在或未导入",
    "main_mailbox_data_exists": "导入失败，主邮箱数据已存在",
    "main_mailbox_required": "请选择主邮箱",
    "no_valid_recipients": "文件中没有可导入的有效收件人",
    "file_persistence_failed": "文件导入失败，已回滚该文件",
    "no_valid_records": "没有可导入的有效记录",
    "import_failed": "导入失败，请稍后重试",
}


def recipient_link_now() -> datetime:
    return datetime.now(timezone.utc)


def recipient_link_timestamp(value: datetime | None = None) -> str:
    current = value or recipient_link_now()
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def generate_recipient_mail_token() -> str:
    return secrets.token_urlsafe(RECIPIENT_LINK_TOKEN_BYTES)


def digest_recipient_mail_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def recipient_link_json_response(payload: dict[str, Any], status: int = 200):
    response = make_response(jsonify(payload), status)
    response.headers["Cache-Control"] = "no-store"
    return response


def recipient_link_json_error(code: str, status: int, **extra):
    payload = {
        "success": False,
        "error_code": code,
        "error": RECIPIENT_LINK_ERROR_MESSAGES.get(code, "请求处理失败"),
    }
    payload.update(extra)
    return recipient_link_json_response(payload, status)


def recipient_link_no_store(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        response = make_response(f(*args, **kwargs))
        response.headers["Cache-Control"] = "no-store"
        return response

    return decorated


@app.after_request
def recipient_link_import_after_request(response):
    path = request.path or ""
    if path in {"/verification-links", "/verification-links/manage"} or path.startswith("/api/verification-links"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _main_email_from_filename(filename: Any) -> str:
    source_name = Path(str(filename or "")).name
    if source_name.lower().endswith(".txt"):
        return source_name[:-4]
    return ""


def _read_recipient_import_upload(item: Any, remaining: int) -> bytes:
    stream = getattr(item, "stream", None)
    if stream is None:
        raise RecipientLinkInputError("total_size_exceeded")

    content = stream.read(max(0, int(remaining)) + 1)
    if len(content) > remaining:
        raise RecipientLinkInputError("total_size_exceeded")
    return content


def parse_recipient_import_request() -> dict[str, Any]:
    mode = str(request.form.get("mode") or "").strip().lower()
    if mode not in {"single", "batch"}:
        raise RecipientLinkInputError("invalid_mode")

    files = request.files.getlist("files")
    if not files or len(files) > RECIPIENT_LINK_MAX_FILES:
        raise RecipientLinkInputError("invalid_file_count")
    if mode == "single" and len(files) != 1:
        raise RecipientLinkInputError("invalid_file_count")

    explicit_main_email = str(request.form.get("mainemail") or "").strip()
    total_bytes = 0
    total_bindings = 0
    raw_files: list[dict[str, Any]] = []

    for item in files:
        source_file = Path(str(getattr(item, "filename", "") or "")).name
        if not source_file.lower().endswith(".txt"):
            raise RecipientLinkInputError("invalid_file_type")

        remaining = RECIPIENT_LINK_MAX_TOTAL_BYTES - total_bytes
        content = _read_recipient_import_upload(item, remaining)
        total_bytes += len(content)

        parsed = None
        file_error = None
        main_email = explicit_main_email if mode == "single" and explicit_main_email else ""
        try:
            if main_email:
                parsed = decode_recipient_txt(content)
            else:
                main_email, parsed = decode_recipient_txt_with_main_email(content)
            total_bindings += len(parsed.recipients)
        except RecipientLinkInputError as exc:
            file_error = exc.code

        raw_files.append(
            {
                "source_file": source_file,
                "main_email": main_email,
                "parsed": parsed,
                "file_error": file_error,
            }
        )

    if total_bindings > RECIPIENT_LINK_MAX_BINDINGS:
        raise RecipientLinkInputError("recipient_limit_exceeded")

    expires_at = parse_expiry(
        request.form.get("expiry"),
        request.form.get("expires_at"),
        recipient_link_now(),
    )
    return {"mode": mode, "raw_files": raw_files, "expires_at": expires_at}


def _begin_recipient_import_transaction(db) -> bool:
    if getattr(db, "in_transaction", False):
        app.logger.error("recipient import transaction already active")
        return False
    db.execute("BEGIN")
    return True


def _cleanup_recipient_import_savepoint(db, name: str) -> None:
    try:
        db.execute(f"ROLLBACK TO SAVEPOINT {name}")
        db.execute(f"RELEASE SAVEPOINT {name}")
    except sqlite3.Error:
        app.logger.exception("recipient import savepoint cleanup failed")
        raise


def _recipient_import_summary(
    *,
    successful_files: int,
    failed_files: int,
    created_records: int,
    reused_records: int,
    invalid_lines: int,
) -> dict[str, int]:
    return {
        "successful_files": successful_files,
        "failed_files": failed_files,
        "created_records": created_records,
        "reused_records": reused_records,
        "invalid_lines": invalid_lines,
    }


def _recipient_link_row(row, include_token: bool = False) -> dict[str, Any]:
    record = dict(row)
    record["primary_access_count"] = int(record.get("primary_access_count") or 0)
    expires_at = record.get("expires_at")
    record["status"] = (
        "expired"
        if expires_at and str(expires_at) <= recipient_link_timestamp()
        else "active"
    )
    if include_token:
        record["token"] = decrypt_data(record.get("token_encrypted") or "")
    record.pop("token_encrypted", None)
    record.pop("token_hash", None)
    return record


def _get_recipient_mail_link_row(db, account_id: int, recipient_email_normalized: str):
    return db.execute(
        """
        SELECT *
        FROM recipient_mail_links
        WHERE account_id = ? AND recipient_email_normalized = ?
        """,
        (account_id, recipient_email_normalized),
    ).fetchone()


def _return_existing_recipient_mail_link(
    db,
    row,
    *,
    expires_at: str | None,
    updated_at: str,
) -> dict[str, Any]:
    db.execute(
        """
        UPDATE recipient_mail_links
        SET expires_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (expires_at, updated_at, row["id"]),
    )
    refreshed = db.execute(
        "SELECT * FROM recipient_mail_links WHERE id = ?",
        (row["id"],),
    ).fetchone()
    record = _recipient_link_row(refreshed, include_token=True)
    record["created"] = False
    return record


def _is_recipient_token_hash_collision(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc)
    return message in {
        "UNIQUE constraint failed: recipient_mail_links.token_hash",
        "UNIQUE constraint failed: token_hash",
    }


def upsert_recipient_mail_link(
    db,
    account_id,
    main_email_display,
    recipient_email_display,
    recipient_email_normalized,
    expires_at,
):
    existing = _get_recipient_mail_link_row(db, account_id, recipient_email_normalized)
    if existing is not None:
        return _return_existing_recipient_mail_link(
            db,
            existing,
            expires_at=expires_at,
            updated_at=recipient_link_timestamp(),
        )

    for _attempt in range(3):
        token = generate_recipient_mail_token()
        token_hash = digest_recipient_mail_token(token)
        now = recipient_link_timestamp()
        db.execute("SAVEPOINT recipient_mail_link_insert")
        try:
            cursor = db.execute(
                """
                INSERT INTO recipient_mail_links (
                    account_id,
                    main_email_display,
                    recipient_email_display,
                    recipient_email_normalized,
                    token_hash,
                    token_encrypted,
                    expires_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    main_email_display,
                    recipient_email_display,
                    recipient_email_normalized,
                    token_hash,
                    encrypt_data(token),
                    expires_at,
                    now,
                    now,
                ),
            )
            db.execute("RELEASE SAVEPOINT recipient_mail_link_insert")
            row = db.execute(
                "SELECT * FROM recipient_mail_links WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            record = _recipient_link_row(row, include_token=True)
            record["created"] = True
            return record
        except sqlite3.IntegrityError as exc:
            db.execute("ROLLBACK TO SAVEPOINT recipient_mail_link_insert")
            db.execute("RELEASE SAVEPOINT recipient_mail_link_insert")
            existing = _get_recipient_mail_link_row(
                db,
                account_id,
                recipient_email_normalized,
            )
            if existing is not None:
                return _return_existing_recipient_mail_link(
                    db,
                    existing,
                    expires_at=expires_at,
                    updated_at=recipient_link_timestamp(),
                )
            if not _is_recipient_token_hash_collision(exc):
                raise

    raise RuntimeError("recipient token collision limit reached")


def _recipient_import_export_response(export_items: list[dict[str, Any]], *, force_zip: bool = False):
    if not export_items:
        return None
    if len(export_items) == 1 and not force_zip:
        item = export_items[0]
        if item["status"] == "failed":
            return _recipient_link_attachment_response(
                _recipient_link_import_failure_text(
                    source_file=item["source_file"],
                    main_email=item["main_email"],
                    error_code=item["error_code"],
                    errors=item.get("errors") or [],
                ),
                "text/plain; charset=utf-8",
                _recipient_link_source_export_name(item["source_file"], failed=True),
            )
        group = item
        return _recipient_link_attachment_response(
            _recipient_link_txt_for_import_records(group["main_email"], group["records"]),
            "text/plain; charset=utf-8",
            _recipient_link_source_export_name(group["source_file"]),
        )

    used_names: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for item in export_items:
            if item["status"] == "failed":
                archive.writestr(
                    _recipient_link_source_export_name(item["source_file"], failed=True, used_names=used_names),
                    _recipient_link_import_failure_text(
                        source_file=item["source_file"],
                        main_email=item["main_email"],
                        error_code=item["error_code"],
                        errors=item.get("errors") or [],
                    ),
                )
            else:
                archive.writestr(
                    _recipient_link_source_export_name(item["source_file"], used_names=used_names),
                    _recipient_link_txt_for_import_records(item["main_email"], item["records"]),
                )
    timestamp = recipient_link_now().strftime("%Y%m%d-%H%M%S")
    return _recipient_link_attachment_response(
        buffer.getvalue(),
        "application/zip",
        f"verification-links-{timestamp}.zip",
    )


@app.route("/api/verification-links/import", methods=["POST"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_import_recipient_verification_links():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    try:
        parsed_request = parse_recipient_import_request()
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    db = get_db()
    groups: list[dict[str, Any]] = []
    failed_files: list[dict[str, Any]] = []
    created_total = 0
    reused_total = 0
    invalid_total = 0
    export_items: list[dict[str, Any]] = []
    started_transaction = False
    auto_export = str(request.form.get("auto_export") or "").strip().lower() in {"1", "true", "yes"}

    try:
        started_transaction = _begin_recipient_import_transaction(db)
        if not started_transaction:
            return recipient_link_json_error("import_failed", 500)
        for index, import_file in enumerate(parsed_request["raw_files"], start=1):
            parsed_file = import_file["parsed"]
            file_errors = []
            if parsed_file is not None:
                file_errors = list(parsed_file.errors)

            if import_file["file_error"]:
                failed_files.append(
                    {
                        "source_file": import_file["source_file"],
                        "main_email": import_file["main_email"],
                        "error_code": import_file["file_error"],
                        "errors": [],
                    }
                )
                if auto_export and parsed_request["mode"] == "batch":
                    export_items.append(
                        {
                            "status": "failed",
                            "source_file": import_file["source_file"],
                            "main_email": import_file["main_email"],
                            "error_code": import_file["file_error"],
                            "errors": [],
                        }
                    )
                continue

            account = resolve_account_by_address(import_file["main_email"])
            if account is None:
                invalid_total += len(file_errors)
                failed_files.append(
                    {
                        "source_file": import_file["source_file"],
                        "main_email": import_file["main_email"],
                        "error_code": "main_mailbox_not_found",
                        "errors": file_errors,
                    }
                )
                if auto_export and parsed_request["mode"] == "batch":
                    export_items.append(
                        {
                            "status": "failed",
                            "source_file": import_file["source_file"],
                            "main_email": import_file["main_email"],
                            "error_code": "main_mailbox_not_found",
                            "errors": file_errors,
                        }
                    )
                continue

            if not parsed_file.recipients:
                invalid_total += len(file_errors)
                failed_files.append(
                    {
                        "source_file": import_file["source_file"],
                        "main_email": import_file["main_email"],
                        "error_code": "no_valid_recipients",
                        "errors": file_errors,
                    }
                )
                if auto_export and parsed_request["mode"] == "batch":
                    export_items.append(
                        {
                            "status": "failed",
                            "source_file": import_file["source_file"],
                            "main_email": import_file["main_email"],
                            "error_code": "no_valid_recipients",
                            "errors": file_errors,
                        }
                    )
                continue

            if parsed_request["mode"] == "single":
                existing_count = int(
                    db.execute(
                        "SELECT COUNT(*) AS count FROM recipient_mail_links WHERE account_id = ?",
                        (int(account["id"]),),
                    ).fetchone()["count"]
                    or 0
                )
                if existing_count:
                    db.rollback()
                    return recipient_link_json_error(
                        "main_mailbox_data_exists",
                        409,
                        main_email=import_file["main_email"],
                        error=f"导入失败，{import_file['main_email']} 数据已存在",
                    )

            savepoint_name = f"recipient_import_{index}"
            db.execute(f"SAVEPOINT {savepoint_name}")
            try:
                records = [
                    upsert_recipient_mail_link(
                        db,
                        int(account["id"]),
                        import_file["main_email"],
                        recipient.display,
                        recipient.normalized,
                        parsed_request["expires_at"],
                    )
                    for recipient in parsed_file.recipients
                ]
                db.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except sqlite3.Error:
                _cleanup_recipient_import_savepoint(db, savepoint_name)
                invalid_total += len(file_errors)
                failed_files.append(
                    {
                        "source_file": import_file["source_file"],
                        "main_email": import_file["main_email"],
                        "error_code": "file_persistence_failed",
                        "errors": file_errors,
                    }
                )
                if auto_export and parsed_request["mode"] == "batch":
                    export_items.append(
                        {
                            "status": "failed",
                            "source_file": import_file["source_file"],
                            "main_email": import_file["main_email"],
                            "error_code": "file_persistence_failed",
                            "errors": file_errors,
                        }
                    )
                continue
            except Exception:
                _cleanup_recipient_import_savepoint(db, savepoint_name)
                raise

            created_count = sum(1 for record in records if record.get("created"))
            reused_count = len(records) - created_count
            created_total += created_count
            reused_total += reused_count
            invalid_total += len(file_errors)
            groups.append(
                {
                    "source_file": import_file["source_file"],
                    "main_email": import_file["main_email"],
                    "account_id": int(account["id"]),
                    "record_ids": [int(record["id"]) for record in records],
                    "created_count": created_count,
                    "reused_count": reused_count,
                    "errors": file_errors,
                }
            )
            export_items.append(
                {
                    "status": "success",
                    "source_file": import_file["source_file"],
                    "main_email": import_file["main_email"],
                    "records": records,
                }
            )

        summary = _recipient_import_summary(
            successful_files=len(groups),
            failed_files=len(failed_files),
            created_records=created_total,
            reused_records=reused_total,
            invalid_lines=invalid_total,
        )
        if not groups and not auto_export:
            db.rollback()
            return recipient_link_json_error(
                "no_valid_records",
                422,
                groups=[],
                failed_files=failed_files,
                summary=summary,
            )

        db.commit()
        if auto_export:
            return _recipient_import_export_response(
                export_items,
                force_zip=parsed_request["mode"] == "batch",
            )
        return recipient_link_json_response(
            {
                "success": True,
                "summary": summary,
                "groups": groups,
                "failed_files": failed_files,
            }
        )
    except Exception:
        if started_transaction:
            db.rollback()
        app.logger.exception("recipient link import commit failed")
        return recipient_link_json_error("import_failed", 500)


def _recipient_link_parse_positive_int(value: Any, *, code: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise RecipientLinkInputError(code)
    try:
        parsed = int(str(value).strip())
    except Exception as exc:
        raise RecipientLinkInputError(code) from exc
    if parsed < minimum or parsed > maximum:
        raise RecipientLinkInputError(code)
    return parsed


def parse_recipient_link_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value or len(value) > 10_000:
        raise RecipientLinkInputError("invalid_record_ids")

    deduped: list[int] = []
    seen: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise RecipientLinkInputError("invalid_record_ids")
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def parse_absolute_expiry(value: Any) -> str | None:
    if value is None:
        return None
    return parse_expiry("custom", value, recipient_link_now())


def effective_recipient_link_base_url() -> str:
    configured = str(get_setting(RECIPIENT_LINK_SETTING, "") or "").strip()
    if configured:
        return normalize_public_base_url(configured)
    prefix = str(request.script_root or "").rstrip("/")
    return request.host_url.rstrip("/") + prefix


def build_recipient_link_url(token: str) -> str:
    return f"{effective_recipient_link_base_url().rstrip('/')}/api/v2/mailboxes/{token}"


def serialize_recipient_link(row) -> dict[str, Any]:
    record = _recipient_link_row(row, include_token=True)
    token = record.pop("token", "")
    record["share_url"] = build_recipient_link_url(token)
    return record


def recipient_link_html_response(message: Any, status: int = 200):
    response = public_mailbox_html_message_response(message, status)
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def resolve_recipient_link_token(token: Any):
    normalized = str(token or "").strip()
    if not normalized or len(normalized) > 256:
        return None
    return get_db().execute(
        """
        SELECT l.*, a.id AS bound_account_exists
        FROM recipient_mail_links AS l
        LEFT JOIN accounts AS a ON a.id = l.account_id
        WHERE l.token_hash = ?
        LIMIT 1
        """,
        (digest_recipient_mail_token(normalized),),
    ).fetchone()


def touch_primary_recipient_link(record_id: int) -> bool:
    db = get_db()
    now = recipient_link_timestamp()
    db.execute(
        """
        UPDATE recipient_mail_links
        SET primary_access_count = COALESCE(primary_access_count, 0) + 1,
            last_accessed_at = ?
        WHERE id = ?
        """,
        (
            now,
            record_id,
        ),
    )
    db.commit()
    return True


@app.route("/api/v2/mailboxes/", defaults={"token": ""}, methods=["GET"])
@app.route("/api/v2/mailboxes/<token>", methods=["GET"])
@recipient_link_no_store
@csrf_exempt
def api_public_recipient_mailbox_messages(token: str):
    row = resolve_recipient_link_token(token)
    if row is None:
        return recipient_link_html_response("链接不存在", 404)

    expires_at = str(row["expires_at"] or "").strip()
    if expires_at and expires_at <= recipient_link_timestamp():
        return recipient_link_html_response("链接已过期", 410)

    if not row["bound_account_exists"]:
        return recipient_link_html_response("链接不存在", 404)

    if CLUSTER_CONFIG.is_replica:
        replica_state = _load_replica_state_with_repair()
        is_ready, error_code = replica_readiness(
            replica_state,
            datetime.now(timezone.utc),
            CLUSTER_CONFIG.max_stale_seconds,
        )
        if not is_ready:
            response = _replica_readiness_error_response(error_code, "html")
            response.headers["X-Robots-Tag"] = "noindex"
            return response

    account = get_account_by_id(int(row["account_id"]))
    if not account:
        return recipient_link_html_response("链接不存在", 404)

    if not CLUSTER_CONFIG.is_replica:
        try:
            touch_primary_recipient_link(int(row["id"]))
        except Exception:
            app.logger.exception("recipient mailbox access count update failed")
            try:
                get_db().rollback()
            except sqlite3.Error:
                app.logger.exception("recipient mailbox access count rollback failed")

    result = find_public_mailbox_messages(
        account,
        str(row["recipient_email_normalized"] or ""),
        1,
    )
    response = public_mailbox_html_result_response(result)
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def _recipient_link_escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _recipient_link_rows_by_ids(db, ids: list[int]) -> list[Any]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return db.execute(
        f"""
        SELECT *
        FROM recipient_mail_links
        WHERE id IN ({placeholders})
        ORDER BY id
        """,
        tuple(ids),
    ).fetchall()


def _recipient_link_rows_map_by_id(db, ids: list[int]) -> dict[int, Any]:
    rows = _recipient_link_rows_by_ids(db, ids)
    return {int(row["id"]): row for row in rows}


def _recipient_link_group_key(account_id: int, main_email: str) -> tuple[int, str]:
    return int(account_id), str(main_email or "").strip().lower()


def _recipient_link_group_rows(rows: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], dict[str, Any]] = {}
    order: list[tuple[int, str]] = []
    for row in rows:
        key = _recipient_link_group_key(row["account_id"], row["main_email_display"])
        if key not in grouped:
            grouped[key] = {
                "account_id": int(row["account_id"]),
                "main_email": str(row["main_email_display"] or "").strip(),
                "record_ids": [],
                "_record_ids_seen": set(),
            }
            order.append(key)
        group = grouped[key]
        record_id = int(row["id"])
        if record_id in group["_record_ids_seen"]:
            continue
        group["_record_ids_seen"].add(record_id)
        group["record_ids"].append(record_id)
    result: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        group.pop("_record_ids_seen", None)
        result.append(group)
    return result


def _recipient_link_txt_for_rows(rows: list[Any]) -> bytes:
    lines = [str(rows[0]["main_email_display"] or "").strip()] if rows else []
    for row in rows:
        token = decrypt_data(row["token_encrypted"] or "")
        lines.append(f"{row['recipient_email_display']}----{build_recipient_link_url(token)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _recipient_link_txt_for_import_records(main_email: str, records: list[dict[str, Any]]) -> bytes:
    lines = [str(main_email or "").strip()]
    for record in records:
        lines.append(f"{record['recipient_email_display']}----{build_recipient_link_url(record['token'])}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _recipient_link_import_failure_text(
    *,
    source_file: str,
    main_email: str,
    error_code: str,
    errors: list[dict[str, Any]] | None = None,
) -> bytes:
    lines = [
        "导入失败",
        f"文件：{source_file}",
    ]
    if main_email:
        lines.append(f"主邮箱：{main_email}")
    lines.append(f"原因：{RECIPIENT_LINK_ERROR_MESSAGES.get(error_code, '请求处理失败')}")
    for error in errors or []:
        value = str(error.get("value") or "").strip()
        line = error.get("line")
        code = str(error.get("error_code") or "").strip()
        detail = f"第{line}行" if line else "内容"
        if value:
            detail = f"{detail} {value}"
        if code:
            detail = f"{detail} ({code})"
        lines.append(detail)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _recipient_link_attachment_response(content: bytes, mimetype: str, filename: str):
    response = Response(content, content_type=mimetype)
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    response.headers["Cache-Control"] = "no-store"
    return response


def _recipient_link_source_export_name(
    source_file: Any,
    *,
    failed: bool = False,
    used_names: set[str] | None = None,
) -> str:
    source_name = Path(str(source_file or "")).name
    stem = source_name[:-4] if source_name.lower().endswith(".txt") else source_name
    prefix = "失败-" if failed else "api-"
    candidate = f"{prefix}{safe_export_stem(stem)}.txt"
    if used_names is None:
        return candidate
    suffix = 2
    while candidate in used_names:
        candidate = f"{prefix}{safe_export_stem(stem)}-{suffix}.txt"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _recipient_link_unique_export_name(base_name: str, used_names: set[str]) -> str:
    stem = safe_export_stem(base_name)
    candidate = f"{stem}.txt"
    suffix = 2
    while candidate in used_names:
        candidate = f"{stem}-{suffix}.txt"
        suffix += 1
    used_names.add(candidate)
    return candidate


def _recipient_link_export_payload_for_rows(rows: list[Any]) -> tuple[str, bytes, str]:
    rows = sorted(rows, key=lambda row: int(row["id"]))
    if len(rows) == 1:
        filename = f"{safe_export_stem(rows[0]['main_email_display'])}.txt"
        return filename, _recipient_link_txt_for_rows(rows), "text/plain; charset=utf-8"

    merged_groups = _recipient_link_group_rows(rows)
    if len(merged_groups) == 1:
        filename = f"{safe_export_stem(merged_groups[0]['main_email'])}.txt"
        return filename, _recipient_link_txt_for_rows(rows), "text/plain; charset=utf-8"

    used_names: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for group in merged_groups:
            group_rows = [row for row in rows if _recipient_link_group_key(row["account_id"], row["main_email_display"]) == _recipient_link_group_key(group["account_id"], group["main_email"])]
            name = _recipient_link_unique_export_name(group["main_email"], used_names)
            archive.writestr(name, _recipient_link_txt_for_rows(group_rows))
    return f"verification-links-{recipient_link_timestamp().replace(':', '').replace('-', '')}.zip", buffer.getvalue(), "application/zip"


@app.route("/verification-links", methods=["GET"])
@recipient_link_no_store
@login_required
def verification_links_management_page():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    return render_template("verification_links.html")


@app.route("/verification-links/manage", methods=["GET"])
@recipient_link_no_store
@login_required
def verification_links_imported_mailboxes_page():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    return render_template("verification_links_manage.html")


@app.route("/api/verification-links/main-mailboxes", methods=["GET"])
@recipient_link_no_store
@login_required
def api_search_recipient_link_main_mailboxes():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    try:
        limit = _recipient_link_parse_positive_int(
            request.args.get("limit", str(RECIPIENT_LINK_MAX_MAILBOX_OPTIONS)),
            code="invalid_page_size",
            minimum=1,
            maximum=RECIPIENT_LINK_MAX_MAILBOX_OPTIONS,
        )
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    query = _recipient_link_escape_like(str(request.args.get("q") or "").strip())
    params: list[Any] = []
    primary_filter = ""
    alias_filter = ""
    if query:
        primary_filter = "WHERE email COLLATE NOCASE LIKE ? ESCAPE '\\'"
        alias_filter = "WHERE alias_email COLLATE NOCASE LIKE ? ESCAPE '\\'"
        params.extend([f"%{query}%", f"%{query}%"])

    db = get_db()
    rows = db.execute(
        f"""
        SELECT value
        FROM (
            SELECT email AS value, LOWER(email) AS sort_key, 0 AS source_order
            FROM accounts
            {primary_filter}
            UNION
            SELECT alias_email AS value, LOWER(alias_email) AS sort_key, 1 AS source_order
            FROM account_aliases
            {alias_filter}
        )
        WHERE value IS NOT NULL AND TRIM(value) != ''
        ORDER BY source_order, sort_key
        LIMIT ?
        """,
        tuple(params) + (limit,),
    ).fetchall()
    return recipient_link_json_response(
        {
            "success": True,
            "items": [str(row["value"]).strip() for row in rows],
            "limit": limit,
        }
    )


@app.route("/api/verification-links", methods=["GET"])
@recipient_link_no_store
@login_required
def api_list_recipient_verification_links():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    try:
        page = _recipient_link_parse_positive_int(
            request.args.get("page", "1"), code="invalid_page", minimum=1, maximum=2_147_483_647
        )
        page_size = _recipient_link_parse_positive_int(
            request.args.get("page_size", "50"),
            code="invalid_page_size",
            minimum=1,
            maximum=RECIPIENT_LINK_MAX_PAGE_SIZE,
        )
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    status = str(request.args.get("status") or "all").strip().lower()
    if status not in {"all", "active", "expired"}:
        return recipient_link_json_error("invalid_status", 400)

    query = _recipient_link_escape_like(str(request.args.get("query") or "").strip())
    main_email = str(request.args.get("main_email") or "").strip()
    now = recipient_link_timestamp()
    clauses = []
    params: list[Any] = []
    if main_email:
        account = resolve_account_by_address(main_email)
        if account:
            clauses.append("account_id = ?")
            params.append(int(account["id"]))
        else:
            clauses.append("0 = 1")
    if query:
        clauses.append(
            "(main_email_display COLLATE NOCASE LIKE ? ESCAPE '\\' OR recipient_email_display COLLATE NOCASE LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{query}%", f"%{query}%"])
    if status == "active":
        clauses.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(now)
    elif status == "expired":
        clauses.append("(expires_at IS NOT NULL AND expires_at <= ?)")
        params.append(now)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    db = get_db()
    total = int(
        db.execute(
            f"SELECT COUNT(*) AS count FROM recipient_mail_links {where_sql}",
            tuple(params),
        ).fetchone()["count"]
        or 0
    )
    pages = 0 if total == 0 else (total + page_size - 1) // page_size
    offset = (page - 1) * page_size
    rows = db.execute(
        f"""
        SELECT *
        FROM recipient_mail_links
        {where_sql}
        ORDER BY main_email_display COLLATE NOCASE, recipient_email_display COLLATE NOCASE, id
        LIMIT ? OFFSET ?
        """,
        tuple(params) + (page_size, offset),
    ).fetchall()
    return recipient_link_json_response(
        {
            "success": True,
            "items": [serialize_recipient_link(row) for row in rows],
            "pagination": {"page": page, "page_size": page_size, "total": total, "pages": pages},
        }
    )


@app.route("/api/verification-links/settings", methods=["GET", "PUT"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_recipient_verification_link_settings():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    if request.method == "GET":
        configured = str(get_setting(RECIPIENT_LINK_SETTING, "") or "").strip()
        return recipient_link_json_response(
            {
                "success": True,
                "configured_base_url": configured,
                "effective_base_url": effective_recipient_link_base_url(),
            }
        )

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload.keys()) != {"base_url"}:
        return recipient_link_json_error("invalid_export_input", 400)

    raw_base_url = payload.get("base_url")
    if raw_base_url is None or str(raw_base_url).strip() == "":
        if not set_setting(RECIPIENT_LINK_SETTING, ""):
            return recipient_link_json_error("import_failed", 500)
    else:
        try:
            normalized = normalize_public_base_url(raw_base_url)
        except RecipientLinkInputError as exc:
            return recipient_link_json_error(exc.code, 400)
        if not set_setting(RECIPIENT_LINK_SETTING, normalized):
            return recipient_link_json_error("import_failed", 500)

    return recipient_link_json_response(
        {
            "success": True,
            "configured_base_url": str(get_setting(RECIPIENT_LINK_SETTING, "") or "").strip(),
            "effective_base_url": effective_recipient_link_base_url(),
        }
    )


def _recipient_link_mutate_rows(ids: list[int], *, expires_at: str | None = None, delete: bool = False):
    db = get_db()
    db.execute("BEGIN")
    try:
        rows = _recipient_link_rows_by_ids(db, ids)
        if len(rows) != len(ids):
            db.rollback()
            return None
        now = recipient_link_timestamp()
        if delete:
            db.execute(
                f"DELETE FROM recipient_mail_links WHERE id IN ({','.join('?' for _ in ids)})",
                tuple(ids),
            )
        else:
            for row_id in ids:
                db.execute(
                    """
                    UPDATE recipient_mail_links
                    SET expires_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (expires_at, now, row_id),
                )
        db.commit()
        return rows
    except Exception:
        db.rollback()
        raise


@app.route("/api/verification-links/main-mailbox", methods=["DELETE"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_delete_recipient_verification_links_by_main_mailbox():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload.keys()) != {"main_email"}:
        return recipient_link_json_error("invalid_export_input", 400)

    main_email = str(payload.get("main_email") or "").strip()
    if not main_email:
        return recipient_link_json_error("main_mailbox_required", 400)

    account = resolve_account_by_address(main_email)
    if account is None:
        return recipient_link_json_error("main_mailbox_not_found", 404)

    db = get_db()
    db.execute("BEGIN")
    try:
        cursor = db.execute(
            "DELETE FROM recipient_mail_links WHERE account_id = ?",
            (int(account["id"]),),
        )
        db.commit()
        return recipient_link_json_response(
            {
                "success": True,
                "account_id": int(account["id"]),
                "main_email": main_email,
                "deleted_count": int(cursor.rowcount or 0),
            }
        )
    except Exception:
        db.rollback()
        raise


@app.route("/api/verification-links/<int:link_id>", methods=["PATCH", "DELETE"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_manage_recipient_verification_link(link_id: int):
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    if request.method == "DELETE":
        rows = _recipient_link_mutate_rows([link_id], delete=True)
        if rows is None:
            return recipient_link_json_error("invalid_record_ids", 404)
        return recipient_link_json_response({"success": True, "affected_ids": [link_id]})

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload.keys()) != {"expires_at"}:
        return recipient_link_json_error("invalid_record_ids", 400)

    try:
        expires_at = parse_absolute_expiry(payload.get("expires_at"))
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    rows = _recipient_link_mutate_rows([link_id], expires_at=expires_at, delete=False)
    if rows is None:
        return recipient_link_json_error("invalid_record_ids", 404)
    refreshed = get_db().execute(
        "SELECT * FROM recipient_mail_links WHERE id = ?",
        (link_id,),
    ).fetchone()
    return recipient_link_json_response({"success": True, "item": serialize_recipient_link(refreshed)})


@app.route("/api/verification-links/batch-expiry", methods=["POST"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_batch_expire_recipient_verification_links():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload.keys()) != {"ids", "expires_at"}:
        return recipient_link_json_error("invalid_export_input", 400)

    try:
        ids = parse_recipient_link_ids(payload.get("ids"))
        expires_at = parse_absolute_expiry(payload.get("expires_at"))
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    rows = _recipient_link_mutate_rows(ids, expires_at=expires_at, delete=False)
    if rows is None:
        return recipient_link_json_error("invalid_record_ids", 404)
    return recipient_link_json_response({"success": True, "affected_ids": ids})


@app.route("/api/verification-links/batch-delete", methods=["POST"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_batch_delete_recipient_verification_links():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or set(payload.keys()) != {"ids"}:
        return recipient_link_json_error("invalid_export_input", 400)

    try:
        ids = parse_recipient_link_ids(payload.get("ids"))
    except RecipientLinkInputError as exc:
        return recipient_link_json_error(exc.code, 400)

    rows = _recipient_link_mutate_rows(ids, delete=True)
    if rows is None:
        return recipient_link_json_error("invalid_record_ids", 404)
    return recipient_link_json_response({"success": True, "affected_ids": ids})


def _recipient_link_validate_export_groups(db, groups_payload: list[Any]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    all_ids: list[int] = []
    seen_ids: set[int] = set()

    for group in groups_payload:
        if not isinstance(group, dict):
            raise RecipientLinkInputError("invalid_export_input")
        if set(group.keys()) != {"main_email", "account_id", "record_ids"}:
            raise RecipientLinkInputError("invalid_export_input")
        account_id = _recipient_link_parse_positive_int(
            group.get("account_id"), code="invalid_export_input", minimum=1, maximum=2_147_483_647
        )
        main_email = str(group.get("main_email") or "").strip()
        if not main_email:
            raise RecipientLinkInputError("invalid_export_input")
        record_ids = parse_recipient_link_ids(group.get("record_ids"))
        prepared.append(
            {
                "account_id": account_id,
                "main_email": main_email,
                "record_ids": record_ids,
            }
        )
        for record_id in record_ids:
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            all_ids.append(record_id)

    rows_map = _recipient_link_rows_map_by_id(db, all_ids)
    if len(rows_map) != len(all_ids):
        raise RecipientLinkInputError("invalid_record_ids")

    merged: dict[tuple[int, str], dict[str, Any]] = {}
    order: list[tuple[int, str]] = []
    for group in prepared:
        for record_id in group["record_ids"]:
            row = rows_map[record_id]
            if int(row["account_id"]) != group["account_id"]:
                raise RecipientLinkInputError("invalid_group_binding")
            if str(row["main_email_display"] or "").strip().lower() != group["main_email"].lower():
                raise RecipientLinkInputError("invalid_group_binding")
            key = _recipient_link_group_key(group["account_id"], group["main_email"])
            if key not in merged:
                merged[key] = {
                    "account_id": group["account_id"],
                    "main_email": group["main_email"],
                    "record_ids": [],
                    "_seen": set(),
                }
                order.append(key)
            bucket = merged[key]
            if record_id in bucket["_seen"]:
                continue
            bucket["_seen"].add(record_id)
            bucket["record_ids"].append(record_id)

    result: list[dict[str, Any]] = []
    for key in order:
        bucket = merged[key]
        bucket.pop("_seen", None)
        result.append(bucket)
    return result


def _recipient_link_export_response_from_rows(rows: list[Any]):
    if not rows:
        return None
    if len(rows) == 1:
        filename = f"{safe_export_stem(rows[0]['main_email_display'])}.txt"
        return _recipient_link_attachment_response(
            _recipient_link_txt_for_rows(rows),
            "text/plain; charset=utf-8",
            filename,
        )

    groups = _recipient_link_group_rows(rows)
    if len(groups) == 1:
        filename = f"{safe_export_stem(groups[0]['main_email'])}.txt"
        return _recipient_link_attachment_response(
            _recipient_link_txt_for_rows(rows),
            "text/plain; charset=utf-8",
            filename,
        )

    used_names: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for group in groups:
            group_rows = [
                row
                for row in rows
                if _recipient_link_group_key(row["account_id"], row["main_email_display"])
                == _recipient_link_group_key(group["account_id"], group["main_email"])
            ]
            archive.writestr(
                _recipient_link_unique_export_name(group["main_email"], used_names),
                _recipient_link_txt_for_rows(group_rows),
            )
    timestamp = recipient_link_now().strftime("%Y%m%d-%H%M%S")
    return _recipient_link_attachment_response(
        buffer.getvalue(),
        "application/zip",
        f"verification-links-{timestamp}.zip",
    )


@app.route("/api/verification-links/export", methods=["POST"])
@recipient_link_no_store
@csrf_exempt
@login_required
def api_export_recipient_verification_links():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return recipient_link_json_error("invalid_export_input", 400)

    has_ids = "ids" in payload and payload.get("ids") is not None
    has_groups = "groups" in payload and payload.get("groups") is not None
    if has_ids == has_groups:
        return recipient_link_json_error("invalid_export_input", 400)

    db = get_db()
    try:
        if has_ids:
            ids = parse_recipient_link_ids(payload.get("ids"))
            rows = _recipient_link_rows_by_ids(db, ids)
            if len(rows) != len(ids):
                return recipient_link_json_error("invalid_record_ids", 404)
            return _recipient_link_export_response_from_rows(rows)

        groups_payload = payload.get("groups")
        if not isinstance(groups_payload, list) or not groups_payload:
            return recipient_link_json_error("invalid_export_input", 400)
        prepared_groups = _recipient_link_validate_export_groups(db, groups_payload)
        if not prepared_groups:
            return recipient_link_json_error("invalid_export_input", 400)
        row_ids: list[int] = []
        for group in prepared_groups:
            row_ids.extend(group["record_ids"])
        rows = _recipient_link_rows_by_ids(db, row_ids)
        if len(rows) != len(row_ids):
            return recipient_link_json_error("invalid_record_ids", 404)
        return _recipient_link_export_response_from_rows(rows)
    except RecipientLinkInputError as exc:
        status = 404 if exc.code == "invalid_record_ids" else 400
        return recipient_link_json_error(exc.code, status)
