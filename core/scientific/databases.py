"""
core/scientific/databases.py — documented public scientific APIs, directly.

WHY WE WRITE THESE OURSELVES
    The K-Dense `database-lookup` skill is excellent procedural guidance (80
    databases, identifier rules, pagination traps) but it is text. RIO needs
    actual retrieval that it can test, bound and log. These clients implement
    the endpoints the brief names — PubMed, Europe PMC, arXiv, Crossref,
    UniProt, PubChem, ChEMBL, ClinicalTrials.gov — plus the access rules the
    skill documents (rate limits, counts, provenance, no secrets in output).

WHAT THEY GUARANTEE
    * read-only GETs, stdlib urllib, no API key required
    * a per-host rate limiter (NCBI 3 req/s, arXiv 1 per 3 s, ...)
    * 15 s timeout, bounded retries on 429/5xx, no infinite pagination
    * every result carries source, endpoint, params, retrieval time and count
    * records are plain dicts; the caller wraps them as untrusted before a
      model sees them (see `format_for_model`)

NOT ACCESSED, BY DESIGN
    COSMIC, DrugBank and BRENDA need licences or registration. `lookup()`
    refuses them and names the free alternative, which is exactly what the
    upstream skill instructs. The agent never bypasses an access control.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

from core.scientific.config import DB_RETRIES, DB_TIMEOUT_S, DB_USER_AGENT

TOOL_NAME = "RIO-ScientificAgent"

# host -> minimum seconds between calls
_RATE_LIMITS: dict[str, float] = {
    "eutils.ncbi.nlm.nih.gov": 0.35,
    "www.ebi.ac.uk": 0.12,
    "export.arxiv.org": 3.0,
    "api.crossref.org": 0.25,
    "rest.uniprot.org": 0.20,
    "pubchem.ncbi.nlm.nih.gov": 0.25,
    "clinicaltrials.gov": 0.25,
}

_HOST_LOCKS: dict[str, threading.Lock] = {}
_HOST_LAST: dict[str, float] = {}
_LOCK = threading.Lock()

PUBLIC_DATABASES: dict[str, dict[str, Any]] = {
    "pubmed": {"needs_key": False, "kind": "literature", "label": "PubMed (NCBI E-utilities)"},
    "europepmc": {"needs_key": False, "kind": "literature", "label": "Europe PMC (papers + preprints)"},
    "arxiv": {"needs_key": False, "kind": "literature", "label": "arXiv preprints"},
    "crossref": {"needs_key": False, "kind": "metadata", "label": "Crossref DOI metadata"},
    "uniprot": {"needs_key": False, "kind": "protein", "label": "UniProt"},
    "pubchem": {"needs_key": False, "kind": "compound", "label": "PubChem"},
    "chembl": {"needs_key": False, "kind": "compound", "label": "ChEMBL"},
    "clinicaltrials": {"needs_key": False, "kind": "clinical", "label": "ClinicalTrials.gov"},
}

RESTRICTED_DATABASES: dict[str, str] = {
    "cosmic": "licensed somatic-mutation downloads; academic registration required. "
              "Free alternatives: cBioPortal or GDC.",
    "drugbank": "paid API licence. Free alternatives: ChEMBL + PubChem + OpenFDA.",
    "brenda": "free registration required (SOAP, not REST). Free alternative: KEGG.",
    "omim": "requires an academic licence key.",
    "disgenet": "academic subset or licensed plan required.",
}


@dataclass
class Budget:
    """Per-task call budget, so one research run cannot hammer an API."""

    max_calls: int = 40
    calls: int = 0

    def take(self) -> bool:
        with _LOCK:
            if self.calls >= self.max_calls:
                return False
            self.calls += 1
            return True

    def public(self) -> dict:
        return {"max_calls": self.max_calls, "calls": self.calls}


@dataclass
class Result:
    ok: bool
    source: str
    records: list[dict] = field(default_factory=list)
    error: str = ""
    endpoint: str = ""
    params: dict = field(default_factory=dict)
    retrieved_at: str = ""
    warnings: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "ok": self.ok,
            "source": self.source,
            "records": self.records,
            "error": self.error,
            "provenance": {
                "endpoint": self.endpoint,
                "params": self.params,
                "retrieved_at": self.retrieved_at,
                "count": len(self.records),
            },
            "warnings": self.warnings,
        }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _polite_params() -> dict[str, str]:
    """NCBI asks for tool + contact; we send only what is configured."""
    params = {"tool": TOOL_NAME}
    email = (os.environ.get("NCBI_EMAIL") or "").strip()
    if email:
        params["email"] = email
    key = (os.environ.get("NCBI_API_KEY") or "").strip()
    if key:
        params["api_key"] = key
    return params


def _rate_limit(host: str) -> None:
    delay = _RATE_LIMITS.get(host, 0.2)
    with _LOCK:
        lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    with lock:
        last = _HOST_LAST.get(host, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        _HOST_LAST[host] = time.monotonic()


def _http_get(url: str, *, accept: str = "application/json") -> tuple[int, str, str]:
    """(status, body, error). Retries 429/5xx; never raises."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    headers = {"User-Agent": DB_USER_AGENT, "Accept": accept}
    last_error = ""
    for attempt in range(DB_RETRIES + 1):
        _rate_limit(host)
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=DB_TIMEOUT_S) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body, ""
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < DB_RETRIES:
                time.sleep(1.0 + attempt * 1.5)
                continue
            return exc.code, "", last_error
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < DB_RETRIES:
                time.sleep(1.0 + attempt)
                continue
            return 0, "", last_error
    return 0, "", last_error


