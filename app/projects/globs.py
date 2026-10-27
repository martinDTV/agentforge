"""Minimal, explicit glob matching for repository paths.

Semantics (POSIX-like, anchored to the whole relative path):

- ``**`` matches any sequence of path components, including none;
- ``*`` matches any run of characters except ``/``;
- ``?`` matches one character except ``/``;
- ``[...]`` character classes are passed through (``[!...]`` negates).

Patterns never match a path with ``..`` because such paths are refused before
matching. No shell, no ``fnmatch`` surprises (``*`` crossing directories).
"""

import re
from collections.abc import Iterable
from functools import lru_cache

_PATTERN_RE = re.compile(r"^[A-Za-z0-9_./*?\[\]!\-@+ ,%=~]{1,512}$")


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i : i + 2] == "**":
                # ``**/`` -> zero or more directories; trailing/standalone ``**`` -> anything.
                if pattern[i : i + 3] == "**/":
                    out.append("(?:[^/]+/)*")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end
        else:
            out.append(re.escape(ch))
        i += 1
    return "^" + "".join(out) + "$"


@lru_cache(maxsize=1024)
def compile_glob(pattern: str) -> re.Pattern[str]:
    if (
        not _PATTERN_RE.fullmatch(pattern)
        or pattern.startswith(("/", "../", "~"))
        or "/../" in pattern
    ):
        raise ValueError(f"patrón glob no admitido: {pattern!r}")
    return re.compile(_translate(pattern))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(compile_glob(p).match(path) is not None for p in patterns)
