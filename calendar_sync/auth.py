"""Admin password handling: policy and salted scrypt hashing (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

MIN_PASSWORD_LENGTH = 12
_N, _R, _P = 2**14, 8, 1
HASH_SETTING_KEY = "admin_password_hash"


def password_problems(new: str, confirm: str | None = None, current: str | None = None) -> list[str]:
    problems = []
    if len(new) < MIN_PASSWORD_LENGTH:
        problems.append(f"Heslo musí mít alespoň {MIN_PASSWORD_LENGTH} znaků.")
    if len(new) > 256:
        problems.append("Heslo je příliš dlouhé (max. 256 znaků).")
    if confirm is not None and new != confirm:
        problems.append("Nové heslo a jeho potvrzení se neshodují.")
    if current is not None and new == current:
        problems.append("Nové heslo musí být jiné než současné.")
    if new.strip() != new:
        problems.append("Heslo nesmí začínat ani končit mezerou.")
    return problems


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
