#!/usr/bin/env python3
"""
scripts/jarvis_inbox.py — the ONE normalized entry point from Hermes into
Jarvis (Telegram / WhatsApp → Hermes → this script → dashboard API → Jarvis).

Hermes' gateway (or a Hermes skill calling this script) forwards every inbound
message event here, regardless of platform. The script normalizes it (source
channel, chat id, text, attachments — spec §8) and hands it to the dashboard's
/api/message endpoint. Jarvis decides intent (normal / RAG / live data),
processes remember-vs-summarize, and the reply arm in main.py delivers the
answer back through `hermes send` on the same channel.

USAGE (from Hermes gateway hooks / skills):
    # text message:
    jarvis_inbox.py --channel telegram --chat 12345 --text "analyze NIFTY"

    # document with caption (THE pdf flow):
    jarvis_inbox.py --channel telegram --chat 12345 \
        --file "$HOME/Mark-III/uploads/report.pdf" \
        --text "remember this document"

    # document only (caption arrives as a separate message in some clients):
    jarvis_inbox.py --channel whatsapp --chat 911234567890 \
        --file "/path/to/notes.pdf"

CONFIG (env, both optional — defaults below):
    JARVIS_DASHBOARD_URL   default: http://127.0.0.1:8000
    JARVIS_DASHBOARD_TOKEN dashboard static token (⚙ Settings → API access token)

RAG MODE (--rag)
    --rag [collection] ingests the --file document DIRECTLY into the RAG
    knowledge base through the dashboard's /api/rag/ingest-path endpoint —
    no Gemini session, no LLM: pure indexing (extract → chunk → embed →
    index). Use it when the caption says remember/save: the reply comes back
    as structured JSON from the RAG service (doc_id, pages, chunks), which
    the reply arm forwards to the channel. Without --rag the message goes to
    the normal Jarvis session (model decides read-once vs remember).

EXIT: 0 = delivered to Jarvis, 1 = failed (with reason on stderr).
"""
import os
import ssl
import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

# The local dashboard serves HTTPS with a self-signed cert (config/certs).
DASHBOARD_URL = os.environ.get("JARVIS_DASHBOARD_URL", "https://127.0.0.1:8000")
BASE = Path(__file__).resolve().parent.parent
TOKEN_FILE = BASE / "config" / "api_keys.json"

# The local dashboard usually serves HTTPS with a self-signed cert
# (config/certs). Verify nothing for loopback delivery — the token is the
# credential here, and the connection never leaves the machine.
_SSL_CTX = ssl._create_unverified_context() if DASHBOARD_URL.startswith("https") else None


def _token() -> str:
    """The dashboard's static bearer token — same one curl/scripts use."""
    env = os.environ.get("JARVIS_DASHBOARD_TOKEN", "").strip()
    if env:
        return env
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        return str(data.get("dashboard_auth_token") or "").strip()
    except Exception:
        return ""


def send(channel: str, chat_id: str, text: str, file_path: str | None) -> int:
    token = _token()
    if not token:
        print("error: no dashboard token (set JARVIS_DASHBOARD_TOKEN "
              "or save it via the dashboard settings)", file=sys.stderr)
        return 1

    boundary = "----JarvisInboxBoundary7f3a2b"
    body = bytearray()

    def field(name: str, value: str) -> None:
        body.extend(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode())

    field("channel", channel)
    if chat_id:
        field("chat_id", chat_id)
    if text:
        field("text", text)
    if file_path:
        p = Path(file_path).expanduser()
        if not p.exists():
            print(f"error: file not found: {p}", file=sys.stderr)
            return 1
        body.extend(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; "
            f"filename=\"{p.name}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n".encode())
        body.extend(p.read_bytes())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{DASHBOARD_URL}/api/message", data=bytes(body), method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            out = json.loads(resp.read().decode())
            if out.get("ok"):
                where = f" + {out.get('file')}" if out.get("file") else ""
                print(f"delivered to Jarvis{where}")
                return 0
            print(f"error: {out}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            pass
        print(f"error: HTTP {e.code} {detail}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def send_rag(file_path: str, collection: str, title: str | None,
             chat_id: str) -> int:
    """Direct RAG ingestion — /api/rag/ingest-path, no LLM session."""
    token = _token()
    if not token:
        print("error: no dashboard token (set JARVIS_DASHBOARD_TOKEN "
              "or save it via the dashboard settings)", file=sys.stderr)
        return 1
    p = Path(file_path).expanduser()
    if not p.exists():
        print(f"error: file not found: {p}", file=sys.stderr)
        return 1
    payload = {
        "path": str(p),
        "collection": collection,
        "source": "hermes",
        "metadata": {"channel_chat": chat_id} if chat_id else {},
    }
    if title:
        payload["title"] = title
    req = urllib.request.Request(
        f"{DASHBOARD_URL}/api/rag/ingest-path",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            out = json.loads(resp.read().decode())
            if out.get("ok"):
                if out.get("duplicate"):
                    print(f"already remembered: {out.get('title')}")
                else:
                    print(f"indexed '{out.get('title')}' — "
                          f"{out.get('pages')} pages, {out.get('chunks')} chunks "
                          f"(doc_id {out.get('doc_id')}, collection "
                          f"{out.get('collection')})")
                return 0
            print(f"error: {out.get('error', out)}", file=sys.stderr)
            return 1
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            pass
        print(f"error: HTTP {e.code} {detail}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Forward a Hermes/Telegram/"
                                             "WhatsApp message into Jarvis")
    ap.add_argument("--channel", default="telegram",
                    help="telegram | whatsapp (default: telegram)")
    ap.add_argument("--chat", dest="chat_id", default="",
                    help="chat/user id on that platform (for the reply routing)")
    ap.add_argument("--text", default="",
                    help="message text or the document's caption")
    ap.add_argument("--file", dest="file_path", default=None,
                    help="local path of the attachment Hermes already saved")
    ap.add_argument("--rag", dest="rag_collection", nargs="?", const="default",
                    default=None,
                    help="ingest --file directly into the RAG knowledge base "
                         "(optionally: collection name); skips the LLM session")
    ap.add_argument("--title", default=None,
                    help="optional display title for --rag ingestion")
    a = ap.parse_args()

    if a.rag_collection is not None:
        if not a.file_path:
            print("error: --rag needs --file with the document path",
                  file=sys.stderr)
            sys.exit(1)
        collection = a.rag_collection
        if collection == "default":
            collection = "personal_knowledge"
            text = (a.text or "").lower()
            for cand in ("trading_books", "strategies", "research",
                         "market_rules", "temporary"):
                if cand in text:
                    collection = cand
                    break
        sys.exit(send_rag(a.file_path, collection, a.title, a.chat_id))

    sys.exit(send(a.channel, a.chat_id, a.text, a.file_path))
