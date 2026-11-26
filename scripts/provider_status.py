#!/usr/bin/env python3
"""One-line status of every configured LLM provider."""
import json,os,sys,urllib.request,urllib.error
from pathlib import Path
BASE=Path(__file__).resolve().parent.parent
CFG=BASE/"config/api_keys.json"
ENV=Path.home()/".hermes/.env"
def env(k):
    for l in ENV.read_text().splitlines():
        if l.startswith(k+"="): return l.split("=",1)[1].strip()
    return ""
def cfg(k):
    try: return str(json.loads(CFG.read_text()).get(k) or "")
    except: return ""
def kok(cc,ec): return bool((cfg(cc) or env(ec)).strip())
def sget(url,h={},t=5):
    try:
        r=urllib.request.urlopen(urllib.request.Request(url,headers=h or {}),timeout=t)
        b=r.read(2_000_000).decode("utf-8",errors="ignore")
        return r.status,json.loads(b) if b.strip().startswith("{") else {}
    except urllib.error.HTTPError as e: return e.code,{}
    except: return 0,{}

print("\n"+"="*78+"\n  PROVIDER STATUS — Mark-III / Hermes\n"+"="*78)
print(f"  {'Provider':<20} {'Key':>5} {'Catalog':>9} {'Live':<32} {'Role'}")
print("─"*78)
for name,cc,ec,url,hdr,key_name in [
    ("NVIDIA NIM","nvidia_api_key","NVIDIA_API_KEY",
     "https://integrate.api.nvidia.com/v1/models",{"Authorization":f"Bearer {env('NVIDIA_API_KEY')}"},{"model":"PRIMARY"}),
    ("OpenRouter","openrouter_api_key","OPENROUTER_API_KEY",
     "https://openrouter.ai/api/v1/models",{"Authorization":f"Bearer {env('OPENROUTER_API_KEY')}"},{"model":"FALLBACK#2"}),
    ("Gemini Direct","gemini_api_key","GEMINI_API_KEY",
     f"https://generativelanguage.googleapis.com/v1beta/models?key={env('GEMINI_API_KEY')}" if env("GEMINI_API_KEY") else "",
     {"x-goog-api-key":env("GEMINI_API_KEY")} if env("GEMINI_API_KEY") else {},{"model":"FALLBACK#1"}),
    ("Ollama Local","","","http://localhost:11434/v1/models",{},{"model":"FALLBACK#3"}),
]:
    k="✓" if kok(cc,ec) else ("—" if cc=="" else "✗")
    code,data=sget(url,h=hdr if hdr else {})
    n=len(data.get("data",data.get("models",[]))) if code>=200 else 0
    live=f"YES ({n} models)" if code>=200 else (f"HTTP {code}" if code else "NO")
    role={"PRIMARY":"PRIMARY","FALLBACK#1":"FALLBACK #1","FALLBACK#2":"FALLBACK #2","FALLBACK#3":"FALLBACK #3"}.get(hdr.get("model",""),hdr.get("model",""))
    if name=="NVIDIA NIM": role="PRIMARY"
    elif name=="OpenRouter": role="FALLBACK #2"
    elif name=="Gemini Direct": role="FALLBACK #1"
    elif name=="Ollama Local": role="FALLBACK #3"
    print(f"  {name:<20} {k:>5} {n:>9} {live:<32} {role}")
print("="*78+"\n")
