"""
core/scientific/skill_index.py — metadata index and discovery over the
vendored K-Dense skill library.

DESIGN
    The index is METADATA ONLY. Building it parses each `SKILL.md` frontmatter
    (177 files, ~30 ms) and never reads a skill body or a reference into
    memory. A `SKILL.md` body is loaded by `skill_loader` only after discovery
    has chosen the skill — that is the lazy-loading requirement, and it is why
    carrying all 177 skills costs nothing at runtime.

    The vendored tree is read-only. Enable/disable state, notes and
    provisioning status live in `memory/scientific/registry.json`, so a skill's
    upstream hash never changes because we toggled it.

DISCOVERY
    Deterministic scoring over name, tags and description, with a small
    synonym map for the scientific vocabulary the brief uses ("literature
    review", "papers", "statistics", "plot", "ML", "gaps"). A model is only
    consulted for a tie-break, never required — a small local model or no model
    at all still yields a usable skill plan.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.scientific import frontmatter
from core.scientific.config import (
    CORE_SKILLS,
    GUARDED_SKILLS,
    INDEX_PATH,
    SKILLS_ROOT,
    STATE_DIR,
    UPSTREAM_COMMIT,
    UPSTREAM_TAG,
    load_config,
)
from core.scientific.registry import enabled_names, is_enabled

INDEX_VERSION = 1


# ── skill record ────────────────────────────────────────────────────────────


@dataclass
class ScienceSkill:
    name: str
    description: str
    version: str = ""
    last_reviewed: str = ""
    author: str = ""
    license: str = ""
    compatibility: str = ""
    allowed_tools: tuple[str, ...] = ()
    required_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    requires_verified: bool = False
    tags: tuple[str, ...] = ()
    env_profile: str = "core"
    path: str = ""
    sha256: str = ""
    size_bytes: int = 0
    references: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    core: bool = False
    enabled: bool = True
    guarded_reason: str = ""
    score: float = 0.0

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("allowed_tools", "required_env", "optional_env", "requires",
                    "tags", "references", "scripts"):
            data[key] = list(getattr(self, key))
        data["score"] = round(self.score, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScienceSkill":
        fields = cls.__dataclass_fields__
        kwargs = {}
        for key, value in data.items():
            if key not in fields:
                continue
            if key in ("allowed_tools", "required_env", "optional_env", "requires",
                       "tags", "references", "scripts"):
                kwargs[key] = tuple(value or ())
            else:
                kwargs[key] = value
        return cls(**kwargs)


@dataclass
class ScienceIndex:
    skills: dict[str, ScienceSkill] = field(default_factory=dict)
    built_at: float = 0.0
    source_tag: str = UPSTREAM_TAG
    source_commit: str = UPSTREAM_COMMIT
    source_root: str = str(SKILLS_ROOT)

    def get(self, name: str) -> ScienceSkill | None:
        return self.skills.get(name)

    def __len__(self) -> int:
        return len(self.skills)

    def enabled(self) -> list[ScienceSkill]:
        return [s for s in self.skills.values() if s.enabled]

    def summary(self) -> dict[str, Any]:
        profiles: dict[str, int] = {}
        for skill in self.skills.values():
            profiles[skill.env_profile] = profiles.get(skill.env_profile, 0) + 1
        return {
            "skills": len(self.skills),
            "enabled": len(self.enabled()),
            "core": len([s for s in self.skills.values() if s.core]),
            "guarded": len(GUARDED_SKILLS),
            "env_profiles": profiles,
            "built_at": self.built_at,
            "source_tag": self.source_tag,
            "source_commit": self.source_commit,
        }


# ── hashing / metadata helpers ──────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    """sha256 over sorted relative paths + bytes (the repo's existing scheme)."""
    digest = hashlib.sha256()
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file())
    except OSError:
        return ""
    for path in files:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        digest.update(rel.encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def _dir_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[\s,]+", value.strip()) if v]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


# ── dependency + profile derivation ─────────────────────────────────────────

# Canonical package -> the import name plus aliases seen in prose.
_KNOWN_PACKAGES: dict[str, tuple[str, ...]] = {
    "numpy": ("numpy",),
    "pandas": ("pandas",),
    "scipy": ("scipy",),
    "scikit-learn": ("scikit-learn", "sklearn"),
    "matplotlib": ("matplotlib",),
    "seaborn": ("seaborn",),
    "statsmodels": ("statsmodels",),
    "pingouin": ("pingouin",),
    "pymc": ("pymc",),
    "arviz": ("arviz",),
    "plotly": ("plotly",),
    "biopython": ("biopython",),
    "rdkit": ("rdkit",),
    "torch": ("torch", "pytorch"),
    "transformers": ("transformers",),
    "umap-learn": ("umap-learn", "umap"),
    "shap": ("shap",),
    "scanpy": ("scanpy",),
    "anndata": ("anndata",),
    "pysam": ("pysam",),
    "openpyxl": ("openpyxl",),
    "requests": ("requests",),
    "pyzotero": ("pyzotero",),
    "astropy": ("astropy",),
    "networkx": ("networkx",),
    "sympy": ("sympy",),
    "geopandas": ("geopandas",),
    "polars": ("polars",),
    "pydicom": ("pydicom",),
    "nibabel": ("nibabel",),
    "cantera": ("cantera",),
    "qiskit": ("qiskit",),
    "cirq": ("cirq",),
    "pennylane": ("pennylane",),
    "pymatgen": ("pymatgen",),
    "pybamm": ("pybamm",),
    "cobrapy": ("cobrapy", "cobra"),
    "bioservices": ("bioservices",),
    "gget": ("gget",),
}

_STATS_PACKAGES = {"pingouin", "statsmodels", "pymc", "arviz", "scipy", "plotly"}
_CHEM_BIO_PACKAGES = {"rdkit", "biopython", "scanpy", "anndata", "pysam", "pydicom",
                      "nibabel", "cobrapy", "bioservices", "gget"}
_VIZ_PACKAGES = {"matplotlib", "seaborn"}
_PIN_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9._-]{1,40})\s*==\s*([0-9][0-9A-Za-z.+-]*)")
_DEP_SECTION_RE = re.compile(
    r"(?ims)^#{2,4}\s*(?:installation|dependencies|requirements|setup)\s*$(.*?)(?=^#{1,3}\s|\Z)"
)


