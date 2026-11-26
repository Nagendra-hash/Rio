"""Offline tests for the scientific database clients (HTTP is stubbed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.scientific import databases  # noqa: E402

PUBMED_SEARCH = {"esearchresult": {"count": "5", "idlist": ["111", "222"]}}
PUBMED_SUMMARY = {
    "result": {
        "111": {
            "title": "CRISPR base editing in human cells",
            "authors": [{"name": "Doe J"}, {"name": "Roe R"}],
            "fulljournalname": "Nature",
            "pubdate": "2024 Mar",
            "articleids": [{"idtype": "doi", "value": "10.1000/abc"}],
        },
        "222": {
            "title": "Off-target effects of base editors",
            "authors": [{"name": "Smith A"}],
            "fulljournalname": "Cell",
            "pubdate": "2023",
            "articleids": [],
        },
    }
}

EUROPEPMC = {
    "hitCount": 42,
    "resultList": {"result": [{
        "id": "PPR123", "source": "PPR", "doi": "10.1101/xyz",
        "title": "A preprint on base editing", "authorString": "Lee K, Park S",
        "pubYear": "2025", "abstractText": "<p>We show something.</p>",
    }]},
}

ARXIV_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2501.00001v1</id>
    <published>2025-01-01T00:00:00Z</published>
    <title>Deep learning for protein folding</title>
    <summary>We predict structures.</summary>
    <author><name>Ada L</name></author>
    <author><name>Grace H</name></author>
  </entry>
</feed>
"""

CROSSREF_DOI = {"message": {
    "DOI": "10.1038/nature12373", "title": ["Nanometre-scale thermometry"],
    "author": [{"given": "G.", "family": "Kucsko"}],
    "container-title": ["Nature"], "issued": {"date-parts": [[2013]]},
    "publisher": "Springer", "type": "journal-article",
    "URL": "https://doi.org/10.1038/nature12373",
}}

