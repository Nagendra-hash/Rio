"""
core/repo_agent/security.py — validation, risk scanning and redaction for the
Universal Repository Agent.

Everything in here treats repository contents as UNTRUSTED input:

    * a GitHub URL is accepted only in the canonical github.com/owner/repo form
    * a repository NAME can never select an arbitrary destination path
    * README / manifest / script text is scanned for dangerous operations
      BEFORE anything is executed
    * log text is scrubbed of API keys / tokens before it is stored

This module is stdlib-only and side-effect free so it can be unit-tested and
imported from the dashboard, the actions layer and the worker alike.
"""
from __future__ import annotations

import re
from typing import Any

__all__ = [
    "RepoSecurityError", "validate_repo_url", "safe_repo_name", "safe_dir_name",
    "scan_text_risk", "risk_summary", "redact", "scrub_env", "SECRET_PATTERNS",
]


class RepoSecurityError(ValueError):
    """Raised when a request or repository content fails a security check."""


# ── URL + name validation ────────────────────────────────────────────────────

_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9][A-Za-z0-9_.-]{0,38})"
    r"/([A-Za-z0-9][A-Za-z0-9_.-]{0,99}?)(?:\.git)?/?$")

# A repo NAME becomes a directory name — it must never traverse or escape.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

_RESERVED_NAMES = {"", ".", "..", "registry.json", ".jarvis"}


def validate_repo_url(url: str) -> tuple[str, str]:
    """Return (owner, repo) for a canonical github.com URL, else raise.

    Rejects: non-github hosts, path traversal inside owner/repo, query
    strings, credentials in URL, and everything else we do not explicitly
    support. The LLM may only ever pass a URL it read from the user.
    """
    text = str(url or "").strip()
    if not text:
        raise RepoSecurityError("empty repository URL")
    if any(c in text for c in ("?", "#", "@", " ", "\t", "\n")):
        raise RepoSecurityError("URL contains characters that are not allowed")
    match = _REPO_RE.match(text)
    if not match:
        raise RepoSecurityError(
            "only canonical GitHub URLs (github.com/owner/repo) are supported")
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    for part in (owner, repo):
        if ".." in part or part.startswith("."):
            raise RepoSecurityError(f"unsafe path component in URL: {part!r}")
    return owner, repo


def safe_dir_name(name: str) -> str:
    """Normalise a repository name into a safe managed-directory name.

    The result is the ONLY thing ever joined onto the tools root, so a name
    can never escape `~/JarvisTools` (no separators, no leading dot, no `..`).
    Empty and reserved names are rejected outright; everything else is
    normalised (invalid characters become '-') and re-validated.
    """
    original = str(name or "").strip()
    if not original or original.lower() in _RESERVED_NAMES:
        raise RepoSecurityError(f"repository name is not safe: {name!r}")
    text = re.sub(r"[^a-z0-9._-]+", "-", original.lower())
    if not text or ".." in text:
        raise RepoSecurityError(f"repository name is not safe: {name!r}")
    text = text.strip("-._")
    if not text or not _NAME_RE.match(text) or \
            text in _RESERVED_NAMES or ".." in text:
        raise RepoSecurityError(f"repository name is not safe: {name!r}")
    return text


# Backwards-friendly alias used by the manager.
safe_repo_name = safe_dir_name


# ── static risk scan (README / manifests / scripts) ─────────────────────────

_RISK_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    ("high", "runs commands as root (sudo)",
     re.compile(r"(?:^|[\s`|(])sudo\s", re.M)),
    ("high", "pipes a downloaded script straight into a shell",
     re.compile(r"curl[^|;]*\|\s*(?:ba)?sh|wget[^|;]*\|\s*(?:ba)?sh",
                re.I)),
    ("high", "reads SSH private keys",
     re.compile(r"\.ssh/id_|id_rsa|id_ed25519")),
    ("high", "reads cloud credentials",
     re.compile(r"\.aws/credentials|\.kube/config|\.netrc|\.npmrc.*_auth", re.I)),
    ("high", "recursive force delete of a filesystem root or home",
     re.compile(r"rm\s+-rf\s+(?:/|~|\$HOME)(?:\s|$)", re.I)),
    ("high", "installs a systemd unit / cron entry",
     re.compile(r"systemctl\s+enable|/etc/systemd/system|crontab\s+-")),
    ("medium", "downloads and executes a remote script (setup.sh pattern)",
     re.compile(r"curl\s+[^\s]*\|\s*python|wget\s+[^\s]*\|\s*python", re.I)),
    ("medium", "collects or exports environment secrets",
     re.compile(r"(?:printenv|env)\s*[>|].*(?:upload|curl|post|send)", re.I)),
    ("medium", "reverse shell / raw socket hints",
     re.compile(r"/dev/tcp/|nc\s+-e\s|socket\.connect.*shell", re.I)),
    ("medium", "modifies git hooks or git config",
     re.compile(r"git\s+config\s+--global|\.git/hooks/post-checkout", re.I)),
    ("medium", "changes file permissions world-writable",
     re.compile(r"chmod\s+(?:-R\s+)?777", re.I)),
    ("low", "installs with elevated package managers",
     re.compile(r"apt-get\s+install|yum\s+install|dnf\s+install", re.I)),
]