def derive_requires(meta: dict[str, Any], body: str) -> tuple[list[str], bool]:
    """Best-effort package requirements from prose + explicit pins.

    Upstream declares dependencies in `compatibility:` prose and in
    Installation/Dependencies sections — there is no structured field. We
    extract pinned `name==version` requirements (authoritative, `verified=True`)
    and otherwise record known package names mentioned there (unverified).
    """
    prose_parts = [str(meta.get("compatibility") or "")]
    for match in _DEP_SECTION_RE.finditer(body or ""):
        prose_parts.append(match.group(1)[:4000])
    prose = "\n".join(prose_parts)

    pinned: dict[str, str] = {}
    for match in _PIN_RE.finditer(prose):
        raw, version = match.group(1), match.group(2)
        canonical = _canonical_package(raw)
        if canonical:
            pinned.setdefault(canonical, version)

    mentioned: list[str] = []
    lowered = prose.lower()
    for canonical, aliases in _KNOWN_PACKAGES.items():
        if canonical in pinned:
            continue
        if any(re.search(rf"(?<![a-z0-9-]){re.escape(alias)}(?![a-z0-9-])", lowered) for alias in aliases):
            mentioned.append(canonical)

    requires = [f"{name}=={version}" for name, version in sorted(pinned.items())]
    requires.extend(mentioned)
    return requires, bool(pinned)


def _canonical_package(raw: str) -> str:
    lowered = raw.lower().strip("`'\" ")
    for canonical, aliases in _KNOWN_PACKAGES.items():
        if lowered in aliases:
            return canonical
    return ""


def env_profile_for(requires: Iterable[str]) -> str:
    names = {req.split("==")[0].strip().lower() for req in requires}
    if names & _CHEM_BIO_PACKAGES:
        return "chem"
    if names & _STATS_PACKAGES:
        return "stats"
    if names & _VIZ_PACKAGES:
        return "viz"
    return "core"


