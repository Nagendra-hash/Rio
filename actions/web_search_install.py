"""web_search_install.py — Search the web for install instructions and run them.

Use cases:
  * "install Ollama"
  * "how do I set up whisper locally"
  * "find and install ffmpeg on Ubuntu"
  * "look up pip package X and install it"
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def _search(query: str, max_results: int = 5) -> list[dict]:
    """Search DuckDuckGo HTML (no API key needed)."""
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return [{"error": f"search failed: {e}"}]
    
    results = []
    # Simple extraction from DDG HTML
    import re
    snippets = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', html, re.S)
    desc = re.findall(r'class="result__snippet"[^>]*>(.*?)</span>', html, re.S)
    for (link, title), d in zip(snippets, desc):
        title = re.sub(r'<[^>]+>', '', title).strip()
        snippet = re.sub(r'<[^>]+>', '', d).strip()
        if title and link and len(results) < max_results:
            results.append({"title": title, "url": link, "snippet": snippet})
    return results[:max_results]


def _run_cmd(cmd: str, timeout: int = 300) -> tuple[int, str, str]:
    """Run a shell command safely. Returns (returncode, stdout, stderr)."""
    safe, reason = _is_safe_command(cmd)
    if not safe:
        return 1, "", f"unsafe command rejected: {reason}"
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout[:2000], proc.stderr[:2000]
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"
    except Exception as e:
        return 1, "", str(e)


def _is_safe_command(cmd: str) -> tuple[bool, str]:
    """Basic safety check — no rm -rf /, no destructive system commands."""
    dangerous = ["rm -rf /", "mkfs", "dd if=/dev/zero", "chmod 777 /"]
    for bad in dangerous:
        if bad in cmd.lower():
            return False, f"contains dangerous pattern: {bad}"
    return True, ""


def web_search_install(parameters: dict, player=None, speak=None) -> str:
    parameters = parameters or {}
    query = str(parameters.get("query") or "").strip()
    dry_run = str(parameters.get("dry_run", "false")).lower() in ("true", "yes", "1")
    if not query:
        return "error: missing 'query' parameter"
    try:
        results = _search(query)
        if not results or (results and results[0].get("error")):
            return f"No search results for: {query}"
        best = results[0]
        plan = f"Search found: {best['title']}\nURL: {best['url']}\nSnippet: {best.get('snippet','')[:200]}"
        if dry_run:
            return f"[DRY RUN] {plan}"
        # Extract install command from snippet (heuristic)
        snippet = best.get("snippet", "").lower()
        if "apt install" in snippet or "pip install" in snippet:
            import re
            cmds = re.findall(r"(?:apt[-\s]install|pip[-\s]install|npm[-\s]install|brew[-\s]install)\s+\S+", snippet)
            if cmds:
                cmd = cmds[0]
                rc, out, err = _run_cmd(cmd)
                return f"Ran: {cmd}\n{out}{err}" if rc == 0 else f"Failed ({rc}): {err[:200]}"
        return f"Plan:\n{plan}\n\n(Specify an explicit --command to execute)"
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


TOOL = {
    "name": "web_search_install",
    "description": (
        "Search the web for how to install software or a tool, then optionally "
        "run the install command. Use for things like: 'install Ollama', 'set up "
        "Whisper locally', 'find and install ffmpeg', 'look up pip package X and "
        "install it'. Supports a dry_run=true parameter to preview without executing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Natural-language request describing what to search for and install.",
            },
            "dry_run": {
                "type": "BOOLEAN",
                "description": "If true, only show planned commands without executing. Default false.",
            },
        },
        "required": ["query"],
    },
    "handler": web_search_install,
}
