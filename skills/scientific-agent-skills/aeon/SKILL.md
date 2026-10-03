---
name: aeon
description: This skill should be used for time series machine learning tasks including classification, regression, clustering, forecasting, anomaly detection, segmentation, and similarity search. Use when working with temporal data, sequential patterns, or time-indexed observations requiring specialized algorithms beyond standard ML approaches. Particularly suited for univariate and multivariate time series analysis with scikit-learn compatible APIs.
license: BSD-3-Clause license
allowed-tools: Read Write Edit Bash
compatibility: Requires Python 3.11-3.14 and aeon 1.6.0. Optional stumpy for matrix profiles, TensorFlow for deep learning. Network needed only for installation and remote datasets/results.
metadata: 
  version: 1.3
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
---

# Aeon Time Series Machine Learning

## Overview

Aeon is a scikit-learn compatible Python toolkit for time series machine learning ([aeon-toolkit.org](https://www.aeon-toolkit.org/)). It provides algorithms across classification, regression, clustering, forecasting, anomaly detection, segmentation, similarity search, distances, transformations, benchmarking, and visualization — with a consistent estimator API.

**Version note:** Reviewed against **aeon 1.6.0** (Python 3.13). Small synthetic checks cover classification, regression, clustering, forecasts, preprocessing, distances, search, segmentation, matrix profiles, metrics, and local dataset I/O. Remote archive and TensorFlow training snippets are illustrative; they were not executed during this review. Reference catalogs are selected methods, not exhaustive lists. See the [1.6 release notes](https://www.aeon-toolkit.org/en/stable/changelogs/v1.6.html).

## When to Use This Skill

Apply this skill when:
- Classifying or predicting from time series data
- Detecting anomalies or change points in temporal sequences
- Clustering similar time series patterns
- Forecasting future values
- Finding repeated patterns (motifs) or unusual subsequences (discords)
- Comparing time series with specialized distance metrics
- Extracting features from temporal data

## Installation

Requires **Python 3.11-3.14**. Pin the reviewed release for reproducibility:

```bash
uv pip install "aeon==1.6.0"
```

Install only the extras required by the chosen estimator. The broad optional set is available as:

```bash
uv pip install "aeon[all_extras]==1.6.0"
```

On zsh, quote the extras: `uv pip install "aeon[all_extras]==1.6.0"`.

For the matrix-profile examples: `uv pip install "aeon==1.6.0" stumpy`. Range precision/recall/F-score depend on `prts`, whose current NumPy<2 requirement conflicts with aeon 1.6; use the runnable AUC metrics or a separately validated environment. Deep learning estimators use TensorFlow. Inspect an estimator's `python_dependencies` tag before installing optional packages.

### Experimental modules

Upstream treats **forecasting**, **anomaly_detection**, **segmentation**, **similarity_search**, and **visualisation** as experimental — interfaces may change between minor releases. Prefer stable modules (classification, regression, clustering, distances, transformations) for production pipelines unless you need these tasks.

## Core Capabilities

### 1. Time Series Classification

Categorize time series into predefined classes. See `references/classification.md` for selected methods.

**Quick Start:**
```python
from aeon.classification.convolution_based import RocketClassifier
from aeon.datasets import load_classification

# Load data
X_train, y_train = load_classification("GunPoint", split="train")
X_test, y_test = load_classification("GunPoint", split="test")

# Train classifier
clf = RocketClassifier(n_kernels=10000)
clf.fit(X_train, y_train)
accuracy = clf.score(X_test, y_test)
```

**Algorithm Selection:**
- **Speed + Performance**: `MiniRocketClassifier`, `Arsenal`
- **Accuracy candidates to validate**: `HIVECOTEV2`, `InceptionTimeClassifier`
- **Interpretability**: `ShapeletTransformClassifier`, `Catch22Classifier`
- **Small Datasets**: `KNeighborsTimeSeriesClassifier` with DTW distance

### 2. Time Series Regression

Predict continuous values from time series. See `references/regression.md` for algorithms.

**Quick Start:**
```python
from aeon.regression.convolution_based import RocketRegressor
from aeon.datasets import load_regression

X_train, y_train = load_regression("Covid3Month", split="train")
X_test, y_test = load_regression("Covid3Month", split="test")

reg = RocketRegressor()
reg.fit(X_train, y_train)
predictions = reg.predict(X_test)
```

### 3. Time Series Clustering

Group similar time series without labels. See `references/clustering.md` for methods.

**Quick Start:**
```python
from aeon.clustering import TimeSeriesKMeans

clusterer = TimeSeriesKMeans(
    n_clusters=3,
    distance="dtw",
    averaging_method="ba"
)
labels = clusterer.fit_predict(X_train)
centers = clusterer.cluster_centers_
```

### 4. Forecasting

Predict future time series values (experimental module in aeon 1.x). See `references/forecasting.md` for forecasters.

**Quick Start:**
```python
import numpy as np
from aeon.forecasting import NaiveForecaster
from aeon.forecasting.stats import ARIMA

y_train = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])

# predict returns one value at the configured horizon, not a 5-value vector
naive = NaiveForecaster(strategy="drift", horizon=5)
naive.fit(y_train)
y_at_5 = naive.predict(y_train)  # 15.0

# ARIMA uses p/d/q (not order=); multi-step via iterative_forecast
arima = ARIMA(p=1, d=1, q=1)
y_pred = arima.iterative_forecast(y_train, prediction_horizon=5)  # fits once
```

### 5. Anomaly Detection

Identify unusual patterns or outliers. See `references/anomaly_detection.md` for detectors.

**Quick Start:**
```python
from aeon.anomaly_detection.series.distance_based import STOMP

detector = STOMP(window_size=50)
anomaly_scores = detector.fit_predict(y)

# Higher scores indicate anomalies
threshold = np.percentile(anomaly_scores, 95)
anomalies = anomaly_scores > threshold
```

### 6. Segmentation

Partition time series into regions with change points. See `references/segmentation.md`.

**Quick Start:**
```python
from aeon.segmentation import ClaSPSegmenter

segmenter = ClaSPSegmenter()
change_points = segmenter.fit_predict(y)
```

### 7. Similarity Search

Find similar patterns within or a