def scan_text_risk(text: str) -> list[dict[str, str]]:
    """Scan README / script / manifest text for dangerous operations.

    Returns a list of {severity, finding, evidence} dicts. This is a
    best-effort STATIC filter — it informs the approval decision, it is not a
    sandbox. Installation itself only ever runs the repository's declared
    package-manager command (uv/pip/npm), never README shell snippets.
    """
    text = str(text or "")
    findings: list[dict[str, str]] = []
    for severity, finding, pattern in _RISK_RULES:
        match = pattern.search(text)
        if match:
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            evidence = text[line_start:line_end if line_end != -1 else None]
            findings.append({"severity": severity, "finding": finding,
                             "evidence": evidence.strip()[:160]})
    return findings


def risk_summary(findings: list[dict[str, str]]) -> str:
    """Collapse findings into one of: low / medium / high."""
    severities = {f.get("severity") for f in findings or []}
    if "high" in severities:
        return "high"
    if "medium" in severities:
        return "medium"
    return "low"


# ── secret redaction in logs ─────────────────────────────────────────────────

_SECRET_PATTERNS = [
    re.compile(r"(sk|rk)-[A-Za-z0-9_-]{16,}"),                 # openai-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                 # github tokens
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),                     # google keys
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # aws access key
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{18,}=?"),
    re.compile(r"(?i)(api[_-]?key|api[_-]?secret|token|password|passwd)"
               r"(\s*[=:]\s*|\s+)(['\"]?)[A-Za-z0-9._~+/-]{8,}\3"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
SECRET_PATTERNS = tuple(_SECRET_PATTERNS)   # __all__ re-exports this name

_REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Scrub obvious secrets out of a log line before it is stored."""
    out = str(text or "")
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def scrub_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment for untrusted child processes.

    Deliberately EXCLUDED: every JARVIS_*/VIMAX_* variable, PYTHONPATH,
    PYTHONSTARTUP, shell history/env dumps. Deliberately INCLUDED: a per-repo
    HOME (created by the manager) so repo code cannot silently read the real
    ~/.ssh, ~/.aws, ~/.hermes or browser profiles, plus explicit cache dirs so
    package managers still work inside that fake HOME.
    """
    env: dict[str, str] = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    for key, value in (extra or {}).items():
        env[key] = str(value)
    return env


def coerce_param(schema: dict[str, Any], name: str, value: Any) -> Any:
    """Validate + normalise one argument against a skill input-schema entry.

    The schema is a flat {param: {"type": "string"|"number"|"integer"|"boolean",
    "required": bool, "enum": [...]}} map. Unknown parameters are rejected —
    model-generated arguments never reach a shell unsanitised.
    """
    spec = (schema or {}).get(name)
    if spec is None:
        raise RepoSecurityError(f"unknown parameter '{name}' for this operation")
    kind = str(spec.get("type") or "string").lower()
    if value is None or value == "":
        if spec.get("required"):
            raise RepoSecurityError(f"parameter '{name}' is required")
        return spec.get("default")
    if kind in ("string", "str"):
        out = str(value)
        limit = int(spec.get("max_chars") or 4000)
        if len(out) > limit:
            raise RepoSecurityError(f"parameter '{name}' is too long")
        if any(c in out for c in "\x00\r\n"):
            raise RepoSecurityError(f"parameter '{name}' has control characters")
    elif kind in ("number", "float"):
        out = float(value)
    elif kind in ("integer", "int"):
        out = int(value)
        if "minimum" in spec and out < int(spec["minimum"]):
            raise RepoSecurityError(f"parameter '{name}' is below the minimum")
        if "maximum" in spec and out > int(spec["maximum"]):
            raise RepoSecurityError(f"parameter '{name}' is above the maximum")
    elif kind in ("boolean", "bool"):
        out = 1 if (value in (True, 1, "1", "true", "yes", "on")) else 0
    else:
        raise RepoSecurityError(f"parameter '{name}' has an unsupported type")
    enum = spec.get("enum")
    if enum and str(out) not in {str(e) for e in enum}:
        raise RepoSecurityError(
            f"parameter '{name}' must be one of: {', '.join(map(str, enum))}")
    return out
