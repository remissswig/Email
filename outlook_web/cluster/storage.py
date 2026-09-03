import base64
import hashlib
import hmac
import re
import sqlite3
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .crypto import generate_x25519_keypair, public_key_fingerprint


REPLICATED_SETTING_KEYS = frozenset({
    'public_mailbox_api_key_auth_enabled',
    'mailboxes_messages_scanned_count',
})
MAX_INCREMENT_EVENTS = 500
CLUSTER_PROTOCOL_VERSION = 3
_IDENTITY_KEY_VERSION = 1
_RFC3339_UTC_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$'
)
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_ACCOUNT_PAYLOAD_KEYS = (
    'id',
    'email',
    'password',
    'client_id',
    'refresh_token',
    'status',
    'account_type',
    'provider',
    'imap_host',
    'imap_port',
    'imap_password',
    'proxy_url',
    'fallback_proxy_url_1',
    'fallback_proxy_url_2',
    'recipient_share_segment',
    'created_at',
    'updated_at',
)
_ALIAS_PAYLOAD_KEYS = ('id', 'account_id', 'alias_email', 'created_at', 'updated_at')
_API_KEY_PAYLOAD_KEYS = ('id', 'key_digest', 'key_suffix', 'expires_at', 'account_id', 'created_at')
_RECIPIENT_LINK_PAYLOAD_KEYS = (
    'id',
    'account_id',
    'main_email_display',
    'recipient_email_display',
    'recipient_email_normalized',
    'token_hash',
    'expires_at',
    'created_at',
    'updated_at',
)
_SNAPSHOT_PAYLOAD_KEYS = ('snapshot_cursor', 'accounts', 'aliases', 'api_keys', 'recipient_links', 'settings')
_INCREMENT_PAYLOAD_KEYS = (
    'from_cursor',
    'next_cursor',
    'accounts',
    'aliases',
    'api_keys',
    'recipient_links',
    'settings',
    'deletes',
)
_DELETE_PAYLOAD_KEYS = ('entity_type', 'entity_id')
_ENTITY_TYPES = frozenset({'account', 'alias', 'api_key', 'recipient_link', 'setting'})
_DELETE_ORDER = {
    'recipient_link': 0,
    'api_key': 1,
    'alias': 2,
    'account': 3,
    'setting': 4,
}


class ReplicaApplyError(ValueError):
    """Raised when a replica payload cannot be validated or applied atomically."""


def _require_exact_dict(value: Any, expected_keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be an object')
    actual_keys = set(value.keys())
    if actual_keys != set(expected_keys):
        raise ValueError(f'{label} has unexpected keys')
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f'{label} must be a list')
    return value


def _require_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f'{label} must be an integer')
    if value < 0:
        raise ValueError(f'{label} must be non-negative')
    return value


def _parse_stored_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f'{label} must be an integer')
    if isinstance(value, int):
        return _require_non_negative_int(value, label)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.isdigit():
            return _require_non_negative_int(int(candidate), label)
    raise TypeError(f'{label} must be an integer')


def _require_bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    normalized = _require_non_negative_int(value, label)
    if normalized < minimum or normalized > maximum:
        raise ValueError(f'{label} must be between {minimum} and {maximum}')
    return normalized


def _require_text(
    value: Any,
    label: str,
    *,
    allow_none: bool = False,
    max_length: int = 4096,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f'{label} must be a string')
    if not isinstance(value, str):
        raise ValueError(f'{label} must be a string')
    if len(value) > max_length:
        raise ValueError(f'{label} is too long')
    return value


def _require_rfc3339_text(value: Any, label: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f'{label} must be a UTC RFC3339 string')
    text = _require_text(value, label, max_length=64)
    if text is None or not _RFC3339_UTC_RE.match(text):
        raise ValueError(f'{label} must be a UTC RFC3339 string')
    datetime.fromisoformat(text.replace('Z', '+00:00'))
    return text


def _normalize_stored_timestamp(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 64:
            raise ValueError(f'{label} must be a valid timestamp')
        try:
            candidate = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError(f'{label} must be a valid timestamp') from exc
    else:
        raise ValueError(f'{label} must be a valid timestamp')
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    else:
        candidate = candidate.astimezone(timezone.utc)
    return candidate.isoformat().replace('+00:00', 'Z')


def _normalize_email(value: Any, label: str) -> str:
    text = _require_text(value, label, max_length=320)
    assert text is not None
    normalized = text.strip().lower()
    if text != normalized or not _EMAIL_RE.match(normalized):
        raise ValueError(f'{label} must be a normalized email address')
    return normalized


def _normalize_stored_email(value: Any, label: str) -> str:
    text = _require_text(value, label, max_length=320)
    assert text is not None
    normalized = text.strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise ValueError(f'{label} must be a valid email address')
    return normalized


def _validate_recipient_share_segment(value: Any) -> str:
    segment = _require_text(value, 'account.recipient_share_segment', max_length=128)
    assert segment is not None
    if segment and not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', segment):
        raise ValueError('account.recipient_share_segment must be base64url text')
    return segment


def _normalize_setting_value(key: str, value: Any) -> str:
    text = _require_text(value, f'settings.{key}', max_length=64)
    assert text is not None
    if key == 'public_mailbox_api_key_auth_enabled':
        if text not in {'true', 'false'}:
            raise ValueError('settings.public_mailbox_api_key_auth_enabled must be true or false')
        return text
    if key == 'mailboxes_messages_scanned_count':
        if not text.isdigit():
            raise ValueError('settings.mailboxes_messages_scanned_count must be decimal digits')
        count = int(text)
        if count < 1 or count > 10000:
            raise ValueError('settings.mailboxes_messages_scanned_count must be between 1 and 10000')
        return str(count)
    raise ValueError('unknown replicated setting')


def _decrypt_if_present(value: Any, decrypt_sensitive: Callable[[str], str]) -> Any:
    if value in (None, ''):
        return value
    return decrypt_sensitive(str(value))


def _encrypt_if_present(value: Any, encrypt_sensitive: Callable[[str], str]) -> Any:
    if value in (None, ''):
        return value
    return str(encrypt_sensitive(str(value)))


def _serialize_account_row(row: sqlite3.Row, decrypt_sensitive: Callable[[str], str]) -> dict[str, Any]:
    return {
        'id': _require_non_negative_int(row['id'], 'account.id'),
        'email': _normalize_stored_email(row['email'], 'account.email'),
        'password': _decrypt_if_present(row['password'], decrypt_sensitive),
        'client_id': _require_text(row['client_id'] or '', 'account.client_id'),
        'refresh_token': _decrypt_if_present(row['refresh_token'], decrypt_sensitive),
        'status': _require_text(row['status'] or '', 'account.status', max_length=128),
        'account_type': _require_text(row['account_type'] or '', 'account.account_type', max_length=128),
        'provider': _require_text(row['provider'] or '', 'account.provider', max_length=128),
        'imap_host': _require_text(row['imap_host'] or '', 'account.imap_host', max_length=512),
        'imap_port': _require_non_negative_int(row['imap_port'], 'account.imap_port'),
        'imap_password': _decrypt_if_present(row['imap_password'], decrypt_sensitive),
        'proxy_url': _require_text(row['proxy_url'] or '', 'account.proxy_url', max_length=2048),
        'fallback_proxy_url_1': _require_text(row['fallback_proxy_url_1'] or '', 'account.fallback_proxy_url_1', max_length=2048),
        'fallback_proxy_url_2': _require_text(row['fallback_proxy_url_2'] or '', 'account.fallback_proxy_url_2', max_length=2048),
        'recipient_share_segment': _validate_recipient_share_segment(row['recipient_share_segment'] or ''),
        'created_at': _normalize_stored_timestamp(row['created_at'], 'account.created_at'),
        'updated_at': _normalize_stored_timestamp(row['updated_at'], 'account.updated_at'),
    }


def _serialize_alias_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'id': _require_non_negative_int(row['id'], 'alias.id'),
        'account_id': _require_non_negative_int(row['account_id'], 'alias.account_id'),
        'alias_email': _normalize_stored_email(row['alias_email'], 'alias.alias_email'),
        'created_at': _normalize_stored_timestamp(row['created_at'], 'alias.created_at'),
        'updated_at': _normalize_stored_timestamp(row['updated_at'], 'alias.updated_at'),
    }


def _serialize_api_key_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'id': _require_non_negative_int(row['id'], 'api_key.id'),
        'key_digest': _require_text(row['key_digest'], 'api_key.key_digest', max_length=512),
        'key_suffix': _require_text(row['key_suffix'] or '', 'api_key.key_suffix', max_length=64),
        'expires_at': _normalize_stored_timestamp(row['expires_at'], 'api_key.expires_at'),
        'account_id': (
            None
            if row['account_id'] is None
            else _require_non_negative_int(row['account_id'], 'api_key.account_id')
        ),
        'created_at': _normalize_stored_timestamp(row['created_at'], 'api_key.created_at'),
    }


