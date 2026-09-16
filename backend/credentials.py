"""Credentials never cross the HTTP boundary after being saved."""
from __future__ import annotations

import base64
import os
import uuid


def protect(value: str) -> str:
    if not value or value.startswith(("dpapi:", "keyring:")):
        return value
    if os.name == "nt":
        import win32crypt
        return "dpapi:" + base64.b64encode(win32crypt.CryptProtectData(value.encode(), "AzurJuus", None, None, None, 0)).decode()
    import keyring
    reference = uuid.uuid4().hex
    keyring.set_password("AzurJuus", reference, value)
    return "keyring:" + reference


def reveal(value: str) -> str:
    if value.startswith("dpapi:"):
        import win32crypt
        return win32crypt.CryptUnprotectData(base64.b64decode(value[6:]), None, None, None, 0)[1].decode()
    if value.startswith("keyring:"):
        import keyring
        return keyring.get_password("AzurJuus", value[8:]) or ""
    return value  # Read legacy values during the one-time migration.


def public_settings(settings: dict) -> dict:
    result = dict(settings)
    for key in [k for k in result if k.lower().endswith("apikey")]:
        result[key + "Configured"] = bool(result.pop(key, ""))
    return result


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(s in k.lower() for s in ("apikey", "api_key", "authorization", "password", "secret", "token")) and not k.endswith("Configured") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
