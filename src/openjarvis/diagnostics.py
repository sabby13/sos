"""Helpers for keeping provider and system details out of user-facing diagnostics."""

from __future__ import annotations

import re


def sanitize_diagnostic(value: str) -> str:
    value = re.sub(r"(?:https?|wss?)://\S+", "[redacted-url]", value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)\bBearer\s+\S+", "******", value)
    return re.sub(
        r"(?i)\b(api[_-]?key|token|secret|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        value,
    )
