"""Secret scrubbing from error context, tracebacks, and environment variables."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Pattern, Union

# Values ``scrub_dict`` walks: strings and nested dicts/lists of them, plus the
# leaves (numbers, bools, None) it passes through untouched.
_Scrubbable = Union[
    str,
    int,
    float,
    bool,
    None,
    Dict[str, "_Scrubbable"],
    List["_Scrubbable"],
]

# Default secret patterns
_DEFAULT_PATTERNS: List[Pattern[str]] = [
    # AWS Access Key IDs (AKIA...)
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # AWS Secret Keys (40 char base64-ish)
    re.compile(r"(?<=[^A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?=[^A-Za-z0-9/+=]|$)"),
    # Bearer tokens
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    # Common token/key patterns in assignment context
    re.compile(r"""(?<=['"])[A-Za-z0-9\-._]{20,}(?=['"])"""),
    # Passwords in URLs: scheme://user:password@host
    re.compile(r"://[^:]+:([^@]+)@"),
]

# Credentials passed as URL query parameters. Same parameter set and 16-char
# floor as krepis.logging's redaction filter, so short ID-like values stay.
_URL_QUERY_CREDENTIAL: Pattern[str] = re.compile(
    # `&amp;` is an HTML-escaped separator; `%`, `+`, `/`, `=`, `~` let a
    # URL-encoded token match whole instead of stopping short of the floor.
    r"((?:[?&]|&amp;)(?:apikey|api_key|key|token|access_token|auth_token)=)"
    r"[A-Za-z0-9_\-.~%+/=]{16,}",
    re.IGNORECASE,
)

# Environment variable names that should have their values scrubbed
_SECRET_ENV_SUFFIXES = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "_CREDENTIAL", "_API_KEY")
_SECRET_ENV_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "SLACK_WEBHOOK_URL",
    "GMAIL_APP_PASSWORD",
    "DATABASE_PASSWORD",
}

REDACTED = "[REDACTED]"


class Scrubber:
    """Scrubs secrets from strings and dicts."""

    def __init__(self, extra_patterns: Optional[List[str]] = None) -> None:
        self._patterns = list(_DEFAULT_PATTERNS)
        if extra_patterns:
            for p in extra_patterns:
                self._patterns.append(re.compile(p))

    def scrub_string(self, text: str) -> str:
        """Remove secret patterns from a string."""
        if not text:
            return text
        result = text
        # Scrub AWS access key IDs
        result = re.sub(r"AKIA[0-9A-Z]{16}", REDACTED, result)
        # Scrub Bearer tokens
        result = re.sub(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", f"Bearer {REDACTED}", result, flags=re.IGNORECASE)
        # Scrub passwords in URLs
        result = re.sub(r"(://[^:]+:)([^@]+)(@)", rf"\1{REDACTED}\3", result)
        # Scrub credentials carried as URL query parameters (?apiKey=...).
        # requests puts the full URL in HTTPError text, and a Polygon page
        # URL put the live key into a Telegram alert on 2026-10-02
        # (nous-ergon-ops-I2002). Case-insensitive: Polygon spells it apiKey.
        result = _URL_QUERY_CREDENTIAL.sub(rf"\1{REDACTED}", result)
        # Scrub any extra user-defined patterns
        for pattern in self._patterns[3:]:  # skip the first 3 we already handled
            if pattern.pattern not in (p.pattern for p in _DEFAULT_PATTERNS[:3]):
                result = pattern.sub(REDACTED, result)
        return result

    def scrub_env_vars(self, env: Dict[str, str]) -> Dict[str, str]:
        """Scrub secret environment variable values."""
        scrubbed: Dict[str, str] = {}
        for key, value in env.items():
            if self._is_secret_env_name(key):
                scrubbed[key] = REDACTED
            else:
                scrubbed[key] = value
        return scrubbed

    def scrub_dict(self, d: Dict[str, _Scrubbable]) -> Dict[str, _Scrubbable]:
        """Recursively scrub secrets from a dictionary."""
        result: Dict[str, _Scrubbable] = {}
        for key, value in d.items():
            if self._is_secret_key(key):
                result[key] = REDACTED
            elif isinstance(value, str):
                result[key] = self.scrub_string(value)
            elif isinstance(value, dict):
                result[key] = self.scrub_dict(value)
            elif isinstance(value, list):
                result[key] = [
                    self.scrub_string(item) if isinstance(item, str)
                    else self.scrub_dict(item) if isinstance(item, dict)
                    else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    @staticmethod
    def _is_secret_env_name(name: str) -> bool:
        """Check if an env var name matches known secret patterns."""
        upper = name.upper()
        if upper in _SECRET_ENV_NAMES:
            return True
        return any(upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES)

    @staticmethod
    def _is_secret_key(key: str) -> bool:
        """Check if a dict key looks like it holds a secret."""
        upper = key.upper()
        if upper in _SECRET_ENV_NAMES:
            return True
        if any(upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES):
            return True
        # Also match keys that contain common secret words
        secret_words = ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "API_KEY")
        return any(word in upper for word in secret_words)
