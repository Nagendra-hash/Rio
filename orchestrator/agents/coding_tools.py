"""
orchestrator/agents/coding_tools.py — tool permissions + toolbox for the
coding agent.

WHY A SEPARATE TOOLBOX (and not just prompt the model)
    The Phase-5 coding agent edits real files and runs real processes. A
    model can hallucinate a path, read an API key it "found", or run
    `git push` because the task "seemed to want it". So every operation the
    agent performs goes through ToolPermissions FIRST, and every piece of
    text a tool produces (file contents, diffs, test output, reports) is
    masked for secrets before it leaves this file. The model never sees raw
    output and never touches the filesystem directly — it only proposes
    steps, which this toolbox executes under the rules below.

PERMISSION MODEL
    READ      — inspect the repo tree, read files, search, read-only git
                (status/diff/log). Refused for secret-shaped files: the
                agent cannot leak what it was never allowed to read.
    WRITE     — create/edit files INSIDE the repo root (realpath-checked,
                symlink-escape-proof). A deletion larger than
                LARGE_DELETE_LINES — or any whole-file delete — is not a
                WRITE, it is DANGEROUS (see below).
    EXECUTE   — run the project's tests and its own Python files, a guarded
                Android `gradle assembleDebug/…` build, plus read-only git.
                Denylist first: git push/reset/clean/rebase, rm, sudo, curl,
                pip … never run, no matter who asks.
    DANGEROUS — deletion of large areas, anything on the never-run list if
                a future feature ever needs it: surfaces as
                TaskStatus.NEEDS_APPROVAL (orchestrator/approvals.py), the
                same gate the trading stub uses. Never auto-granted.

SECRET SAFETY
    mask_secrets() runs over EVERY output (tool results, diffs, test
    output, reports, checkpoints). Known key shapes (Google, OpenAI,
    GitHub, AWS, Slack, bearer, generic "key": "value" config pairs) are
    replaced with the name of what was redacted — the agent explains
    "redacted an API key" instead of printing it.

Everything here is offline and dependency-free: stdlib only, subprocess
with argv lists (never shell=True), hard timeouts, bounded outputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

# ── tunables ─────────────────────────────────────────────────────────────────
LARGE_DELETE_LINES = 60      # removing more than this many lines needs approval
MAX_READ_BYTES    = 48_000    # one file read cap (chars handed to the model)
MAX_OUTPUT_BYTES  = 12_000    # one subprocess output cap (stdout or stderr)
MAX_SEARCH_HITS   = 40        # search results cap
MAX_TREE_ENTRIES  = 400       # repo tree entries cap
DEFAULT_TIMEOUT_S = 60        # per-process timeout
ANDROID_BUILD_TIMEOUT_S = 900  # a cold gradle build downloads ~200 MB of deps
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".tox", ".idea", ".vscode"}

TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".md", ".txt",
                 ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".css",
                 ".html", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp",
                 ".rb", ".gitignore", ".env.example",
                 # Android / Gradle / mobile text formats (the coding agent
                 # scaffolds and edits Android projects — see android_support).
                 ".xml", ".kt", ".kts", ".gradle", ".properties", ".pro",
                 ".aidl", ".swift", ".dart", ".plist", ".cs", ".m", ".mm"}


# ── secrets ──────────────────────────────────────────────────────────────────
# Shapes of credentials we must never echo back. Order matters only for
# reporting; every match is masked independently.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("Google API key",      re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("OpenAI-style key",    re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("GitHub token",        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key",      re.compile(r"A(?:KIA|SIA)[0-9A-Z]{16}")),
    ("Slack token",         re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("JWT",                 re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer token",        re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=+/]{20,}")),
    ("password assignment", re.compile(r"(?i)\b(password|passwd|secret|token|api_key|apikey)\b\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]")),
)

# Files the agent must never read or write — credentials and their stores.
SECRET_FILE_NAMES = {
    "api_keys.json", "client_secret.json", "credentials.json", ".npmrc",
    ".netrc", "id_rsa", "id_ed25519", ".pypirc", ".htpasswd", ".env",
}
SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
SECRET_FILE_PARTS = ("secret", "token", "credential", "password")
# .env.local / .env.production carry the same class of secrets as .env.
ENV_PREFIX = ".env."


def mask_secrets(text: str) -> tuple[str, list[str]]:
    """Mask every credential shape in `text`. Returns (masked, kinds found).

    Applied at the single choke point every tool output flows through, so
    "never expose secrets" is enforced once, here — not everywhere.
    """
    if not text:
        return text, []
    kinds: list[str] = []
    masked = text
    for name, pat in SECRET_PATTERNS:
        def _sub(m: re.Match, _name: str = name) -> str:
            if _name not in kinds:
                kinds.append(_name)
            if _name == "password assignment":
                return f"{m.group(1)}: '<redacted {_name}>'"
            return f"<redacted {_name}>"
        masked = pat.sub(_sub, masked)
    return masked, kinds


def path_is_secret(path: Path) -> Optional[str]:
    """Why this file may not be read/written by the agent, or None if fine."""
    name = path.name.lower()
    if name in SECRET_FILE_NAMES or name.startswith(ENV_PREFIX):
        return f"'{path.name}' is a credential store"
    if path.suffix.lower() in SECRET_FILE_SUFFIXES:
        return f"'{path.suffix}' files can hold private keys"
    if any(part in name for part in SECRET_FILE_PARTS):
        return f"'{path.name}' looks like a credential file"
    return None


# ── permissions ──────────────────────────────────────────────────────────────
class ToolPermissions:
    """The guard every toolbox call consults. Repo-rooted, offline, denylist-
    first. Answers ('ok', reason) pairs — reasons become user-visible."""
    # Executables allowed to run; everything else is denied before it spawns.
    EXECUTABLES = {"python", "python3", sys.executable.lower() if sys.executable else "python3"}
    # git subcommands that only read (safe on approval-free EXECUTE).
    GIT_READONLY = {"status", "diff", "log", "show", "rev-parse", "branch", "ls-files"}
    # git subcommands that change repo state: allowed only when explicitly
    # approved. The agent never uses them unprompted.
    GIT_MUTATING = {"add", "commit", "restore", "stash", "tag"}
    # git subcommands that leave the machine or rewrite history: NEVER run.
    GIT_NEVER = {"push", "pull", "fetch", "remote", "reset", "rebase",
                 "clean", "cherry-pick", "filter-branch", "submodule"}
    # First words of commands that must never run, git or not.
    NEVER_BINARIES = {"rm", "rmdir", "sudo", "curl", "wget", "pip", "pip3",
                      "npm", "npx", "sh", "bash", "zsh", "powershell",
                      "shutdown", "reboot", "mkfs", "dd", "chmod", "chown",
                      "kill", "killall", "systemctl", "ssh", "scp"}

    def __init__(self, repo_root: str | os.PathLike) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()

    # ── path checks ──────────────────────────────────────────────────────
    def resolve_in_repo(self, path: str | os.PathLike) -> Path:
        """Resolve `path` and prove it stays inside the repo.

        Raises PermissionError with a user-visible reason when it does not —
        symlinks and .. are the two classic escapes, so containment is
        checked on the RESOLVED path, not the literal one."""
        p = Path(str(path))
        if p.is_absolute():
            candidate = p
        else:
            candidate = self.repo_root / p
        real = candidate.expanduser().resolve()
        root = self.repo_root.resolve()
        if real != root and root not in real.parents:
            raise PermissionError(
                f"'{path}' resolves outside the repository "
                f"({real} is not under {root})")
        return real

    def can_read(self, path: str | os.PathLike) -> tuple[bool, str]:
        try:
            real = self.resolve_in_repo(path)
        except PermissionError as exc:
            return False, str(exc)
        if real == self.repo_root:
            return False, "cannot read a directory as a file"
        why = path_is_secret(real)
        if why:
            return False, f"refusing to read: {why} (secrets are never exposed)"
        return True, ""

    def can_write(self, path: str | os.PathLike) -> tuple[bool, str]:
        try:
            real = self.resolve_in_repo(path)
        except PermissionError as exc:
            return False, str(exc)
        if real == self.repo_root:
            return False, "refusing to write the repo root itself"
        if real == self.repo_root / ".git" or self.repo_root / ".git" in real.parents:
            return False, ".git internals are never edited by the agent"
        why = path_is_secret(real)
        if why:
            return False, f"refusing to write: {why} (secrets are never exposed)"
        if real.suffix and real.suffix.lower() not in TEXT_SUFFIXES and \
                real.name not in ("Makefile", "Dockerfile", "pytest.ini"):
            # Binary-looking files are out of scope for a text editor agent.
            return False, f"refusing to edit '{real.name}': not a known text format"
        return True, ""

    def removal_size(self, before: str, after: str) -> int:
        """How many lines a change REMOVES (the approval-relevant direction)."""
        before_lines = before.count("\n") + 1 if before else 0
        after_lines = after.count("\n") + 1 if after else 0
        return max(0, before_lines - after_lines)

    def is_large_deletion(self, before: str, after: str) -> bool:
        return self.removal_size(before, after) > LARGE_DELETE_LINES

    # ── command checks ───────────────────────────────────────────────────
    def check_command(self, argv: list[str]) -> tuple[bool, str]:
        """Allow/deny for running `argv` in the repo. Never raises."""
        if not argv or not isinstance(argv, list):
            return False, "empty or malformed command"
        exe = Path(str(argv[0])).name.lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe in self.NEVER_BINARIES:
            return False, f"'{exe}' is never run by the coding agent"
        args = [str(a) for a in argv[1:]]
        if exe in ("git",):
            if not args:
                return False, "git needs a subcommand"
            sub = args[0].lower()
            if sub in self.GIT_NEVER:
                return False, (f"git {sub} is never run — the coding agent "
                               f"never touches remotes or rewrites history")
            if sub in self.GIT_MUTATING:
                return False, (f"git {sub} changes the repo; ask for approval "
                               f"or do it yourself (rollback info is in the report)")
            if sub not in self.GIT_READONLY:
                return False, f"git {sub} is not on the read-only allowlist"
            return True, ""
        if exe in ("gradle", "gradlew"):
            # Android builds only: a strict task/flag allowlist (gradle can run
            # arbitrary build scripts, so nothing beyond assemble*/clean runs).
            from orchestrator.agents.android_support import gradle_argv_ok
            return gradle_argv_ok(argv)
        if exe in self.EXECUTABLES or exe in ("python", "python3"):
            # Tests and project files only: -m pytest, a script path, or a
            # bare run. -c one-liners would be arbitrary code injection.
            if "-c" in args:
                return False, "python -c (inline code) is not allowed; run a file"
            if "-m" in args:
                i = args.index("-m")
                module = args[i + 1] if i + 1 < len(args) else ""
                if not re.match(r"^[\w\.]+$", module):
                    return False, f"-m target '{module}' is not a module name"
                allowed_modules = {"pytest", "unittest", "compileall"}
                if module.split(".")[0] not in allowed_modules:
                    return False, (f"python -m {module} is not on the allowlist "
                                   f"(pytest/unittest/compileall only)")
                return True, ""
            for a in args:
                if a.startswith("-"):
                    if a in ("--version", "-V", "-q", "-v", "-x", "-s",
                             "--collect-only", "--no-header", "-ra", "-l",
                             "--tb=short", "--tb=line", "--tb=no", "-q", "-o"):
                        continue
                    return False, f"flag '{a}' is not on the python allowlist"
                # A positional must be a repo-internal path or a pytest
                # node id (tests/test_x.py::Class::method).
                try:
                    self.resolve_in_repo(a)
                except PermissionError:
                    if "::" in a:
                        try:
                            self.resolve_in_repo(a.split("::")[0])
                        except PermissionError:
                            return False, f"'{a}' is outside the repository"
                        continue
                    return False, f"'{a}' is outside the repository"
            return True, ""
        return False, f"'{exe}' is not an allowed executable " \
                      f"(python/python3/pytest, gradle assembleDebug, " \
                      f"git read-only only)"

    def approved_command(self, argv: list[str]) -> tuple[bool, str]:
        """Same as check_command but permits git-mutating subcommands — used
        only after an explicit human approval. NEVER allows GIT_NEVER."""
        ok, reason = self.check_command(argv)
        if ok:
            return True, ""
        exe = Path(str(argv[0])).name.lower() if argv else ""
        args = [str(a) for a in argv[1:]] if len(argv) > 1 else []
        if exe == "git" and args and args[0].lower() in self.GIT_MUTATING:
            return True, ""
        return False, reason


# ── change records ───────────────────────────────────────────────────────────
def _sha(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:12]


ROLLBACK_STORE_LIMIT = 24_000   # per-field cap for stored before/after text


def change_record(rel_path: str, kind: str, before: str | None,
                  after: str | None) -> dict[str, Any]:
    """One file the agent touched, with everything rollback needs.

    Oversized file bodies are NOT stored (a checkpoint that swallows whole
    large files is its own hazard); the sha is still recorded so a rollback
    can honestly say 'this one must be restored from git' instead of
    writing back half a file."""
    added = removed = 0
    if kind == "edit" and before is not None and after is not None:
        b = set(before.splitlines())
        a = set(after.splitlines())
        removed = len([l for l in b if l not in a])
        added = len([l for l in a if l not in b])
    elif kind == "create":
        added = (after or "").count("\n") + 1 if after else 0
    elif kind == "delete":
        removed = (before or "").count("\n") + 1 if before else 0
    before_stored = after_stored = True
    if before is not None and len(before) > ROLLBACK_STORE_LIMIT:
        before, before_stored = None, False
    if after is not None and len(after) > ROLLBACK_STORE_LIMIT:
        after, after_stored = None, False
    return {
        "path": rel_path, "kind": kind,
        "before": before, "after": after,
        "before_stored": before_stored, "after_stored": after_stored,
        "before_sha": _sha(before), "after_sha": _sha(after),
        "lines_added": added, "lines_removed": removed,
    }


# ── the toolbox ──────────────────────────────────────────────────────────────
class CodingToolbox:
    """Every capability the coding agent has, as guarded, never-raising
    calls that return dicts. All text fields are secret-masked."""

    def __init__(self, repo_root: str | os.PathLike,
                 permissions: Optional[ToolPermissions] = None,
                 undo_hook: Optional[Callable[[str, Callable[[], str]], None]] = None,
                 ) -> None:
        self.perms = permissions or ToolPermissions(repo_root)
        self.root = self.perms.repo_root
        # Reuse the shared Jarvis undo stack when core is importable (it is
        # reversible work — exactly what core/undo.py is for). Optional so
        # pure-offline tests never need the core package.
        if undo_hook is None:
            try:
                from core.undo import push_undo as undo_hook  # type: ignore
            except Exception:
                undo_hook = lambda label, fn: None          # noqa: E731
        self._push_undo = undo_hook

    # ── output plumbing ──────────────────────────────────────────────────
    @staticmethod
    def _mask(text: str) -> tuple[str, list[str]]:
        return mask_secrets(text or "")

    # ── INSPECT ──────────────────────────────────────────────────────────
    def inspect(self) -> dict[str, Any]:
        """Repo overview: tree summary, languages, git state, test command."""
        tree: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            rel_dir = Path(dirpath).relative_to(self.root)
            for fn in sorted(filenames):
                rel = str(rel_dir / fn) if str(rel_dir) != "." else fn
                if not path_is_secret(Path(fn)):
                    tree.append(rel)
                if len(tree) >= MAX_TREE_ENTRIES:
                    break
            if len(tree) >= MAX_TREE_ENTRIES:
                break
        exts: dict[str, int] = {}
        for t in tree:
            ext = Path(t).suffix.lower() or "(none)"
            exts[ext] = exts.get(ext, 0) + 1
        languages = sorted(exts, key=exts.get, reverse=True)
        info: dict[str, Any] = {
            "root": str(self.root),
            "files": tree,
            "file_count": len(tree),
            "languages": languages[:8],
            "git": self._git_summary(),
            "test_command": self._detect_test_command(tree),
            "frameworks": self._detect_frameworks(tree),
            "is_android": self._looks_android(tree),
        }
        masked, kinds = self._mask(json.dumps(info, default=str))
        if kinds:                                   # never actually expected
            info["masked_kinds"] = kinds
        return info

    def _git_summary(self) -> dict[str, Any]:
        """Read-only git facts: is this a repo, what branch, is it dirty."""
        if not (self.root / ".git").exists():
            return {"is_repo": False, "reason": "no .git directory"}
        out = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 10)
        branch = out.get("stdout", "").strip() or "?"
        dirty = self._run(["git", "status", "--porcelain"], 10)
        lines = [l for l in dirty.get("stdout", "").splitlines() if l.strip()]
        return {"is_repo": True, "branch": branch,
                "dirty_files": len(lines), "dirty_sample": lines[:10]}

    @staticmethod
    def _detect_test_command(tree: list[str]) -> str:
        if CodingToolbox._looks_android(tree):
            return "gradle assembleDebug (android build, not auto-run here)"
        if "pytest.ini" in tree or any(t.startswith("tests/") for t in tree):
            return "pytest -q"
        if any(t.endswith("_test.py") or t.startswith("test_") for t in tree):
            return "pytest -q"
        if "package.json" in tree:
            return "npm test (not auto-run; see permissions)"
        return ""

    @staticmethod
    def _looks_android(tree: list[str]) -> bool:
        return any(t.endswith((".kt", ".kts", "AndroidManifest.xml"))
                   or t in ("settings.gradle", "settings.gradle.kts",
                            "build.gradle", "build.gradle.kts")
                   for t in tree)

    @staticmethod
    def _detect_frameworks(tree: list[str]) -> list[str]:
        found: list[str] = []
        if CodingToolbox._looks_android(tree):
            found.append("android")
        if "requirements.txt" in tree or "pyproject.toml" in tree:
            found.append("python")
        if "package.json" in tree:
            found.append("node")
        if "setup.py" in tree:
            found.append("python-packaging")
        return found

    # ── READ ─────────────────────────────────────────────────────────────
    def read_file(self, rel_path: str, start: int = 1,
                  limit: int = 400) -> dict[str, Any]:
        ok, reason = self.perms.can_read(rel_path)
        if not ok:
            return {"ok": False, "error": reason}
        real = self.perms.resolve_in_repo(rel_path)
        try:
            lines = real.read_text(encoding="utf-8", errors="replace") \
                .splitlines()
        except OSError as exc:
            return {"ok": False, "error": f"could not read: {exc}"}
        start = max(1, int(start))
        window = lines[start - 1: start - 1 + int(limit)]
        body = "\n".join(f"{n:>5} | {l}" for n, l in
                         enumerate(window, start))
        if len(body) > MAX_READ_BYTES:
            body = body[:MAX_READ_BYTES] + "\n… (truncated)"
        masked, kinds = self._mask(body)
        return {"ok": True, "path": str(rel_path), "total_lines": len(lines),
                "start": start, "content": masked, "masked_kinds": kinds}

    # ── SEARCH ───────────────────────────────────────────────────────────
    def search(self, pattern: str, glob: str = "*.py",
               max_results: int = MAX_SEARCH_HITS) -> dict[str, Any]:
        """Regex search across text files in the repo. Bounded and masked."""
        if not pattern:
            return {"ok": False, "error": "empty pattern"}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"ok": False, "error": f"bad regex: {exc}"}
        # "*.py, *.md" → suffixes (".py", ".md"); "*" means every file.
        suffixes = tuple(
            s if s.startswith(".") else "" + s
            for s in (part.strip().lstrip("*").lower()
                      for part in glob.split(","))
            if s)
        hits: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if suffixes and not fn.lower().endswith(suffixes):
                    continue
                real = Path(dirpath) / fn
                if path_is_secret(real):
                    continue
                try:
                    text = real.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = str(real.relative_to(self.root))
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        masked, kinds = self._mask(line.strip())
                        hits.append({"path": rel, "line": i, "text": masked,
                                     "masked": bool(kinds)})
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            if len(hits) >= max_results:
                break
        return {"ok": True, "pattern": pattern, "hits": hits,
                "truncated": len(hits) >= max_results}

    # ── CHANGE (edit / create / delete) ──────────────────────────────────
    def edit_file(self, rel_path: str, old: str, new: str,
                  force_large: bool = False) -> dict[str, Any]:
        """Replace the FIRST occurrence of `old` with `new`. `old` must match
        exactly (the model proposes from what it read). Large deletions are
        refused with needs_approval=True — approval is the caller's job.
        `force_large=True` is honoured ONLY after a human approval has been
        recorded upstream; the agent passes it through and the report
        mentions that approval was granted."""
        ok, reason = self.perms.can_write(rel_path)
        if not ok:
            return {"ok": False, "error": reason}
        real = self.perms.resolve_in_repo(rel_path)
        if not real.exists():
            return {"ok": False, "error": f"'{rel_path}' does not exist "
                                          f"(use create_file)"}
        try:
            before = real.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return {"ok": False, "error": "not decodable as UTF-8 text"}
        if old not in before:
            return {"ok": False, "error": "old text not found — re-read the "
                                          "file; it may have changed"}
        after = before.replace(old, new, 1)
        if after == before:
            return {"ok": False, "error": "edit would change nothing"}
        if self.perms.is_large_deletion(before, after) and not force_large:
            return {"ok": False, "needs_approval": True,
                    "error": (f"edit would remove "
                              f"{self.perms.removal_size(before, after)} lines "
                              f"(limit {LARGE_DELETE_LINES}) — needs approval"),
                    "path": str(rel_path)}
        return self._write_change(rel_path, "edit", before, after)

    def create_file(self, rel_path: str, content: str) -> dict[str, Any]:
        ok, reason = self.perms.can_write(rel_path)
        if not ok:
            return {"ok": False, "error": reason}
        real = self.perms.resolve_in_repo(rel_path)
        if real.exists():
            return self.edit_file(rel_path, real.read_text(
                encoding="utf-8", errors="replace"), content)
        real.parent.mkdir(parents=True, exist_ok=True)
        return self._write_change(rel_path, "create", None, content)

    def delete_file(self, rel_path: str, force: bool = False) -> dict[str, Any]:
        """Whole-file deletion is ALWAYS dangerous → needs approval.
        `force=True` is honoured only after a human approval upstream; the
        deletion is then done through the shared undo stack (reversible)."""
        ok, reason = self.perms.can_write(rel_path)
        if not ok:
            return {"ok": False, "error": reason}
        if not force:
            return {"ok": False, "needs_approval": True,
                    "error": "deleting a file needs human approval",
                    "path": str(rel_path)}
        real = self.perms.resolve_in_repo(rel_path)
        if not real.is_file():
            return {"ok": False, "error": f"'{rel_path}' is not a file"}
        before = real.read_text(encoding="utf-8", errors="replace")
        real.unlink()
        rec = change_record(str(rel_path), "delete", before, None)
        self._push_undo(
            f"coding_agent: delete {rel_path}",
            (lambda b=before, p=real: self._restore(p, b, str(rel_path))))
        return {"ok": True, "change": rec}

    def _write_change(self, rel_path: str, kind: str,
                      before: str | None, after: str) -> dict[str, Any]:
        real = self.perms.resolve_in_repo(rel_path)
        try:
            real.write_text(after, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"write failed: {exc}"}
        rec = change_record(str(rel_path), kind, before, after)
        # Mask what the model wrote BEFORE building anything user-visible:
        # a credential in `after` must not survive into `after`, `diff` or
        # the masked_kinds report.
        masked_after, kinds = self._mask(after)
        rec["after"] = masked_after if kinds else rec["after"]
        rec["masked_kinds"] = list(kinds)
        if kind == "edit":
            # The diff is built from the RAW texts, so it must be masked
            # separately — the model could have introduced a credential.
            diff_masked, diff_kinds = self._mask(
                self._unified_diff(before or "", after))
            rec["diff"] = diff_masked
            if diff_kinds:
                rec["masked_kinds"] = sorted(
                    set(rec["masked_kinds"]) | set(diff_kinds))
        # Shared undo stack: the user can say "undo" without knowing this
        # module exists.
        if before is not None:
            self._push_undo(
                f"coding_agent: {kind} {rel_path}",
                (lambda b=before, p=real: self._restore(p, b, str(rel_path))))
        return {"ok": True, "change": rec}

    @staticmethod
    def _restore(path: Path, content: str, label: str) -> str:
        path.write_text(content, encoding="utf-8")
        return f"restored {label}"

    @staticmethod
    def _unified_diff(before: str, after: str) -> str:
        import difflib
        diff = difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile="before", tofile="after", lineterm="", n=2)
        return "\n".join(diff)[:MAX_OUTPUT_BYTES]

    # ── EXECUTE ──────────────────────────────────────────────────────────
    def run(self, argv: list[str], timeout: float = DEFAULT_TIMEOUT_S,
            ) -> dict[str, Any]:
        """Run one allowlisted command in the repo. Output is capped and
        masked; exit status preserved."""
        ok, reason = self.perms.check_command(argv)
        if not ok:
            return {"ok": False, "error": reason, "argv": argv}
        out = self._run(argv, timeout)
        return out

    def run_tests(self, argv: list[str] | None = None,
                  timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        cmd = argv or (["pytest", "-q"] if self._detect_test_command(
            [str(p.relative_to(self.root)) for p in self.root.rglob("*")
             if p.is_file()][:MAX_TREE_ENTRIES]) else ["python", "-m",
                                                       "unittest", "discover"])
        res = self.run(cmd, timeout)
        res["command"] = cmd
        res["passed"] = bool(res.get("ok") and res.get("returncode") == 0)
        return res

    def run_python_file(self, rel_path: str,
                        timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        ok, reason = self.perms.can_read(rel_path)
        if not ok:
            return {"ok": False, "error": reason}
        return self.run([sys.executable, str(rel_path)], timeout)

    # ── ANDROID (Gradle builds) ──────────────────────────────────────────
    def is_android(self) -> bool:
        """True when this workspace is an Android project (gradle build files or
        Kotlin/Java sources under src/main)."""
        from orchestrator.agents.android_support import is_android_project
        return is_android_project(self.root)

    def android_scaffold(self,
                         dependencies: Optional[list[str]] = None,
                         app_name: Optional[str] = None,
                         entry_activity: Optional[str] = None) -> dict[str, Any]:
        """Write/refresh the Gradle skeleton the Kotlin/XML sources need.

        Deterministic, idempotent, and OUTSIDE the change/undo ledger — the
        scaffold OWNS these files (it rewrites the manifest, build files and
        resources on every run), so they are reported under `scaffold`, not as
        model changes."""
        from orchestrator.agents import android_support
        # Capture previous content of every scaffold-owned file BEFORE writing,
        # so an existing project's files can be restored on rollback (a new
        # project's fresh files are removed instead — see rollback_checkpoint).
        before: dict[str, Optional[str]] = {}
        for rel in android_support.scaffold_paths():
            try:
                p = self.perms.resolve_in_repo(rel)
                before[rel] = p.read_text(encoding="utf-8") if p.is_file() else None
            except Exception:
                before[rel] = None
        try:
            note = android_support.scaffold_project(self.root, dependencies,
                                                    app_name, entry_activity)
        except Exception as exc:                  # never break the agent
            return {"ok": False, "error": f"scaffold failed: {exc}"}
        entries: list[dict[str, Any]] = []
        for rel in android_support.scaffold_paths():
            try:
                p = self.perms.resolve_in_repo(rel)
            except Exception:
                continue
            if not p.is_file():
                continue
            try:
                after = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                after = None
            entries.append({"path": rel, "before": before.get(rel),
                            "after": after})
        return {"ok": True, "note": note,
                "files": android_support.scaffold_paths(), "entries": entries}

    def build_android(self, timeout: float = ANDROID_BUILD_TIMEOUT_S) -> dict:
        """Build a debug APK. Success requires BOTH gradle exit 0 and a REAL
        APK: one that contains the app's own launcher class. A module whose
        sources never compiled still yields an APK full of *library* code, so a
        bytes-only check lets a hollow app through — it installs, then crashes
        with ClassNotFoundException on open."""
        from orchestrator.agents import android_support
        argv = android_support.build_argv(self.root)
        if not argv:
            return {"ok": False, "passed": False,
                    "error": ("no gradle installed — run "
                              "scripts/android_bootstrap.sh once, then rebuild")}
        res = self.run(argv, timeout)
        res["command"] = argv
        apk = android_support.find_apk(self.root)
        res["apk"] = str(apk) if apk else None
        launcher = android_support.launcher_activity(self.root)
        res["launcher"] = launcher
        res["apk_real"] = bool(apk and android_support.apk_looks_real(
            apk, launcher))
        if res.get("ok") and res.get("returncode") == 0 and not res["apk_real"]:
            res.setdefault("stderr", "")
            res["stderr"] = ((res.get("stderr") or "") +
                             "\nBUILD FAILED: the APK does not contain the "
                             f"app's launcher class ({launcher}) — the sources "
                             "did not compile into the module (empty shell). "
                             "It would install and then crash on open.").strip()
        res["passed"] = bool(res.get("ok") and res.get("returncode") == 0
                             and res["apk_real"])
        return res

    def export_to_desktop(self, apk: Optional[str],
                          app_name: Optional[str] = None) -> dict[str, Any]:
        """Save the built APK on the Desktop (see android_support)."""
        from orchestrator.agents import android_support
        return android_support.export_to_desktop(self.root, apk, app_name)

    def _run(self, argv: list[str], timeout: float) -> dict[str, Any]:
        # 'python' on PATH is whatever the shell found — never trust it for
        # tests. Always run OUR interpreter (same contract as the router's
        # local rung and actions/dev_agent.py's substitution).
        if Path(str(argv[0])).name.lower() in ("python", "python3"):
            argv = [sys.executable] + list(argv[1:])
        try:
            proc = subprocess.run(
                argv, cwd=str(self.root), capture_output=True, text=True,
                errors="replace", timeout=max(1.0, timeout),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            stdout, kinds_out = self._mask(proc.stdout or "")
            stderr, kinds_err = self._mask(proc.stderr or "")
            return {
                "ok": True, "returncode": proc.returncode,
                "stdout": stdout[:MAX_OUTPUT_BYTES],
                "stderr": stderr[:MAX_OUTPUT_BYTES],
                "masked_kinds": kinds_out + kinds_err,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {"ok": True, "returncode": -1, "timed_out": True,
                    "stdout": self._mask((exc.stdout or b"").decode(
                        "utf-8", "replace"))[0][:MAX_OUTPUT_BYTES],
                    "stderr": f"timed out after {timeout}s"}
        except FileNotFoundError:
            return {"ok": False, "error": f"command not found: {argv[0]}"}
        except Exception as exc:                    # never break the agent
            return {"ok": False, "error": f"could not run {argv[0]}: {exc}"}

    # ── diffs ────────────────────────────────────────────────────────────
    def git_diff(self, timeout: float = 20) -> dict[str, Any]:
        """Working-tree diff for REVIEW — read-only git, masked."""
        if not (self.root / ".git").exists():
            return {"ok": False, "error": "not a git repository"}
        out = self._run(["git", "diff", "--stat"], timeout)
        full = self._run(["git", "diff"], timeout)
        return {"ok": True,
                "stat": out.get("stdout", ""),
                "diff": full.get("stdout", "")[:MAX_OUTPUT_BYTES],
                "masked_kinds": out.get("masked_kinds", []) +
                                full.get("masked_kinds", [])}

    def changes_summary(self, changes: list[dict]) -> str:
        """Deterministic change summary for the REPORT stage."""
        if not changes:
            return "No files were changed."
        rows = []
        for c in changes:
            rows.append(f"  {c['kind']:>6}  {c['path']}  "
                        f"(+{c['lines_added']} / -{c['lines_removed']}, "
                        f"{c['before_sha']}→{c['after_sha']})")
        return f"{len(changes)} file(s) changed:\n" + "\n".join(rows)


# ── error understanding ──────────────────────────────────────────────────────
def classify_error(output: str) -> str:
    """dependency | syntax | import | runtime | none — same vocabulary the
    existing dev_agent prints, so logs stay comparable. Android/Gradle
    failure classes (scaffold/compile/build) are distinguished too, because
    the fix differs: a scaffold error is repaired by re-scaffolding, not by
    editing the model's code."""
    low = (output or "").lower()
    if any(x in low for x in ("no module named", "modulenotfounderror")):
        return "dependency"
    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax"
    if "importerror" in low or "cannot import" in low:
        return "import"
    # Generated-code errors MUST win over the generic "error:" pattern below:
    # "package R does not exist" is produced by AAPT from the manifest, so an
    # edit of the activity can never fix it — the scaffold must.
    if any(x in low for x in ("package r does not exist", "r cannot be resolved",
                              "cannot find symbol: class r")):
        return "scaffold"
    # A build that "succeeded" but produced a library-only shell: the module
    # compiled none of the app's sources (they were written outside the module).
    # Re-scaffolding relocates them — never an edit of the model's code.
    if any(x in low for x in ("does not contain the app's launcher class",
                              "launcher class (", "empty shell")):
        return "scaffold"
    # Resource linking: a missing @string/@color — deterministic to fix.
    if any(x in low for x in ("resource linking failed", "aapt:",
                              "android resource linking failed",
                              "error: resource", "resource not found")):
        return "resource"
    # Kotlin/Javac compilation errors the model CAN fix by editing code.
    if any(x in low for x in ("unresolved reference", "compilation error",
                              "compilation failed", "e: file")):
        return "compile"
    if re.search(r"\.(?:kt|java):\d+:\s*(?:error|warning)", output or "",
                 re.IGNORECASE):
        return "compile"
    # Process/toolchain failures the model cannot fix by editing code.
    if any(x in low for x in ("command not found", "no such file or directory",
                              "sdk location not found", "failed to install",
                              "gradle: not found", "could not find or load")):
        return "environment"
    if re.search(r"\b(gradlew?|adb|sdkmanager|javac?|kotlinc)\b[^\n]{0,40}"
                 r"not found", output or "", re.IGNORECASE):
        return "environment"
    if any(x in low for x in ("traceback", "exception", "error:", "nameerror",
                              "typeerror", "attributeerror", "valueerror",
                              "keyerror", "indexerror", "zerodivisionerror",
                              "filenotfounderror", "assertionerror")):
        return "runtime"
    return "none"