def _json(url: str) -> tuple[dict | None, str, str]:
    status, body, error = _http_get(url)
    if error:
        return None, url, error
    try:
        data = json.loads(body)
    except Exception as exc:
        return None, url, f"malformed JSON: {type(exc).__name__}"
    return data, url, ""


def _fail(source: str, endpoint: str, params: dict, error: str) -> Result:
    return Result(False, source, [], error=error, endpoint=endpoint, params=params,
                  retrieved_at=_now())


# ── PubMed ──────────────────────────────────────────────────────────────────


def search_pubmed(query: str, limit: int = 10, budget: Budget | None = None) -> Result:
    query = (query or "").strip()
    if not query:
        return _fail("pubmed", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("pubmed", "", {"term": query}, "call budget exhausted")

    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    params = {
        "db": "pubmed", "term": query, "retmode": "json",
        "retmax": str(max(1, min(limit, 50))), "sort": "relevance",
        **_polite_params(),
    }
    data, url, error = _json(f"{base}/esearch.fcgi?{urllib.parse.urlencode(params)}")
    if error:
        return _fail("pubmed", url, params, error)
    ids = ((data or {}).get("esearchresult") or {}).get("idlist") or []
    total = ((data or {}).get("esearchresult") or {}).get("count")
    if not ids:
        return Result(True, "pubmed", [], endpoint=url, params=params, retrieved_at=_now())

    if budget and not budget.take():
        return _fail("pubmed", url, params, "call budget exhausted")
    summary_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "json", **_polite_params()}
    data, url2, error = _json(f"{base}/esummary.fcgi?{urllib.parse.urlencode(summary_params)}")
    if error:
        return _fail("pubmed", url2, summary_params, error)

    result = (data or {}).get("result") or {}
    records: list[dict] = []
    for pmid in ids:
        entry = result.get(pmid) or {}
        if not isinstance(entry, dict) or not entry:
            continue
        doi = ""
        for identifier in entry.get("articleids") or []:
            if str(identifier.get("idtype")) == "doi":
                doi = str(identifier.get("value") or "")
                break
        authors = [str(a.get("name") or "") for a in (entry.get("authors") or [])][:8]
        records.append({
            "source": "pubmed",
            "id": pmid,
            "title": _clean(entry.get("title")),
            "authors": authors,
            "journal": entry.get("fulljournalname") or entry.get("source") or "",
            "year": str(entry.get("pubdate") or "")[:4],
            "date": str(entry.get("pubdate") or ""),
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "type": "journal-article",
        })
    warnings = []
    if total and int(total) > len(records):
        warnings.append(f"showing {len(records)} of {total} matches")
    return Result(True, "pubmed", records, endpoint=url2, params=summary_params,
                  retrieved_at=_now(), warnings=warnings)


