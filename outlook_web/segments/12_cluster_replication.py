from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import threading
import time
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import requests

from outlook_web.cluster.client import (
    ReplicaSyncClient,
    SQLiteReplicaIdentityStore,
    SQLiteReplicaStore,
    _state_lock_path,
    start_replica_sync_worker,
)

from outlook_web.cluster.crypto import (
    ClusterCryptoError,
    decrypt_json,
    derive_node_keys,
    derive_enrollment_key,
    encrypt_json,
    public_key_fingerprint,
    verify_request_signature,
)
from outlook_web.cluster.storage import (
    CLUSTER_PROTOCOL_VERSION,
    MAX_INCREMENT_EVENTS,
    activate_cluster_node,
    build_increment,
    build_snapshot,
    consume_enrollment_token,
    create_cluster_node,
    delete_cluster_node,
    issue_enrollment_token,
    list_cluster_nodes,
    register_cluster_request_nonce,
    revoke_cluster_node,
    rotate_cluster_node_secret,
    touch_cluster_node,
    update_cluster_node_ack,
)

if TYPE_CHECKING:
    from web_outlook_app import *  # noqa: F403


def cluster_replication_enabled() -> bool:
    return CLUSTER_CONFIG.is_replica


def _cluster_error(code: str, message: str, status: int):
    return jsonify({'success': False, 'error_code': code, 'error': message}), status


def _cluster_read_only_error():
    return _cluster_error('replica_read_only', '只读副本不允许此操作', 403)


_REPLICA_ALLOWED_EXACT_PATHS = {
    '/',
    '/health/live',
    '/health/ready',
    '/api/v1/mailboxes/messages',
    '/api/v1/cluster/status',
    '/api/v1/cluster/sync/snapshot',
    '/api/v1/cluster/sync/pull',
    '/api/v1/cluster/sync/ack',
}
_REPLICA_ALLOWED_PREFIXES = ('/static/', '/api/v2/mailboxes/', '/show/', '/query/')


def _replica_request_is_allowed(path: str) -> bool:
    return path in _REPLICA_ALLOWED_EXACT_PATHS or any(path.startswith(prefix) for prefix in _REPLICA_ALLOWED_PREFIXES)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

_CLUSTER_PROTOCOL_VERSION = CLUSTER_PROTOCOL_VERSION
_CLUSTER_MAX_CLOCK_SKEW_SECONDS = 120
_CLUSTER_PREVIOUS_SECRET_GRACE_SECONDS = 600
_CLUSTER_MAX_REQUEST_BODY_BYTES = 64 * 1024
_CLUSTER_MAX_INCREMENT_PLAINTEXT_BYTES = 2 * 1024 * 1024


class _ClusterRequestError(ValueError):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _base64url_decode(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ClusterCryptoError('invalid enrollment envelope')
    text = value.strip()
    padding = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode('ascii'))


def _load_primary_identity(conn) -> dict[str, Any]:
    row = conn.execute(
        '''
        SELECT encrypted_private_key, public_key, fingerprint, created_at, key_version
        FROM cluster_identity
        WHERE id = 1
        '''
    ).fetchone()
    if row is None:
        raise RuntimeError('cluster identity missing')
    private_key_text = decrypt_data(row['encrypted_private_key'])
    private_key = base64.b64decode(private_key_text.encode('ascii'))
    public_key = base64.b64decode(row['public_key'].encode('ascii'))
    fingerprint = public_key_fingerprint(public_key)
    if fingerprint != row['fingerprint']:
        raise RuntimeError('cluster identity fingerprint mismatch')
    return {
        'private_key': private_key,
        'public_key': public_key,
        'fingerprint': fingerprint,
        'created_at': row['created_at'],
        'key_version': int(row['key_version']),
    }


def _serialize_enrollment_token_response(node: dict[str, Any], token: str, expires_at: datetime, primary_identity: dict[str, Any]) -> dict[str, Any]:
    return {
        'success': True,
        'node': node,
        'enrollment_token': token,
        'enrollment_expires_at': expires_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'primary_fingerprint': primary_identity['fingerprint'],
    }