def parse_traceback(output: str, project_files: list[str]) -> tuple[str, int]:
    """(file, line) of the deepest frame in one of OUR files — the frame an
    edit is most likely to target. ('', 0) when nothing matches.

    Understands Python tracebacks and Kotlin/Java compiler errors
    (`e: file:///path/File.kt:19:56 …` and `/path/File.java:30: error: …`)."""
    pat = re.compile(r'File "([^"\']+\.py)", line (\d+)')
    for raw_path, line in reversed(pat.findall(output or "")):
        name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == name or str(pf) == raw_path or \
                    raw_path.endswith(str(pf)):
                return pf, int(line)
    # Kotlin/gradle: "e: file:///path/File.kt:19:56 text"
    kpat = re.compile(r"[ew]:\s+file://(\S+?\.(?:kt|java)):(\d+)",
                      re.IGNORECASE)
    k_matches = kpat.findall(output or "")
    if not k_matches:
        # javac: "/abs/path/File.java:30: error: text"
        k_matches = re.compile(
            r"([^\s:]+\.(?:kt|java)):(\d+):\s*error",
            re.IGNORECASE).findall(output or "")
    # kotlinc reports independent errors (no frame stack) — the FIRST one is
    # the primary complaint, so no reversal here.
    for raw_path, line in k_matches:
        name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == name or str(pf) == raw_path or \
                    raw_path.endswith(str(pf)):
                return pf, int(line)
    return "", 0


def missing_module(output: str) -> str:
    m = re.search(r"No module named ['\"]([\w\-\./]+)['\"]", output or "")
    return m.group(1) if m else ""
