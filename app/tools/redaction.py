"""Best-effort secret redaction for text that leaves a tool.

A second net, not the first: the filesystem tools already refuse files that
usually hold credentials. Patterns are deliberately conservative (long
tokens, explicit key markers) to avoid mangling ordinary code.
"""

import re

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
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
        # key = value / "key": "value" / Authorization: Bearer value. Values
        # already replaced by a pattern above are left alone.
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key|authorization)\b"
            r"(['\"]?)(\s*:\s*bearer\s+|\s*[:=]\s*)(['\"]?)(?!\[REDACTED)(?!bearer\b)([^\s'\"]{6,})"
        ),
        rf"\1\2\3\4{REDACTED}",
    ),
)


def redact(text: str) -> tuple[str, int]:
    """Return the redacted text and how many replacements were made."""
    total = 0
    for pattern, replacement in _PATTERNS:
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total
