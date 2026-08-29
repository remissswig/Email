from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import sqlite3
import sys
import tempfile
from typing import Any, TextIO
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from outlook_web.runtime import resolve_secret_key

from .crypto import (
    ClusterCryptoError,
    decrypt_json,
    derive_enrollment_key,
    encrypt_json,
    generate_x25519_keypair,
    public_key_fingerprint,
)
from .storage import initialize_replica_schema


EXIT_INVALID_FINGERPRINT = 11
EXIT_TOKEN_UNAVAILABLE = 12
EXIT_NODE_REVOKED = 13
EXIT_NETWORK_ERROR = 14
EXIT_STATE_CONFLICT = 15
EXIT_PROTOCOL_ERROR = 16

_IDENTITY_DB_NAME = 'identity.db'
_FINGERPRINT_RE = re.compile(r'^SHA256:[0-9a-fA-F]{64}$')
_NODE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_ENCRYPTION_SALT = b'outlook_email_encryption_salt_v1'
_REQUEST_TIMEOUT_SECONDS = 30


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m outlook_web.cluster.cli')
    subparsers = parser.add_subparsers(dest='command', required=True)
    enroll = subparsers.add_parser('enroll')
    enroll.add_argument('--master', required=True)
    enroll.add_argument('--node-id', required=True)
    enroll.add_argument('--master-fingerprint', required=True)
    enroll.add_argument('--identity-dir', required=True)
    return parser


def _validate_master_url(value: str) -> str:
    parsed = urlsplit(str(value or '').strip())
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('master URL must be an absolute http or https URL without query or fragment')
    normalized_path = parsed.path.rstrip('/')
    if normalized_path == '/':
        normalized_path = ''
    normalized = SplitResult(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc,
        path=normalized_path,
        query='',
        fragment='',
    )
    return urlunsplit(normalized)


def _validate_node_id(value: str) -> str:
    node_id = str(value or '').strip()
    if not _NODE_ID_RE.fullmatch(node_id):
        raise ValueError('node id must use letters, digits, dot, dash, or underscore')
    return node_id


def _validate_fingerprint(value: str) -> str:
    fingerprint = str(value or '').strip()
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise ValueError('master fingerprint must look like SHA256:<64 hex chars>')
    return fingerprint


def _read_token_line(stdin: TextIO) -> str:
    line = stdin.readline()
    if line == '':
        raise ValueError('enrollment token is required on stdin')
    token = line.rstrip('\r\n')
    remainder = stdin.read()
    if token.strip() != token or not token or any(character not in '\r\n' for character in remainder):
        raise ValueError('stdin must contain exactly one enrollment token line')
    return token


def _identity_db_path(identity_dir: pathlib.Path) -> pathlib.Path:
    return identity_dir / _IDENTITY_DB_NAME


def _connect_database(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _derive_cipher(secret_key: str) -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_ENCRYPTION_SALT,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(secret_key.encode('utf-8')))
    return Fernet(key)


def _encrypt_sensitive(secret_key: str, value: str) -> str:
    if not value or value.startswith('enc:'):
        return value
    return 'enc:' + _derive_cipher(secret_key).encrypt(value.encode('utf-8')).decode('utf-8')


def _decrypt_sensitive(secret_key: str, value: str) -> str:
    if not value or not value.startswith('enc:'):
        return value
    return _derive_cipher(secret_key).decrypt(value[4:].encode('utf-8')).decode('utf-8')


def _load_existing_identity(identity_dir: pathlib.Path, secret_key: str) -> dict[str, Any] | None:
    db_path = _identity_db_path(identity_dir)
    if not db_path.exists():
        return None
    try:
        conn = _connect_database(db_path)
        try:
            rows = {
                str(row['key']): str(row['value'] or '')
                for row in conn.execute('SELECT key, value FROM cluster_replica_state').fetchall()
            }
        finally:
            conn.close()
        sync_secret = _decrypt_sensitive(secret_key, rows.get('sync_secret', ''))
        credential_version = int(rows.get('credential_version', '1'))
    except (sqlite3.Error, InvalidToken, ValueError) as exc:
        raise ValueError('existing identity is invalid') from exc
    identity = {
        'node_id': rows.get('node_id', ''),
        'sync_secret': sync_secret,
        'credential_version': credential_version,
        'master_url': rows.get('master_url', ''),
        'master_fingerprint': rows.get('master_fingerprint', ''),
    }
    if not identity['node_id'] or not identity['sync_secret'] or credential_version < 1:
        raise ValueError('existing identity is invalid')
    return identity


def _ensure_identity_directory(identity_dir: pathlib.Path) -> None:
    identity_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(identity_dir, 0o700)
    except OSError:
        pass


