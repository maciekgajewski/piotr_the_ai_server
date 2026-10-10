from __future__ import annotations

import base64
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class DeviceIdentity:
    """One durable identity per logical device; malformed keys never regenerate."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        raw_public_key = key.public_key().public_bytes_raw()
        self.device_id = hashlib.sha256(raw_public_key).hexdigest()
        self.public_key = _base64url(raw_public_key)

    @classmethod
    def load_or_create(cls, path: Path) -> DeviceIdentity:
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise ValueError("identity directory must be owned by this user and not writable by others")
        if not path.exists() and not path.is_symlink():
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            descriptor, temporary_name = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(pem)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary_name, path)
                except FileExistsError:
                    pass  # Another process published the durable identity first.
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                os.unlink(temporary_name)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError("identity must be a private regular file owned by this user")
            if info.st_size > 4096:
                raise ValueError("identity file is too large")
            pem = stream.read(4097)
        if len(pem) > 4096:
            raise ValueError("identity file is too large")
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("identity must contain an Ed25519 private key")
        return cls(key)

    def sign_connect(
        self, *, client_id: str, mode: str, role: str, scopes: tuple[str, ...],
        token: str | None, nonce: str, timestamp_ms: int, platform: str, device_family: str,
    ) -> dict[str, str | int]:
        # OpenClaw 2026.9.8 buildDeviceAuthPayloadV3: metadata folds ASCII only.
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        payload = "|".join((
            "v3", self.device_id, client_id, mode, role, ",".join(scopes),
            str(timestamp_ms), token or "", nonce,
            platform.strip().translate(ascii_lower), device_family.strip().translate(ascii_lower),
        ))
        return {
            "id": self.device_id,
            "publicKey": self.public_key,
            "signature": _base64url(self._key.sign(payload.encode("utf-8"))),
            "signedAt": timestamp_ms,
            "nonce": nonce,
        }


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