UNIPROT_ENTRY = {
    "primaryAccession": "P04637",
    "entryType": "UniProtKB reviewed (Swiss-Prot)",
    "proteinDescription": {"recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}},
    "organism": {"scientificName": "Homo sapiens"},
    "genes": [{"geneName": {"value": "TP53"}}],
    "sequence": {"length": 393},
}

PUBMED_CHEMBL = {"molecules": [{
    "molecule_chembl_id": "CHEMBL25", "pref_name": "ASPIRIN", "max_phase": 4,
    "molecule_properties": {"full_mwt": "180.16", "full_molformula": "C9H8O4"},
    "molecule_structures": {"canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O"},
}]}

CT_GOV = {"studies": [{
    "protocolSection": {
        "identificationModule": {"nctId": "NCT00000001", "briefTitle": "Metformin trial"},
        "statusModule": {"overallStatus": "RECRUITING",
                         "startDateStruct": {"date": "2024-01"}},
        "designModule": {"phases": ["PHASE2"]},
        "conditionsModule": {"conditions": ["Type 2 Diabetes"]},
    }
}]}


def _fake_http(url: str, **_kwargs):
    if "esearch.fcgi" in url:
        return 200, json.dumps(PUBMED_SEARCH), ""
    if "esummary.fcgi" in url:
        return 200, json.dumps(PUBMED_SUMMARY), ""
    if "europepmc" in url:
        return 200, json.dumps(EUROPEPMC), ""
    if "export.arxiv.org" in url:
        return 200, ARXIV_XML, ""
    if "api.crossref.org/works/" in url:
        return 200, json.dumps(CROSSREF_DOI), ""
    if "rest.uniprot.org/uniprotkb/P04637" in url:
        return 200, json.dumps(UNIPROT_ENTRY), ""
    if "rest.uniprot.org/uniprotkb/search" in url:
        return 200, json.dumps({"results": [UNIPROT_ENTRY]}), ""
    if "/compound/name/" in url:
        return 200, json.dumps({"IdentifierList": {"CID": [2244]}}), ""
    if "/property/" in url:
        return 200, json.dumps({"PropertyTable": {"Properties": [{
            "CID": 2244, "IUPACName": "2-acetoxybenzoic acid",
            "MolecularFormula": "C9H8O4", "MolecularWeight": "180.16",
            "CanonicalSMILES": "CC(=O)Oc1ccccc1C(=O)O", "InChIKey": "BSYNRYMUTXBXSQ",
        }]}}), ""
    if "chembl" in url:
        return 200, json.dumps(PUBMED_CHEMBL), ""
    if "clinicaltrials.gov" in url:
        return 200, json.dumps(CT_GOV), ""
    return 404, "", "HTTP 404"


def test_pubmed_parsing(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    result = databases.search_pubmed("base editing", limit=2)
    assert result.ok and len(result.records) == 2
    assert result.records[0]["doi"] == "10.1000/abc"
    assert result.records[0]["authors"] == ["Doe J", "Roe R"]
    assert result.records[0]["url"].endswith("/111/")
    # count reconciliation: 5 matches, 2 retrieved
    assert result.warnings and "5" in result.warnings[0]


def test_europepmc_marks_preprints(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    result = databases.search_europepmc("base editing")
    record = result.records[0]
    assert record["is_preprint"] is True and record["type"] == "preprint"
    assert "<p>" not in record["abstract"]


def test_arxiv_atom_parsing(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    result = databases.search_arxiv("protein folding")
    assert result.ok and result.records[0]["authors"] == ["Ada L", "Grace H"]
    assert result.records[0]["id"] == "2501.00001v1"


def test_crossref_doi_verification(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    result = databases.lookup_doi("https://doi.org/10.1038/nature12373")
    assert result.ok and result.records[0]["verified"] is True
    assert result.records[0]["year"] == "2013"


def test_uniprot_accession_and_search(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    by_accession = databases.lookup_uniprot("P04637")
    assert by_accession.records[0]["gene"] == "TP53"
    assert by_accession.records[0]["length"] == 393
    by_name = databases.lookup_uniprot("p53")
    assert by_name.ok and by_name.records[0]["accession"] == "P04637"


def test_pubchem_two_step_lookup(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    result = databases.lookup_pubchem("aspirin")
    assert result.ok and result.records[0]["cid"] == 2244
    assert result.records[0]["formula"] == "C9H8O4"


def test_chembl_and_clinicaltrials(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    chembl = databases.search_chembl("aspirin")
    assert chembl.records[0]["chembl_id"] == "CHEMBL25"
    trials = databases.search_clinicaltrials("metformin")
    assert trials.records[0]["nct_id"] == "NCT00000001"
    assert trials.records[0]["status"] == "RECRUITING"


def test_literature_merges_and_deduplicates(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    merged = databases.literature("base editing", limit=10)
    assert merged["ok"]
    assert set(merged["sources_ok"]) == {"pubmed", "europepmc", "arxiv"}
    keys = [databases._dedupe_key(r) for r in merged["records"]]
    assert len(keys) == len(set(keys))
    assert merged["total_found"] >= 3
    assert merged["retrieved_at"].endswith("Z")


def test_literature_reports_failed_sources(monkeypatch):
    def only_pubmed(url, **_kwargs):
        if "esearch.fcgi" in url:
            return 200, json.dumps(PUBMED_SEARCH), ""
        if "esummary.fcgi" in url:
            return 200, json.dumps(PUBMED_SUMMARY), ""
        return 0, "", "connection refused"

    monkeypatch.setattr(databases, "_http_get", only_pubmed)
    merged = databases.literature("base editing", limit=5)
    assert merged["sources_ok"] == ["pubmed"]
    assert len(merged["sources_failed"]) == 2


def test_budget_stops_calls(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", _fake_http)
    # One PubMed search is two calls: esearch, then esummary.
    budget = databases.Budget(max_calls=2)
    first = databases.search_pubmed("base editing", budget=budget)
    assert first.ok
    assert budget.calls == 2
    second = databases.search_pubmed("another query", budget=budget)
    assert not second.ok and "budget" in second.error


def test_restricted_and_unknown_databases_explain_themselves():
    cosmic = databases.lookup("cosmic", "BRAF")
    assert not cosmic.ok
    assert "licensed" in cosmic.error and "cBioPortal" in cosmic.error
    unknown = databases.lookup("not-a-db", "x")
    assert not unknown.ok and "unknown database" in unknown.error


def test_http_error_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(databases, "_http_get", lambda url, **k: (500, "", "HTTP 500"))
    result = databases.search_pubmed("x")
    assert not result.ok and "500" in result.error


def test_format_for_model_prefers_full_text():
    text = databases.format_for_model([
        {"title": "T", "authors": ["A B"], "year": "2024", "doi": "10.1/x",
         "url": "https://x", "abstract": "short abstract",
         "full_text": "the actual body text " * 5},
    ])
    assert "full-text excerpt" in text and "short abstract" not in text
    assert "doi:10.1/x" in text and "[1]" in text


def test_looks_like_doi():
    assert databases.looks_like_doi("10.1038/nature12373")
    assert databases.looks_like_doi("https://doi.org/10.1038/nature12373")
    assert not databases.looks_like_doi("TP53")
    assert not databases.looks_like_doi("10.1038")
