"""
core/scientific/analysis.py — recipes, validation and job directories.

The agent never writes analysis code. It selects a recipe from this registry,
supplies named parameters, and gets back a structured result plus files. That
is what makes the numeric work reproducible and reviewable: the recipe is the
reviewed artifact, the parameters are data, and the worker writes its own
`result.json`, `table.csv` and figures.

Validation is a separate step on purpose. A worker can exit 0 and still produce
something unusable — a figure that is 0 bytes, a p-value outside [0,1], a table
that was never written. `_validate` checks the artifacts, so the agent can say
"the plot failed" instead of handing a broken path to the user.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.scientific import envs, runner
from core.scientific.config import load_config


@dataclass(frozen=True)
class Recipe:
    name: str
    description: str
    profile: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    produces_figure: bool = False

    def public(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "profile": self.profile,
            "required_params": list(self.required),
            "optional_params": list(self.optional),
            "produces_figure": self.produces_figure,
        }


RECIPES: dict[str, Recipe] = {
    "describe": Recipe(
        "describe",
        "Exploratory summary of a CSV/Excel dataset: shape, dtypes, missing "
        "values, duplicates, per-column mean/SD/median/IQR and IQR outliers.",
        "core", ("path",), ("sheet",),
    ),
    "ttest": Recipe(
        "ttest",
        "Welch independent-samples t-test with Levene and Shapiro diagnostics, "
        "Cohen's d with a 95% CI, and a pingouin cross-check when installed.",
        "core", ("path", "value_col", "group_col"),
    ),
    "anova": Recipe(
        "anova",
        "One-way ANOVA with eta squared and post-hoc comparisons (Tukey via "
        "statsmodels, otherwise pairwise Welch with Holm correction).",
        "core", ("path", "value_col", "group_col"),
    ),
    "correlation": Recipe(
        "correlation",
        "Pearson or Spearman correlation with p-value, n and r squared.",
        "core", ("path", "x", "y"), ("method",),
    ),
    "regression": Recipe(
        "regression",
        "OLS regression with coefficients, standard errors, p-values and fit "
        "statistics (requires the stats profile: statsmodels).",
        "stats", ("path", "y"), ("x",),
    ),
    "ml_classify": Recipe(
        "ml_classify",
        "Train/test classifier (logistic or random forest) with accuracy, "
        "confusion matrix, per-class report and ROC-AUC for binary targets.",
        "core", ("path", "target"), ("features", "model", "test_size"),
    ),
    "plot": Recipe(
        "plot",
        "Publication-quality figure (hist, box, scatter, line, bar) exported as "
        "300 dpi PNG and vector PDF.",
        "viz", ("path", "kind"), ("x", "y", "group", "title", "bins"),
    ),
}


@dataclass
class AnalysisResult:
    ok: bool
    recipe: str
    job_dir: str
    payload: dict = field(default_factory=dict)
    figures: list[str] = field(default_factory=list)
    tables: dict = field(default_factory=dict)
    summary: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    peak_rss_mb: float = 0.0
    interpreter: str = ""

    def public(self) -> dict:
        return {
            "ok": self.ok,
            "recipe": self.recipe,
            "job_dir": self.job_dir,
            "summary": self.summary,
            "figures": self.figures,
            "tables": self.tables,
            "warnings": self.warnings,
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "stats": (self.payload or {}).get("stats") or {},
            "rows": (self.payload or {}).get("rows") or {},
            "versions": (self.payload or {}).get("versions") or {},
        }


def list_recipes() -> list[dict]:
    return [recipe.public() for recipe in RECIPES.values()]


def describe_recipe(name: str) -> dict | None:
    recipe = RECIPES.get(name)
    return recipe.public() if recipe else None


def validate_params(recipe_name: str, params: dict) -> tuple[list[str], dict]:
    """Return (errors, cleaned params). Never mutates the caller's dict."""
    recipe = RECIPES.get(recipe_name)
    if recipe is None:
        return [f"unknown recipe '{recipe_name}'"], {}
    cleaned: dict[str, Any] = {}
    errors: list[str] = []
    for key in recipe.required:
        if key not in params or params[key] in (None, "", []):
            errors.append(f"missing required parameter: {key}")
        else:
            cleaned[key] = params[key]
    for key in recipe.optional:
        if key in params and params[key] not in (None, "", []):
            cleaned[key] = params[key]
    path = cleaned.get("path")
    if path:
        # The worker runs with cwd=job_dir, so a relative path from the caller
        # must be resolved here or it silently points at the job directory.
        resolved = Path(str(path)).expanduser().resolve()
        if not resolved.is_file():
            errors.append(f"dataset not found: {resolved}")
        else:
            cleaned["path"] = str(resolved)
    if recipe_name == "plot" and "kind" in cleaned:
        if str(cleaned["kind"]).lower() not in {"hist", "histogram", "box", "boxplot",
                                                "scatter", "line", "bar"}:
            errors.append(f"unsupported plot kind: {cleaned['kind']}")
    return errors, cleaned