def _parse_cluster_node_timestamp(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    return datetime.fromisoformat(str(value).strip().replace('Z', '+00:00')).astimezone(timezone.utc)


def _cluster_node_offline_threshold_seconds(max_stale_seconds: int) -> int:
    threshold = max(30, int(getattr(CLUSTER_CONFIG, 'poll_seconds', 10)) * 3)
    if max_stale_seconds:
        return min(threshold, max_stale_seconds)
    return threshold


def _derive_cluster_node_display_status(
    node: dict[str, Any],
    *,
    primary_cursor: int,
    now: datetime,
) -> str:
    status = str(node.get('status') or '').strip()
    if status in {'pending', 'revoked'}:
        return status
    if status != 'active':
        return status or 'pending'

    max_stale_seconds = max(0, int(getattr(CLUSTER_CONFIG, 'max_stale_seconds', 0) or 0))
    last_sync_at = _parse_cluster_node_timestamp(node.get('last_sync_at'))
    if last_sync_at is None:
        return 'synchronizing'

    age_seconds = max(0, int((now - last_sync_at).total_seconds()))
    if max_stale_seconds and age_seconds > max_stale_seconds:
        return 'expired'

    if int(node.get('last_ack_cursor') or 0) < int(primary_cursor or 0):
        return 'synchronizing'

    offline_threshold_seconds = _cluster_node_offline_threshold_seconds(max_stale_seconds)
    if age_seconds > offline_threshold_seconds:
        return 'offline'

    return 'online'


def _serialize_cluster_nodes_response(nodes: list[dict[str, Any]], primary_identity: dict[str, Any]) -> dict[str, Any]:
    primary_cursor_row = get_db().execute(
        'SELECT COALESCE(MAX(cursor), 0) AS cursor FROM replication_events'
    ).fetchone()
    primary_cursor = int(primary_cursor_row['cursor'] or 0)
    now = _utc_now()
    return {
        'success': True,
        'nodes': [
            {
                **node,
                'display_status': _derive_cluster_node_display_status(
                    node,
                    primary_cursor=primary_cursor,
                    now=now,
                ),
            }
            for node in nodes
        ],
        'primary_fingerprint': primary_identity['fingerprint'],
        'primary_cursor': primary_cursor,
    }


@app.route('/api/v1/cluster/identity', methods=['GET'])
def api_cluster_identity():
    primary_identity = _load_primary_identity(get_db())
    return jsonify({
        'success': True,
        'primary_public_key': base64.b64encode(primary_identity['public_key']).decode('ascii'),
        'primary_fingerprint': primary_identity['fingerprint'],
    })


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


def _require_cluster_node_id(node_id: Any) -> str:
    normalized = str(node_id or '').strip()
    if not normalized or len(normalized) > 128:
        raise _ClusterRequestError('cluster_node_invalid', 400, 'cluster node id is invalid')
    return normalized


def _read_cluster_raw_body() -> bytes:
    if request.content_length is not None and request.content_length > _CLUSTER_MAX_REQUEST_BODY_BYTES:
        raise _ClusterRequestError('cluster_body_too_large', 413, 'request body exceeds maximum size')
    raw_body = request.stream.read(_CLUSTER_MAX_REQUEST_BODY_BYTES + 1)
    if len(raw_body) > _CLUSTER_MAX_REQUEST_BODY_BYTES:
        raise _ClusterRequestError('cluster_body_too_large', 413, 'request body exceeds maximum size')
    return raw_body


def _parse_cluster_json_body(raw_body: bytes, *, allow_empty: bool) -> dict[str, Any]:
    if not raw_body:
        if allow_empty:
            return {}
        raise _ClusterRequestError('cluster_body_invalid', 400, 'request body must be a JSON object')
    try:
        payload = json.loads(raw_body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ClusterRequestError('cluster_body_invalid', 400, 'request body must be valid JSON') from exc
    if not isinstance(payload, dict):
        raise _ClusterRequestError('cluster_body_invalid', 400, 'request body must be a JSON object')
    return payload


def _parse_cluster_header(name: str, *, allow_empty: bool = False) -> str:
    value = request.headers.get(name)
    if value is None:
        if allow_empty:
            return ''
        raise _ClusterRequestError('cluster_auth_required', 401, 'cluster authentication is required')
    normalized = str(value).strip()
    if not normalized and not allow_empty:
        raise _ClusterRequestError('cluster_auth_required', 401, 'cluster authentication is required')
    return normalized


def _parse_cluster_int_header(name: str, *, code: str, status: int) -> int:
    raw_value = _parse_cluster_header(name)
    if not raw_value.isdigit():
        raise _ClusterRequestError(code, status, 'cluster authentication header is invalid')
    return int(raw_value)


def _parse_cluster_secret(sync_secret_text: str) -> bytes:
    try:
        secret_bytes = _base64url_decode(sync_secret_text)
    except (ClusterCryptoError, ValueError) as exc:
        raise _ClusterRequestError('cluster_sync_failed', 500, 'cluster synchronization failed') from exc
    if len(secret_bytes) != 32:
        raise _ClusterRequestError('cluster_sync_failed', 500, 'cluster synchronization failed')
    return secret_bytes


def _load_cluster_auth_row(conn, node_id: str):
    return conn.execute(
        '''
        SELECT
            node_id,
            status,
            credential_version,
            encrypted_sync_secret,
            previous_encrypted_sync_secret,
            previous_credential_version,
            previous_secret_expires_at,
            revoked_at,
            last_ack_cursor
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (node_id,),
    ).fetchone()


def _select_cluster_sync_secret(row, credential_version: int, now: datetime) -> str:
    current_credential_version = int(row['credential_version'] or 0)
    if credential_version == current_credential_version:
        secret_text = str(row['encrypted_sync_secret'] or '').strip()
        if not secret_text:
            raise _ClusterRequestError('cluster_credential_mismatch', 409, 'cluster credential version is invalid')
        return secret_text

    previous_credential_version = row['previous_credential_version']
    previous_secret = str(row['previous_encrypted_sync_secret'] or '').strip()
    previous_expires_at = str(row['previous_secret_expires_at'] or '').strip()
    if (
        previous_credential_version is not None
        and credential_version == int(previous_credential_version)
        and previous_secret
        and previous_expires_at
    ):
        try:
            expires_at = datetime.fromisoformat(previous_expires_at.replace('Z', '+00:00'))
        except ValueError as exc:
            raise _ClusterRequestError('cluster_sync_failed', 500, 'cluster synchronization failed') from exc
        if expires_at >= now:
            return previous_secret
    raise _ClusterRequestError('cluster_credential_mismatch', 409, 'cluster credential version is invalid')


def _authenticate_cluster_request(raw_body: bytes) -> dict[str, Any]:
    if CLUSTER_CONFIG.is_replica:
        raise _ClusterRequestError('cluster_primary_required', 403, 'cluster synchronization is primary-only')

    node_id = _require_cluster_node_id(_parse_cluster_header('X-Cluster-Node-Id'))
    protocol_version = _parse_cluster_int_header(
        'X-Cluster-Protocol-Version',
        code='cluster_protocol_mismatch',
        status=409,
    )
    credential_version = _parse_cluster_int_header(
        'X-Cluster-Credential-Version',
        code='cluster_credential_mismatch',
        status=409,
    )
    timestamp = _parse_cluster_int_header(
        'X-Cluster-Timestamp',
        code='cluster_clock_skew',
        status=401,
    )
    nonce = _parse_cluster_header('X-Cluster-Nonce')
    signature = _parse_cluster_header('X-Cluster-Signature')
    if protocol_version != _CLUSTER_PROTOCOL_VERSION:
        raise _ClusterRequestError('cluster_protocol_mismatch', 409, 'cluster protocol version is unsupported')

    now = _utc_now()
    now_epoch = int(now.timestamp())
    if abs(now_epoch - timestamp) > _CLUSTER_MAX_CLOCK_SKEW_SECONDS:
        raise _ClusterRequestError('cluster_clock_skew', 401, 'cluster request clock skew is too large')

    db = get_db()
    row = _load_cluster_auth_row(db, node_id)
    if row is None:
        raise _ClusterRequestError('cluster_auth_required', 401, 'cluster authentication is required')

    status = str(row['status'] or '').strip()
    if status == 'revoked':
        raise _ClusterRequestError('cluster_node_revoked', 403, 'cluster node has been revoked')
    if status != 'active':
        raise _ClusterRequestError('cluster_node_inactive', 403, 'cluster node is not active')

    encrypted_secret = _select_cluster_sync_secret(row, credential_version, now)
    secret_text = decrypt_data(encrypted_secret)
    node_keys = derive_node_keys(_parse_cluster_secret(secret_text), credential_version)
    nonce_digest = hashlib.sha256(nonce.encode('utf-8')).hexdigest()
    nonce_expires_at = max(now_epoch, timestamp) + _CLUSTER_MAX_CLOCK_SKEW_SECONDS

    with db:
        if not register_cluster_request_nonce(db, node_id, nonce_digest, nonce_expires_at, now):
            raise _ClusterRequestError('cluster_replay_detected', 409, 'cluster replay detected')
        if not verify_request_signature(
            node_keys.request_auth,
            signature,
            protocol_version,
            credential_version,
            node_id,
            request.method,
            request.path,
            raw_body,
            timestamp,
            nonce,
        ):
            raise _ClusterRequestError('cluster_signature_invalid', 401, 'cluster request signature is invalid')
        touch_cluster_node(
            db,
            node_id,
            now,
            protocol_version=protocol_version,
            source_ip=request.remote_addr,
        )

    context = {
        'node_id': node_id,
        'protocol_version': protocol_version,
        'credential_version': credential_version,
        'sync_secret': secret_text,
        'node_keys': node_keys,
        'timestamp': timestamp,
        'nonce': nonce,
        'active_credential_version': int(row['credential_version'] or 0),
        'active_encrypted_sync_secret': str(row['encrypted_sync_secret'] or '').strip(),
        'previous_secret_expires_at': str(row['previous_secret_expires_at'] or '').strip(),
    }
    g.cluster_node = context
    return context


def _build_pending_cluster_credential(cluster_node: dict[str, Any]) -> dict[str, Any] | None:
    if cluster_node['credential_version'] == cluster_node['active_credential_version']:
        return None
    encrypted_sync_secret = cluster_node['active_encrypted_sync_secret']
    previous_secret_expires_at = cluster_node['previous_secret_expires_at']
    if not encrypted_sync_secret or not previous_secret_expires_at:
        return None
    return {
        'sync_secret': decrypt_data(encrypted_sync_secret),
        'credential_version': cluster_node['active_credential_version'],
        'expires_at': previous_secret_expires_at,
    }


def _build_cluster_sync_response_payload(
    cluster_node: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    response_payload = dict(payload)
    pending_credential = _build_pending_cluster_credential(cluster_node)
    if pending_credential is not None:
        response_payload['next_credential'] = pending_credential
    return response_payload


def _encrypt_cluster_sync_response(
    cluster_node: dict[str, Any],
    path: str,
    payload: dict[str, Any],
    **cursor_metadata: Any,
) -> dict[str, Any]:
    response_payload = _build_cluster_sync_response_payload(cluster_node, payload)
    metadata = {
        'node_id': cluster_node['node_id'],
        'protocol_version': cluster_node['protocol_version'],
        'credential_version': cluster_node['credential_version'],
        'path': path,
        'timestamp': cluster_node['timestamp'],
        'nonce': cluster_node['nonce'],
    }
    metadata.update(cursor_metadata)
    return {
        'success': True,
        'metadata': metadata,
        'envelope': encrypt_json(
            response_payload,
            cluster_node['node_keys'].response_encryption,
            metadata,
        ),
    }


def _cluster_error_from_exception(exc: _ClusterRequestError):
    return _cluster_error(exc.code, exc.message, exc.status)


def _pull_cursor_from_payload(payload: dict[str, Any]) -> int:
    if set(payload.keys()) != {'cursor'}:
        raise _ClusterRequestError('cluster_cursor_invalid', 400, 'cursor payload is invalid')
    cursor = payload.get('cursor')
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise _ClusterRequestError('cluster_cursor_invalid', 400, 'cursor payload is invalid')
    return cursor


def _snapshot_body_is_empty(payload: dict[str, Any]) -> bool:
    return not payload


def _replication_cursor_window(conn, cursor: int) -> tuple[int | None, int]:
    row = conn.execute(
        '''
        SELECT
            (SELECT cursor FROM replication_events WHERE cursor > ? ORDER BY cursor LIMIT 1) AS next_cursor,
            COALESCE(MAX(cursor), 0) AS latest_cursor
        FROM replication_events
        ''',
        (cursor,),
    ).fetchone()
    next_cursor = row['next_cursor']
    if next_cursor is None:
        return None, int(row['latest_cursor'] or 0)
    return int(next_cursor), int(row['latest_cursor'] or 0)


def _measure_cluster_increment_payload(
    increment: dict[str, Any],
    extra_payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    payload = {'result': 'increment', 'increment': increment}
    if extra_payload:
        payload.update(extra_payload)
    return len(_canonical_json_bytes(payload)), payload


def _bounded_increment_payload(
    conn,
    cursor: int,
    *,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    low = 1
    high = MAX_INCREMENT_EVENTS
    best_batch = build_increment(conn, cursor, 1, decrypt_data)
    best_payload_size, best_payload = _measure_cluster_increment_payload(best_batch, extra_payload)
    if best_payload_size > _CLUSTER_MAX_INCREMENT_PLAINTEXT_BYTES:
        raise _ClusterRequestError('cluster_payload_too_large', 413, 'cluster payload exceeds maximum size')

    while low <= high:
        limit = (low + high) // 2
        candidate = build_increment(conn, cursor, limit, decrypt_data)
        payload_size, payload = _measure_cluster_increment_payload(candidate, extra_payload)
        if payload_size <= _CLUSTER_MAX_INCREMENT_PLAINTEXT_BYTES:
            best_batch = candidate
            best_payload = payload
            if candidate['next_cursor'] == cursor:
                break
            low = limit + 1
        else:
            high = limit - 1
    return best_payload


def _decrypt_enrollment_request(payload: dict[str, Any], primary_identity: dict[str, Any]) -> dict[str, Any]:
    node_id = str(payload.get('node_id') or '').strip()
    primary_fingerprint_text = str(payload.get('primary_fingerprint') or '').strip()
    ephemeral_public_key_text = str(payload.get('ephemeral_public_key') or '').strip()
    envelope = payload.get('envelope')
    if not node_id or not primary_fingerprint_text or not ephemeral_public_key_text or not isinstance(envelope, dict):
        raise ClusterCryptoError('invalid enrollment envelope')
    if primary_fingerprint_text != primary_identity['fingerprint']:
        raise ValueError('primary fingerprint mismatch')
    ephemeral_public_key = _base64url_decode(ephemeral_public_key_text)
    if len(ephemeral_public_key) != 32:
        raise ClusterCryptoError('invalid enrollment envelope')
    shared_key = derive_enrollment_key(
        primary_identity['private_key'],
        ephemeral_public_key,
        primary_identity['fingerprint'].encode('utf-8'),
    )
    decrypted = decrypt_json(
        envelope,
        shared_key,
        {
            'node_id': node_id,
            'primary_fingerprint': primary_identity['fingerprint'],
        },
    )
    token = str(decrypted.get('token') or '').strip()
    node_public_key_text = str(decrypted.get('node_public_key') or '').strip()
    if not token or not node_public_key_text:
        raise ClusterCryptoError('invalid enrollment envelope')
    node_public_key = _base64url_decode(node_public_key_text)
    if len(node_public_key) != 32:
        raise ClusterCryptoError('invalid enrollment envelope')
    return {
        'node_id': node_id,
        'token': token,
        'node_public_key': node_public_key,
    }


def _build_enrollment_response(
    primary_identity: dict[str, Any],
    node_id: str,
    node_public_key: bytes,
    node: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    sync_secret = secrets.token_bytes(32)
    secret_digest = hashlib.sha256(sync_secret).hexdigest()
    sync_secret_text = base64.urlsafe_b64encode(sync_secret).decode('ascii').rstrip('=')
    response_key = derive_enrollment_key(
        primary_identity['private_key'],
        node_public_key,
        primary_identity['fingerprint'].encode('utf-8'),
    )
    response_envelope = encrypt_json(
        {
            'node_id': node_id,
            'sync_secret': sync_secret_text,
            'secret_digest': secret_digest,
            'credential_version': 1,
            'node': node,
        },
        response_key,
        {
            'node_id': node_id,
            'primary_fingerprint': primary_identity['fingerprint'],
        },
    )
    return response_envelope, sync_secret_text, secret_digest


@app.route('/api/settings/cluster/nodes', methods=['GET'])
@login_required
def api_cluster_nodes_list():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    primary_identity = _load_primary_identity(get_db())
    return jsonify(_serialize_cluster_nodes_response(list_cluster_nodes(get_db()), primary_identity))


@app.route('/api/settings/cluster/nodes', methods=['POST'])
@login_required
@csrf_exempt
def api_cluster_nodes_create():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _cluster_error('cluster_node_invalid', '鑺傜偣鍙傛暟鏃犳晥', 400)
    name = str(payload.get('name') or '').strip()
    remark = payload.get('remark')
    if not name:
        return _cluster_error('cluster_node_name_required', '节点名称不能为空', 400)

    db = get_db()
    try:
        primary_identity = _load_primary_identity(db)
        node = create_cluster_node(db, name, remark, _utc_now())
        token, expires_at = issue_enrollment_token(db, node['id'], _utc_now())
        return jsonify(_serialize_enrollment_token_response(node, token, expires_at, primary_identity)), 201
    except ValueError as exc:
        message = str(exc)
        if message == 'duplicate node name':
            return _cluster_error('cluster_node_name_conflict', '节点名称已存在', 409)
        return _cluster_error('cluster_node_invalid', '节点参数无效', 400)


@app.route('/api/v1/cluster/nodes/enroll', methods=['POST'])
@csrf_exempt
def api_cluster_nodes_enroll():
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _cluster_error('malformed_enrollment_envelope', 'enrollment 封包无效', 400)

    db = get_db()
    primary_identity = _load_primary_identity(db)
    try:
        request_data = _decrypt_enrollment_request(payload, primary_identity)
    except ClusterCryptoError:
        return _cluster_error('malformed_enrollment_envelope', 'enrollment 封包无效', 400)
    except ValueError:
        return _cluster_error('primary_fingerprint_mismatch', '主节点指纹不匹配', 409)

    now = _utc_now()
    try:
        with db:
            if not consume_enrollment_token(db, request_data['node_id'], request_data['token'], now):
                token_row = db.execute(
                    '''
                    SELECT expires_at, consumed_at
                    FROM cluster_enrollment_tokens
                    WHERE node_id = ?
                    ORDER BY expires_at DESC, id DESC
                    LIMIT 1
                    ''',
                    (request_data['node_id'],),
                ).fetchone()
                if token_row is not None:
                    expires_at_text = str(token_row['expires_at'] or '')
                    if expires_at_text and expires_at_text <= now.isoformat().replace('+00:00', 'Z'):
                        return _cluster_error('enrollment_token_expired', 'enrollment token 已过期', 409)
                return _cluster_error('enrollment_token_unavailable', 'enrollment token 无效', 409)

            node_row = db.execute(
                '''
                SELECT
                    id, node_id, name, remark, public_key, encrypted_sync_secret, secret_digest,
                    credential_version, status, created_at, enrolled_at, last_seen_at, last_sync_at,
                    revoked_at, last_ack_cursor, replica_app_version, replica_protocol_version,
                    last_error_sanitized, previous_encrypted_sync_secret, previous_secret_digest,
                    previous_credential_version, previous_secret_expires_at
                FROM cluster_nodes
                WHERE node_id = ?
                ''',
                (request_data['node_id'],),
            ).fetchone()
            if node_row is None:
                return _cluster_error('cluster_node_missing', '节点不存在', 404)
            node = {
                'id': node_row['node_id'],
                'name': node_row['name'],
                'remark': node_row['remark'] or '',
                'status': node_row['status'],
                'created_at': node_row['created_at'],
                'enrolled_at': node_row['enrolled_at'],
                'last_seen_at': node_row['last_seen_at'],
                'last_sync_at': node_row['last_sync_at'],
                'revoked_at': node_row['revoked_at'],
                'last_ack_cursor': int(node_row['last_ack_cursor'] or 0),
            }
            response_envelope, sync_secret_text, secret_digest = _build_enrollment_response(
                primary_identity,
                request_data['node_id'],
                request_data['node_public_key'],
                node,
            )
            encrypted_sync_secret = encrypt_data(sync_secret_text)
            node = activate_cluster_node(
                db,
                request_data['node_id'],
                request_data['node_public_key'],
                encrypted_sync_secret,
                secret_digest,
                now,
            )
    except ValueError as exc:
        message = str(exc)
        if message == 'cluster node is not pending':
            return _cluster_error('cluster_node_not_pending', '节点状态不允许激活', 409)
        return _cluster_error('cluster_node_activation_failed', '节点激活失败', 409)

    return jsonify({
        'success': True,
        'node': node,
        'enrollment': response_envelope,
        'primary_fingerprint': primary_identity['fingerprint'],
    })


@app.route('/api/v1/cluster/sync/snapshot', methods=['POST'])
@csrf_exempt
def api_cluster_sync_snapshot():
    try:
        raw_body = _read_cluster_raw_body()
        cluster_node = _authenticate_cluster_request(raw_body)
        payload = _parse_cluster_json_body(raw_body, allow_empty=True)
        if not _snapshot_body_is_empty(payload):
            raise _ClusterRequestError('cluster_body_invalid', 400, 'snapshot request body must be empty')
        snapshot = build_snapshot(get_db(), decrypt_data)
        now = _utc_now()
        with get_db():
            touch_cluster_node(
                get_db(),
                cluster_node['node_id'],
                now,
                protocol_version=cluster_node['protocol_version'],
                mark_synced=True,
                source_ip=request.remote_addr,
            )
        return jsonify(_encrypt_cluster_sync_response(
            cluster_node,
            request.path,
            {
                'result': 'snapshot',
                'snapshot': snapshot,
            },
            result='snapshot',
            snapshot_cursor=snapshot['snapshot_cursor'],
        ))
    except _ClusterRequestError as exc:
        return _cluster_error_from_exception(exc)
    except Exception:
        return _cluster_error('cluster_sync_failed', 'cluster synchronization failed', 500)


@app.route('/api/v1/cluster/sync/pull', methods=['POST'])
@csrf_exempt
def api_cluster_sync_pull():
    try:
        raw_body = _read_cluster_raw_body()
        cluster_node = _authenticate_cluster_request(raw_body)
        payload = _parse_cluster_json_body(raw_body, allow_empty=False)
        cursor = _pull_cursor_from_payload(payload)
        db = get_db()
        next_cursor, latest_cursor = _replication_cursor_window(db, cursor)
        if next_cursor is not None and next_cursor != cursor + 1:
            response_payload = {
                'result': 'snapshot_required',
                'snapshot_cursor': latest_cursor,
            }
            metadata = {
                'result': 'snapshot_required',
                'snapshot_cursor': latest_cursor,
            }
        else:
            pending_credential = _build_pending_cluster_credential(cluster_node)
            response_payload = _bounded_increment_payload(
                db,
                cursor,
                extra_payload={'next_credential': pending_credential} if pending_credential is not None else None,
            )
            increment = response_payload['increment']
            metadata = {
                'result': 'increment',
                'from_cursor': increment['from_cursor'],
                'next_cursor': increment['next_cursor'],
            }
        now = _utc_now()
        with db:
            touch_cluster_node(
                db,
                cluster_node['node_id'],
                now,
                protocol_version=cluster_node['protocol_version'],
                mark_synced=True,
                source_ip=request.remote_addr,
            )
        return jsonify(_encrypt_cluster_sync_response(
            cluster_node,
            request.path,
            response_payload,
            **metadata,
        ))
    except _ClusterRequestError as exc:
        return _cluster_error_from_exception(exc)
    except Exception:
        return _cluster_error('cluster_sync_failed', 'cluster synchronization failed', 500)


@app.route('/api/v1/cluster/sync/ack', methods=['POST'])
@csrf_exempt
def api_cluster_sync_ack():
    try:
        raw_body = _read_cluster_raw_body()
        cluster_node = _authenticate_cluster_request(raw_body)
        payload = _parse_cluster_json_body(raw_body, allow_empty=False)
        cursor = _pull_cursor_from_payload(payload)
        db = get_db()
        try:
            with db:
                update_cluster_node_ack(
                    db,
                    cluster_node['node_id'],
                    cursor,
                    _utc_now(),
                    credential_version=cluster_node['credential_version'],
                )
        except ValueError as exc:
            if str(exc) == 'cluster ack cursor regression':
                return _cluster_error('cluster_ack_out_of_order', 'cluster ack cursor must be monotonic', 409)
            raise
        return jsonify(_encrypt_cluster_sync_response(
            cluster_node,
            request.path,
            {
                'result': 'acknowledged',
                'cursor': cursor,
            },
            result='acknowledged',
            cursor=cursor,
        ))
    except _ClusterRequestError as exc:
        return _cluster_error_from_exception(exc)
    except Exception:
        return _cluster_error('cluster_sync_failed', 'cluster synchronization failed', 500)


def _lookup_management_node_or_404(conn, node_id: str):
    row = conn.execute(
        '''
        SELECT status
        FROM cluster_nodes
        WHERE node_id = ?
        ''',
        (_require_cluster_node_id(node_id),),
    ).fetchone()
    if row is None:
        return None
    return str(row['status'] or '').strip()


@app.route('/api/settings/cluster/nodes/<node_id>/token', methods=['POST'])
@login_required
@csrf_exempt
def api_cluster_nodes_issue_token(node_id: str):
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    db = get_db()
    status = _lookup_management_node_or_404(db, node_id)
    if status is None:
        return _cluster_error('cluster_node_missing', 'cluster node is missing', 404)
    if status == 'revoked':
        return _cluster_error('cluster_node_revoked', 'cluster node has been revoked', 409)
    if status != 'pending':
        return _cluster_error('cluster_node_token_forbidden', 'cluster node token can only be issued for pending nodes', 409)

    primary_identity = _load_primary_identity(db)
    token, expires_at = issue_enrollment_token(db, node_id, _utc_now())
    return jsonify(_serialize_enrollment_token_response(
        next(item for item in list_cluster_nodes(db) if item['id'] == node_id),
        token,
        expires_at,
        primary_identity,
    ))


@app.route('/api/settings/cluster/nodes/<node_id>/rotate', methods=['POST'])
@login_required
@csrf_exempt
def api_cluster_nodes_rotate(node_id: str):
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    db = get_db()
    sync_secret = secrets.token_bytes(32)
    sync_secret_text = base64.urlsafe_b64encode(sync_secret).decode('ascii').rstrip('=')
    secret_digest = hashlib.sha256(sync_secret).hexdigest()
    encrypted_sync_secret = encrypt_data(sync_secret_text)
    try:
        with db:
            node = rotate_cluster_node_secret(
                db,
                node_id,
                encrypted_sync_secret,
                secret_digest,
                _utc_now(),
                grace_period_seconds=_CLUSTER_PREVIOUS_SECRET_GRACE_SECONDS,
            )
    except ValueError as exc:
        message = str(exc)
        if message == 'cluster node missing':
            return _cluster_error('cluster_node_missing', 'cluster node is missing', 404)
        if message == 'cluster node revoked':
            return _cluster_error('cluster_node_revoked', 'cluster node has been revoked', 409)
        if message == 'cluster node is not active':
            return _cluster_error('cluster_node_not_active', 'cluster node must be active', 409)
        return _cluster_error('cluster_rotation_failed', 'cluster credential rotation failed', 409)
    return jsonify({'success': True, 'node': node})


@app.route('/api/settings/cluster/nodes/<node_id>/revoke', methods=['POST'])
@login_required
@csrf_exempt
def api_cluster_nodes_revoke(node_id: str):
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    try:
        with get_db():
            node = revoke_cluster_node(get_db(), node_id, _utc_now())
    except ValueError as exc:
        message = str(exc)
        if message == 'cluster node missing':
            return _cluster_error('cluster_node_missing', 'cluster node is missing', 404)
        if message == 'cluster node revoked':
            return _cluster_error('cluster_node_revoked', 'cluster node has been revoked', 409)
        return _cluster_error('cluster_revoke_failed', 'cluster node revoke failed', 409)
    return jsonify({'success': True, 'node': node})


@app.route('/api/settings/cluster/nodes/<node_id>', methods=['DELETE'])
@login_required
@csrf_exempt
def api_cluster_nodes_delete(node_id: str):
    if CLUSTER_CONFIG.is_replica:
        return _cluster_read_only_error()
    try:
        with get_db():
            deleted = delete_cluster_node(get_db(), node_id)
    except ValueError as exc:
        message = str(exc)
        if message == 'cluster node missing':
            return _cluster_error('cluster_node_missing', 'cluster node is missing', 404)
        if message == 'cluster node delete requires revoked':
            return _cluster_error('cluster_node_delete_requires_revoked', 'cluster node must be revoked before deletion', 409)
        return _cluster_error('cluster_delete_failed', 'cluster node deletion failed', 409)
    return jsonify({'success': deleted, 'node_id': node_id})


def _replica_sync_connection_factory() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def _replica_identity_connection_factory() -> sqlite3.Connection:
    identity_db_path = Path(DATABASE).resolve().parent / 'cluster' / 'identity.db'
    identity_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(identity_db_path)
    conn.row_factory = sqlite3.Row
    initialize_replica_schema(conn)
    return conn


def _build_replica_sync_client() -> ReplicaSyncClient:
    config = types.SimpleNamespace(
        master_url=CLUSTER_CONFIG.master_url,
        poll_seconds=CLUSTER_CONFIG.poll_seconds,
    )
    return ReplicaSyncClient(
        config=config,
        identity_store=SQLiteReplicaIdentityStore(
            _replica_identity_connection_factory,
            decrypt_data,
            encrypt_data,
        ),
        replica_store=SQLiteReplicaStore(
            _replica_sync_connection_factory,
            decrypt_data,
            encrypt_data,
        ),
        http_session=requests.Session(),
        clock=lambda: datetime.now(timezone.utc),
        sleeper=time.sleep,
    )


def _start_replica_sync_worker_if_needed() -> None:
    if not CLUSTER_CONFIG.is_replica:
        return None
    return start_replica_sync_worker(
        client_factory=_build_replica_sync_client,
        lock_path=_state_lock_path(Path(DATABASE).resolve().parent),
    )


_replica_sync_worker_start_lock = threading.Lock()
_replica_sync_worker_start_attempted = False


@app.before_request
def _enforce_replica_route_policy():
    if not CLUSTER_CONFIG.is_replica:
        return None
    if _replica_request_is_allowed(request.path or ''):
        return None
    return _cluster_read_only_error()


@app.before_request
def _start_replica_sync_worker_on_first_request():
    global _replica_sync_worker_start_attempted
    if not CLUSTER_CONFIG.is_replica or _replica_sync_worker_start_attempted:
        return None
    with _replica_sync_worker_start_lock:
        if _replica_sync_worker_start_attempted:
            return None
        try:
            _start_replica_sync_worker_if_needed()
        finally:
            _replica_sync_worker_start_attempted = True
    return None


_STATUS_SENSITIVE_MARKERS = ('email', 'secret', 'token', 'key', 'password', 'messages')


def _sanitize_cluster_status_text(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    lowered = text.lower()
    if any(marker in lowered for marker in _STATUS_SENSITIVE_MARKERS):
        return '[redacted]'
    return text


def _cluster_status_counts(conn) -> dict[str, int]:
    row = conn.execute(
        '''
        SELECT
            (SELECT COUNT(*) FROM accounts) AS accounts,
            (SELECT COUNT(*) FROM account_aliases) AS aliases,
            (SELECT COUNT(*) FROM public_mailbox_api_keys) AS public_api,
            (SELECT COUNT(*) FROM settings) AS settings
        '''
    ).fetchone()
    return {
        'accounts': int(row['accounts'] or 0),
        'aliases': int(row['aliases'] or 0),
        'public_api': int(row['public_api'] or 0),
        'settings': int(row['settings'] or 0),
    }


def _cluster_status_payload() -> dict[str, Any]:
    if CLUSTER_CONFIG.is_replica:
        replica_state = _load_replica_state_with_repair()
        now = datetime.now(timezone.utc)
        is_ready, _ = replica_readiness(
            replica_state,
            now,
            CLUSTER_CONFIG.max_stale_seconds,
        )
        stale_deadline_at = None
        if replica_state.last_success_at is not None and CLUSTER_CONFIG.max_stale_seconds:
            stale_deadline_at = (
                replica_state.last_success_at + timedelta(seconds=CLUSTER_CONFIG.max_stale_seconds)
            ).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
        return {
            'success': True,
            'role': 'replica',
            'read_only': True,
            'ready': is_ready,
            'app_version': APP_VERSION,
            'node_id': replica_state.node_id or 'replica',
            'cursor': replica_state.cursor,
            'protocol_version': replica_state.protocol_version,
            'counts': _cluster_status_counts(get_db()),
            'stale_deadline_at': stale_deadline_at,
            'last_success_at': (
                replica_state.last_success_at.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                if replica_state.last_success_at is not None
                else None
            ),
            'last_error': _sanitize_cluster_status_text(replica_state.last_error),
        }
    return {
        'success': True,
        'role': 'primary',
        'read_only': False,
        'ready': True,
        'app_version': APP_VERSION,
        'node_id': 'primary',
        'cursor': 0,
        'protocol_version': _CLUSTER_PROTOCOL_VERSION,
        'counts': _cluster_status_counts(get_db()),
        'stale_deadline_at': None,
        'last_success_at': None,
        'last_error': '',
    }


@app.route('/api/v1/cluster/status', methods=['GET'])
def api_cluster_status():
    return public_mailbox_json_response(_cluster_status_payload())


@app.route('/health/live', methods=['GET'])
def health_live():
    return public_mailbox_json_response({
        'success': True,
        'status': 'live',
    })


@app.route('/health/ready', methods=['GET'])
def health_ready():
    if CLUSTER_CONFIG.is_replica:
        replica_state = _load_replica_state_with_repair()
        is_ready, error_code = replica_readiness(
            replica_state,
            datetime.now(timezone.utc),
            CLUSTER_CONFIG.max_stale_seconds,
        )
        if not is_ready:
            return _replica_readiness_error_response(error_code, 'json')
    return public_mailbox_json_response({
        'success': True,
        'status': 'ready',
    })
