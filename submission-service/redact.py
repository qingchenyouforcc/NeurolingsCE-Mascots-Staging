"""Sensitive-data redaction for logs and error messages."""

from __future__ import annotations

import re

_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-github-token",
}

_TOKEN_VALUE_RE = re.compile(
    r"(?i)(bearer\s+|token\s*=\s*|access_token[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_\-\.]{8,})"
)

_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(\"?(?:access_token|refresh_token|github_token|client_secret"
    r"|private_key|password)\"?\s*[:=]\s*[\"']?)[^,\s\"'}]+"
)


def redact_text(text: str) -> str:
    """Replace token-like values with [REDACTED]."""
    if not text:
        return text
    text = _TOKEN_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_FIELD_RE.sub(r"\1[REDACTED]", text)
    return text


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in headers.items():
        if key.lower() in _SENSITIVE_HEADER_NAMES:
            out[key] = "[REDACTED]"
        else:
            out[key] = value
    return out


def redact_error_message(message: str) -> str:
    return redact_text(message)
