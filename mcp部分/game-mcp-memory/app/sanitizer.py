from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


REDACTED = "[REDACTED]"

SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "jwt",
    "password",
    "private_key",
    "secret",
    "session_key",
    "token",
}

SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"
    ),
)


def sanitize_text(value: str) -> str:
    sanitized = value
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(REDACTED, sanitized)
    return sanitized


def sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            result[str(key)] = (
                REDACTED if normalized in SENSITIVE_KEYS else sanitize_value(item)
            )
        return result

    if isinstance(value, str):
        return sanitize_text(value)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_value(item) for item in value]

    return value
