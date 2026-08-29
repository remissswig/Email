from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .crypto import (
    ClusterCryptoError,
    decrypt_json,
    derive_node_keys,
    encrypt_json,
    sign_request,
    verify_request_signature,
)
from .storage import (
    CLUSTER_PROTOCOL_VERSION,
    ReplicaApplyError,
    apply_increment,
    apply_snapshot,
)


@dataclass(frozen=True)
class ReplicaState:
    cursor: int
    last_success_at: Optional[datetime]
    node_id: str
    protocol_version: int
    last_error: str


def replica_readiness(state: ReplicaState, now: datetime, max_stale_seconds: int) -> tuple[bool, str]:
    if state.last_success_at is None:
        return False, 'replica_not_ready'
    age = max(0, int((now - state.last_success_at).total_seconds()))
    if max_stale_seconds and age > max_stale_seconds:
        return False, 'replica_data_expired'
    return True, ''


def _ensure_utc_datetime(value: datetime | Callable[[], datetime]) -> datetime:
    if callable(value):
        value = value()
    if not isinstance(value, datetime):
        raise TypeError('clock must return a datetime')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc_text(value: datetime | Callable[[], datetime]) -> str:
    return _ensure_utc_datetime(value).isoformat().replace('+00:00', 'Z')


def _parse_utc_text(value: Any) -> Optional[datetime]:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        candidate = value
    else:
        candidate = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        allow_nan=False,
    ).encode('utf-8')


def _normalize_text(value: Any, default: str = '') -> str:
    return str(value if value is not None else default).strip()


def _is_sqlite_corruption_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.Error):
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            'database disk image is malformed',
            'file is not a database',
            'database is malformed',
            'database corruption',
            'malformed database schema',
        )
    )


