"""Best-effort secret scrubbing for ingested text (sessions, commits).

The brain must never hold credentials. Connectors run this before anything is
stored or pushed. It is a safety net, not a guarantee.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED:private-key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED:jwt]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[REDACTED:api-key]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|glpat|xox[abprs])[-_][A-Za-z0-9_-]{16,}\b"), "[REDACTED:token]"),
    (re.compile(r"\bpocket_id_[a-z]{2}_[A-Za-z0-9._-]{16,}"), "[REDACTED:pocket-id-code]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws-key]"),
    (re.compile(r"(?i)\b(Bearer|token)\s+[A-Za-z0-9._~+/=-]{24,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|private[_-]?key)[\w.-]*)(\s*[=:]\s*)(?!\[REDACTED)[\"']?[^\s\"',;]{6,}[\"']?"),
     r"\1\2[REDACTED]"),
    (re.compile(r"://([^/\s:@]+):([^/\s@]{4,})@"), r"://\1:[REDACTED]@"),
    # bare 40-hex (Gitea/GitHub style) and long base64-ish blobs
    (re.compile(r"\b[0-9a-f]{40}\b(?<![0-9a-f]{41})"), "[REDACTED:hex40]"),
]


def redact(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text