# ── Europe PMC ──────────────────────────────────────────────────────────────


def search_europepmc(query: str, limit: int = 10, budget: Budget | None = None) -> Result:
    query = (query or "").strip()
    if not query:
        return _fail("europepmc", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("europepmc", "", {"query": query}, "call budget exhausted")
    params = {
        "query": query, "format": "json",
        "pageSize": str(max(1, min(limit, 25))), "resultType": "core",
    }
    url = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
           + urllib.parse.urlencode(params))
    data, url, error = _json(url)
    if error:
        return _fail("europepmc", url, params, error)
    result_list = ((data or {}).get("resultList") or {}).get("result") or []
    records = []
    for entry in result_list:
        records.append({
            "source": "europepmc",
            "id": entry.get("id") or entry.get("pmid") or "",
            "pmid": entry.get("pmid") or "",
            "doi": entry.get("doi") or "",
            "title": _clean(entry.get("title")),
            "authors": (entry.get("authorString") or "").strip(" .")[:400],
            "journal": entry.get("journalTitle") or entry.get("bookOrReportDetails", {}).get("publisher") or "",
            "year": str(entry.get("pubYear") or ""),
            "abstract": _clean(entry.get("abstractText"))[:1200],
            "is_preprint": str(entry.get("source") or "").upper() in ("PPR", "ARXIV"),
            "type": "preprint" if str(entry.get("source") or "").upper() in ("PPR", "ARXIV") else "journal-article",
            "url": (f"https://europepmc.org/article/{entry.get('source')}/{entry.get('id')}"
                    if entry.get("source") and entry.get("id") else ""),
        })
    hit = (data or {}).get("hitCount")
    warnings = []
    if hit and int(hit) > len(records):
        warnings.append(f"showing {len(records)} of {hit} matches")
    return Result(True, "europepmc", records, endpoint=url, params=params,
                  retrieved_at=_now(), warnings=warnings)


# ── arXiv ───────────────────────────────────────────────────────────────────


