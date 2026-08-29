from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENROLLMENT_LABEL = b'outlook-email/enrollment/v1'
_REQUEST_LABEL = b'outlook-email/request/v1/'
_RESPONSE_LABEL = b'outlook-email/response/v1/'
_NONCE_SIZE = 12


class ClusterCryptoError(ValueError):
    pass


@dataclass(frozen=True)
class NodeKeys:
    request_auth: bytes
    response_encryption: bytes


def derive_node_keys(secret: bytes, credential_version: int) -> NodeKeys:
    secret_bytes = _require_bytes(secret, 'secret')
    version = _ascii_version(credential_version)
    request_auth = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_REQUEST_LABEL + version,
    ).derive(secret_bytes)
    response_encryption = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_RESPONSE_LABEL + version,
    ).derive(secret_bytes)
    return NodeKeys(request_auth=request_auth, response_encryption=response_encryption)


def generate_x25519_keypair() -> tuple[bytes, bytes]:
    private_key = x25519.X25519PrivateKey.generate()
    public_key = private_key.public_key()
    return (
        private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )


def public_key_fingerprint(public_key: bytes) -> str:
    public_key_bytes = _require_bytes(public_key, 'public_key')
    _load_public_key(public_key_bytes)
    return f'SHA256:{hashlib.sha256(public_key_bytes).hexdigest()}'


def derive_enrollment_key(private_key: bytes, peer_public_key: bytes, context: bytes) -> bytes:
    private_key_bytes = _require_bytes(private_key, 'private_key')
    peer_public_key_bytes = _require_bytes(peer_public_key, 'peer_public_key')
    context_bytes = _require_bytes(context, 'context')
    try:
        shared_secret = _load_private_key(private_key_bytes).exchange(
            _load_public_key(peer_public_key_bytes)
        )
    except ValueError as exc:
        raise ClusterCryptoError('invalid enrollment key material') from exc

    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_ENROLLMENT_LABEL + b'\0' + context_bytes,
    ).derive(shared_secret)


def encrypt_json(payload: dict, key: bytes, associated_data: dict) -> dict:
    payload_bytes = _canonical_json_bytes(_require_dict(payload, 'payload'))
    aad_bytes = _canonical_json_bytes(_require_dict(associated_data, 'associated_data'))
    key_bytes = _require_aes_key(key)
    nonce = os.urandom(_NONCE_SIZE)
    ciphertext = AESGCM(key_bytes).encrypt(nonce, payload_bytes, aad_bytes)
    return {
        'nonce': _urlsafe_b64encode(nonce),
        'ciphertext': _urlsafe_b64encode(ciphertext),
    }


def decrypt_json(envelope: dict, key: bytes, associated_data: dict) -> dict:
    envelope_dict = _require_dict(envelope, 'envelope')
    aad_bytes = _canonical_json_bytes(_require_dict(associated_data, 'associated_data'))
    key_bytes = _require_aes_key(key)
    try:
        nonce_text = envelope_dict['nonce']
        ciphertext_text = envelope_dict['ciphertext']
        if not isinstance(nonce_text, str) or not isinstance(ciphertext_text, str):
            raise TypeError('envelope fields must be strings')
        nonce = _urlsafe_b64decode(nonce_text)
        ciphertext = _urlsafe_b64decode(ciphertext_text)
        if len(nonce) != _NONCE_SIZE:
            raise ValueError('nonce must be 12 bytes')
        plaintext = AESGCM(key_bytes).decrypt(nonce, ciphertext, aad_bytes)
        payload = json.loads(plaintext.decode('utf-8'), parse_constant=_reject_json_constant)
        if not isinstance(payload, dict):
            raise TypeError('payload must be a JSON object')
        return payload
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, InvalidTag, binascii.Error) as exc:
        raise ClusterCryptoError('invalid encrypted payload') from exc