# ── tag inference ───────────────────────────────────────────────────────────

_TAG_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("literature", re.compile(r"\b(literature review|systematic review|scoping review|prisma|pubmed|semantic scholar|biorxiv)\b", re.I)),
    ("papers", re.compile(r"\b(papers?|preprints?|citation|doi|crossref|abstracts?)\b", re.I)),
    ("databases", re.compile(r"\b(database|api|pubchem|chembl|uniprot|clinicaltrials|ensembl|ncbi|cosmic)\b", re.I)),
    ("statistics", re.compile(r"\b(statistic|t-test|anova|regression|bayesian|effect size|p-value|power analysis)\b", re.I)),
    ("data-analysis", re.compile(r"\b(data analysis|exploratory|pandas|csv|dataframe|eda)\b", re.I)),
    ("machine-learning", re.compile(r"\b(machine learning|scikit|classifier|regression model|deep learning|neural)\b", re.I)),
    ("visualization", re.compile(r"\b(plot|figure|chart|visuali[sz]|matplotlib|seaborn|heatmap)\b", re.I)),
    ("writing", re.compile(r"\b(writing|manuscript|report|proposal|grant|abstract|paper draft)\b", re.I)),
    ("citations", re.compile(r"\b(citation|bibliograph|bibtex|csl|reference style|zotero)\b", re.I)),
    ("hypothesis", re.compile(r"\b(hypothes\w*|brainstorm\w*|research questions?|gaps?)\b", re.I)),
    ("methodology", re.compile(r"\b(experimental design|methodolog|critical thinking|peer review|power|randomi[sz])\b", re.I)),
    ("chemistry", re.compile(r"\b(molecul|cheminform|smiles|inchi|reaction|docking|ligand)\b", re.I)),
    ("biology", re.compile(r"\b(genom|proteom|sequence|gene|protein|cell|rna|dna)\b", re.I)),
)


def infer_tags(name: str, description: str) -> list[str]:
    text = f"{name} {description}"
    return [tag for tag, pattern in _TAG_RULES if pattern.search(text)]


# ── build / load ────────────────────────────────────────────────────────────


