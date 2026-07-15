from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_VERSION = 1
ENVELOPE_CONTENT_TYPE = "application/vnd.fmbsm.encrypted+json"
ENVELOPE_AAD = b"FMBSM-TOKEN-BUNDLE-V1"
MAX_ENVELOPE_BYTES = 36 * 1024 * 1024


class EnvelopeError(ValueError):
    pass


def load_private_key(path: Path):
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:
        raise EnvelopeError(f"Cannot load the token transport private key: {exc}") from exc


def decrypt_envelope(payload: bytes, private_key) -> bytes:
    if len(payload) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError("Encrypted upload exceeds the envelope size limit")
    try:
        envelope = json.loads(payload.decode("utf-8"))
        if not isinstance(envelope, dict) or int(envelope.get("version", 0)) != ENVELOPE_VERSION:
            raise ValueError("unsupported envelope version")
        encrypted_key = _decode(envelope["encrypted_key"])
        nonce = _decode(envelope["nonce"])
        ciphertext = _decode(envelope["ciphertext"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"Malformed encrypted upload: {exc}") from exc
    if len(nonce) != 12:
        raise EnvelopeError("Encrypted upload has an invalid nonce")
    try:
        aes_key = private_key.decrypt(
            encrypted_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=ENVELOPE_AAD,
            ),
        )
        if len(aes_key) != 32:
            raise ValueError("invalid AES key length")
        return AESGCM(aes_key).decrypt(nonce, ciphertext, ENVELOPE_AAD)
    except Exception as exc:
        raise EnvelopeError("Encrypted upload authentication/decryption failed") from exc


def _decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("envelope field is not text")
    return base64.b64decode(value, validate=True)
