"""Offline tests for the analysis recipes, validation and bounded runner.

These run the real worker in the real interpreters (no HTTP). They assert
numbers against an independent scipy computation and check the artifacts that
are produced, because "the skill file exists" proves nothing.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.scientific import analysis, envs, runner  # noqa: E402
from core.scientific.config import load_config  # noqa: E402

CSV = BASE / "tests" / "fixtures" / "science" / "sample.csv"


@pytest.fixture(scope="module")
def dataset():
    assert CSV.is_file(), f"missing fixture: {CSV}"
    return str(CSV)


def test_recipe_registry_is_declared_completely():
    recipes = {recipe["name"]: recipe for recipe in analysis.list_recipes()}
    assert set(recipes) == {"describe", "ttest", "anova", "correlation",
                            "regression", "ml_classify", "plot"}
    for name, recipe in recipes.items():
        assert recipe["profile"] in {"core", "viz", "stats", "chem"}
        assert recipe["required_params"], name


def test_validation_rejects_missing_and_bad_parameters(dataset):
    errors, _ = analysis.validate_params("ttest", {"path": dataset})
    assert any("value_col" in error for error in errors)
    errors, _ = analysis.validate_params("describe", {"path": "/no/such/file.csv"})
    assert any("not found" in error for error in errors)
    errors, _ = analysis.validate_params("plot", {"path": dataset, "kind": "pie", "x": "score"})
    assert any("plot kind" in error for error in errors)
    errors, _ = analysis.validate_params("nope", {})
    assert errors


def test_describe_reports_shape_and_outliers(dataset):
    result = analysis.run("describe", {"path": dataset})
    assert result.ok, result.error
    stats = result.payload["stats"]
    assert stats["rows"] == 60 and len(stats["columns"]) == 6
    assert stats["duplicate_rows"] == 0
    assert set(stats["numeric"]) >= {"score", "age", "dose"}
    assert result.payload["rows"]["analysed"] == 60
    assert any(str(path).endswith("table.csv") for path in result.tables.values())


def test_ttest_matches_an_independent_computation(dataset):
    from scipy import stats as scipy_stats
    import pandas as pd
    import numpy as np

    result = analysis.run("ttest", {"path": dataset, "value_col": "score",
                                    "group_col": "group"})
    assert result.ok, result.error
    values = result.payload["stats"]

    frame = pd.read_csv(dataset)
    a = frame[frame.group == "A"].score
    b = frame[frame.group == "B"].score
    t_stat, p_value = scipy_stats.ttest_ind(a, b, equal_var=False)
    n1, n2 = len(a), len(b)
    pooled = np.sqrt(((n1 - 1) * a.var(ddof=1) + (n2 - 1) * b.var(ddof=1)) / (n1 + n2 - 2))
    d = (a.mean() - b.mean()) / pooled

    assert abs(values["t"] - float(t_stat)) < 1e-6
    assert abs(values["p"] - float(p_value)) < 1e-9
    assert abs(values["cohen_d"] - float(d)) < 1e-6
    assert values["cohen_d_ci95"][0] < values["cohen_d"] < values["cohen_d_ci95"][1]
    cross = values["cross_check"]
    assert cross is not None
    if cross.get("agrees") is not None:
        assert cross["agrees"] is True


def test_anova_matches_scipy(dataset):
    from scipy import stats as scipy_stats
    import pandas as pd

    result = analysis.run("anova", {"path": dataset, "value_col": "score",
                                    "group_col": "site"})
    assert result.ok, result.error
    frame = pd.read_csv(dataset)
    groups = [chunk.score.values for _, chunk in frame.groupby("site")]
    f_stat, p_value = scipy_stats.f_oneway(*groups)
    assert abs(result.payload["stats"]["F"] - float(f_stat)) < 1e-6
    assert abs(result.payload["stats"]["p"] - float(p_value)) < 1e-9
    assert result.payload["stats"]["posthoc"], "post-hoc comparisons missing"


def test_correlation_and_plot_artifacts(dataset):
    correlation = analysis.run("correlation", {"path": dataset, "x": "dose", "y": "score"})
    assert correlation.ok, correlation.error
    assert 0 < correlation.payload["stats"]["r"] < 1
    assert correlation.payload["stats"]["method"] == "pearson"

    plot = analysis.run("plot", {"path": dataset, "kind": "box", "x": "score",
                                 "group": "group", "title": "Score by group"})
    assert plot.ok, plot.error
    png = [path for path in plot.figures if path.endswith(".png")]
    pdf = [path for path in plot.figures if path.endswith(".pdf")]
    assert png and pdf
    data = Path(png[0]).read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) > 2000


def test_ml_classify_reports_metrics(dataset):
    result = analysis.run("ml_classify", {
        "path": dataset, "target": "group", "features": ["score", "dose", "age"],
    })
    assert result.ok, result.error
    stats = result.payload["stats"]
    assert stats["accuracy"] >= 0.7
    assert stats["n_train"] > stats["n_test"] > 0
    assert len(stats["confusion_matrix"]) == len(stats["classes"])
    assert result.payload["versions"].get("scikit-learn")


def test_regression_uses_the_stats_profile(dataset):
    if not envs.science_venv_ready():
        pytest.skip("science venv not provisioned (stats profile)")
    result = analysis.run("regression", {"path": dataset, "y": "score", "x": ["dose", "age"]})
    assert result.ok, result.error
    stats = result.payload["stats"]
    assert 0 <= stats["r_squared"] <= 1
    assert set(stats["terms"]) >= {"const", "dose", "age"}


def test_validation_flags_a_broken_figure(tmp_path):
    tiny = tmp_path / "figure.png"
    tiny.write_bytes(b"not a png")
    warnings, errors, artifacts = analysis._validate(
        {"ok": True, "stats": {"p": 0.04}, "figures": [str(tiny)], "tables": {}},
        tmp_path,
    )
    assert errors and any("figure invalid" in error for error in errors)
    assert artifacts["figures"] == []


def test_validation_flags_an_out_of_range_p(tmp_path):
    warnings, errors, _ = analysis._validate({"ok": True, "stats": {"p": 1.7}}, tmp_path)
    assert any("p-value" in error for error in errors)


def test_child_environment_is_scrubbed_and_job_scoped(tmp_path):
    env = runner._child_env(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["MPLBACKEND"] == "Agg"
    assert env["OMP_NUM_THREADS"] == "2"
    for forbidden in ("JARVIS_HOME", "PYTHONPATH", "OPENAI_API_KEY", "NVIDIA_API_KEY"):
        assert forbidden not in env


def test_runner_enforces_its_timeout(tmp_path):
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\nprint('{\"ok\": true}')\ntime.sleep(30)\n", encoding="utf-8")
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    outcome = runner.execute_worker(
        python=Path(sys.executable), recipe="describe", params_path=params,
        out_dir=tmp_path, timeout_s=1.0, worker=worker,
    )
    assert outcome.timed_out is True
    assert outcome.ok is False
    assert outcome.duration_s < 10


def test_runner_can_be_cancelled(tmp_path):
    worker = tmp_path / "slow_worker.py"
    worker.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    box: dict = {}

    def _run():
        box["outcome"] = runner.execute_worker(
            python=Path(sys.executable), recipe="describe", params_path=params,
            out_dir=tmp_path, timeout_s=60.0, worker=worker, task_id="cancel-me",
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(50):
        if runner.active_tasks().get("cancel-me"):
            break
        time.sleep(0.1)
    killed = runner.kill_task("cancel-me")
    thread.join(timeout=20)
    assert killed >= 1
    assert box["outcome"].ok is False
    assert box["outcome"].duration_s < 20
    assert runner.active_tasks().get("cancel-me") is None


def test_outputs_live_under_the_state_dir(dataset):
    result = analysis.run("describe", {"path": dataset})
    assert result.ok
    state_dir = load_config().state_dir.resolve()
    assert Path(result.job_dir).resolve().is_relative_to(state_dir)