def _serialize_recipient_link_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'id': _require_non_negative_int(row['id'], 'recipient_link.id'),
        'account_id': _require_non_negative_int(row['account_id'], 'recipient_link.account_id'),
        'main_email_display': _require_text(row['main_email_display'], 'recipient_link.main_email_display', max_length=320),
        'recipient_email_display': _require_text(row['recipient_email_display'], 'recipient_link.recipient_email_display', max_length=320),
        'recipient_email_normalized': _require_text(row['recipient_email_normalized'], 'recipient_link.recipient_email_normalized', max_length=320),
        'token_hash': _require_text(row['token_hash'], 'recipient_link.token_hash', max_length=64),
        'expires_at': _normalize_stored_timestamp(row['expires_at'], 'recipient_link.expires_at'),
        'created_at': _normalize_stored_timestamp(row['created_at'], 'recipient_link.created_at'),
        'updated_at': _normalize_stored_timestamp(row['updated_at'], 'recipient_link.updated_at'),
    }


def _fetch_snapshot_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        'SELECT COALESCE(MAX(cursor), 0) AS cursor FROM replication_events'
    ).fetchone()
    return _require_non_negative_int(_row_value(row, 'cursor', 0), 'snapshot cursor')


def _select_rows_by_ids(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    ids: list[int],
    columns_sql: str,
) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    placeholders = ', '.join('?' for _ in ids)
    rows = conn.execute(
        f'SELECT {columns_sql} FROM {table} WHERE {id_column} IN ({placeholders})',
        tuple(ids),
    ).fetchall()
    return {
        _require_non_negative_int(_row_value(row, id_column), f'{table}.{id_column}'): row
        for row in rows
    }


def _run_in_read_transaction(conn: sqlite3.Connection, callback: Callable[[], Any]) -> Any:
    started_transaction = False
    if not conn.in_transaction:
        conn.execute('BEGIN')
        started_transaction = True
    try:
        return callback()
    finally:
        if started_transaction:
            conn.rollback()


def _validate_account_payload(item: Any) -> dict[str, Any]:
    payload = _require_exact_dict(item, _ACCOUNT_PAYLOAD_KEYS, 'account payload')
    return {
        'id': _require_non_negative_int(payload['id'], 'account.id'),
        'email': _normalize_email(payload['email'], 'account.email'),
        'password': _require_text(payload['password'], 'account.password', allow_none=True, max_length=4096),
        'client_id': _require_text(payload['client_id'], 'account.client_id', max_length=512),
        'refresh_token': _require_text(payload['refresh_token'], 'account.refresh_token', allow_none=True, max_length=4096),
        'status': _require_text(payload['status'], 'account.status', max_length=128),
        'account_type': _require_text(payload['account_type'], 'account.account_type', max_length=128),
        'provider': _require_text(payload['provider'], 'account.provider', max_length=128),
        'imap_host': _require_text(payload['imap_host'], 'account.imap_host', max_length=512),
        'imap_port': _require_bounded_int(payload['imap_port'], 'account.imap_port', 0, 65535),
        'imap_password': _require_text(payload['imap_password'], 'account.imap_password', allow_none=True, max_length=4096),
        'proxy_url': _require_text(payload['proxy_url'], 'account.proxy_url', max_length=2048),
        'fallback_proxy_url_1': _require_text(payload['fallback_proxy_url_1'], 'account.fallback_proxy_url_1', max_length=2048),
        'fallback_proxy_url_2': _require_text(payload['fallback_proxy_url_2'], 'account.fallback_proxy_url_2', max_length=2048),
        'recipient_share_segment': _validate_recipient_share_segment(payload['recipient_share_segment']),
        'created_at': _require_rfc3339_text(payload['created_at'], 'account.created_at'),
        'updated_at': _require_rfc3339_text(payload['updated_at'], 'account.updated_at'),
    }


def _validate_alias_payload(item: Any) -> dict[str, Any]:
    payload = _require_exact_dict(item, _ALIAS_PAYLOAD_KEYS, 'alias payload')
    return {
        'id': _require_non_negative_int(payload['id'], 'alias.id'),
        'account_id': _require_non_negative_int(payload['account_id'], 'alias.account_id'),
        'alias_email': _normalize_email(payload['alias_email'], 'alias.alias_email'),
        'created_at': _require_rfc3339_text(payload['created_at'], 'alias.created_at'),
        'updated_at': _require_rfc3339_text(payload['updated_at'], 'alias.updated_at'),
    }


def _validate_api_key_payload(item: Any) -> dict[str, Any]:
    payload = _require_exact_dict(item, _API_KEY_PAYLOAD_KEYS, 'api_key payload')
    account_id = payload['account_id']
    return {
        'id': _require_non_negative_int(payload['id'], 'api_key.id'),
        'key_digest': _require_text(payload['key_digest'], 'api_key.key_digest', max_length=512),
        'key_suffix': _require_text(payload['key_suffix'], 'api_key.key_suffix', max_length=64),
        'expires_at': _require_rfc3339_text(payload['expires_at'], 'api_key.expires_at', allow_none=True),
        'account_id': None if account_id is None else _require_non_negative_int(account_id, 'api_key.account_id'),
        'created_at': _require_rfc3339_text(payload['created_at'], 'api_key.created_at'),
    }


def _validate_recipient_link_payload(item: Any) -> dict[str, Any]:
    payload = _require_exact_dict(item, _RECIPIENT_LINK_PAYLOAD_KEYS, 'recipient_link payload')
    main_email_display = _require_text(payload['main_email_display'], 'recipient_link.main_email_display', max_length=320)
    assert main_email_display is not None
    _normalize_stored_email(main_email_display, 'recipient_link.main_email_display')
    recipient_email_display = _require_text(
        payload['recipient_email_display'],
        'recipient_link.recipient_email_display',
        max_length=320,
    )
    assert recipient_email_display is not None
    _normalize_stored_email(recipient_email_display, 'recipient_link.recipient_email_display')
    recipient_email_normalized = _require_text(
        payload['recipient_email_normalized'],
        'recipient_link.recipient_email_normalized',
        max_length=320,
    )
    assert recipient_email_normalized is not None
    normalized_display = recipient_email_display.strip().lower()
    if recipient_email_normalized != normalized_display:
        raise ValueError('recipient_link.recipient_email_normalized must match recipient_email_display')
    token_hash = _require_text(payload['token_hash'], 'recipient_link.token_hash', max_length=64)
    assert token_hash is not None
    if not re.fullmatch(r'[0-9a-f]{64}', token_hash):
        raise ValueError('recipient_link.token_hash must be a lowercase hex digest')
    account_id = _require_non_negative_int(payload['account_id'], 'recipient_link.account_id')
    return {
        'id': _require_non_negative_int(payload['id'], 'recipient_link.id'),
        'account_id': account_id,
        'main_email_display': main_email_display,
        'recipient_email_display': recipient_email_display,
        'recipient_email_normalized': normalized_display,
        'token_hash': token_hash,
        'expires_at': _require_rfc3339_text(payload['expires_at'], 'recipient_link.expires_at', allow_none=True),
        'created_at': _require_rfc3339_text(payload['created_at'], 'recipient_link.created_at'),
        'updated_at': _require_rfc3339_text(payload['updated_at'], 'recipient_link.updated_at'),
    }


