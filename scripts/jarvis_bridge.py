#!/usr/bin/env python3
"""scripts/jarvis_bridge.py — the read-only window Hermes gets into Jarvis.

Surfaces: rag-query / rag-answer / rag-docs / memory-search / memory-list / agents-status
Identity: caller "hermes" (read-only, config/rag_callers.json grants __all__)
"""
from __future__ import annotations
import argparse, json, os, ssl, sys, urllib.error, urllib.request, urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_FILE = BASE_DIR / "config" / "api_keys.json"
CALLER = "hermes"
_LOOPBACK = ("127.0.0.1", "localhost", "::1")

def _dashboard_urls():
    configured = os.environ.get("JARVIS_DASHBOARD_URL", "").strip().rstrip("/")
    if not configured: return ["https://127.0.0.1:8000", "http://127.0.0.1:8000"]
    urls = [configured]
    if any(h in configured for h in _LOOPBACK):
        if configured.startswith("https://"): urls.append("http://" + configured[8:])
        elif configured.startswith("http://"): urls.append("https://" + configured[7:])
    return urls

def _token():
    t = os.environ.get("JARVIS_DASHBOARD_TOKEN","").strip()
    if t: return t
    try: return str(json.loads(TOKEN_FILE.read_text()).get("dashboard_auth_token") or "").strip()
    except: return ""

def _post(path, data, headers=None, timeout=120):
    last_err = None
    body = json.dumps(data).encode("utf-8") if isinstance(data, dict) else data
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers or {})
    # Inject bearer token if we have one
    token = _token()
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    for base in _dashboard_urls():
        ctx = ssl._create_unverified_context() if base.startswith("https") else None
        req = urllib.request.Request(base+path, data=body, method="POST", headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError: raise
        except Exception as e:
            reason = getattr(e,"reason",e)
            if isinstance(reason, ssl.SSLError) or "SSL" in str(e): last_err=e; continue
            raise
    raise last_err or RuntimeError("dashboard unreachable")

def rag_query(query, top_k=5, as_json=False):
    out = _post("/api/rag/query", {"query":query,"top_k":top_k,"caller":CALLER}, {}, 120)
    if as_json: print(json.dumps(out,indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out.get('error') or out}",file=sys.stderr); return 1
    results=out.get("results") or []; 
    if not results: print(f"No library matches for: {query}"); return 0
    print(f"{len(results)} match(es) for {query!r} (mode: {out.get('mode')})")
    for i,r in enumerate(results,1):
        print(f"\n[{i}] {(r.get('title') or r.get('source') or '?')[:60]}{' p.'+str(r.get('page')) if r.get('page') else ''} ({r.get('collection','?')})")
        txt=" ".join(str(r.get("text","")).split()); print(f"    {txt[:400]}{'…' if len(txt)>400 else ''}")
    return 0

def rag_answer(query, as_json=False):
    out = _post("/api/rag/answer", {"query":query,"caller":CALLER}, {}, 120)
    if as_json: print(json.dumps(out,indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out.get('error') or out}",file=sys.stderr); return 1
    print(out.get("answer") or "(no answer)")
    for c in (out.get("citations") or []): print(f"  - {c}")
    return 0

def rag_docs(as_json=False):
    token = _token()
    for base in _dashboard_urls():
        ctx = ssl._create_unverified_context() if base.startswith("https") else None
        url = f"{base}/api/rag/documents?caller={CALLER}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                out = json.loads(r.read().decode())
            break
        except Exception as e:
            out = {"ok": False, "error": str(e)}
    if as_json: print(json.dumps(out,indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out.get('error') or out}",file=sys.stderr); return 1
    docs=out.get("documents") or []; 
    if not docs: print("The library is empty for the 'hermes' caller."); return 0
    print(f"{len(docs)} document(s):")
    for d in docs: print(f"  - {d.get('title','untitled')} [{d.get('collection','?')}]")
    return 0

def memory_search(query, limit=8, as_json=False):
    token = _token()
    url = f"{_dashboard_urls()[0]}/api/memory/search?query={urllib.parse.quote(query)}&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        ctx = ssl._create_unverified_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            out = json.loads(r.read().decode())
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    if as_json: print(json.dumps(out, indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out.get('error') or out}", file=sys.stderr); return 1
    hits = out.get("matches") or []
    total = out.get("total", len(hits))
    if not hits: print(f"No matches for {query!r}"); return 0
    print(f"{total} stored fact(s), {len(hits)} match for {query!r}:")
    for e in hits:
        cat = e.get("category", "?")
        key = e.get("key", "?")
        val = str(e.get("value", "") or "").strip()[:200]
        print(f"  [{cat}] {key}: {val}")
    return 0

def memory_list(limit=20, as_json=False):
    token = _token()
    url = f"{_dashboard_urls()[0]}/api/memory/list?limit={limit}&category="
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        ctx = ssl._create_unverified_context() if url.startswith("https") else None
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
            out = json.loads(r.read().decode())
    except Exception as e:
        out = {"ok": False, "error": str(e)}
    if as_json: print(json.dumps(out, indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out.get('error') or out}", file=sys.stderr); return 1
    entries = out.get("entries") or []
    total = out.get("total", len(entries))
    print(f"{total} stored fact(s) (showing up to {limit}):")
    for e in entries[:limit]:
        cat = e.get("category", "?")
        key = e.get("key", "?")
        val = str(e.get("value", "") or "").strip()[:160]
        print(f"  [{cat}] {key}: {val}")
    return 0

def agents_status(as_json=False):
    token = _token()
    last_err = None
    for base in _dashboard_urls():
        ctx = ssl._create_unverified_context() if base.startswith("https") else None
        url = f"{base}/api/autonomy/status"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                out = json.loads(r.read().decode())
            break
        except Exception as e:
            last_err = e
            if base.startswith("https"): continue
            out = {"ok": False, "error": str(e)}
    if as_json: print(json.dumps(out,indent=2)); return 0 if out.get("ok") else 1
    if not out.get("ok"): print(f"error: {out}",file=sys.stderr); return 1
    print(f"emergency_stop : {out.get('emergency_stop')}"); print(f"tasks          : {out.get('tasks')}")
    return 0

def main(argv=None):
    ap=argparse.ArgumentParser(prog="jarvis-bridge",description="Read-only access to Jarvis's RAG, memory and agents.")
    ap.add_argument("--json",action="store_true")
    sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("rag-query"); p.add_argument("query"); p.add_argument("--top-k",type=int,default=5)
    p=sub.add_parser("rag-answer"); p.add_argument("query")
    sub.add_parser("rag-docs")
    p=sub.add_parser("memory-search"); p.add_argument("query"); p.add_argument("--limit",type=int,default=8)
    p=sub.add_parser("memory-list"); p.add_argument("--limit",type=int,default=20)
    sub.add_parser("agents-status")
    args=ap.parse_args(argv); aj=bool(getattr(args,"json",False))
    try:
        if args.cmd=="rag-query": return rag_query(args.query,args.top_k,aj)
        if args.cmd=="rag-answer": return rag_answer(args.query,aj)
        if args.cmd=="rag-docs": return rag_docs(aj)
        if args.cmd=="memory-search": return memory_search(args.query,args.limit,aj)
        if args.cmd=="memory-list": return memory_list(args.limit,aj)
        if args.cmd=="agents-status": return agents_status(aj)
    except RuntimeError as e: print(f"error: {e}",file=sys.stderr); return 1
    except Exception as e: print(f"error: {type(e).__name__}: {e}",file=sys.stderr); return 1
    return 2

if __name__=="__main__": sys.exit(main())