def quarantine_corrupt_replica_database(
    database_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str],
    *,
    connection: sqlite3.Connection | None = None,
    now: datetime | Callable[[], datetime] | None = None,
) -> pathlib.Path:
    source = pathlib.Path(database_path).resolve()
    data_root = pathlib.Path(data_dir).resolve()
    try:
        source.relative_to(data_root)
    except ValueError as exc:
        raise ValueError('replica database must stay within the configured data directory') from exc

    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass

    timestamp = _ensure_utc_datetime(now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    quarantine_path = source.with_name(f'{source.name}.corrupt-{timestamp}')
    if source.exists():
        source.replace(quarantine_path)
    return quarantine_path


def load_replica_state(conn: sqlite3.Connection) -> ReplicaState:
    rows = {
        str(row['key']): row['value']
        for row in conn.execute(
            '''
            SELECT key, value
            FROM cluster_replica_state
            WHERE key IN ('cursor', 'last_success_at', 'node_id', 'protocol_version', 'last_error')
            '''
        ).fetchall()
    }
    cursor_raw = rows.get('cursor', '0')
    protocol_raw = rows.get('protocol_version', '1')
    try:
        cursor = int(cursor_raw)
        protocol_version = int(protocol_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('replica state is invalid') from exc
    if cursor < 0 or protocol_version < 0:
        raise ValueError('replica state is invalid')
    return ReplicaState(
        cursor=cursor,
        last_success_at=_parse_utc_text(rows.get('last_success_at')),
        node_id=_normalize_text(rows.get('node_id'), 'replica'),
        protocol_version=protocol_version,
        last_error=_normalize_text(rows.get('last_error')),
    )


def save_replica_state(conn: sqlite3.Connection, state: ReplicaState) -> None:
    entries = {
        'cursor': str(int(state.cursor)),
        'node_id': _normalize_text(state.node_id, 'replica') or 'replica',
        'protocol_version': str(int(state.protocol_version)),
        'last_error': _normalize_text(state.last_error),
    }
    if state.last_success_at is None:
        conn.execute("DELETE FROM cluster_replica_state WHERE key = 'last_success_at'")
    else:
        entries['last_success_at'] = _iso_utc_text(state.last_success_at)
    for key, value in entries.items():
        conn.execute(
            '''
            INSERT INTO cluster_replica_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            ''',
            (key, value),
        )


class SQLiteReplicaIdentityStore:
    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        decrypt_sensitive: Callable[[str], str] = lambda value: value,
        encrypt_sensitive: Callable[[str], str] = lambda value: value,
    ):
        self._connection_factory = connection_factory
        self._decrypt_sensitive = decrypt_sensitive
        self._encrypt_sensitive = encrypt_sensitive

    def load(self) -> dict[str, object]:
        conn = self._connection_factory()
        try:
            state = load_replica_state(conn)
            sync_secret_text = _normalize_text(_read_state_value(conn, 'sync_secret'))
            sync_secret = self._decrypt_sensitive(sync_secret_text) if sync_secret_text else ''
            return {
                'node_id': state.node_id,
                'sync_secret': sync_secret,
                'credential_version': int(_read_state_value(conn, 'credential_version') or 1),
            }
        finally:
            conn.close()

    def save(self, identity: dict[str, object]) -> None:
        conn = self._connection_factory()
        try:
            payload = {
                'node_id': _normalize_text(identity.get('node_id'), 'replica') or 'replica',
                'sync_secret': self._encrypt_sensitive(_normalize_text(identity.get('sync_secret'))),
                'credential_version': str(int(identity.get('credential_version') or 1)),
            }
            for key, value in payload.items():
                conn.execute(
                    '''
                    INSERT INTO cluster_replica_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    ''',
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()


class SQLiteReplicaStore:
    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        decrypt_sensitive: Callable[[str], str],
        encrypt_sensitive: Callable[[str], str],
    ):
        self._connection_factory = connection_factory
        self._decrypt_sensitive = decrypt_sensitive
        self._encrypt_sensitive = encrypt_sensitive

    def load_state(self) -> ReplicaState:
        conn = self._connection_factory()
        try:
            return load_replica_state(conn)
        finally:
            conn.close()

    def save_state(self, state: ReplicaState) -> None:
        conn = self._connection_factory()
        try:
            save_replica_state(conn, state)
            conn.commit()
        finally:
            conn.close()

    def read_cursor(self) -> int:
        conn = self._connection_factory()
        try:
            return load_replica_state(conn).cursor
        finally:
            conn.close()

    def apply_snapshot(self, payload: dict[str, Any]) -> int:
        conn = self._connection_factory()
        try:
            with conn:
                return apply_snapshot(conn, payload, self._encrypt_sensitive)
        finally:
            conn.close()

    def apply_increment(self, payload: dict[str, Any]) -> int:
        conn = self._connection_factory()
        try:
            with conn:
                return apply_increment(conn, payload, self._encrypt_sensitive)
        finally:
            conn.close()


def _read_state_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        'SELECT value FROM cluster_replica_state WHERE key = ?',
        (key,),
    ).fetchone()
    return _normalize_text(row['value'] if row else '')


