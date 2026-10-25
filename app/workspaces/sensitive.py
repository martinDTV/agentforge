"""Names that conventionally hold credentials. Shared by the read tools (which
refuse to open them) and the sandbox archiver (which never ships them)."""

from fnmatch import fnmatch

DENIED_DIRECTORIES: frozenset[str] = frozenset({".git", ".hg", ".svn"})
SENSITIVE_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "*credentials*.json",
    "*.tfstate",
    "*.tfstate.*",
)
# Templates are documentation, not secrets.
SENSITIVE_NAME_EXCEPTIONS: frozenset[str] = frozenset({".env.example", ".env.sample"})


def is_sensitive_name(name: str) -> bool:
    if name in SENSITIVE_NAME_EXCEPTIONS:
        return False
    return any(fnmatch(name, pattern) for pattern in SENSITIVE_NAME_PATTERNS)