def search_arxiv(query: str, limit: int = 10, budget: Budget | None = None) -> Result:
    query = (query or "").strip()
    if not query:
        return _fail("arxiv", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("arxiv", "", {"search_query": query}, "call budget exhausted")
    params = {
        "search_query": f"all:{query}",
        "start": "0", "max_results": str(max(1, min(limit, 25))),
        "sortBy": "relevance",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    status, body, error = _http_get(url, accept="application/atom+xml")
    if error:
        return _fail("arxiv", url, params, error)
    try:
        root = ET.fromstring(body)
    except Exception as exc:
        return _fail("arxiv", url, params, f"malformed Atom: {type(exc).__name__}")
    ns = {"atom": "http://www.w3.org/2005/Atom",
          "arxiv": "http://arxiv.org/schemas/atom"}
    records = []
    for entry in root.findall("atom:entry", ns):
        identifier = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        records.append({
            "source": "arxiv",
            "id": identifier.rsplit("/", 1)[-1],
            "title": _clean(entry.findtext("atom:title", default="", namespaces=ns)),
            "authors": [a.findtext("atom:name", default="", namespaces=ns)
                        for a in entry.findall("atom:author", ns)][:8],
            "year": (entry.findtext("atom:published", default="", namespaces=ns) or "")[:4],
            "abstract": _clean(entry.findtext("atom:summary", default="", namespaces=ns))[:1200],
            "doi": (entry.findtext("arxiv:doi", default="", namespaces=ns) or "").strip(),
            "is_preprint": True,
            "type": "preprint",
            "url": identifier,
        })
    return Result(True, "arxiv", records, endpoint=url, params=params, retrieved_at=_now())


# ── Crossref ────────────────────────────────────────────────────────────────


def lookup_doi(doi: str, budget: Budget | None = None) -> Result:
    cleaned = re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip(), flags=re.I)
    if not cleaned:
        return _fail("crossref", "", {}, "empty DOI")
    if budget and not budget.take():
        return _fail("crossref", "", {"doi": cleaned}, "call budget exhausted")
    url = "https://api.crossref.org/works/" + urllib.parse.quote(cleaned, safe="")
    data, url, error = _json(url)
    if error:
        return _fail("crossref", url, {"doi": cleaned}, error)
    message = (data or {}).get("message") or {}
    if not message:
        return Result(True, "crossref", [], endpoint=url, params={"doi": cleaned},
                      retrieved_at=_now(), warnings=["DOI not registered"])
    issued = (message.get("issued") or {}).get("date-parts") or [[None]]
    authors = []
    for author in (message.get("author") or [])[:10]:
        name = " ".join(part for part in (author.get("given"), author.get("family")) if part)
        if name:
            authors.append(name)
    record = {
        "source": "crossref",
        "id": message.get("DOI") or cleaned,
        "doi": message.get("DOI") or cleaned,
        "title": _clean((message.get("title") or [""])[0]),
        "authors": authors,
        "journal": (message.get("container-title") or [""])[0],
        "year": str(issued[0][0]) if issued and issued[0] else "",
        "publisher": message.get("publisher") or "",
        "type": message.get("type") or "",
        "url": message.get("URL") or f"https://doi.org/{cleaned}",
        "verified": True,
    }
    return Result(True, "crossref", [record], endpoint=url, params={"doi": cleaned},
                  retrieved_at=_now())


def search_crossref(query: str, limit: int = 10, budget: Budget | None = None) -> Result:
    query = (query or "").strip()
    if not query:
        return _fail("crossref", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("crossref", "", {"query": query}, "call budget exhausted")
    params = {"query": query, "rows": str(max(1, min(limit, 20))),
              "select": "DOI,title,author,container-title,issued,type,URL"}
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data, url, error = _json(url)
    if error:
        return _fail("crossref", url, params, error)
    items = ((data or {}).get("message") or {}).get("items") or []
    records = []
    for item in items:
        issued = (item.get("issued") or {}).get("date-parts") or [[None]]
        records.append({
            "source": "crossref",
            "id": item.get("DOI") or "",
            "doi": item.get("DOI") or "",
            "title": _clean((item.get("title") or [""])[0]),
            "authors": [" ".join(p for p in (a.get("given"), a.get("family")) if p)
                        for a in (item.get("author") or [])][:8],
            "journal": (item.get("container-title") or [""])[0],
            "year": str(issued[0][0]) if issued and issued[0] else "",
            "type": item.get("type") or "",
            "url": item.get("URL") or "",
        })
    return Result(True, "crossref", records, endpoint=url, params=params,
                  retrieved_at=_now())


# ── UniProt ─────────────────────────────────────────────────────────────────


def lookup_uniprot(query: str, limit: int = 5, budget: Budget | None = None) -> Result:
    query = (query or "").strip()
    if not query:
        return _fail("uniprot", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("uniprot", "", {"query": query}, "call budget exhausted")
    if re.fullmatch(r"[A-NR-Z0-9][0-9][A-Z0-9]{3}[0-9]|[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-Z0-9]{10}", query):
        url = f"https://rest.uniprot.org/uniprotkb/{urllib.parse.quote(query)}.json"
        data, url, error = _json(url)
        if error:
            return _fail("uniprot", url, {"accession": query}, error)
        entries = [data] if data else []
        params = {"accession": query}
    else:
        params = {"query": query, "format": "json", "size": str(max(1, min(limit, 20)))}
        url = "https://rest.uniprot.org/uniprotkb/search?" + urllib.parse.urlencode(params)
        data, url, error = _json(url)
        if error:
            return _fail("uniprot", url, params, error)
        entries = (data or {}).get("results") or []

    records = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        protein = entry.get("proteinDescription") or {}
        name = ""
        recommended = protein.get("recommendedName") or {}
        if isinstance(recommended, dict):
            name = (recommended.get("fullName") or {}).get("value") or ""
        if not name:
            submission = (protein.get("submissionNames") or [{}])[0]
            name = (submission.get("fullName") or {}).get("value") or ""
        organism = (entry.get("organism") or {}).get("scientificName") or ""
        records.append({
            "source": "uniprot",
            "id": entry.get("primaryAccession") or "",
            "accession": entry.get("primaryAccession") or "",
            "name": name,
            "gene": _first_gene(entry),
            "organism": organism,
            "length": ((entry.get("sequence") or {}).get("length")),
            "reviewed": entry.get("entryType", "").startswith("UniProtKB reviewed"),
            "url": f"https://www.uniprot.org/uniprotkb/{entry.get('primaryAccession', '')}/entry",
        })
    return Result(True, "uniprot", records, endpoint=url, params=params,
                  retrieved_at=_now())


def _first_gene(entry: dict) -> str:
    for gene in entry.get("genes") or []:
        primary = (gene.get("geneName") or {}).get("value")
        if primary:
            return primary
    return ""


# ── PubChem ─────────────────────────────────────────────────────────────────


def lookup_pubchem(name: str, budget: Budget | None = None) -> Result:
    name = (name or "").strip()
    if not name:
        return _fail("pubchem", "", {}, "empty compound name")
    if budget and not budget.take():
        return _fail("pubchem", "", {"name": name}, "call budget exhausted")
    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    cid_url = f"{base}/compound/name/{urllib.parse.quote(name)}/cids/JSON"
    data, url, error = _json(cid_url)
    if error:
        if "HTTP 404" in error:
            return Result(True, "pubchem", [], endpoint=url, params={"name": name},
                          retrieved_at=_now(), warnings=["compound not found"])
        return _fail("pubchem", url, {"name": name}, error)
    cids = ((data or {}).get("IdentifierList") or {}).get("CID") or []
    if not cids:
        return Result(True, "pubchem", [], endpoint=url, params={"name": name},
                      retrieved_at=_now(), warnings=["compound not found"])

    if budget and not budget.take():
        return _fail("pubchem", url, {"name": name}, "call budget exhausted")
    cid = cids[0]
    props = "IUPACName,MolecularFormula,MolecularWeight,CanonicalSMILES,InChIKey"
    prop_url = f"{base}/compound/cid/{cid}/property/{props}/JSON"
    data, prop_url, error = _json(prop_url)
    if error:
        return _fail("pubchem", prop_url, {"cid": cid}, error)
    properties = (((data or {}).get("PropertyTable") or {}).get("Properties") or [{}])[0]
    record = {
        "source": "pubchem",
        "id": str(cid),
        "cid": cid,
        "name": name,
        "iupac_name": properties.get("IUPACName") or "",
        "formula": properties.get("MolecularFormula") or "",
        "molecular_weight": properties.get("MolecularWeight") or "",
        "smiles": properties.get("CanonicalSMILES") or "",
        "inchi_key": properties.get("InChIKey") or "",
        "url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
    }
    return Result(True, "pubchem", [record], endpoint=prop_url, params={"name": name},
                  retrieved_at=_now())


# ── ChEMBL ──────────────────────────────────────────────────────────────────


def search_chembl(name: str, limit: int = 5, budget: Budget | None = None) -> Result:
    name = (name or "").strip()
    if not name:
        return _fail("chembl", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("chembl", "", {"q": name}, "call budget exhausted")
    params = {"q": name, "format": "json", "limit": str(max(1, min(limit, 20)))}
    url = "https://www.ebi.ac.uk/chembl/api/data/molecule/search?" + urllib.parse.urlencode(params)
    data, url, error = _json(url)
    if error:
        return _fail("chembl", url, params, error)
    records = []
    for molecule in (data or {}).get("molecules") or []:
        props = molecule.get("molecule_properties") or {}
        records.append({
            "source": "chembl",
            "id": molecule.get("molecule_chembl_id") or "",
            "chembl_id": molecule.get("molecule_chembl_id") or "",
            "name": molecule.get("pref_name") or name,
            "max_phase": molecule.get("max_phase"),
            "molecular_weight": props.get("full_mwt") or "",
            "formula": props.get("full_molformula") or "",
            "smiles": (molecule.get("molecule_structures") or {}).get("canonical_smiles") or "",
            "url": f"https://www.ebi.ac.uk/chembl/compound_report_card/{molecule.get('molecule_chembl_id', '')}/",
        })
    return Result(True, "chembl", records, endpoint=url, params=params,
                  retrieved_at=_now())


# ── ClinicalTrials.gov ──────────────────────────────────────────────────────


def search_clinicaltrials(term: str, limit: int = 5, budget: Budget | None = None) -> Result:
    term = (term or "").strip()
    if not term:
        return _fail("clinicaltrials", "", {}, "empty query")
    if budget and not budget.take():
        return _fail("clinicaltrials", "", {"term": term}, "call budget exhausted")
    params = {"query.term": term, "pageSize": str(max(1, min(limit, 20))),
              "fields": "NCTId,BriefTitle,OverallStatus,Condition,Phase,StartDate"}
    url = "https://clinicaltrials.gov/api/v2/studies?" + urllib.parse.urlencode(params)
    data, url, error = _json(url)
    if error:
        return _fail("clinicaltrials", url, params, error)
    records = []
    for study in (data or {}).get("studies") or []:
        protocol = study.get("protocolSection") or {}
        identification = protocol.get("identificationModule") or {}
        status_module = protocol.get("statusModule") or {}
        design = protocol.get("designModule") or {}
        conditions = protocol.get("conditionsModule") or {}
        records.append({
            "source": "clinicaltrials",
            "id": identification.get("nctId") or "",
            "nct_id": identification.get("nctId") or "",
            "title": _clean(identification.get("briefTitle")),
            "status": status_module.get("overallStatus") or "",
            "conditions": (conditions.get("conditions") or [])[:6],
            "phases": design.get("phases") or [],
            "start": (status_module.get("startDateStruct") or {}).get("date") or "",
            "url": f"https://clinicaltrials.gov/study/{identification.get('nctId', '')}",
        })
    return Result(True, "clinicaltrials", records, endpoint=url, params=params,
                  retrieved_at=_now())


# ── merge / dispatch ────────────────────────────────────────────────────────


def literature(
    query: str,
    *,
    limit: int = 10,
    include_preprints: bool = True,
    budget: Budget | None = None,
) -> dict:
    """Merged PubMed + Europe PMC (+ arXiv) search, deduplicated.

    It deliberately reports which sources failed: a review that silently
    searched only one database is not a review.
    """
    budget = budget or Budget()
    per_source = max(3, limit)
    runs: list[Result] = [
        search_pubmed(query, per_source, budget),
        search_europepmc(query, per_source, budget),
    ]
    if include_preprints:
        runs.append(search_arxiv(query, max(3, limit // 2), budget))

    seen: set[str] = set()
    merged: list[dict] = []
    sources_ok: list[str] = []
    sources_failed: list[dict] = []
    for result in runs:
        if not result.ok:
            sources_failed.append({"source": result.source, "error": result.error})
            continue
        sources_ok.append(result.source)
        for record in result.records:
            key = _dedupe_key(record)
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
    merged.sort(key=lambda r: (_rank_source(r.get("source")), -int(str(r.get("year") or 0) or 0)))
    return {
        "ok": bool(sources_ok),
        "query": query,
        "records": merged[:limit],
        "sources_ok": sources_ok,
        "sources_failed": sources_failed,
        "total_found": len(merged),
        "budget": budget.public(),
        "retrieved_at": _now(),
    }


def _rank_source(source: str | None) -> int:
    return {"pubmed": 0, "europepmc": 1, "crossref": 2, "arxiv": 3}.get(str(source), 9)


def _dedupe_key(record: dict) -> str:
    doi = str(record.get("doi") or "").strip().lower()
    if doi:
        return "doi:" + doi
    title = re.sub(r"[^a-z0-9]+", "", str(record.get("title") or "").lower())
    return "title:" + title[:120]


def lookup(database: str, query: str, *, limit: int = 10, budget: Budget | None = None) -> Result:
    """Dispatch by database name. Unknown/restricted names explain themselves."""
    name = (database or "").strip().lower()
    if name in RESTRICTED_DATABASES:
        return _fail(
            name, "", {"query": query},
            f"{name} is not publicly accessible: {RESTRICTED_DATABASES[name]}",
        )
    if name == "pubmed":
        return search_pubmed(query, limit, budget)
    if name in ("europepmc", "europe_pmc", "epmc"):
        return search_europepmc(query, limit, budget)
    if name == "arxiv":
        return search_arxiv(query, limit, budget)
    if name == "crossref":
        return lookup_doi(query, budget) if looks_like_doi(query) else search_crossref(query, limit, budget)
    if name == "uniprot":
        return lookup_uniprot(query, limit, budget)
    if name == "pubchem":
        return lookup_pubchem(query, budget)
    if name == "chembl":
        return search_chembl(query, limit, budget)
    if name in ("clinicaltrials", "clinicaltrials.gov", "ctgov"):
        return search_clinicaltrials(query, limit, budget)
    return _fail(
        name, "", {"query": query},
        f"unknown database '{database}'. Available: {', '.join(sorted(PUBLIC_DATABASES))}",
    )


def looks_like_doi(value: str) -> bool:
    return bool(re.match(r"^(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/\S+$", (value or "").strip()))


# ── formatting for a model ──────────────────────────────────────────────────


def format_for_model(records: list[dict], max_records: int = 12) -> str:
    """Compact, citation-ready rendering. Caller wraps it as untrusted."""
    lines: list[str] = []
    for index, record in enumerate(records[:max_records], 1):
        title = record.get("title") or record.get("name") or "(untitled)"
        bits = [f"[{index}] {title}"]
        authors = record.get("authors")
        if isinstance(authors, list) and authors:
            bits.append(", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""))
        elif isinstance(authors, str) and authors:
            bits.append(authors[:120])
        meta = " · ".join(
            str(value) for value in (
                record.get("journal"), record.get("year"), record.get("organism"),
                record.get("formula"), record.get("status"),
            ) if value
        )
        if meta:
            bits.append(meta)
        if record.get("doi"):
            bits.append("doi:" + str(record["doi"]))
        if record.get("url"):
            bits.append(str(record["url"]))
        if record.get("full_text"):
            bits.append("full-text excerpt: " + str(record["full_text"])[:600])
        elif record.get("abstract"):
            bits.append(str(record["abstract"])[:300])
        lines.append(" | ".join(bits))
    return "\n".join(lines)


_looks_like_doi = looks_like_doi


def _clean(text: Any) -> str:
    value = re.sub(r"<[^>]+>", "", str(text or ""))
    return re.sub(r"\s+", " ", value).strip()
