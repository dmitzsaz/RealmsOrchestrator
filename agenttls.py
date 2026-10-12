"""TLS for the agent listener with nothing to manage.

The key and a self-signed certificate are derived from AGENT_SECRET, so they
are the same after every restart and never expire. Agents get the
certificate's SHA-256 in their env and accept only that certificate: no CA,
domain or renewal is involved, and an attacker without AGENT_SECRET cannot
impersonate the orchestrator.
"""
import datetime
import hashlib
import hmac
import os
import ssl
import tempfile
from functools import lru_cache

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from config import settings


@lru_cache(maxsize=1)
def _material() -> tuple[bytes, bytes, str]:
    # Ed25519 signatures are deterministic, so the certificate is byte for
    # byte the same each time and its fingerprint stays valid for running agents.
    seed = hmac.new(settings.agent_secret, b"realms-agent-tls-v1", hashlib.sha256).digest()
    key = Ed25519PrivateKey.from_private_bytes(seed)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "realms-orchestrator")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc))
        # RFC 5280: no well-defined expiration date.
        .not_valid_after(datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc))
        .sign(key, None)
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert_pem, key_pem, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def fingerprint() -> str:
    """SHA-256 of the certificate (hex), passed to agents as their pin."""
    return _material()[2]


def server_context() -> ssl.SSLContext:
    cert_pem, key_pem, _ = _material()
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    # load_cert_chain only reads files.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "agent.pem")
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(key_pem + cert_pem)
        context.load_cert_chain(path)
    return context