class ReplicaSyncClient:
    def __init__(
        self,
        config: Any,
        identity_store: Any,
        replica_store: Any,
        http_session: Any,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], None],
        *,
        timeout_seconds: int = 30,
    ):
        self.config = config
        self.identity_store = identity_store
        self.replica_store = replica_store
        self.http_session = http_session
        self.clock = clock
        self.sleeper = sleeper
        self.timeout_seconds = int(timeout_seconds)

    def run_once(self) -> int:
        state = self._load_state()
        identity = self._load_identity()
        now = _ensure_utc_datetime(self.clock)
        if state.last_success_at is None:
            response, request_context = self._request(identity, '/api/v1/cluster/sync/snapshot', b'')
            payload = self._decrypt_response(response, identity, request_context)
            committed_cursor, payload = self._apply_snapshot_payload(payload)
            return self._finish_iteration(state, identity, payload, committed_cursor, now)

        response, request_context = self._request(
            identity,
            '/api/v1/cluster/sync/pull',
            _canonical_json_bytes({'cursor': int(state.cursor)}),
        )
        payload = self._decrypt_response(response, identity, request_context)
        result = _normalize_text(payload.get('result'))
        if result == 'snapshot_required':
            response, request_context = self._request(identity, '/api/v1/cluster/sync/snapshot', b'')
            payload = self._decrypt_response(response, identity, request_context)
            committed_cursor, payload = self._apply_snapshot_payload(payload)
            return self._finish_iteration(state, identity, payload, committed_cursor, now)

        committed_cursor, payload = self._apply_increment_payload(payload)
        return self._finish_iteration(state, identity, payload, committed_cursor, now)

    def run_forever(self, stop_event) -> None:
        failure_delay = 5
        success_delay = max(5, min(10, int(getattr(self.config, 'poll_seconds', 10))))
        while not stop_event.is_set():
            try:
                self.run_once()
                failure_delay = 5
                if stop_event.is_set():
                    break
                self.sleeper(success_delay)
            except Exception:
                if stop_event.is_set():
                    break
                self.sleeper(failure_delay)
                failure_delay = min(failure_delay * 2, 60)

    def _load_state(self) -> ReplicaState:
        if hasattr(self.replica_store, 'load_state'):
            state = self.replica_store.load_state()
        else:
            state = load_replica_state(self.replica_store)
        if not isinstance(state, ReplicaState):
            raise TypeError('replica_store.load_state must return ReplicaState')
        return state

    def _save_state(self, state: ReplicaState) -> None:
        if hasattr(self.replica_store, 'save_state'):
            self.replica_store.save_state(state)
        else:
            save_replica_state(self.replica_store, state)

    def _load_identity(self) -> dict[str, object]:
        identity = self.identity_store.load()
        if not isinstance(identity, dict):
            raise TypeError('identity_store.load must return a dict')
        return identity

    def _save_identity(self, identity: dict[str, object]) -> None:
        self.identity_store.save(identity)

    def _request(self, identity: dict[str, object], path: str, body: bytes) -> tuple[Any, dict[str, Any]]:
        node_id = _normalize_text(identity.get('node_id')) or self._load_state().node_id
        credential_version = int(identity.get('credential_version') or 1)
        sync_secret = _decode_sync_secret(str(identity.get('sync_secret') or ''))
        keys = derive_node_keys(sync_secret, credential_version)
        timestamp = int(_ensure_utc_datetime(self.clock).timestamp())
        nonce = _nonce()
        headers = {
            'X-Cluster-Node-Id': node_id,
            'X-Cluster-Protocol-Version': str(CLUSTER_PROTOCOL_VERSION),
            'X-Cluster-Credential-Version': str(credential_version),
            'X-Cluster-Timestamp': str(timestamp),
            'X-Cluster-Nonce': nonce,
            'X-Cluster-Signature': sign_request(
                keys.request_auth,
                CLUSTER_PROTOCOL_VERSION,
                credential_version,
                node_id,
                'POST',
                path,
                body,
                timestamp,
                nonce,
            ),
        }
        response = self.http_session.post(
            self._build_url(path),
            data=body,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if int(getattr(response, 'status_code', 500)) != 200:
            raise ValueError('upgrade_required')
        return response, {
            'node_id': node_id,
            'protocol_version': CLUSTER_PROTOCOL_VERSION,
            'credential_version': credential_version,
            'path': path,
            'timestamp': timestamp,
            'nonce': nonce,
        }

    def _decrypt_response(
        self,
        response,
        identity: dict[str, object],
        request_context: dict[str, Any],
    ) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ClusterCryptoError('invalid encrypted payload')
        metadata = payload.get('metadata')
        envelope = payload.get('envelope')
        if not isinstance(metadata, dict) or not isinstance(envelope, dict):
            raise ClusterCryptoError('invalid encrypted payload')
        expected_metadata = self._expected_metadata(identity, request_context, metadata)
        decrypted = decrypt_json(envelope, self._response_key(identity, metadata), expected_metadata)
        if _normalize_text(decrypted.get('result')) != _normalize_text(metadata.get('result')):
            raise ValueError('upgrade_required')
        return decrypted

    def _expected_metadata(
        self,
        identity: dict[str, object],
        request_context: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        expected = {
            'node_id': _normalize_text(request_context.get('node_id')) or _normalize_text(identity.get('node_id')) or self._load_state().node_id,
            'protocol_version': int(request_context.get('protocol_version') or CLUSTER_PROTOCOL_VERSION),
            'credential_version': int(request_context.get('credential_version') or 1),
            'path': _normalize_text(request_context.get('path')),
            'timestamp': int(request_context.get('timestamp') or 0),
            'nonce': _normalize_text(request_context.get('nonce')),
        }
        if 'result' in metadata:
            expected['result'] = metadata['result']
        if 'cursor' in metadata:
            expected['cursor'] = metadata['cursor']
        if 'snapshot_cursor' in metadata:
            expected['snapshot_cursor'] = metadata['snapshot_cursor']
        if 'from_cursor' in metadata:
            expected['from_cursor'] = metadata['from_cursor']
        if 'next_cursor' in metadata:
            expected['next_cursor'] = metadata['next_cursor']
        return expected

    def _response_key(self, identity: dict[str, object], metadata: dict[str, Any]) -> bytes:
        credential_version = int(metadata.get('credential_version') or identity.get('credential_version') or 1)
        sync_secret = _decode_sync_secret(str(identity.get('sync_secret') or ''))
        return derive_node_keys(sync_secret, credential_version).response_encryption

    def _apply_snapshot_payload(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        result = _normalize_text(payload.get('result'))
        if result != 'snapshot':
            raise ValueError('upgrade_required')
        snapshot = payload.get('snapshot')
        if not isinstance(snapshot, dict):
            raise ValueError('upgrade_required')
        try:
            committed_cursor = self._apply_snapshot(snapshot)
        except (KeyError, ReplicaApplyError, ValueError, TypeError, ClusterCryptoError) as exc:
            self._store_upgrade_required()
            raise ValueError('upgrade_required') from exc
        return committed_cursor, payload

    def _apply_increment_payload(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        result = _normalize_text(payload.get('result'))
        if result != 'increment':
            raise ValueError('upgrade_required')
        increment = payload.get('increment')
        if not isinstance(increment, dict):
            raise ValueError('upgrade_required')
        try:
            committed_cursor = self._apply_increment(increment)
        except (KeyError, ReplicaApplyError, ValueError, TypeError, ClusterCryptoError) as exc:
            self._store_upgrade_required()
            raise ValueError('upgrade_required') from exc
        return committed_cursor, payload

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> int:
        if hasattr(self.replica_store, 'apply_snapshot'):
            return int(self.replica_store.apply_snapshot(snapshot))
        return int(apply_snapshot(self.replica_store, snapshot, self._encrypt_sensitive))

    def _apply_increment(self, increment: dict[str, Any]) -> int:
        if hasattr(self.replica_store, 'apply_increment'):
            return int(self.replica_store.apply_increment(increment))
        return int(apply_increment(self.replica_store, increment, self._encrypt_sensitive))

    def _encrypt_sensitive(self, value: str) -> str:
        if hasattr(self.replica_store, 'encrypt_sensitive'):
            return self.replica_store.encrypt_sensitive(value)
        return value

    def _finish_iteration(
        self,
        previous_state: ReplicaState,
        identity: dict[str, object],
        payload: dict[str, Any],
        committed_cursor: int,
        now: datetime,
    ) -> int:
        next_credential = payload.get('next_credential')
        active_identity = dict(identity)
        if isinstance(next_credential, dict):
            credential = self._normalize_next_credential(
                next_credential,
                current_credential_version=int(active_identity.get('credential_version') or previous_state.protocol_version or 1),
                now=now,
            )
            active_identity['sync_secret'] = credential['sync_secret']
            active_identity['credential_version'] = credential['credential_version']
            self._save_identity(active_identity)

        self._ack(active_identity, committed_cursor)
        success_state = replace(
            previous_state,
            cursor=committed_cursor,
            last_success_at=now,
            last_error='',
            node_id=_normalize_text(active_identity.get('node_id')) or previous_state.node_id,
            protocol_version=previous_state.protocol_version,
        )
        self._save_state(success_state)
        return committed_cursor

    def _ack(self, identity: dict[str, object], cursor: int) -> None:
        identity = dict(identity)
        response, request_context = self._request(
            identity,
            '/api/v1/cluster/sync/ack',
            _canonical_json_bytes({'cursor': int(cursor)}),
        )
        payload = self._decrypt_response(response, identity, request_context)
        ack_cursor = payload.get('cursor', -1)
        if ack_cursor is None:
            ack_cursor = -1
        if _normalize_text(payload.get('result')) != 'acknowledged' or int(ack_cursor) != int(cursor):
            raise ValueError('upgrade_required')

    def _normalize_next_credential(
        self,
        payload: dict[str, Any],
        *,
        current_credential_version: int,
        now: datetime,
    ) -> dict[str, Any]:
        sync_secret = _normalize_text(payload.get('sync_secret'))
        credential_version = int(payload.get('credential_version') or 0)
        expires_at = _normalize_text(payload.get('expires_at'))
        if not sync_secret or credential_version <= current_credential_version or not expires_at:
            raise ValueError('upgrade_required')
        expires_at_dt = _parse_utc_text(expires_at)
        if expires_at_dt is None or expires_at_dt <= now:
            raise ValueError('upgrade_required')
        return {
            'sync_secret': sync_secret,
            'credential_version': credential_version,
            'expires_at': expires_at,
        }

    def _store_upgrade_required(self) -> None:
        state = self._load_state()
        self._save_state(replace(state, last_error='upgrade_required'))

    def _build_url(self, path: str) -> str:
        base = str(getattr(self.config, 'master_url', '') or '').rstrip('/')
        return f'{base}{path}'


_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()
_worker_lock_handle: Any = None


def _nonce() -> str:
    return f'{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}'


def _decode_sync_secret(sync_secret: str) -> bytes:
    if not sync_secret:
        raise ValueError('sync_secret is required')
    padding = '=' * (-len(sync_secret) % 4)
    return __import__('base64').urlsafe_b64decode((sync_secret + padding).encode('ascii'))


def _state_lock_path(data_dir: str | os.PathLike[str]) -> pathlib.Path:
    return pathlib.Path(data_dir).resolve() / 'replica-sync.lock'


def _acquire_process_lock(lock_path: pathlib.Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        handle.close()
        return None
    return handle


def start_replica_sync_worker(
    *,
    client_factory: Callable[[], ReplicaSyncClient],
    stop_event: threading.Event | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    daemon: bool = True,
) -> threading.Thread | None:
    global _worker_thread, _worker_lock_handle
    if os.getenv('PYTEST_CURRENT_TEST'):
        return None
    if stop_event is None:
        stop_event = threading.Event()
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return _worker_thread
        if lock_path is not None:
            _worker_lock_handle = _acquire_process_lock(pathlib.Path(lock_path))
            if _worker_lock_handle is None:
                return None

        def _run() -> None:
            client = client_factory()
            client.run_forever(stop_event)

        worker = threading.Thread(
            target=_run,
            name='replica-sync-client',
            daemon=daemon,
        )
        worker.start()
        _worker_thread = worker
        return worker


__all__ = [
    'ReplicaState',
    'ReplicaSyncClient',
    'ReplicaApplyError',
    'SQLiteReplicaIdentityStore',
    'SQLiteReplicaStore',
    'load_replica_state',
    'quarantine_corrupt_replica_database',
    'replica_readiness',
    'save_replica_state',
    'start_replica_sync_worker',
]
