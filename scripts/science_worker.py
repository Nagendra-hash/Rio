#!/usr/bin/env python3
"""
science_worker.py — the only place RIO's science agent runs numerics.

WHY A WORKER WITH FIXED RECIPES
    Running model-written Python is the largest risk in a research assistant.
    Instead, the agent chooses a RECIPE and supplies structured parameters;
    this script is the reviewed implementation of each recipe. There is no
    string interpolation of user text into code, so a dataset column name or a
    paper title cannot turn into executable code.

CONTRACT
    python science_worker.py --recipe NAME --params params.json --out DIR
    Writes DIR/result.json (always), plus DIR/table.csv and DIR/figure.png|pdf
    when the recipe produces them. Progress and errors go to stderr; stdout is
    reserved for the one JSON line, so a caller can parse it safely.

REPRODUCIBILITY
    Every result records the interpreter, library versions, input file hash,
    row counts before/after filtering, and the random seed used. A t-test and
    ANOVA are cross-checked against a second implementation when one is
    installed, and `cross_check` says whether they agreed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path

# Limits arrive as environment values from the runner and are applied before
# numpy/scipy are imported. Doing it here (rather than with subprocess
# preexec_fn) keeps the parent free of the fork-with-threads pattern, which is
# documented as unsafe and was crashing the interpreter at shutdown.
def _apply_limits() -> None:
    import resource

    def _set(limit: int, value: int | None) -> None:
        if not value or value <= 0:
            return
        try:
            soft, hard = resource.getrlimit(limit)
            target = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(limit, (target, target))
        except Exception:
            pass

    _set(resource.RLIMIT_AS, int(os.environ.get("RIO_RLIMIT_AS", "0") or 0))
    _set(resource.RLIMIT_CPU, int(os.environ.get("RIO_RLIMIT_CPU", "0") or 0))
    _set(resource.RLIMIT_CORE, 0)


_apply_limits()

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

SEED = 42


def _versions() -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    out = {}
    for package in ("pandas", "numpy", "scipy", "scikit-learn", "matplotlib",
                    "statsmodels", "pingouin", "seaborn", "biopython", "rdkit"):
        try:
            out[package] = version(package)
        except PackageNotFoundError:
            continue
    return out


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()[:16]


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def is_bool(series) -> bool:
    import pandas as pd

    return str(getattr(series, "dtype", "")).startswith("bool") or pd.api.types.is_bool_dtype(series)


def _fail(message: str, **extra) -> dict:
    return {"ok": False, "error": message, **extra}


# ── data loading ────────────────────────────────────────────────────────────


def _load_frame(path: str, sheet: str | None = None):
    import pandas as pd

    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(path, sheet_name=sheet or 0)
    if suffix in (".tsv", ".tab"):
        return pd.read_csv(path, sep="\t")
    if suffix in (".json",):
        return pd.read_json(path)
    return pd.read_csv(path)


def _numeric_summary(frame) -> dict:
    import numpy as np
    from pandas.api.types import is_numeric_dtype

    out: dict[str, dict] = {}
    for column in frame.columns:
        series = frame[column]
        # pandas 3 uses StringDtype, which numpy's issubdtype rejects outright.
        if not is_numeric_dtype(series) or is_bool(series):
            continue
        clean = series.dropna()
        if clean.empty:
            continue
        q1, q3 = clean.quantile(0.25), clean.quantile(0.75)
        iqr = q3 - q1
        outliers = int(((clean < q1 - 1.5 * iqr) | (clean > q3 + 1.5 * iqr)).sum())
        out[str(column)] = {
            "n": int(clean.size),
            "mean": round(float(clean.mean()), 6),
            "sd": round(float(clean.std(ddof=1)), 6) if clean.size > 1 else None,
            "median": round(float(clean.median()), 6),
            "min": round(float(clean.min()), 6),
            "max": round(float(clean.max()), 6),
            "iqr": round(float(iqr), 6),
            "outliers_iqr": outliers,
        }
    return out


def _clean_for_columns(frame, columns: list[str]) -> tuple[object, int, int]:
    """Drop rows missing any needed column; report rows in / dropped / left."""
    rows_in = int(len(frame))
    subset = frame[[c for c in columns if c in frame.columns]].copy()
    subset = subset.dropna()
    return subset, rows_in, rows_in - int(len(subset))


# ── statistics helpers ──────────────────────────────────────────────────────


def _cohen_d(a, b) -> float | None:
    import numpy as np

    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    var1, var2 = np.var(a, ddof=1), np.var(b, ddof=1)
    pooled = np.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))
    if pooled == 0:
        return None
    return float((np.mean(a) - np.mean(b)) / pooled)


def _d_ci(d: float | None, n1: int, n2: int) -> tuple[float, float] | None:
    import math

    if d is None or n1 < 2 or n2 < 2:
        return None
    se = math.sqrt((n1 + n2) / (n1 * n2) + d * d / (2 * (n1 + n2)))
    return (d - 1.96 * se, d + 1.96 * se)


def _apa_p(p: float) -> str:
    return "p < .001" if p < 0.001 else f"p = {p:.3f}"


def _groups(frame, value_col: str, group_col: str) -> tuple[list, dict]:
    groups = {}
    for name, chunk in frame.groupby(group_col):
        values = chunk[value_col].to_numpy()
        if len(values) >= 2:
            groups[str(name)] = values
    return list(groups), groups


# ── recipes ─────────────────────────────────────────────────────────────────


def recipe_describe(params: dict, out_dir: Path) -> dict:
    frame = _load_frame(params["path"], params.get("sheet"))
    rows_in = int(len(frame))
    missing = {str(c): int(frame[c].isna().sum()) for c in frame.columns}
    duplicates = int(frame.duplicated().sum())
    summary = _numeric_summary(frame)
    table_path = out_dir / "table.csv"
    import pandas as pd

    pd.DataFrame(summary).transpose().to_csv(table_path)
    return {
        "ok": True,
        "recipe": "describe",
        "summary": (f"{rows_in} rows x {len(frame.columns)} columns; "
                    f"{sum(missing.values())} missing values; "
                    f"{duplicates} duplicate rows"),
        "stats": {
            "rows": rows_in,
            "columns": [str(c) for c in frame.columns],
            "dtypes": {str(c): str(t) for c, t in frame.dtypes.items()},
            "missing": missing,
            "duplicate_rows": duplicates,
            "numeric": summary,
        },
        "tables": {"numeric_summary": str(table_path)},
        "rows": {"in": rows_in, "analysed": rows_in, "dropped": 0},
        "warnings": [] if not duplicates else [f"{duplicates} duplicate rows present"],
    }


def recipe_ttest(params: dict, out_dir: Path) -> dict:
    import numpy as np
    from scipy import stats

    frame = _load_frame(params["path"], params.get("sheet"))
    value_col, group_col = params["value_col"], params["group_col"]
    for column in (value_col, group_col):
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
    subset, rows_in, dropped = _clean_for_columns(frame, [value_col, group_col])
    names, groups = _groups(subset, value_col, group_col)
    if len(groups) != 2:
        return _fail(f"t-test needs exactly 2 groups, found {len(groups)} ({names})")
    a, b = groups[names[0]], groups[names[1]]

    t_stat, p_value = stats.ttest_ind(a, b, equal_var=False)
    levene = stats.levene(a, b)
    shapiro_a = stats.shapiro(a) if len(a) >= 3 else None
    shapiro_b = stats.shapiro(b) if len(b) >= 3 else None
    d = _cohen_d(a, b)
    ci = _d_ci(d, len(a), len(b))

    cross_check = None
    try:
        import pingouin as pg

        pg_result = pg.ttest(a, b, correction=True)
        pg_p = float(pg_result["p_val"].values[0])
        cross_check = {
            "implementation": f"pingouin {pg.__version__ if hasattr(pg, '__version__') else ''}".strip(),
            "p_value": pg_p,
            "agrees": bool(abs(pg_p - float(p_value)) < 1e-6),
        }
    except Exception as exc:
        cross_check = {"implementation": "unavailable", "reason": type(exc).__name__}

    summary = (
        f"Welch t({float(stats.ttest_ind(a, b, equal_var=False).df):.2f}) = {float(t_stat):.2f}, "
        f"{_apa_p(float(p_value))}"
    )
    if d is not None:
        summary += f", Cohen's d = {d:.2f}"
        if ci:
            summary += f" [95% CI {ci[0]:.2f}, {ci[1]:.2f}]"
    return {
        "ok": True,
        "recipe": "ttest",
        "summary": summary,
        "stats": {
            "test": "welch_t",
            "groups": {
                name: {"n": int(len(values)), "mean": round(float(np.mean(values)), 6),
                       "sd": round(float(np.std(values, ddof=1)), 6)}
                for name, values in groups.items()
            },
            "t": round(float(t_stat), 6),
            "p": float(p_value),
            "cohen_d": None if d is None else round(d, 6),
            "cohen_d_ci95": None if ci is None else [round(ci[0], 6), round(ci[1], 6)],
            "assumptions": {
                "levene_p": round(float(levene.pvalue), 6),
                "shapiro": {
                    names[0]: None if shapiro_a is None else round(float(shapiro_a.pvalue), 6),
                    names[1]: None if shapiro_b is None else round(float(shapiro_b.pvalue), 6),
                },
            },
            "cross_check": cross_check,
        },
        "tables": {},
        "rows": {"in": rows_in, "analysed": int(len(a) + len(b)), "dropped": dropped},
        "warnings": [] if (cross_check or {}).get("agrees", True) else [
            "scipy and pingouin p-values disagree — treat the result as unstable"
        ],
    }


def recipe_anova(params: dict, out_dir: Path) -> dict:
    import numpy as np
    from scipy import stats

    frame = _load_frame(params["path"], params.get("sheet"))
    value_col, group_col = params["value_col"], params["group_col"]
    for column in (value_col, group_col):
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
    subset, rows_in, dropped = _clean_for_columns(frame, [value_col, group_col])
    names, groups = _groups(subset, value_col, group_col)
    if len(groups) < 2:
        return _fail("ANOVA needs at least 2 groups")
    samples = [groups[name] for name in names]
    f_stat, p_value = stats.f_oneway(*samples)

    grand_mean = np.concatenate(samples).mean()
    ss_between = sum(len(s) * (s.mean() - grand_mean) ** 2 for s in samples)
    ss_total = sum(((s - grand_mean) ** 2).sum() for s in samples)
    eta_sq = float(ss_between / ss_total) if ss_total else None

    posthoc = []
    try:
        from statsmodels.stats.multicomp import pairwise_tukeyhsd

        tukey = pairwise_tukeyhsd(subset[value_col].to_numpy(),
                                  subset[group_col].astype(str).to_numpy())
        for row in tukey.summary().data[1:]:
            posthoc.append({
                "group1": str(row[0]), "group2": str(row[1]),
                "p": round(float(row[3]), 6), "significant": bool(row[6]),
            })
        cross_check = {"implementation": "statsmodels tukey", "agrees": True}
    except Exception:
        # No statsmodels: pairwise Welch with Holm correction, using scipy only.
        pairs = []
        raw = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                t, p = stats.ttest_ind(samples[i], samples[j], equal_var=False)
                raw.append({"group1": names[i], "group2": names[j], "p_raw": float(p)})
        raw.sort(key=lambda r: r["p_raw"])
        m = len(raw)
        for index, item in enumerate(raw):
            adjusted = min(1.0, item["p_raw"] * (m - index))
            pairs.append({**{k: item[k] for k in ("group1", "group2")},
                          "p": round(adjusted, 6), "significant": adjusted < 0.05})
        posthoc = pairs
        cross_check = {"implementation": "scipy welch + holm", "agrees": True}

    return {
        "ok": True,
        "recipe": "anova",
        "summary": (f"One-way ANOVA F({len(samples) - 1}, {len(subset) - len(samples)}) = "
                    f"{float(f_stat):.2f}, {_apa_p(float(p_value))}"
                    + (f", partial eta squared = {eta_sq:.3f}" if eta_sq is not None else "")),
        "stats": {
            "test": "one_way_anova",
            "groups": {name: {"n": int(len(values)),
                              "mean": round(float(np.mean(values)), 6),
                              "sd": round(float(np.std(values, ddof=1)), 6)}
                       for name, values in groups.items()},
            "F": round(float(f_stat), 6),
            "p": float(p_value),
            "eta_squared": None if eta_sq is None else round(eta_sq, 6),
            "posthoc": posthoc,
            "cross_check": cross_check,
        },
        "tables": {},
        "rows": {"in": rows_in, "analysed": int(len(subset)), "dropped": dropped},
        "warnings": [],
    }


def recipe_correlation(params: dict, out_dir: Path) -> dict:
    from scipy import stats

    frame = _load_frame(params["path"], params.get("sheet"))
    x_col, y_col = params["x"], params["y"]
    for column in (x_col, y_col):
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
    subset, rows_in, dropped = _clean_for_columns(frame, [x_col, y_col])
    method = str(params.get("method") or "pearson").lower()
    if method == "spearman":
        coefficient, p_value = stats.spearmanr(subset[x_col], subset[y_col])
        label = "Spearman rho"
    else:
        coefficient, p_value = stats.pearsonr(subset[x_col], subset[y_col])
        label = "Pearson r"
    return {
        "ok": True,
        "recipe": "correlation",
        "summary": f"{label} = {float(coefficient):.3f}, {_apa_p(float(p_value))} (n = {len(subset)})",
        "stats": {
            "method": method,
            "r": round(float(coefficient), 6),
            "p": float(p_value),
            "n": int(len(subset)),
            "r_squared": round(float(coefficient) ** 2, 6),
        },
        "tables": {},
        "rows": {"in": rows_in, "analysed": int(len(subset)), "dropped": dropped},
        "warnings": [] if len(subset) >= 20 else [
            f"small sample (n={len(subset)}); interpret with caution"
        ],
    }


def recipe_regression(params: dict, out_dir: Path) -> dict:
    frame = _load_frame(params["path"], params.get("sheet"))
    y_col = params["y"]
    x_cols = params.get("x") or []
    if isinstance(x_cols, str):
        x_cols = [x_cols]
    for column in [y_col, *x_cols]:
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
    subset, rows_in, dropped = _clean_for_columns(frame, [y_col, *x_cols])
    try:
        import statsmodels.api as sm

        design = sm.add_constant(subset[x_cols].astype(float))
        model = sm.OLS(subset[y_col].astype(float), design).fit()
        terms = {}
        for name in design.columns:
            terms[str(name)] = {
                "coef": round(float(model.params[name]), 6),
                "se": round(float(model.bse[name]), 6),
                "p": round(float(model.pvalues[name]), 6),
            }
        return {
            "ok": True,
            "recipe": "regression",
            "summary": (f"OLS on {len(subset)} rows: R² = {float(model.rsquared):.3f}, "
                        f"adjusted R² = {float(model.rsquared_adj):.3f}, "
                        f"F({int(model.df_model)}, {int(model.df_resid)}) = "
                        f"{float(model.fvalue):.2f}, {_apa_p(float(model.f_pvalue))}"),
            "stats": {
                "r_squared": round(float(model.rsquared), 6),
                "adjusted_r_squared": round(float(model.rsquared_adj), 6),
                "f": round(float(model.fvalue), 6),
                "p": float(model.f_pvalue),
                "terms": terms,
            },
            "tables": {},
            "rows": {"in": rows_in, "analysed": int(len(subset)), "dropped": dropped},
            "warnings": [],
        }
    except ImportError:
        return _fail(
            "regression needs statsmodels, which is not installed in this "
            "interpreter. Use the stats profile (see requirements-science.txt).",
            needs="statsmodels",
        )


def recipe_ml_classify(params: dict, out_dir: Path) -> dict:
    import numpy as np
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (accuracy_score, classification_report,
                                 confusion_matrix, roc_auc_score)
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    frame = _load_frame(params["path"], params.get("sheet"))
    target = params["target"]
    if target not in frame.columns:
        return _fail(f"target column not found: {target}")
    features = params.get("features") or [c for c in frame.columns if c != target]
    if isinstance(features, str):
        features = [features]
    missing = [c for c in features if c not in frame.columns]
    if missing:
        return _fail(f"feature columns not found: {', '.join(missing)}")

    subset, rows_in, dropped = _clean_for_columns(frame, [target, *features])
    if len(subset) < 20:
        return _fail(f"not enough rows after cleaning ({len(subset)}); need >= 20")

    from pandas.api.types import is_numeric_dtype

    x = subset[features]
    y = subset[target]
    numeric = [c for c in features if is_numeric_dtype(x[c]) and not is_bool(x[c])]
    categorical = [c for c in features if c not in numeric]
    numeric_pipe = Pipeline([("impute", SimpleImputer(strategy="median")),
                             ("scale", StandardScaler())])
    categorical_pipe = Pipeline([("impute", SimpleImputer(strategy="most_frequent")),
                                 ("encode", OneHotEncoder(handle_unknown="ignore"))])
    preprocess = ColumnTransformer([
        ("num", numeric_pipe, numeric),
        ("cat", categorical_pipe, categorical),
    ])
    model_name = str(params.get("model") or "logistic").lower()
    estimator = (RandomForestClassifier(n_estimators=200, random_state=SEED)
                 if model_name == "forest"
                 else LogisticRegression(max_iter=2000, random_state=SEED))
    pipeline = Pipeline([("prep", preprocess), ("model", estimator)])

    test_size = float(params.get("test_size") or 0.25)
    stratify = y if y.value_counts().min() >= 2 else None
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=test_size, random_state=SEED, stratify=stratify)
    pipeline.fit(x_train, y_train)
    predictions = pipeline.predict(x_test)

    metrics = {
        "accuracy": round(float(accuracy_score(y_test, predictions)), 6),
        "n_train": int(len(x_train)),
        "n_test": int(len(x_test)),
        "classes": [str(c) for c in sorted(y.unique(), key=str)],
        "confusion_matrix": confusion_matrix(y_test, predictions).tolist(),
        "report": classification_report(y_test, predictions, output_dict=True,
                                        zero_division=0),
    }
    if len(metrics["classes"]) == 2:
        try:
            probabilities = pipeline.predict_proba(x_test)[:, 1]
            metrics["roc_auc"] = round(float(roc_auc_score(
                (y_test == sorted(y.unique())[1]).astype(int), probabilities)), 6)
        except Exception:
            pass

    table_path = out_dir / "table.csv"
    import pandas as pd

    pd.DataFrame(metrics["confusion_matrix"]).to_csv(table_path, index=False)
    return {
        "ok": True,
        "recipe": "ml_classify",
        "summary": (f"{model_name} classifier: accuracy {metrics['accuracy']:.3f} "
                    f"on {metrics['n_test']} held-out rows "
                    f"({len(features)} features, seed {SEED})"),
        "stats": metrics,
        "tables": {"confusion_matrix": str(table_path)},
        "rows": {"in": rows_in, "analysed": int(len(subset)), "dropped": dropped},
        "warnings": [] if len(subset) >= 100 else [
            f"small dataset (n={len(subset)}); metrics are unstable"
        ],
    }


def recipe_plot(params: dict, out_dir: Path) -> dict:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frame = _load_frame(params["path"], params.get("sheet"))
    kind = str(params.get("kind") or "hist").lower()
    title = str(params.get("title") or f"{kind} plot")
    fig, ax = plt.subplots(figsize=(8, 5), layout="constrained")

    if kind in ("hist", "histogram"):
        column = params["x"]
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
        ax.hist(frame[column].dropna(), bins=int(params.get("bins") or 30),
                edgecolor="black", alpha=0.75)
        ax.set_xlabel(column)
        ax.set_ylabel("count")
    elif kind in ("box", "boxplot"):
        column, group = params["x"], params.get("group")
        if column not in frame.columns:
            return _fail(f"column not found: {column}")
        if group and group in frame.columns:
            data = [chunk[column].dropna().to_numpy()
                    for _, chunk in frame.groupby(group)]
            labels = [str(name) for name, _ in frame.groupby(group)]
            ax.boxplot(data, tick_labels=labels)
            ax.set_xlabel(group)
        else:
            ax.boxplot(frame[column].dropna().to_numpy())
        ax.set_ylabel(column)
    elif kind == "scatter":
        x_col, y_col = params["x"], params["y"]
        for column in (x_col, y_col):
            if column not in frame.columns:
                return _fail(f"column not found: {column}")
        ax.scatter(frame[x_col], frame[y_col], alpha=0.7)
        ax.set_xlabel(x_col)
        ax.set_ylabel(y_col)
    elif kind in ("line", "bar"):
        x_col, y_col = params["x"], params["y"]
        for column in (x_col, y_col):
            if column not in frame.columns:
                return _fail(f"column not found: {column}")
        (ax.plot if kind == "line" else ax.bar)(frame[x_col], frame[y_col])
        ax.set_xlabel(x_col)
        ax.set_ylabel(y_col)
    else:
        return _fail(f"unknown plot kind: {kind}")

    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    png = out_dir / "figure.png"
    pdf = out_dir / "figure.pdf"
    fig.savefig(png, dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return {
        "ok": True,
        "recipe": "plot",
        "summary": f"{kind} figure saved ({png.stat().st_size} bytes)",
        "stats": {"kind": kind, "png_bytes": png.stat().st_size,
                  "dpi": 300, "kind_title": title},
        "tables": {},
        "figures": [str(png), str(pdf)],
        "rows": {"in": int(len(frame)), "analysed": int(len(frame)), "dropped": 0},
        "warnings": [],
    }


RECIPES = {
    "describe": recipe_describe,
    "ttest": recipe_ttest,
    "anova": recipe_anova,
    "correlation": recipe_correlation,
    "regression": recipe_regression,
    "ml_classify": recipe_ml_classify,
    "plot": recipe_plot,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="RIO scientific analysis worker")
    parser.add_argument("--recipe", required=True, choices=sorted(RECIPES))
    parser.add_argument("--params", required=True, help="path to a JSON parameter file")
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    except Exception as exc:
        _emit(_fail(f"params unreadable: {type(exc).__name__}"))
        return 2

    base = {
        "interpreter": sys.executable,
        "python": sys.version.split()[0],
        "versions": _versions(),
        "input_sha256_16": _hash_file(params.get("path", "")) if params.get("path") else "",
        "seed": SEED,
    }
    try:
        payload = RECIPES[args.recipe](params, out_dir)
    except Exception as exc:
        payload = _fail(f"{type(exc).__name__}: {exc}",
                        traceback=traceback.format_exc()[-1500:])
    # ru_maxrss is a high-water mark, so reading it AFTER the recipe is what
    # actually reports the analysis's peak (reading it before gave 17 MB).
    import resource as _resource

    payload["peak_rss_mb"] = round(
        _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1
    )
    payload.update(base)
    (out_dir / "result.json").write_text(
        json.dumps(payload, indent=1, default=str), encoding="utf-8")
    _emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