def _last_json_line(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
    return {}


def _png_ok(path: Path) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, "missing"
        size = path.stat().st_size
        if size < 1000:
            return False, f"too small ({size} bytes)"
        with path.open("rb") as handle:
            magic = handle.read(8)
        if magic != b"\x89PNG\r\n\x1a\n":
            return False, "not a PNG"
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}"


def _validate(payload: dict, job_dir: Path) -> tuple[list[str], list[str], dict]:
    """(warnings, hard_errors, {figures, tables}) — artifacts are checked."""
    warnings: list[str] = []
    errors: list[str] = []
    figures: list[str] = []
    tables: dict[str, str] = {}

    if not payload:
        return ["worker produced no result"], ["no result.json"], {"figures": [], "tables": {}}
    if payload.get("ok") is not True:
        return warnings, [str(payload.get("error") or "recipe reported failure")], {
            "figures": [], "tables": {}}

    stats = payload.get("stats") or {}
    p_value = stats.get("p")
    if isinstance(p_value, (int, float)) and not (0.0 <= float(p_value) <= 1.0):
        errors.append(f"p-value out of range: {p_value}")

    rows = payload.get("rows") or {}
    if rows and isinstance(rows.get("in"), int) and isinstance(rows.get("analysed"), int):
        if rows["analysed"] > rows["in"]:
            errors.append("rows analysed exceeds rows read")

    for figure in payload.get("figures") or []:
        path = Path(str(figure))
        if path.suffix.lower() == ".png":
            ok, reason = _png_ok(path)
            if not ok:
                errors.append(f"figure invalid ({path.name}: {reason})")
                continue
        elif not path.is_file() or path.stat().st_size == 0:
            errors.append(f"figure missing ({path.name})")
            continue
        figures.append(str(path))

    for name, table in (payload.get("tables") or {}).items():
        path = Path(str(table))
        if path.is_file() and path.stat().st_size > 0:
            tables[str(name)] = str(path)
        else:
            errors.append(f"table not written: {name}")

    cross = (stats.get("cross_check") or {})
    if cross and cross.get("agrees") is False:
        warnings.append("two implementations disagreed on the p-value")

    return warnings, errors, {"figures": figures, "tables": tables}


def run(
    recipe_name: str,
    params: dict,
    *,
    task_id: str = "",
    profile: str | None = None,
    timeout_s: float | None = None,
) -> AnalysisResult:
    """Execute one recipe in the right interpreter and validate its artifacts."""
    cfg = load_config()
    errors, cleaned = validate_params(recipe_name, params)
    recipe = RECIPES.get(recipe_name)
    if errors or recipe is None:
        return AnalysisResult(False, recipe_name, "", error="; ".join(errors))

    chosen_profile = profile or recipe.profile
    python = envs.interpreter_for(chosen_profile)
    if python is None:
        return AnalysisResult(False, recipe_name, "",
                              error=f"no interpreter for profile {chosen_profile}")

    job_dir = Path(cfg.jobs_dir) / f"{recipe_name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    params_path = job_dir / "params.json"
    params_path.write_text(json.dumps(cleaned, indent=1), encoding="utf-8")

    outcome = runner.execute_worker(
        python=Path(python),
        recipe=recipe_name,
        params_path=params_path,
        out_dir=job_dir,
        task_id=task_id,
        timeout_s=timeout_s,
    )

    payload: dict = {}
    result_path = job_dir / "result.json"
    if result_path.is_file():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    if not payload:
        payload = _last_json_line(outcome.stdout)

    warnings, hard_errors, artifacts = _validate(payload, job_dir)
    if outcome.timed_out:
        hard_errors.append(f"analysis exceeded its {timeout_s or cfg.run_timeout_s:.0f}s budget")
    elif outcome.cancelled:
        hard_errors.append("analysis was cancelled")
    elif not payload and outcome.stderr:
        hard_errors.append(outcome.stderr.strip().splitlines()[-1][:300])

    meta = {
        "recipe": recipe_name,
        "params": cleaned,
        "profile": chosen_profile,
        "interpreter": str(python),
        "command": outcome.command,
        "outcome": outcome.public(),
        "validation_errors": hard_errors,
        "warnings": warnings,
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    return AnalysisResult(
        ok=not hard_errors,
        recipe=recipe_name,
        job_dir=str(job_dir),
        payload=payload,
        figures=artifacts["figures"],
        tables=artifacts["tables"],
        summary=str(payload.get("summary") or ""),
        error="; ".join(hard_errors),
        warnings=warnings + list(payload.get("warnings") or []),
        duration_s=outcome.duration_s,
        peak_rss_mb=float(payload.get("peak_rss_mb") or 0.0),
        interpreter=str(python),
    )
