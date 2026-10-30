"""Text and URL hygiene for everything that goes into an e-mail.

Mail bodies mix trusted platform facts with untrusted text (a task written by a
human, a summary written by a model, a file path from a repository). Every
string passes through here before rendering: control characters removed,
whitespace folded, length bounded, obvious secrets redacted. URLs are rendered
only when they are ``https`` to a host we expect; model text is never turned
into a link. HTML escaping itself happens in the renderer.
"""

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REDACTED = "[REDACTED]"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
        "[REDACTED:private_key]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[REDACTED:jwt]",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws_access_key]"),
    (
        re.compile(
            r"\b(?:sk-ant-|sk-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[baprs]-|AIza|glpat-)"
            r"[A-Za-z0-9_\-]{16,}\b"
        ),
        "[REDACTED:api_token]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|contraseña|secret|token|api[_-]?key|private[_-]?key|"
            r"authorization)\b(['\"]?)(\s*:\s*bearer\s+|\s*[:=]\s*)(['\"]?)(?!\[REDACTED)"
            r"(?!bearer\b)([^\s'\"]{6,})"
        ),
        rf"\1\2\3\4{REDACTED}",
    ),
)


def redact_secrets(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def clean_text(value: Any, limit: int = 300, *, keep_lines: bool = False) -> str:
    """Bounded, printable, secret-free text. ``keep_lines`` keeps paragraph breaks."""
    if value is None:
        return ""
    text = _CONTROL.sub("", str(value))
    text = _SPACES.sub(" ", text)
    text = _BLANK_LINES.sub("\n", text).strip() if keep_lines else " ".join(text.split())
    text = redact_secrets(text)
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def clean_lines(values: Any, *, limit: int = 200, max_items: int = 16) -> list[str]:
    """A bounded list of cleaned strings; non-lists yield an empty list."""
    if not isinstance(values, list | tuple):
        return []
    cleaned = [clean_text(v, limit) for v in values[:max_items]]
    return [c for c in cleaned if c]


def short_id(value: Any, length: int = 8) -> str:
    return clean_text(value, 64)[:length]


def short_sha(value: Any) -> str:
    text = clean_text(value, 64)
    return text[:12] if _HEX40.match(text) else text[:16]


def is_long_hash(text: str) -> bool:
    return bool(_HEX40.match(text) or _HEX64.match(text))


def safe_https_url(value: Any, *, allowed_hosts: tuple[str, ...] | None = None) -> str | None:
    """``value`` as a normalised ``https`` URL, or ``None`` when it must not be linked.

    Refuses anything but ``https``, credentials in the authority, loopback hosts
    and, when ``allowed_hosts`` is given, any other host. Query strings are kept
    (GitHub uses none for pull requests); fragments are dropped.
    """
    if not isinstance(value, str) or len(value) > 2048:
        return None
    text = _CONTROL.sub("", value.strip())
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    if not host or host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return None
    if allowed_hosts is not None and host not in allowed_hosts:
        return None
    if any(ch in text for ch in ("<", ">", '"', "'", " ")):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def github_url(value: Any) -> str | None:
    return safe_https_url(value, allowed_hosts=("github.com", "www.github.com"))