def _validate_settings_payload(item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        raise ValueError('settings must be an object')
    normalized: dict[str, str] = {}
    for key, value in item.items():
        if key not in REPLICATED_SETTING_KEYS:
            raise ValueError('settings contains unknown replicated key')
        normalized[key] = _normalize_setting_value(key, value)
    return normalized


def _validate_delete_payload(item: Any) -> dict[str, Any]:
    payload = _require_exact_dict(item, _DELETE_PAYLOAD_KEYS, 'delete payload')
    entity_type = _require_text(payload['entity_type'], 'delete.entity_type', max_length=32)
    if entity_type not in _ENTITY_TYPES:
        raise ValueError('delete.entity_type is invalid')
    if entity_type == 'setting':
        entity_id = _require_text(payload['entity_id'], 'delete.entity_id', max_length=128)
        if entity_id not in REPLICATED_SETTING_KEYS:
            raise ValueError('delete.entity_id is invalid')
    else:
        entity_id = _require_non_negative_int(payload['entity_id'], 'delete.entity_id')
    return {
        'entity_type': entity_type,
        'entity_id': entity_id,
    }


def _validate_snapshot_payload(payload: Any) -> dict[str, Any]:
    normalized = _require_exact_dict(payload, _SNAPSHOT_PAYLOAD_KEYS, 'snapshot payload')
    accounts = [_validate_account_payload(item) for item in _require_list(normalized['accounts'], 'snapshot.accounts')]
    aliases = [_validate_alias_payload(item) for item in _require_list(normalized['aliases'], 'snapshot.aliases')]
    api_keys = [_validate_api_key_payload(item) for item in _require_list(normalized['api_keys'], 'snapshot.api_keys')]
    recipient_links = [
        _validate_recipient_link_payload(item)
        for item in _require_list(normalized['recipient_links'], 'snapshot.recipient_links')
    ]
    settings = _validate_settings_payload(normalized['settings'])
    account_ids = {item['id'] for item in accounts}
    _ensure_no_duplicate_ids(accounts, 'account')
    _ensure_no_duplicate_ids(aliases, 'alias')
    _ensure_no_duplicate_ids(api_keys, 'api_key')
    _ensure_no_duplicate_ids(recipient_links, 'recipient_link')
    _ensure_no_duplicate_api_key_digests(api_keys)
    _ensure_valid_snapshot_foreign_keys(aliases, api_keys, recipient_links, account_ids)
    return {
        'snapshot_cursor': _require_non_negative_int(normalized['snapshot_cursor'], 'snapshot_cursor'),
        'accounts': accounts,
        'aliases': aliases,
        'api_keys': api_keys,
        'recipient_links': recipient_links,
        'settings': settings,
    }


def _validate_increment_payload(payload: Any, conn: sqlite3.Connection) -> dict[str, Any]:
    normalized = _require_exact_dict(payload, _INCREMENT_PAYLOAD_KEYS, 'increment payload')
    accounts = [_validate_account_payload(item) for item in _require_list(normalized['accounts'], 'increment.accounts')]
    aliases = [_validate_alias_payload(item) for item in _require_list(normalized['aliases'], 'increment.aliases')]
    api_keys = [_validate_api_key_payload(item) for item in _require_list(normalized['api_keys'], 'increment.api_keys')]
    recipient_links = [
        _validate_recipient_link_payload(item)
        for item in _require_list(normalized['recipient_links'], 'increment.recipient_links')
    ]
    settings = _validate_settings_payload(normalized['settings'])
    deletes = [_validate_delete_payload(item) for item in _require_list(normalized['deletes'], 'increment.deletes')]
    _ensure_no_duplicate_ids(accounts, 'account')
    _ensure_no_duplicate_ids(aliases, 'alias')
    _ensure_no_duplicate_ids(api_keys, 'api_key')
    _ensure_no_duplicate_ids(recipient_links, 'recipient_link')
    _ensure_no_duplicate_api_key_digests(api_keys)
    _ensure_no_duplicate_deletes(deletes)
    total_items = len(accounts) + len(aliases) + len(api_keys) + len(recipient_links) + len(settings) + len(deletes)
    if total_items > MAX_INCREMENT_EVENTS:
        raise ValueError('increment payload exceeds maximum size')
    from_cursor = _require_non_negative_int(normalized['from_cursor'], 'from_cursor')
    next_cursor = _require_non_negative_int(normalized['next_cursor'], 'next_cursor')
    if next_cursor < from_cursor:
        raise ValueError('next_cursor must be greater than or equal to from_cursor')
    _ensure_valid_increment_foreign_keys(conn, accounts, aliases, api_keys, recipient_links, deletes)
    return {
        'from_cursor': from_cursor,
        'next_cursor': next_cursor,
        'accounts': accounts,
        'aliases': aliases,
        'api_keys': api_keys,
        'recipient_links': recipient_links,
        'settings': settings,
        'deletes': deletes,
    }


def _ensure_no_duplicate_ids(items: list[dict[str, Any]], entity_name: str) -> None:
    seen: set[int] = set()
    for item in items:
        item_id = item['id']
        if item_id in seen:
            raise ValueError(f'duplicate {entity_name} id')
        seen.add(item_id)


def _ensure_no_duplicate_api_key_digests(items: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for item in items:
        digest = item['key_digest']
        if digest in seen:
            raise ValueError('duplicate api_key key_digest')
        seen.add(digest)


def _ensure_no_duplicate_deletes(items: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, Any]] = set()
    for item in items:
        key = (item['entity_type'], item['entity_id'])
        if key in seen:
            raise ValueError('duplicate delete tombstone')
        seen.add(key)


def _ensure_valid_snapshot_foreign_keys(
    aliases: list[dict[str, Any]],
    api_keys: list[dict[str, Any]],
    recipient_links: list[dict[str, Any]],
    account_ids: set[int],
) -> None:
    for alias in aliases:
        if alias['account_id'] not in account_ids:
            raise ValueError('alias references missing account')
    for api_key in api_keys:
        if api_key['account_id'] is not None and api_key['account_id'] not in account_ids:
            raise ValueError('api_key references missing account')
    for recipient_link in recipient_links:
        if recipient_link['account_id'] not in account_ids:
            raise ValueError('recipient_link references missing account')


def _ensure_valid_increment_foreign_keys(
    conn: sqlite3.Connection,
    accounts: list[dict[str, Any]],
    aliases: list[dict[str, Any]],
    api_keys: list[dict[str, Any]],
    recipient_links: list[dict[str, Any]],
    deletes: list[dict[str, Any]],
) -> None:
    payload_account_ids = {item['id'] for item in accounts}
    local_account_ids = {
        _require_non_negative_int(_row_value(row, 'id', 0), 'accounts.id')
        for row in conn.execute('SELECT id FROM accounts').fetchall()
    }
    deleted_account_ids = {
        item['entity_id']
        for item in deletes
        if item['entity_type'] == 'account'
    }
    available_account_ids = (local_account_ids | payload_account_ids) - deleted_account_ids
    for alias in aliases:
        if alias['account_id'] not in available_account_ids:
            raise ValueError('alias references missing account')
    for api_key in api_keys:
        account_id = api_key['account_id']
        if account_id is not None and account_id not in available_account_ids:
            raise ValueError('api_key references missing account')
    for recipient_link in recipient_links:
        if recipient_link['account_id'] not in available_account_ids:
            raise ValueError('recipient_link references missing account')


def _ensure_utc_datetime(value: Any) -> datetime:
    if callable(value):
        value = value()
    if not isinstance(value, datetime):
        raise TypeError('now must return a datetime')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_iso_text(value: Any) -> str:
    return _ensure_utc_datetime(value).isoformat().replace('+00:00', 'Z')


def _parse_utc_iso_text(value: Any, label: str) -> datetime | None:
    text = _normalize_stored_timestamp(value, label)
    if text is None:
        return None
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _encode_public_key_text(public_key: bytes) -> str:
    return base64.b64encode(public_key).decode('ascii')


def _cluster_node_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        'id': _require_text(_row_value(row, 'node_id', 1), 'cluster_node.node_id', max_length=128),
        'name': _require_text(_row_value(row, 'name', 2), 'cluster_node.name', max_length=256),
        'remark': _require_text(_row_value(row, 'remark', 3) or '', 'cluster_node.remark', max_length=512) or '',
        'status': _require_text(_row_value(row, 'status', 8), 'cluster_node.status', max_length=32),
        'created_at': _normalize_stored_timestamp(_row_value(row, 'created_at', 9), 'cluster_node.created_at'),
        'enrolled_at': _normalize_stored_timestamp(_row_value(row, 'enrolled_at', 10), 'cluster_node.enrolled_at'),
        'last_seen_at': _normalize_stored_timestamp(_row_value(row, 'last_seen_at', 11), 'cluster_node.last_seen_at'),
        'last_sync_at': _normalize_stored_timestamp(_row_value(row, 'last_sync_at', 12), 'cluster_node.last_sync_at'),
        'revoked_at': _normalize_stored_timestamp(_row_value(row, 'revoked_at', 13), 'cluster_node.revoked_at'),
        'source_ip': _require_text(_row_value(row, 'source_ip', 14) or '', 'cluster_node.source_ip', max_length=128) or '',
        'last_ack_cursor': _require_non_negative_int(_row_value(row, 'last_ack_cursor', 15), 'cluster_node.last_ack_cursor'),
        'credential_version': _require_non_negative_int(_row_value(row, 'credential_version', 7), 'cluster_node.credential_version'),
        'replica_app_version': _require_text(_row_value(row, 'replica_app_version', 16) or '', 'cluster_node.replica_app_version', max_length=128) or '',
        'replica_protocol_version': (
            None
            if _row_value(row, 'replica_protocol_version', 17) is None
            else _require_non_negative_int(_row_value(row, 'replica_protocol_version', 17), 'cluster_node.replica_protocol_version')
        ),
        'last_error_sanitized': _require_text(_row_value(row, 'last_error_sanitized', 18) or '', 'cluster_node.last_error_sanitized', max_length=4096) or '',
    }


