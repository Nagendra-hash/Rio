"""
core.scientific — the RIO Scientific Research Agent's library layer.

    frontmatter   stdlib parser for SKILL.md metadata
    config        paths, budgets, depth presets, provenance
    registry      enable/disable + provisioning state (outside the vendor tree)
    skill_index   metadata index + deterministic discovery
    skill_loader  lazy SKILL.md / references loading, budgeted and wrapped
    envs          interpreter resolution + dependency compatibility
    databases     PubMed/Europe PMC/arXiv/Crossref/UniProt/PubChem/ChEMBL/CT.gov
    analysis      vetted, reproducible analysis recipes
    runner        bounded subprocess execution with rlimits + cancellation
    report        evidence assembly, references, gaps/conflicts
    safety        clinical/financial/research-integrity guardrails
    delivery      Telegram text + report attachment
"""

from __future__ import annotations

__all__ = [
    "analysis",
    "config",
    "databases",
    "delivery",
    "envs",
    "frontmatter",
    "registry",
    "report",
    "runner",
    "safety",
    "skill_index",
    "skill_loader",
]