def _write_identity_db(identity_dir: pathlib.Path, payload: dict[str, Any], secret_key: str) -> None:
    _ensure_identity_directory(identity_dir)
    target_path = _identity_db_path(identity_dir)
    existing = _load_existing_identity(identity_dir, secret_key) if target_path.exists() else None
    if existing is not None:
        comparable = {
            'node_id': payload['node_id'],
            'sync_secret': payload['sync_secret'],
            'credential_version': payload['credential_version'],
            'master_url': payload['master_url'],
            'master_fingerprint': payload['master_fingerprint'],
        }
        if existing != comparable:
            raise FileExistsError('existing identity differs from requested node')
        return

    temp_handle, temp_name = tempfile.mkstemp(prefix='identity.db.tmp-', dir=str(identity_dir))
    os.close(temp_handle)
    temp_path = pathlib.Path(temp_name)
    try:
        conn = _connect_database(temp_path)
        try:
            initialize_replica_schema(conn)
            rows = {
                'node_id': payload['node_id'],
                'sync_secret': _encrypt_sensitive(secret_key, payload['sync_secret']),
                'credential_version': str(int(payload['credential_version'])),
                'master_url': payload['master_url'],
                'master_fingerprint': payload['master_fingerprint'],
            }
            for key, value in rows.items():
                conn.execute(
                    '''
                    INSERT INTO cluster_replica_state (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    ''',
                    (key, str(value)),
                )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, target_path)
        try:
            os.chmod(target_path, 0o600)
        except OSError:
            pass
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _build_enrollment_request(token: str, node_id: str, fingerprint: str, primary_public_key: bytes) -> tuple[dict[str, Any], bytes]:
    request_private_key, request_public_key = generate_x25519_keypair()
    node_private_key, node_public_key = generate_x25519_keypair()
    request_key = derive_enrollment_key(
        request_private_key,
        primary_public_key,
        fingerprint.encode('utf-8'),
    )
    request_body = {
        'node_id': node_id,
        'primary_fingerprint': fingerprint,
        'ephemeral_public_key': _b64url_encode(request_public_key),
        'envelope': encrypt_json(
            {
                'token': token,
                'node_public_key': _b64url_encode(node_public_key),
            },
            request_key,
            {
                'node_id': node_id,
                'primary_fingerprint': fingerprint,
            },
        ),
    }
    return request_body, node_private_key


def _parse_error_response(payload: dict[str, Any]) -> tuple[int, str]:
    code = str(payload.get('error_code') or '').strip()
    if code == 'primary_fingerprint_mismatch':
        return EXIT_INVALID_FINGERPRINT, 'master fingerprint mismatch'
    if code in {'enrollment_token_expired', 'enrollment_token_unavailable'}:
        return EXIT_TOKEN_UNAVAILABLE, 'enrollment token expired or unavailable'
    if code == 'cluster_node_revoked':
        return EXIT_NODE_REVOKED, 'node has been revoked'
    if code in {'cluster_node_not_pending', 'cluster_node_activation_failed', 'cluster_node_missing'}:
        return EXIT_STATE_CONFLICT, 'existing identity or node state conflicts with enrollment'
    return EXIT_PROTOCOL_ERROR, 'master returned an invalid enrollment response'


