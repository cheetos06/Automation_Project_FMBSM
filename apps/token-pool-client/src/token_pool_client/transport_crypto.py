from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ENVELOPE_CONTENT_TYPE = "application/vnd.fmbsm.encrypted+json"
ENVELOPE_AAD = b"FMBSM-TOKEN-BUNDLE-V1"


def encrypt_bundle(bundle: bytes, certificate_path: Path) -> bytes:
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    except Exception as exc:
        raise RuntimeError(f"Cannot load the pinned server certificate: {exc}") from exc
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
        raise RuntimeError("The pinned server certificate does not contain a supported RSA key")
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes_key).encrypt(nonce, bundle, ENVELOPE_AAD)
    encrypted_key = public_key.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=ENVELOPE_AAD,
        ),
    )
    envelope = {
        "version": 1,
        "algorithm": "RSA-OAEP-256+A256GCM",
        "encrypted_key": _encode(encrypted_key),
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