def sign_request(
    key: bytes,
    protocol_version: int,
    credential_version: int,
    node_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> str:
    key_bytes = _require_hmac_key(key)
    signing_input = _request_signature_input(
        protocol_version,
        credential_version,
        node_id,
        method,
        path,
        body,
        timestamp,
        nonce,
    )
    digest = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    return _urlsafe_b64encode(digest)


def verify_request_signature(
    key: bytes,
    signature: str,
    protocol_version: int,
    credential_version: int,
    node_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> bool:
    if not isinstance(signature, str):
        return False
    try:
        provided_signature = _urlsafe_b64decode(signature)
        expected_signature = hmac.new(
            _require_hmac_key(key),
            _request_signature_input(
                protocol_version,
                credential_version,
                node_id,
                method,
                path,
                body,
                timestamp,
                nonce,
            ),
            hashlib.sha256,
        ).digest()
    except (ClusterCryptoError, TypeError, ValueError, binascii.Error):
        return False
    return hmac.compare_digest(provided_signature, expected_signature)


def _ascii_version(value: int) -> bytes:
    return str(_require_non_negative_int(value, 'credential version')).encode('ascii')


def _canonical_json_bytes(value: dict) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ClusterCryptoError('invalid JSON payload') from exc


def _request_signature_input(
    protocol_version: int,
    credential_version: int,
    node_id: str,
    method: str,
    path: str,
    body: bytes,
    timestamp: int,
    nonce: str,
) -> bytes:
    if not isinstance(body, bytes):
        raise ClusterCryptoError('invalid body')
    body_bytes = body
    protocol_version_value = _require_non_negative_int(protocol_version, 'protocol version')
    credential_version_value = _require_non_negative_int(
        credential_version,
        'credential version',
    )
    timestamp_value = _require_non_negative_int(timestamp, 'timestamp')
    node_id_text = _require_text(node_id, 'node_id')
    method_text = _require_text(method, 'method').upper()
    path_text = _require_text(path, 'path')
    nonce_text = _require_text(nonce, 'nonce')
    canonical = {
        'body_sha256': hashlib.sha256(body_bytes).hexdigest(),
        'credential_version': credential_version_value,
        'method': method_text,
        'node_id': node_id_text,
        'nonce': nonce_text,
        'path': path_text,
        'protocol_version': protocol_version_value,
        'timestamp': timestamp_value,
    }
    return _canonical_json_bytes(canonical)


def _load_private_key(private_key: bytes) -> x25519.X25519PrivateKey:
    try:
        return x25519.X25519PrivateKey.from_private_bytes(private_key)
    except ValueError as exc:
        raise ClusterCryptoError('invalid private key') from exc


def _load_public_key(public_key: bytes) -> x25519.X25519PublicKey:
    try:
        return x25519.X25519PublicKey.from_public_bytes(public_key)
    except ValueError as exc:
        raise ClusterCryptoError('invalid public key') from exc


def _require_dict(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ClusterCryptoError(f'invalid {name}')
    return value


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ClusterCryptoError(f'invalid {name}')
    return value


def _require_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise ClusterCryptoError(f'invalid {name}')
    return value


def _require_non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClusterCryptoError(f'invalid {name}')
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f'invalid JSON constant: {value}')


def _require_aes_key(key: object) -> bytes:
    key_bytes = _require_bytes(key, 'key')
    if len(key_bytes) not in {16, 24, 32}:
        raise ClusterCryptoError('invalid key')
    return key_bytes


def _require_hmac_key(key: object) -> bytes:
    key_bytes = _require_bytes(key, 'key')
    return key_bytes


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _urlsafe_b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError('invalid base64 value')
    try:
        encoded = value.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError('invalid base64 value') from exc
    padding = b'=' * (-len(encoded) % 4)
    return base64.b64decode(encoded + padding, altchars=b'-_', validate=True)


__all__ = [
    'ClusterCryptoError',
    'NodeKeys',
    'decrypt_json',
    'derive_enrollment_key',
    'derive_node_keys',
    'encrypt_json',
    'generate_x25519_keypair',
    'public_key_fingerprint',
    'sign_request',
    'verify_request_signature',
]