def _parse_response_payload(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise ClusterCryptoError('invalid enrollment response')
    return payload


def _decode_success_response(
    payload: dict[str, Any],
    *,
    node_id: str,
    master_fingerprint: str,
    node_private_key: bytes,
    primary_public_key: bytes,
) -> dict[str, Any]:
    response_fingerprint = _validate_fingerprint(str(payload.get('primary_fingerprint') or ''))
    if response_fingerprint != master_fingerprint:
        raise ValueError('master fingerprint mismatch')
    envelope = payload.get('enrollment')
    if not isinstance(envelope, dict):
        raise ClusterCryptoError('invalid enrollment response')
    response_key = derive_enrollment_key(
        node_private_key,
        primary_public_key,
        master_fingerprint.encode('utf-8'),
    )
    decrypted = decrypt_json(
        envelope,
        response_key,
        {
            'node_id': node_id,
            'primary_fingerprint': master_fingerprint,
        },
    )
    sync_secret = str(decrypted.get('sync_secret') or '').strip()
    returned_node_id = str(decrypted.get('node_id') or '').strip()
    credential_version = int(decrypted.get('credential_version') or 0)
    if returned_node_id != node_id or not sync_secret or credential_version < 1:
        raise ClusterCryptoError('invalid enrollment response')
    return {
        'node_id': returned_node_id,
        'sync_secret': sync_secret,
        'credential_version': credential_version,
    }


def _post_enrollment(
    *,
    master_url: str,
    node_id: str,
    master_fingerprint: str,
    token: str,
    http_session: Any,
) -> dict[str, Any]:
    fingerprint_value = master_fingerprint
    primary_public_key = _fetch_primary_public_key(master_url, fingerprint_value, http_session)
    request_body, node_private_key = _build_enrollment_request(
        token,
        node_id,
        fingerprint_value,
        primary_public_key,
    )
    response = http_session.post(
        f'{master_url}/api/v1/cluster/nodes/enroll',
        json=request_body,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    payload = _parse_response_payload(response)
    if int(getattr(response, 'status_code', 500)) != 200 or not payload.get('success'):
        exit_code, message = _parse_error_response(payload)
        raise EnrollmentCliError(exit_code, message)
    identity = _decode_success_response(
        payload,
        node_id=node_id,
        master_fingerprint=fingerprint_value,
        node_private_key=node_private_key,
        primary_public_key=primary_public_key,
    )
    identity['master_url'] = master_url
    identity['master_fingerprint'] = master_fingerprint
    return identity


class EnrollmentCliError(RuntimeError):
    def __init__(self, exit_code: int, message: str):
        super().__init__(message)
        self.exit_code = int(exit_code)
        self.message = message


def _fetch_primary_public_key(master_url: str, master_fingerprint: str, http_session: Any) -> bytes:
    response = http_session.get(
        f'{master_url}/api/v1/cluster/identity',
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    payload = _parse_response_payload(response)
    if int(getattr(response, 'status_code', 500)) != 200 or not payload.get('success'):
        raise ClusterCryptoError('invalid identity response')
    response_fingerprint = _validate_fingerprint(str(payload.get('primary_fingerprint') or ''))
    if response_fingerprint != master_fingerprint:
        raise ValueError('master fingerprint mismatch')
    public_key = _b64url_decode(str(payload.get('primary_public_key') or ''))
    if public_key_fingerprint(public_key) != master_fingerprint:
        raise ValueError('master fingerprint mismatch')
    return public_key


def _resolve_secret_key(environ: dict[str, str] | None = None) -> str:
    original_secret = os.environ.get('SECRET_KEY')
    try:
        if environ is not None:
            if 'SECRET_KEY' in environ:
                os.environ['SECRET_KEY'] = environ['SECRET_KEY']
            elif 'SECRET_KEY' in os.environ:
                del os.environ['SECRET_KEY']
        secret_key = resolve_secret_key()
    finally:
        if original_secret is None:
            os.environ.pop('SECRET_KEY', None)
        else:
            os.environ['SECRET_KEY'] = original_secret
    if not secret_key:
        raise EnrollmentCliError(EXIT_STATE_CONFLICT, 'SECRET_KEY is required for replica identity encryption')
    return secret_key


def _run_enroll(
    args: argparse.Namespace,
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    http_session: Any,
    environ: dict[str, str] | None,
) -> int:
    master_url = _validate_master_url(args.master)
    node_id = _validate_node_id(args.node_id)
    master_fingerprint = _validate_fingerprint(args.master_fingerprint)
    identity_dir = pathlib.Path(args.identity_dir).expanduser()
    secret_key = _resolve_secret_key(environ)

    try:
        existing = _load_existing_identity(identity_dir, secret_key)
    except ValueError as exc:
        raise EnrollmentCliError(EXIT_STATE_CONFLICT, str(exc)) from exc
    if existing is not None:
        if (
            existing.get('node_id') == node_id
            and existing.get('master_url') == master_url
            and existing.get('master_fingerprint') == master_fingerprint
        ):
            stdout.write('reusing existing identity\n')
            return 0
        raise EnrollmentCliError(EXIT_STATE_CONFLICT, 'existing identity conflicts with requested node')

    token = _read_token_line(stdin)
    try:
        identity = _post_enrollment(
            master_url=master_url,
            node_id=node_id,
            master_fingerprint=master_fingerprint,
            token=token,
            http_session=http_session,
        )
        _write_identity_db(identity_dir, identity, secret_key)
    except requests.Timeout as exc:
        raise EnrollmentCliError(EXIT_NETWORK_ERROR, 'network timeout while contacting master') from exc
    except requests.ConnectionError as exc:
        raise EnrollmentCliError(EXIT_NETWORK_ERROR, 'network error while contacting master') from exc
    except requests.RequestException as exc:
        raise EnrollmentCliError(EXIT_NETWORK_ERROR, 'network error while contacting master') from exc
    except FileExistsError as exc:
        raise EnrollmentCliError(EXIT_STATE_CONFLICT, str(exc)) from exc
    except EnrollmentCliError:
        raise
    except (ClusterCryptoError, ValueError, TypeError, sqlite3.Error) as exc:
        message = str(exc)
        if message == 'master fingerprint mismatch':
            raise EnrollmentCliError(EXIT_INVALID_FINGERPRINT, 'master fingerprint mismatch') from exc
        raise EnrollmentCliError(EXIT_PROTOCOL_ERROR, 'invalid enrollment response from master') from exc

    stdout.write('identity ready\n')
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    http_session: Any | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    parser = _parser()
    parsed = parser.parse_args(argv)
    active_stdin = stdin or sys.stdin
    active_stdout = stdout or sys.stdout
    active_stderr = stderr or sys.stderr
    session = http_session or requests.Session()
    try:
        if parsed.command == 'enroll':
            return _run_enroll(
                parsed,
                stdin=active_stdin,
                stdout=active_stdout,
                stderr=active_stderr,
                http_session=session,
                environ=environ,
            )
        parser.error(f'unknown command: {parsed.command}')
    except EnrollmentCliError as exc:
        active_stderr.write(f'{exc.message}\n')
        return exc.exit_code
    except ValueError as exc:
        active_stderr.write(f'{exc}\n')
        return 2
    return 2


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _b64url_decode(value: str) -> bytes:
    encoded = value.encode('ascii')
    padding = b'=' * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


if __name__ == '__main__':
    raise SystemExit(main())