def _skill_dirs(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def _find_skill_md(directory: Path) -> Path | None:
    direct = directory / "SKILL.md"
    if direct.exists():
        return direct
    candidates = sorted(directory.rglob("SKILL.md"), key=lambda p: (len(p.parts), str(p)))
    return candidates[0] if candidates else None


def build_skill(skill_dir: Path) -> ScienceSkill | None:
    md = _find_skill_md(skill_dir)
    if md is None:
        return None
    meta, body = frontmatter.load(md)
    name = str(meta.get("name") or skill_dir.name).strip()
    description = str(meta.get("description") or "").strip()
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    openclaw = metadata.get("openclaw") if isinstance(metadata.get("openclaw"), dict) else {}

    required_env: list[str] = []
    optional_env: list[str] = []
    for entry in openclaw.get("envVars") or []:
        if not isinstance(entry, dict):
            continue
        var = str(entry.get("name") or "").strip()
        if not var:
            continue
        (required_env if entry.get("required") else optional_env).append(var)

    requires, verified = derive_requires(meta, body)
    references, scripts = _inventory(skill_dir, md.parent)

    return ScienceSkill(
        name=name,
        description=description,
        version=str(metadata.get("version") or ""),
        last_reviewed=str(metadata.get("last-reviewed") or metadata.get("last_reviewed") or ""),
        author=str(metadata.get("skill-author") or metadata.get("skill_author") or ""),
        license=str(meta.get("license") or ""),
        compatibility=str(meta.get("compatibility") or ""),
        allowed_tools=tuple(_as_list(meta.get("allowed-tools"))),
        required_env=tuple(sorted(set(required_env))),
        optional_env=tuple(sorted(set(optional_env))),
        requires=tuple(requires),
        requires_verified=verified,
        tags=tuple(infer_tags(name, description)),
        env_profile=env_profile_for(requires),
        path=str(skill_dir),
        sha256=sha256_tree(skill_dir),
        size_bytes=_dir_size(skill_dir),
        references=tuple(references),
        scripts=tuple(scripts),
        core=skill_dir.name in CORE_SKILLS or name in CORE_SKILLS,
        guarded_reason=GUARDED_SKILLS.get(skill_dir.name, GUARDED_SKILLS.get(name, "")),
    )


def _inventory(skill_dir: Path, base: Path) -> tuple[list[str], list[str]]:
    references: list[str] = []
    scripts: list[str] = []
    for sub, bucket in (("references", references), ("scripts", scripts), ("assets", references)):
        folder = skill_dir / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                try:
                    bucket.append(path.relative_to(base).as_posix())
                except ValueError:
                    bucket.append(path.name)
    return references[:80], scripts[:40]


def build_index(root: Path | None = None, force: bool = False) -> ScienceIndex:
    """Parse every vendored skill into a metadata index (bodies untouched)."""
    cfg = load_config()
    root = Path(root or cfg.skills_root)
    index = ScienceIndex(source_root=str(root))
    for directory in _skill_dirs(root):
        skill = build_skill(directory)
        if skill is None:
            continue
        skill.enabled = is_enabled(skill.name)
        index.skills[skill.name] = skill
    index.built_at = time.time()
    return index


def save_index(index: ScienceIndex, path: Path | None = None) -> Path:
    target = Path(path or load_config().index_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "built_at": index.built_at,
        "source": {
            "tag": index.source_tag,
            "commit": index.source_commit,
            "root": index.source_root,
            "count": len(index.skills),
        },
        "skills": {name: skill.public() for name, skill in sorted(index.skills.items())},
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def load_index(path: Path | None = None) -> ScienceIndex | None:
    target = Path(path or load_config().index_path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
        return None
    source = raw.get("source") or {}
    if source.get("commit") != UPSTREAM_COMMIT:
        return None
    skills: dict[str, ScienceSkill] = {}
    for name, data in (raw.get("skills") or {}).items():
        try:
            skills[name] = ScienceSkill.from_dict(data)
        except Exception:
            continue
    return ScienceIndex(
        skills=skills,
        built_at=float(raw.get("built_at") or 0.0),
        source_tag=str(source.get("tag") or UPSTREAM_TAG),
        source_commit=str(source.get("commit") or UPSTREAM_COMMIT),
        source_root=str(source.get("root") or SKILLS_ROOT),
    )


_INDEX_CACHE: ScienceIndex | None = None


def get_index(refresh: bool = False) -> ScienceIndex:
    """Process-wide index: disk cache first, then a full build."""
    global _INDEX_CACHE
    if _INDEX_CACHE is not None and not refresh:
        return _INDEX_CACHE
    if not refresh:
        cached = load_index()
        expected = len(_skill_dirs(load_config().skills_root))
        if cached is not None and len(cached.skills) == expected and expected:
            for name in enabled_names():
                if name in cached.skills:
                    cached.skills[name].enabled = True
            _INDEX_CACHE = cached
            return cached
    index = build_index()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        save_index(index)
    except Exception:
        pass
    _INDEX_CACHE = index
    return index


def reset_index_cache() -> None:
    global _INDEX_CACHE
    _INDEX_CACHE = None


# ── discovery ───────────────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    """a an the of for with and or to in on at by from my me we our i you your
    is are was were be been being do does did doing have has had this that these
    those it its as into about over under between across using use used
    please can could would should will shall may might must what which who whom
    whose when where why how explain tell give find show""".split()
)

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "review": ("literature", "systematic"),
    "paper": ("literature", "papers", "citation"),
    "papers": ("literature", "papers", "citation"),
    "article": ("papers", "literature"),
    "study": ("experimental", "methodology"),
    "stats": ("statistics", "statistical"),
    "statistical": ("statistics",),
    "plot": ("visualization", "matplotlib"),
    "plots": ("visualization", "matplotlib"),
    "chart": ("visualization", "matplotlib"),
    "figure": ("visualization",),
    "ml": ("machine-learning",),
    "model": ("machine-learning", "statistics"),
    "classifier": ("machine-learning",),
    "classify": ("machine-learning",),
    "classification": ("machine-learning",),
    "train": ("machine-learning",),
    "predict": ("machine-learning",),
    "publication": ("visualization", "writing"),
    "publication-quality": ("visualization",),
    "proposal": ("writing",),
    "grant": ("writing",),
    "manuscript": ("writing",),
    "figures": ("visualization",),
    "visualize": ("visualization",),
    "hypotheses": ("hypothesis",),
    "citation": ("citations",),
    "citations": ("citations",),
    "reference": ("citations",),
    "db": ("databases",),
    "database": ("databases",),
    "pubmed": ("databases", "literature"),
    "uniprot": ("databases", "biology"),
    "pubchem": ("databases", "chemistry"),
    "chembl": ("databases", "chemistry"),
    "clinicaltrials": ("databases", "clinical"),
    "clinical": ("databases",),
    "trial": ("databases",),
    "trials": ("databases",),
    "hypothesis": ("hypothesis",),
    "gap": ("hypothesis",),
    "gaps": ("hypothesis",),
    "csv": ("data-analysis",),
    "dataset": ("data-analysis",),
    "eda": ("data-analysis",),
}


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9][a-z0-9+-]{1,}", (text or "").lower())
    out: list[str] = []
    for token in raw:
        if token in _STOPWORDS:
            continue
        out.append(token)
        out.extend(_SYNONYMS.get(token, ()))
    return out


# Domain vocabulary is unambiguous in a way bag-of-words is not: a query that
# names UniProt wants database-lookup, not whichever skill's prose happens to
# contain the most words. These hints are applied on top of token scoring and
# seed the multi-skill plan, so the obvious specialist always makes the cut.
_INTENT_HINTS: tuple[tuple[re.Pattern, tuple[str, ...]], ...] = (
    (re.compile(r"\b(uniprot|pubchem|chembl|pubmed|clinicaltrials|ensembl|cosmic|"
                r"ncbi|reactome|kegg|pdb|alphafold|gwas|clinvar|clinical\s+trials?)\b", re.I),
     ("database-lookup",)),
    (re.compile(r"\b(systematic|scoping|literature)\s+review\b|\bprisma\b", re.I),
     ("literature-review", "paper-lookup", "citation-management")),
    (re.compile(r"\b(citation|bibliograph|bibtex|csl|reference style|doi)\b", re.I),
     ("citation-management",)),
    (re.compile(r"\b(hypothes|research gaps?|gaps? in)\b", re.I),
     ("hypothesis-generation",)),
    (re.compile(r"\b(t-test|t test|anova|regression|statistical|p-value|effect size|"
                r"power analysis)\b", re.I),
     ("statistical-analysis",)),
    (re.compile(r"\b(csv|excel|dataframe|exploratory data|\beda\b|dataset)\b", re.I),
     ("exploratory-data-analysis",)),
    (re.compile(r"\b(classifier|classification|machine learning|sklearn|scikit|"
                r"train a model|predictive model)\b", re.I),
     ("scikit-learn",)),
    (re.compile(r"\b(figures?|plots?|charts?|visuali[sz])\b", re.I),
     ("scientific-visualization", "matplotlib")),
    (re.compile(r"\b(proposal|grant|manuscript|paper draft)\b", re.I),
     ("scientific-writing",)),
    (re.compile(r"\b(experimental design|randomi[sz]|sample size)\b", re.I),
     ("experimental-design", "statistical-power")),
    (re.compile(r"\b(critical appraisal|risk of bias|evidence quality|peer review)\b", re.I),
     ("scientific-critical-thinking",)),
)


def intent_hints(text: str) -> list[str]:
    """Named skills implied by unambiguous domain vocabulary, in rule order."""
    out: list[str] = []
    for pattern, names in _INTENT_HINTS:
        if pattern.search(text or ""):
            for name in names:
                if name not in out:
                    out.append(name)
    return out


def score_skill(skill: ScienceSkill, tokens: list[str]) -> float:
    """Name/tag evidence dominates; description mentions alone are weak."""
    if not tokens:
        return 0.0
    # "hypothesis-generation" is ONE regex token, so a query saying
    # "hypotheses" never matched its name. Split the identifier too.
    name_tokens = set(_tokens(skill.name))
    name_tokens.update(
        part for part in re.split(r"[-_]", skill.name.lower())
        if part and len(part) > 2 and part not in _STOPWORDS
    )
    tag_set = {tag.lower() for tag in skill.tags}
    desc_lower = skill.description.lower()
    score = 0.0
    name_hits = tag_hits = 0
    for token in set(tokens):
        if token == skill.name:
            score += 8.0
            name_hits += 1
        elif token in name_tokens:
            score += 3.0
            name_hits += 1
        if token in tag_set:
            score += 2.0
            tag_hits += 1
        if token in desc_lower:
            score += 1.0
    # A description-only match is how "paper-lookup" surfaced for "make figures".
    # Require real evidence: the skill's own name or at least one of its tags.
    skill._evidence = (name_hits, tag_hits)  # type: ignore[attr-defined]
    score += 0.5 if skill.core else 0.0
    score /= max(1.0, len(set(tokens)) ** 0.5)
    if not skill.enabled:
        score *= 0.6
    return score


def discover(
    query: str,
    k: int = 3,
    *,
    index: ScienceIndex | None = None,
    include_disabled: bool = True,
    min_score: float = 0.35,
) -> list[ScienceSkill]:
    """Top-k skills for a task, with scores, deterministic and offline."""
    index = index or get_index()
    tokens = _tokens(query)
    hinted = intent_hints(query)
    scored: list[ScienceSkill] = []
    for skill in index.skills.values():
        if not include_disabled and not skill.enabled:
            continue
        value = score_skill(skill, tokens)
        name_hits, tag_hits = getattr(skill, "_evidence", (0, 0))
        if skill.name in hinted:
            value += 3.0 * (hinted.index(skill.name) + 1) ** 0.5
            tag_hits = max(tag_hits, 2)  # an explicit intent is sufficient evidence
        if value < min_score:
            continue
        # Prose-only matches are usually noise ("the weather" once surfaced an
        # experimental-design skill). Require the skill's own name, or at least
        # TWO tags — one shared word is how "research ..." pulled in an oracle
        # skill that only mentions research questions in its description.
        if name_hits == 0 and tag_hits < 2:
            continue
        clone = ScienceSkill.from_dict(skill.public())
        clone.score = value
        scored.append(clone)
    scored.sort(key=lambda s: (-s.score, s.name))
    return scored[: max(1, k)]


def plan_skills(
    task: str,
    *,
    limit: int = 3,
    index: ScienceIndex | None = None,
) -> list[ScienceSkill]:
    """The multi-skill plan for one task: 1..N enabled skills, best first."""
    index = index or get_index()
    chosen = discover(task, k=limit * 4, index=index, include_disabled=False)
    if not chosen:
        return []
    floor = chosen[0].score * 0.55
    selected: list[ScienceSkill] = []
    for hinted_name in intent_hints(task):
        skill = index.get(hinted_name)
        if skill is None or not skill.enabled or skill.guarded_reason:
            continue
        clone = ScienceSkill.from_dict(skill.public())
        clone.score = chosen[0].score
        selected.append(clone)
        if len(selected) >= limit:
            return selected
    for skill in chosen:
        if not skill.enabled or skill.guarded_reason:
            continue
        if any(existing.name == skill.name for existing in selected):
            continue
        if skill.score < floor:
            continue
        tags = set(skill.tags)
        # Skip a near-duplicate of something already chosen (three citation
        # skills would crowd out the statistics skill the task also needs).
        duplicate = False
        for existing in selected:
            other = set(existing.tags)
            if tags and other and len(tags & other) / len(tags | other) >= 0.6:
                duplicate = True
                break
        if duplicate:
            continue
        selected.append(skill)
        if len(selected) >= limit:
            break
    return selected