def _event_timestamp_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _row_value(row: Any, *candidates: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        for candidate in candidates:
            if isinstance(candidate, str):
                try:
                    return row[candidate]
                except (IndexError, KeyError):
                    continue
            if isinstance(candidate, int):
                try:
                    return row[candidate]
                except IndexError:
                    continue
        return None
    for candidate in candidates:
        if isinstance(candidate, int):
            return row[candidate]
    return None


def _create_primary_tables(conn: sqlite3.Connection) -> None:
    statements = (
        '''
        CREATE TABLE IF NOT EXISTS cluster_identity (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            encrypted_private_key TEXT NOT NULL,
            public_key TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            key_version INTEGER NOT NULL
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS cluster_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE,
            remark TEXT DEFAULT '',
            public_key TEXT NOT NULL,
            encrypted_sync_secret TEXT,
            secret_digest TEXT,
            credential_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'revoked')),
            created_at TEXT NOT NULL,
            enrolled_at TEXT,
            last_seen_at TEXT,
            last_sync_at TEXT,
            revoked_at TEXT,
            source_ip TEXT DEFAULT '',
            last_ack_cursor INTEGER NOT NULL DEFAULT 0,
            replica_app_version TEXT DEFAULT '',
            replica_protocol_version INTEGER,
            last_error_sanitized TEXT DEFAULT '',
            previous_encrypted_sync_secret TEXT,
            previous_secret_digest TEXT,
            previous_credential_version INTEGER,
            previous_secret_expires_at TEXT
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS cluster_enrollment_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_digest TEXT NOT NULL UNIQUE,
            node_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (node_id) REFERENCES cluster_nodes(node_id) ON DELETE CASCADE
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS cluster_request_nonces (
            node_id TEXT NOT NULL,
            nonce_digest TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            PRIMARY KEY (node_id, nonce_digest)
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS replication_events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        ''',
        '''
        CREATE INDEX IF NOT EXISTS idx_cluster_nodes_status
        ON cluster_nodes(status, created_at)
        ''',
        '''
        CREATE INDEX IF NOT EXISTS idx_cluster_enrollment_tokens_node_id
        ON cluster_enrollment_tokens(node_id, expires_at)
        ''',
        '''
        CREATE INDEX IF NOT EXISTS idx_cluster_enrollment_tokens_expires_at
        ON cluster_enrollment_tokens(expires_at)
        ''',
        '''
        CREATE INDEX IF NOT EXISTS idx_cluster_request_nonces_expires_at
        ON cluster_request_nonces(expires_at)
        ''',
        '''
        CREATE INDEX IF NOT EXISTS idx_replication_events_created_at
        ON replication_events(created_at, cursor)
        ''',
    )
    for statement in statements:
        conn.execute(statement)
    existing_columns = {
        str(_row_value(row, 'name', 1))
        for row in conn.execute("PRAGMA table_info('cluster_nodes')").fetchall()
    }
    if 'source_ip' not in existing_columns:
        conn.execute("ALTER TABLE cluster_nodes ADD COLUMN source_ip TEXT DEFAULT ''")


def _initialize_primary_identity(
    conn: sqlite3.Connection,
    encrypt_sensitive: Callable[[str], str],
    now: Callable[[], datetime] | datetime,
) -> dict[str, Any]:
    row = conn.execute(
        '''
        SELECT public_key, fingerprint, created_at, key_version
        FROM cluster_identity
        WHERE id = 1
        '''
    ).fetchone()
    if row is not None:
        return {
            'public_key': _row_value(row, 'public_key', 0),
            'fingerprint': _row_value(row, 'fingerprint', 1),
            'created_at': _row_value(row, 'created_at', 2),
            'key_version': _row_value(row, 'key_version', 3),
        }

    private_key, public_key = generate_x25519_keypair()
    private_key_text = base64.b64encode(private_key).decode('ascii')
    public_key_text = base64.b64encode(public_key).decode('ascii')
    encrypted_private_key = str(encrypt_sensitive(private_key_text) or '')
    if not encrypted_private_key.startswith('enc:'):
        raise ValueError('encrypt_sensitive must return an enc:-prefixed value')

    created_at = _utc_iso_text(now)
    fingerprint = public_key_fingerprint(public_key)
    conn.execute(
        '''
        INSERT OR IGNORE INTO cluster_identity (
            id,
            encrypted_private_key,
            public_key,
            fingerprint,
            created_at,
            key_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (
            1,
            encrypted_private_key,
            public_key_text,
            fingerprint,
            created_at,
            _IDENTITY_KEY_VERSION,
        ),
    )
    current_row = conn.execute(
        '''
        SELECT public_key, fingerprint, created_at, key_version
        FROM cluster_identity
        WHERE id = 1
        '''
    ).fetchone()
    if current_row is None:
        raise RuntimeError('cluster identity row missing after initialization')
    return {
        'public_key': _row_value(current_row, 'public_key', 0),
        'fingerprint': _row_value(current_row, 'fingerprint', 1),
        'created_at': _row_value(current_row, 'created_at', 2),
        'key_version': _row_value(current_row, 'key_version', 3),
    }


def _install_replication_triggers(conn: sqlite3.Connection) -> None:
    allowlisted_keys = ', '.join(f"'{key}'" for key in sorted(REPLICATED_SETTING_KEYS))
    ts_expr = _event_timestamp_sql()
    statements = (
        'DROP TRIGGER IF EXISTS cluster_replication_accounts_insert',
        f'''
        CREATE TRIGGER cluster_replication_accounts_insert
        AFTER INSERT ON accounts
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('account', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_accounts_update',
        f'''
        CREATE TRIGGER cluster_replication_accounts_update
        AFTER UPDATE ON accounts
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('account', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_accounts_delete',
        f'''
        CREATE TRIGGER cluster_replication_accounts_delete
        AFTER DELETE ON accounts
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('account', CAST(OLD.id AS TEXT), 'delete', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_account_aliases_insert',
        f'''
        CREATE TRIGGER cluster_replication_account_aliases_insert
        AFTER INSERT ON account_aliases
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('alias', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_account_aliases_update',
        f'''
        CREATE TRIGGER cluster_replication_account_aliases_update
        AFTER UPDATE ON account_aliases
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('alias', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_account_aliases_delete',
        f'''
        CREATE TRIGGER cluster_replication_account_aliases_delete
        AFTER DELETE ON account_aliases
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('alias', CAST(OLD.id AS TEXT), 'delete', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_public_mailbox_api_keys_insert',
        f'''
        CREATE TRIGGER cluster_replication_public_mailbox_api_keys_insert
        AFTER INSERT ON public_mailbox_api_keys
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('api_key', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_public_mailbox_api_keys_update',
        f'''
        CREATE TRIGGER cluster_replication_public_mailbox_api_keys_update
        AFTER UPDATE ON public_mailbox_api_keys
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('api_key', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_public_mailbox_api_keys_delete',
        f'''
        CREATE TRIGGER cluster_replication_public_mailbox_api_keys_delete
        AFTER DELETE ON public_mailbox_api_keys
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('api_key', CAST(OLD.id AS TEXT), 'delete', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_recipient_mail_links_insert',
        f'''
        CREATE TRIGGER cluster_replication_recipient_mail_links_insert
        AFTER INSERT ON recipient_mail_links
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('recipient_link', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_recipient_mail_links_update',
        f'''
        CREATE TRIGGER cluster_replication_recipient_mail_links_update
        AFTER UPDATE OF account_id, main_email_display, recipient_email_display, recipient_email_normalized,
            token_hash, expires_at, created_at, updated_at ON recipient_mail_links
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('recipient_link', CAST(NEW.id AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_recipient_mail_links_delete',
        f'''
        CREATE TRIGGER cluster_replication_recipient_mail_links_delete
        AFTER DELETE ON recipient_mail_links
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('recipient_link', CAST(OLD.id AS TEXT), 'delete', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_settings_insert',
        f'''
        CREATE TRIGGER cluster_replication_settings_insert
        AFTER INSERT ON settings
        WHEN NEW.key IN ({allowlisted_keys})
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('setting', CAST(NEW.key AS TEXT), 'upsert', {ts_expr});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_settings_update',
        f'''
        CREATE TRIGGER cluster_replication_settings_update
        AFTER UPDATE ON settings
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            SELECT 'setting', CAST(OLD.key AS TEXT), 'delete', {ts_expr}
            WHERE OLD.key IN ({allowlisted_keys}) AND OLD.key <> NEW.key;

            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            SELECT 'setting', CAST(NEW.key AS TEXT), 'upsert', {ts_expr}
            WHERE NEW.key IN ({allowlisted_keys});
        END
        ''',
        'DROP TRIGGER IF EXISTS cluster_replication_settings_delete',
        f'''
        CREATE TRIGGER cluster_replication_settings_delete
        AFTER DELETE ON settings
        WHEN OLD.key IN ({allowlisted_keys})
        BEGIN
            INSERT INTO replication_events (entity_type, entity_id, operation, created_at)
            VALUES ('setting', CAST(OLD.key AS TEXT), 'delete', {ts_expr});
        END
        ''',
    )
    for statement in statements:
        conn.execute(statement)


def _require_epoch_cutoff(now_epoch: Any) -> int:
    if isinstance(now_epoch, bool) or not isinstance(now_epoch, int):
        raise TypeError('now_epoch must be an integer')
    return now_epoch


def _require_utc_rfc3339_cutoff(cutoff_iso: Any) -> str:
    if not isinstance(cutoff_iso, str):
        raise TypeError('cutoff_iso must be a UTC RFC3339 string')
    candidate = cutoff_iso.strip()
    if not _RFC3339_UTC_RE.match(candidate):
        raise ValueError('cutoff_iso must be a UTC RFC3339 string')
    datetime.fromisoformat(candidate.replace('Z', '+00:00'))
    return candidate


def initialize_replica_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS cluster_replica_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        '''
    )
    conn.execute(
        '''
        INSERT OR IGNORE INTO cluster_replica_state (key, value)
        VALUES ('cursor', '0')
        '''
    )
    conn.execute(
        '''
        INSERT INTO cluster_replica_state (key, value)
        VALUES ('protocol_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''',
        (str(CLUSTER_PROTOCOL_VERSION),),
    )
    conn.commit()


def initialize_primary_schema(
    conn: sqlite3.Connection,
    encrypt_sensitive: Callable[[str], str],
    now: Callable[[], datetime] | datetime,
) -> dict[str, Any]:
    _create_primary_tables(conn)
    identity = _initialize_primary_identity(conn, encrypt_sensitive, now)
    _install_replication_triggers(conn)
    return identity


def list_cluster_nodes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        ORDER BY created_at, id
        '''
    ).fetchall()
    return [_cluster_node_row_to_dict(row) for row in rows]


def create_cluster_node(conn: sqlite3.Connection, name: str, remark: str | None, now: Any) -> dict[str, Any]:
    normalized_name = _require_text(name, 'name', max_length=256).strip()
    if not normalized_name:
        raise ValueError('name is required')
    normalized_remark = _require_text(remark or '', 'remark', allow_none=True, max_length=512) or ''
    created_at = _utc_iso_text(now)

    while True:
        node_id = f'node-{secrets.token_urlsafe(32)}'
        try:
            conn.execute(
                '''
                INSERT INTO cluster_nodes (
                    node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
                    credential_version, status, created_at, enrolled_at, last_seen_at,
                    last_sync_at, revoked_at, source_ip, last_ack_cursor, replica_app_version,
                    replica_protocol_version, last_error_sanitized, previous_encrypted_sync_secret,
                    previous_secret_digest, previous_credential_version, previous_secret_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    node_id,
                    normalized_name,
                    normalized_remark,
                    '',
                    None,
                    None,
                    1,
                    'pending',
                    created_at,
                    None,
                    None,
                    None,
                    None,
                    '',
                    0,
                    '',
                    None,
                    '',
                    None,
                    None,
                    None,
                    None,
                ),
            )
            break
        except sqlite3.IntegrityError as exc:
            if conn.execute(
                'SELECT 1 FROM cluster_nodes WHERE name = ?',
                (normalized_name,),
            ).fetchone() is not None:
                raise ValueError('duplicate node name') from exc
            continue

    row = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (node_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError('cluster node row missing after creation')
    return _cluster_node_row_to_dict(row)


def issue_enrollment_token(
    conn: sqlite3.Connection,
    node_id: str,
    now: datetime,
) -> tuple[str, datetime]:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    created_at = _ensure_utc_datetime(now)
    expires_at = created_at + timedelta(minutes=10)
    token = secrets.token_urlsafe(32)
    token_digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
    conn.execute(
        '''
        INSERT INTO cluster_enrollment_tokens (
            token_digest, node_id, expires_at, consumed_at, created_at
        ) VALUES (?, ?, ?, NULL, ?)
        ''',
        (
            token_digest,
            normalized_node_id,
            _utc_iso_text(expires_at),
            _utc_iso_text(created_at),
        ),
    )
    conn.commit()
    return token, expires_at


def consume_enrollment_token(
    conn: sqlite3.Connection,
    node_id: str,
    token: str,
    now: datetime,
) -> bool:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    normalized_token = _require_text(token, 'token', max_length=512)
    now_dt = _ensure_utc_datetime(now)
    candidate_digest = hashlib.sha256(normalized_token.encode('utf-8')).hexdigest()
    rows = conn.execute(
        '''
        SELECT id, token_digest, expires_at, consumed_at
        FROM cluster_enrollment_tokens
        WHERE node_id = ?
        ORDER BY expires_at, id
        ''',
        (normalized_node_id,),
    ).fetchall()

    for row in rows:
        consumed_at = _row_value(row, 'consumed_at', 3)
        if consumed_at is not None:
            continue
        expires_at = _parse_utc_iso_text(_row_value(row, 'expires_at', 2), 'cluster_enrollment_tokens.expires_at')
        if expires_at is None or expires_at <= now_dt:
            continue
        token_digest = _require_text(_row_value(row, 'token_digest', 1), 'cluster_enrollment_tokens.token_digest', max_length=128)
        if hmac.compare_digest(token_digest, candidate_digest):
            conn.execute(
                '''
                UPDATE cluster_enrollment_tokens
                SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                ''',
                (_utc_iso_text(now_dt), _require_non_negative_int(_row_value(row, 'id', 0), 'cluster_enrollment_tokens.id')),
            )
            return True
    return False


def activate_cluster_node(
    conn: sqlite3.Connection,
    node_id: str,
    node_public_key: bytes,
    encrypted_sync_secret: str,
    secret_digest: str,
    now: datetime,
) -> dict[str, Any]:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    if not isinstance(node_public_key, bytes) or len(node_public_key) != 32:
        raise ValueError('node_public_key must be raw x25519 bytes')
    public_key_fingerprint(node_public_key)
    encrypted_sync_secret_text = _require_text(encrypted_sync_secret, 'encrypted_sync_secret', max_length=16384)
    secret_digest_text = _require_text(secret_digest, 'secret_digest', max_length=128)
    enrolled_at = _utc_iso_text(now)

    cursor = conn.execute(
        '''
        UPDATE cluster_nodes
        SET
            public_key = ?,
            encrypted_sync_secret = ?,
            secret_digest = ?,
            credential_version = 1,
            status = 'active',
            enrolled_at = ?,
            last_error_sanitized = '',
            revoked_at = NULL,
            previous_encrypted_sync_secret = NULL,
            previous_secret_digest = NULL,
            previous_credential_version = NULL,
            previous_secret_expires_at = NULL
        WHERE node_id = ? AND status = 'pending'
        ''',
        (
            _encode_public_key_text(node_public_key),
            encrypted_sync_secret_text,
            secret_digest_text,
            enrolled_at,
            normalized_node_id,
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        raise ValueError('cluster node is not pending')

    row = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError('cluster node row missing after activation')
    return _cluster_node_row_to_dict(row)


def register_cluster_request_nonce(
    conn: sqlite3.Connection,
    node_id: str,
    nonce_digest: str,
    expires_at_epoch: int,
    now: datetime,
) -> bool:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    normalized_digest = _require_text(nonce_digest, 'nonce_digest', max_length=128)
    expires_at = _require_non_negative_int(expires_at_epoch, 'expires_at_epoch')
    created_at = _utc_iso_text(now)
    try:
        conn.execute(
            '''
            INSERT INTO cluster_request_nonces (
                node_id, nonce_digest, expires_at, created_at
            ) VALUES (?, ?, ?, ?)
            ''',
            (
                normalized_node_id,
                normalized_digest,
                expires_at,
                created_at,
            ),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def touch_cluster_node(
    conn: sqlite3.Connection,
    node_id: str,
    now: datetime,
    *,
    protocol_version: int | None = None,
    mark_synced: bool = False,
    source_ip: str | None = None,
) -> None:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    touched_at = _utc_iso_text(now)
    normalized_protocol = (
        None
        if protocol_version is None
        else _require_non_negative_int(protocol_version, 'protocol_version')
    )
    normalized_source_ip = _require_text(source_ip or '', 'source_ip', max_length=128) or ''
    if mark_synced:
        conn.execute(
            '''
            UPDATE cluster_nodes
            SET
                last_seen_at = ?,
                last_sync_at = ?,
                source_ip = ?,
                replica_protocol_version = COALESCE(?, replica_protocol_version),
                last_error_sanitized = ''
            WHERE node_id = ?
            ''',
            (
                touched_at,
                touched_at,
                normalized_source_ip,
                normalized_protocol,
                normalized_node_id,
            ),
        )
        return
    conn.execute(
        '''
        UPDATE cluster_nodes
        SET
            last_seen_at = ?,
            source_ip = ?,
            replica_protocol_version = COALESCE(?, replica_protocol_version),
            last_error_sanitized = ''
        WHERE node_id = ?
        ''',
        (
            touched_at,
            normalized_source_ip,
            normalized_protocol,
            normalized_node_id,
        ),
    )


def update_cluster_node_ack(
    conn: sqlite3.Connection,
    node_id: str,
    cursor: int,
    now: datetime,
    *,
    credential_version: int,
) -> dict[str, Any]:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    ack_cursor = _require_non_negative_int(cursor, 'cursor')
    active_credential_version = _require_non_negative_int(
        credential_version,
        'credential_version',
    )
    row = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if row is None:
        raise ValueError('cluster node missing')
    current_ack = _require_non_negative_int(
        _row_value(row, 'last_ack_cursor', 14),
        'cluster_node.last_ack_cursor',
    )
    if ack_cursor < current_ack:
        raise ValueError('cluster ack cursor regression')

    current_credential_version = _require_non_negative_int(
        _row_value(row, 'credential_version', 7),
        'cluster_node.credential_version',
    )
    clear_previous = (
        active_credential_version == current_credential_version
        and _row_value(row, 'previous_credential_version', 20) is not None
    )
    seen_at = _utc_iso_text(now)

    if clear_previous:
        conn.execute(
            '''
            UPDATE cluster_nodes
            SET
                last_ack_cursor = ?,
                last_seen_at = ?,
                previous_encrypted_sync_secret = NULL,
                previous_secret_digest = NULL,
                previous_credential_version = NULL,
                previous_secret_expires_at = NULL,
                last_error_sanitized = ''
            WHERE node_id = ?
            ''',
            (
                ack_cursor,
                seen_at,
                normalized_node_id,
            ),
        )
    else:
        conn.execute(
            '''
            UPDATE cluster_nodes
            SET
                last_ack_cursor = ?,
                last_seen_at = ?,
                last_error_sanitized = ''
            WHERE node_id = ?
            ''',
            (
                ack_cursor,
                seen_at,
                normalized_node_id,
            ),
        )

    updated = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if updated is None:
        raise RuntimeError('cluster node row missing after ack update')
    return _cluster_node_row_to_dict(updated)


def rotate_cluster_node_secret(
    conn: sqlite3.Connection,
    node_id: str,
    encrypted_sync_secret: str,
    secret_digest: str,
    now: datetime,
    *,
    grace_period_seconds: int = 600,
) -> dict[str, Any]:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    encrypted_sync_secret_text = _require_text(
        encrypted_sync_secret,
        'encrypted_sync_secret',
        max_length=16384,
    )
    secret_digest_text = _require_text(secret_digest, 'secret_digest', max_length=128)
    grace_seconds = _require_non_negative_int(grace_period_seconds, 'grace_period_seconds')
    row = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if row is None:
        raise ValueError('cluster node missing')
    status = _require_text(_row_value(row, 'status', 8), 'cluster_node.status', max_length=32)
    if status == 'revoked':
        raise ValueError('cluster node revoked')
    if status != 'active':
        raise ValueError('cluster node is not active')
    current_credential_version = _require_non_negative_int(
        _row_value(row, 'credential_version', 7),
        'cluster_node.credential_version',
    )
    rotated_at = _ensure_utc_datetime(now)
    previous_secret_expires_at = _utc_iso_text(
        rotated_at + timedelta(seconds=grace_seconds)
    )
    conn.execute(
        '''
        UPDATE cluster_nodes
        SET
            previous_encrypted_sync_secret = encrypted_sync_secret,
            previous_secret_digest = secret_digest,
            previous_credential_version = credential_version,
            previous_secret_expires_at = ?,
            encrypted_sync_secret = ?,
            secret_digest = ?,
            credential_version = ?,
            last_error_sanitized = ''
        WHERE node_id = ?
        ''',
        (
            previous_secret_expires_at,
            encrypted_sync_secret_text,
            secret_digest_text,
            current_credential_version + 1,
            normalized_node_id,
        ),
    )
    updated = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if updated is None:
        raise RuntimeError('cluster node row missing after rotation')
    return _cluster_node_row_to_dict(updated)


def revoke_cluster_node(
    conn: sqlite3.Connection,
    node_id: str,
    now: datetime,
) -> dict[str, Any]:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    row = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if row is None:
        raise ValueError('cluster node missing')
    status = _require_text(_row_value(row, 'status', 8), 'cluster_node.status', max_length=32)
    if status == 'revoked':
        raise ValueError('cluster node revoked')
    conn.execute(
        '''
        UPDATE cluster_nodes
        SET
            status = 'revoked',
            revoked_at = ?,
            last_error_sanitized = ''
        WHERE node_id = ?
        ''',
        (
            _utc_iso_text(now),
            normalized_node_id,
        ),
    )
    updated = conn.execute(
        '''
        SELECT
            id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
            credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
            revoked_at, source_ip, last_ack_cursor, replica_app_version, replica_protocol_version,
            last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
            previous_credential_version, previous_secret_expires_at
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (normalized_node_id,),
    ).fetchone()
    if updated is None:
        raise RuntimeError('cluster node row missing after revoke')
    return _cluster_node_row_to_dict(updated)


def delete_cluster_node(conn: sqlite3.Connection, node_id: str) -> bool:
    normalized_node_id = _require_text(node_id, 'node_id', max_length=128)
    row = conn.execute(
        'SELECT status FROM cluster_nodes WHERE node_id = ?',
        (normalized_node_id,),
    ).fetchone()
    if row is None:
        raise ValueError('cluster node missing')
    status = _require_text(_row_value(row, 'status', 0), 'cluster_node.status', max_length=32)
    if status != 'revoked':
        raise ValueError('cluster node delete requires revoked')
    cursor = conn.execute(
        'DELETE FROM cluster_nodes WHERE node_id = ?',
        (normalized_node_id,),
    )
    return int(cursor.rowcount or 0) == 1


def read_replica_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        '''
        SELECT value
        FROM cluster_replica_state
        WHERE key = 'cursor'
        '''
    ).fetchone()
    if row is None:
        raise ValueError('replica cursor state is missing')
    raw_value = _row_value(row, 'value', 0)
    if isinstance(raw_value, bool):
        raise ValueError('replica cursor state is invalid')
    try:
        cursor = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('replica cursor state is invalid') from exc
    if cursor < 0:
        raise ValueError('replica cursor state is invalid')
    return cursor


def build_snapshot(
    conn: sqlite3.Connection,
    decrypt_sensitive: Callable[[str], str],
) -> dict[str, Any]:
    def _read_snapshot() -> dict[str, Any]:
        snapshot_cursor = _fetch_snapshot_cursor(conn)
        account_rows = conn.execute(
            '''
            SELECT
                id, email, password, client_id, refresh_token, status,
                account_type, provider, imap_host, imap_port, imap_password,
                proxy_url, fallback_proxy_url_1, fallback_proxy_url_2,
                recipient_share_segment, created_at, updated_at
            FROM accounts
            ORDER BY id
            '''
        ).fetchall()
        alias_rows = conn.execute(
            '''
            SELECT id, account_id, alias_email, created_at, updated_at
            FROM account_aliases
            ORDER BY id
            '''
        ).fetchall()
        api_key_rows = conn.execute(
            '''
            SELECT id, key_digest, key_suffix, expires_at, account_id, created_at
            FROM public_mailbox_api_keys
            ORDER BY id
            '''
        ).fetchall()
        recipient_link_rows = conn.execute(
            '''
            SELECT
                id, account_id, main_email_display, recipient_email_display,
                recipient_email_normalized, token_hash, expires_at, created_at, updated_at
            FROM recipient_mail_links
            ORDER BY id
            '''
        ).fetchall()
        setting_rows = conn.execute(
            '''
            SELECT key, value
            FROM settings
            WHERE key IN (?, ?)
            ORDER BY key
            ''',
            tuple(sorted(REPLICATED_SETTING_KEYS)),
        ).fetchall()
        return {
            'snapshot_cursor': snapshot_cursor,
            'accounts': [_serialize_account_row(row, decrypt_sensitive) for row in account_rows],
            'aliases': [_serialize_alias_row(row) for row in alias_rows],
            'api_keys': [_serialize_api_key_row(row) for row in api_key_rows],
            'recipient_links': [_serialize_recipient_link_row(row) for row in recipient_link_rows],
            'settings': {
                _row_value(row, 'key', 0): _normalize_setting_value(
                    _row_value(row, 'key', 0),
                    _row_value(row, 'value', 1),
                )
                for row in setting_rows
            },
        }

    return _run_in_read_transaction(conn, _read_snapshot)


def build_increment(
    conn: sqlite3.Connection,
    cursor: int,
    limit: int,
    decrypt_sensitive: Callable[[str], str],
) -> dict[str, Any]:
    normalized_cursor = _require_non_negative_int(cursor, 'cursor')
    normalized_limit = _require_bounded_int(limit, 'limit', 1, MAX_INCREMENT_EVENTS)

    def _read_increment() -> dict[str, Any]:
        rows = conn.execute(
            '''
            SELECT cursor, entity_type, entity_id, operation
            FROM replication_events
            WHERE cursor > ?
            ORDER BY cursor
            LIMIT ?
            ''',
            (normalized_cursor, normalized_limit),
        ).fetchall()
        if not rows:
            return {
                'from_cursor': normalized_cursor,
                'next_cursor': normalized_cursor,
                'accounts': [],
                'aliases': [],
                'api_keys': [],
                'recipient_links': [],
                'settings': {},
                'deletes': [],
            }

        latest_by_entity: dict[tuple[str, Any], dict[str, Any]] = {}
        for row in rows:
            entity_type = _require_text(_row_value(row, 'entity_type', 1), 'event.entity_type', max_length=32)
            operation = _require_text(_row_value(row, 'operation', 3), 'event.operation', max_length=16)
            if entity_type not in _ENTITY_TYPES:
                raise ValueError('replication event entity_type is invalid')
            if operation not in {'upsert', 'delete'}:
                raise ValueError('replication event operation is invalid')
            raw_entity_id = _row_value(row, 'entity_id', 2)
            entity_id: Any
            if entity_type == 'setting':
                entity_id = _require_text(raw_entity_id, 'event.entity_id', max_length=128)
                if entity_id not in REPLICATED_SETTING_KEYS:
                    raise ValueError('replication event entity_id is invalid')
            else:
                entity_id = _parse_stored_non_negative_int(raw_entity_id, 'event.entity_id')
            latest_by_entity[(entity_type, entity_id)] = {
                'entity_type': entity_type,
                'entity_id': entity_id,
                'operation': operation,
                'cursor': _require_non_negative_int(_row_value(row, 'cursor', 0), 'event.cursor'),
            }

        account_rows = _select_rows_by_ids(
            conn,
            'accounts',
            'id',
            [item['entity_id'] for item in latest_by_entity.values() if item['entity_type'] == 'account' and item['operation'] == 'upsert'],
            '''
            id, email, password, client_id, refresh_token, status,
            account_type, provider, imap_host, imap_port, imap_password,
            proxy_url, fallback_proxy_url_1, fallback_proxy_url_2,
            recipient_share_segment, created_at, updated_at
            ''',
        )
        alias_rows = _select_rows_by_ids(
            conn,
            'account_aliases',
            'id',
            [item['entity_id'] for item in latest_by_entity.values() if item['entity_type'] == 'alias' and item['operation'] == 'upsert'],
            'id, account_id, alias_email, created_at, updated_at',
        )
        api_key_rows = _select_rows_by_ids(
            conn,
            'public_mailbox_api_keys',
            'id',
            [item['entity_id'] for item in latest_by_entity.values() if item['entity_type'] == 'api_key' and item['operation'] == 'upsert'],
            'id, key_digest, key_suffix, expires_at, account_id, created_at',
        )
        recipient_link_rows = _select_rows_by_ids(
            conn,
            'recipient_mail_links',
            'id',
            [
                item['entity_id']
                for item in latest_by_entity.values()
                if item['entity_type'] == 'recipient_link' and item['operation'] == 'upsert'
            ],
            '''
            id, account_id, main_email_display, recipient_email_display,
            recipient_email_normalized, token_hash, expires_at, created_at, updated_at
            ''',
        )

        accounts: list[dict[str, Any]] = []
        aliases: list[dict[str, Any]] = []
        api_keys: list[dict[str, Any]] = []
        recipient_links: list[dict[str, Any]] = []
        settings: dict[str, str] = {}
        deletes: list[dict[str, Any]] = []

        for item in sorted(latest_by_entity.values(), key=lambda current: current['cursor']):
            if item['operation'] == 'delete':
                deletes.append({
                    'entity_type': item['entity_type'],
                    'entity_id': item['entity_id'],
                })
                continue

            if item['entity_type'] == 'account':
                row = account_rows.get(item['entity_id'])
                if row is not None:
                    accounts.append(_serialize_account_row(row, decrypt_sensitive))
                else:
                    deletes.append({
                        'entity_type': 'account',
                        'entity_id': item['entity_id'],
                    })
            elif item['entity_type'] == 'alias':
                row = alias_rows.get(item['entity_id'])
                if row is not None:
                    aliases.append(_serialize_alias_row(row))
                else:
                    deletes.append({
                        'entity_type': 'alias',
                        'entity_id': item['entity_id'],
                    })
            elif item['entity_type'] == 'api_key':
                row = api_key_rows.get(item['entity_id'])
                if row is not None:
                    api_keys.append(_serialize_api_key_row(row))
                else:
                    deletes.append({
                        'entity_type': 'api_key',
                        'entity_id': item['entity_id'],
                    })
            elif item['entity_type'] == 'recipient_link':
                row = recipient_link_rows.get(item['entity_id'])
                if row is not None:
                    recipient_links.append(_serialize_recipient_link_row(row))
                else:
                    deletes.append({
                        'entity_type': 'recipient_link',
                        'entity_id': item['entity_id'],
                    })
            else:
                row = conn.execute(
                    'SELECT value FROM settings WHERE key = ?',
                    (item['entity_id'],),
                ).fetchone()
                if row is not None:
                    settings[item['entity_id']] = _normalize_setting_value(
                        item['entity_id'],
                        _row_value(row, 'value', 0),
                    )
                else:
                    deletes.append({
                        'entity_type': 'setting',
                        'entity_id': item['entity_id'],
                    })

        return {
            'from_cursor': normalized_cursor,
            'next_cursor': _require_non_negative_int(_row_value(rows[-1], 'cursor', 0), 'next_cursor'),
            'accounts': accounts,
            'aliases': aliases,
            'api_keys': api_keys,
            'recipient_links': recipient_links,
            'settings': settings,
            'deletes': deletes,
        }

    return _run_in_read_transaction(conn, _read_increment)


def _collect_existing_api_key_last_used(conn: sqlite3.Connection) -> tuple[dict[int, Any], dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT id, key_digest, last_used_at
        FROM public_mailbox_api_keys
        '''
    ).fetchall()
    by_id: dict[int, Any] = {}
    by_digest: dict[str, Any] = {}
    for row in rows:
        key_id = _require_non_negative_int(_row_value(row, 'id', 0), 'api_key.id')
        digest = _require_text(_row_value(row, 'key_digest', 1), 'api_key.key_digest', max_length=512)
        last_used_at = _row_value(row, 'last_used_at', 2)
        by_id[key_id] = last_used_at
        if digest is not None:
            by_digest[digest] = last_used_at
    return by_id, by_digest


def _begin_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f'SAVEPOINT {name}')


def _rollback_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f'ROLLBACK TO SAVEPOINT {name}')
    conn.execute(f'RELEASE SAVEPOINT {name}')


def _release_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f'RELEASE SAVEPOINT {name}')


def _set_replica_cursor(conn: sqlite3.Connection, cursor: int) -> None:
    result = conn.execute(
        '''
        UPDATE cluster_replica_state
        SET value = ?
        WHERE key = 'cursor'
        ''',
        (str(cursor),),
    )
    if int(result.rowcount or 0) != 1:
        raise ValueError('replica cursor state is missing')


def _apply_account_upserts(
    conn: sqlite3.Connection,
    accounts: list[dict[str, Any]],
    encrypt_sensitive: Callable[[str], str],
) -> None:
    for account in accounts:
        conn.execute(
            '''
            INSERT INTO accounts (
                id, email, password, client_id, refresh_token, group_id, status,
                account_type, provider, imap_host, imap_port, imap_password,
                proxy_url, fallback_proxy_url_1, fallback_proxy_url_2,
                recipient_share_segment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                password = excluded.password,
                client_id = excluded.client_id,
                refresh_token = excluded.refresh_token,
                group_id = excluded.group_id,
                status = excluded.status,
                account_type = excluded.account_type,
                provider = excluded.provider,
                imap_host = excluded.imap_host,
                imap_port = excluded.imap_port,
                imap_password = excluded.imap_password,
                proxy_url = excluded.proxy_url,
                fallback_proxy_url_1 = excluded.fallback_proxy_url_1,
                fallback_proxy_url_2 = excluded.fallback_proxy_url_2,
                recipient_share_segment = excluded.recipient_share_segment,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            ''',
            (
                account['id'],
                account['email'],
                _encrypt_if_present(account['password'], encrypt_sensitive),
                account['client_id'],
                _encrypt_if_present(account['refresh_token'], encrypt_sensitive),
                None,
                account['status'],
                account['account_type'],
                account['provider'],
                account['imap_host'],
                account['imap_port'],
                _encrypt_if_present(account['imap_password'], encrypt_sensitive),
                account['proxy_url'],
                account['fallback_proxy_url_1'],
                account['fallback_proxy_url_2'],
                account['recipient_share_segment'],
                account['created_at'],
                account['updated_at'],
            ),
        )


def _apply_alias_upserts(conn: sqlite3.Connection, aliases: list[dict[str, Any]]) -> None:
    for alias in aliases:
        conn.execute(
            '''
            INSERT INTO account_aliases (
                id, account_id, alias_email, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_id = excluded.account_id,
                alias_email = excluded.alias_email,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            ''',
            (
                alias['id'],
                alias['account_id'],
                alias['alias_email'],
                alias['created_at'],
                alias['updated_at'],
            ),
        )


def _apply_recipient_link_upserts(conn: sqlite3.Connection, recipient_links: list[dict[str, Any]]) -> None:
    for recipient_link in recipient_links:
        conn.execute(
            '''
            INSERT INTO recipient_mail_links (
                id, account_id, main_email_display, recipient_email_display,
                recipient_email_normalized, token_hash, token_encrypted,
                expires_at, primary_access_count, last_accessed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '', ?, 0, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_id = excluded.account_id,
                main_email_display = excluded.main_email_display,
                recipient_email_display = excluded.recipient_email_display,
                recipient_email_normalized = excluded.recipient_email_normalized,
                token_hash = excluded.token_hash,
                token_encrypted = '',
                expires_at = excluded.expires_at,
                primary_access_count = 0,
                last_accessed_at = NULL,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            ''',
            (
                recipient_link['id'],
                recipient_link['account_id'],
                recipient_link['main_email_display'],
                recipient_link['recipient_email_display'],
                recipient_link['recipient_email_normalized'],
                recipient_link['token_hash'],
                recipient_link['expires_at'],
                recipient_link['created_at'],
                recipient_link['updated_at'],
            ),
        )


def _upsert_api_key_row(
    conn: sqlite3.Connection,
    api_key: dict[str, Any],
    preserved_last_used_by_id: dict[int, Any],
    preserved_last_used_by_digest: dict[str, Any],
) -> None:
    last_used_at = preserved_last_used_by_id.get(api_key['id'])
    if last_used_at is None:
        last_used_at = preserved_last_used_by_digest.get(api_key['key_digest'])

    same_id_row = conn.execute(
        '''
        SELECT id, key_digest
        FROM public_mailbox_api_keys
        WHERE id = ?
        ''',
        (api_key['id'],),
    ).fetchone()
    if same_id_row is not None:
        conn.execute(
            '''
            UPDATE public_mailbox_api_keys
            SET name = '',
                remark = '',
                encrypted_key = '',
                key_digest = ?,
                key_suffix = ?,
                expires_at = ?,
                created_at = ?,
                last_used_at = ?,
                account_id = ?
            WHERE id = ?
            ''',
            (
                api_key['key_digest'],
                api_key['key_suffix'],
                api_key['expires_at'],
                api_key['created_at'],
                last_used_at,
                api_key['account_id'],
                api_key['id'],
            ),
        )
        return

    same_digest_row = conn.execute(
        '''
        SELECT id
        FROM public_mailbox_api_keys
        WHERE key_digest = ?
        ''',
        (api_key['key_digest'],),
    ).fetchone()
    if same_digest_row is not None:
        current_id = _require_non_negative_int(_row_value(same_digest_row, 'id', 0), 'api_key.id')
        if current_id != api_key['id']:
            conn.execute(
                '''
                DELETE FROM public_mailbox_api_keys
                WHERE id = ?
                ''',
                (api_key['id'],),
            )
        conn.execute(
            '''
            UPDATE public_mailbox_api_keys
            SET id = ?,
                name = '',
                remark = '',
                encrypted_key = '',
                key_suffix = ?,
                expires_at = ?,
                created_at = ?,
                last_used_at = ?,
                account_id = ?
            WHERE key_digest = ?
            ''',
            (
                api_key['id'],
                api_key['key_suffix'],
                api_key['expires_at'],
                api_key['created_at'],
                last_used_at,
                api_key['account_id'],
                api_key['key_digest'],
            ),
        )
        return

    conn.execute(
        '''
        INSERT INTO public_mailbox_api_keys (
            id, name, remark, encrypted_key, key_digest, key_suffix,
            expires_at, created_at, last_used_at, account_id
        ) VALUES (?, '', '', '', ?, ?, ?, ?, ?, ?)
        ''',
        (
            api_key['id'],
            api_key['key_digest'],
            api_key['key_suffix'],
            api_key['expires_at'],
            api_key['created_at'],
            last_used_at,
            api_key['account_id'],
        ),
    )


def _apply_api_key_upserts(
    conn: sqlite3.Connection,
    api_keys: list[dict[str, Any]],
    preserved_last_used_by_id: dict[int, Any],
    preserved_last_used_by_digest: dict[str, Any],
) -> None:
    for api_key in api_keys:
        _upsert_api_key_row(
            conn,
            api_key,
            preserved_last_used_by_id,
            preserved_last_used_by_digest,
        )


def _apply_setting_upserts(conn: sqlite3.Connection, settings: dict[str, str]) -> None:
    for key in sorted(settings):
        conn.execute(
            '''
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            ''',
            (
                key,
                settings[key],
                _utc_iso_text(datetime.now(timezone.utc)),
            ),
        )


def _apply_delete_tombstones(conn: sqlite3.Connection, deletes: list[dict[str, Any]]) -> None:
    for item in sorted(deletes, key=lambda current: (_DELETE_ORDER[current['entity_type']], str(current['entity_id']))):
        if item['entity_type'] == 'recipient_link':
            conn.execute('DELETE FROM recipient_mail_links WHERE id = ?', (item['entity_id'],))
        elif item['entity_type'] == 'api_key':
            conn.execute('DELETE FROM public_mailbox_api_keys WHERE id = ?', (item['entity_id'],))
        elif item['entity_type'] == 'alias':
            conn.execute('DELETE FROM account_aliases WHERE id = ?', (item['entity_id'],))
        elif item['entity_type'] == 'account':
            conn.execute('DELETE FROM accounts WHERE id = ?', (item['entity_id'],))
        else:
            conn.execute('DELETE FROM settings WHERE key = ?', (item['entity_id'],))


def _wrap_replica_apply_error(exc: Exception) -> ReplicaApplyError:
    if isinstance(exc, ReplicaApplyError):
        return exc
    return ReplicaApplyError('replica payload validation or application failed')


def apply_snapshot(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    encrypt_sensitive: Callable[[str], str],
) -> int:
    savepoint_name = 'cluster_replica_apply_snapshot'
    try:
        normalized = _validate_snapshot_payload(payload)
        preserved_last_used_by_id, preserved_last_used_by_digest = _collect_existing_api_key_last_used(conn)
        _begin_savepoint(conn, savepoint_name)
        conn.execute('DELETE FROM recipient_mail_links')
        conn.execute('DELETE FROM public_mailbox_api_keys')
        conn.execute('DELETE FROM account_aliases')
        conn.execute('DELETE FROM accounts')
        conn.execute(
            'DELETE FROM settings WHERE key IN (?, ?)',
            tuple(sorted(REPLICATED_SETTING_KEYS)),
        )
        _apply_account_upserts(conn, normalized['accounts'], encrypt_sensitive)
        _apply_alias_upserts(conn, normalized['aliases'])
        _apply_api_key_upserts(
            conn,
            normalized['api_keys'],
            preserved_last_used_by_id,
            preserved_last_used_by_digest,
        )
        _apply_recipient_link_upserts(conn, normalized['recipient_links'])
        _apply_setting_upserts(conn, normalized['settings'])
        _set_replica_cursor(conn, normalized['snapshot_cursor'])
        _release_savepoint(conn, savepoint_name)
        return normalized['snapshot_cursor']
    except Exception as exc:
        try:
            if conn.in_transaction:
                _rollback_savepoint(conn, savepoint_name)
        except sqlite3.Error:
            pass
        raise _wrap_replica_apply_error(exc) from exc


def apply_increment(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    encrypt_sensitive: Callable[[str], str],
) -> int:
    savepoint_name = 'cluster_replica_apply_increment'
    try:
        normalized = _validate_increment_payload(payload, conn)
        current_cursor = read_replica_cursor(conn)
        if normalized['from_cursor'] != current_cursor:
            raise ReplicaApplyError('increment cursor does not match local replica cursor')
        preserved_last_used_by_id, preserved_last_used_by_digest = _collect_existing_api_key_last_used(conn)
        _begin_savepoint(conn, savepoint_name)
        _apply_delete_tombstones(conn, normalized['deletes'])
        _apply_account_upserts(conn, normalized['accounts'], encrypt_sensitive)
        _apply_alias_upserts(conn, normalized['aliases'])
        _apply_api_key_upserts(
            conn,
            normalized['api_keys'],
            preserved_last_used_by_id,
            preserved_last_used_by_digest,
        )
        _apply_recipient_link_upserts(conn, normalized['recipient_links'])
        _apply_setting_upserts(conn, normalized['settings'])
        _set_replica_cursor(conn, normalized['next_cursor'])
        _release_savepoint(conn, savepoint_name)
        return normalized['next_cursor']
    except Exception as exc:
        try:
            if conn.in_transaction:
                _rollback_savepoint(conn, savepoint_name)
        except sqlite3.Error:
            pass
        raise _wrap_replica_apply_error(exc) from exc


def prune_expired_nonces(conn: sqlite3.Connection, now_epoch: int) -> int:
    cutoff = _require_epoch_cutoff(now_epoch)
    cursor = conn.execute(
        'DELETE FROM cluster_request_nonces WHERE expires_at < ?',
        (cutoff,),
    )
    return int(cursor.rowcount or 0)


def prune_replication_events(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    cutoff = _require_utc_rfc3339_cutoff(cutoff_iso)
    cursor = conn.execute(
        'DELETE FROM replication_events WHERE julianday(created_at) < julianday(?)',
        (cutoff,),
    )
    return int(cursor.rowcount or 0)


__all__ = [
    'CLUSTER_PROTOCOL_VERSION',
    'MAX_INCREMENT_EVENTS',
    'REPLICATED_SETTING_KEYS',
    'ReplicaApplyError',
    'apply_increment',
    'apply_snapshot',
    'activate_cluster_node',
    'consume_enrollment_token',
    'create_cluster_node',
    'build_increment',
    'build_snapshot',
    'delete_cluster_node',
    'initialize_primary_schema',
    'initialize_replica_schema',
    'issue_enrollment_token',
    'list_cluster_nodes',
    'prune_expired_nonces',
    'prune_replication_events',
    'read_replica_cursor',
    'register_cluster_request_nonce',
    'revoke_cluster_node',
    'rotate_cluster_node_secret',
    'touch_cluster_node',
    'update_cluster_node_ack',
]
