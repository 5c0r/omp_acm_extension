"""Sanitize untrusted memory text at service boundaries."""
import re
import unicodedata

_SECRET = re.compile(r"(?:sk|pk|rk|tok|key|secret|token|password)[-_A-Za-z0-9]{12,}", re.IGNORECASE)
_INJECTION = re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions?(?:\s+and\s+reveal\s+secrets?)?\b", re.IGNORECASE)


def neutralize_injection(value: str) -> str:
    value = "".join(" " if unicodedata.category(char) in {"Cc", "Cf"} else char for char in value)
    return _INJECTION.sub("[REDACTED]", value.replace("`", "").replace("<", "").replace(">", ""))


def redact_secrets(value: str) -> str:
    return _SECRET.sub("[REDACTED]", value)


def sanitize(value: str) -> str:
    return " ".join(redact_secrets(neutralize_injection(value)).split())
