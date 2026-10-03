---
name: optimize-for-gpu
description: GPU-accelerates scientific Python on NVIDIA hardware and verifies that the result is correct and faster. Use for CUDA/GPU optimization; CPU-bound NumPy, SciPy, pandas, scikit-learn, NetworkX, scikit-image, vector-search, image-processing, graph, simulation, or file-I/O workloads; CuPy, cuDF, cuML, cuGraph, cuVS, cuCIM, KvikIO, Warp, Newton, Numba-CUDA, or RAFT questions; and profiling, memory-transfer, kernel, or multi-GPU bottlenecks. Also use when large data-parallel Python code is slow and GPU acceleration is a plausible option, even if the user does not name CUDA.
license: MIT
compatibility: Requires an NVIDIA CUDA-capable GPU for GPU execution. RAPIDS 26.08 requires Python 3.11-3.14 on Linux or WSL2, NumPy 2, CuPy 14, and compatible CUDA 12 or 13 wheels. Package installation needs network access.
metadata: 
  version: 1.6
  last-reviewed: 2026-10-01
  skill-author: K-Dense, Inc.
---

# GPU Optimization for Python with NVIDIA

Treat GPU acceleration as an evidence-driven optimization, not an automatic rewrite. Preserve the
user's numerical and algorithmic contract, measure with representative data, and keep the GPU
version only when synchronized end-to-end benchmarks show a useful improvement.

Reviewed against RAPIDS 26.08, CuPy 14.2, Numba-CUDA 0.30.4, and Warp 1.17.
The references contain illustrative GPU examples: source/API review is not execution on CUDA
hardware. Validate them on the user's target GPU before reporting correctness or speedup.
Do not use a moving `latest` documentation page to infer compatibility with a pinned release.

## When This Skill Applies

- User wants to speed up numerical/scientific Python code
- User is working with large arrays, matrices, or dataframes
- User mentions CUDA, GPU, NVIDIA, or parallel computing
- User has NumPy, pandas, SciPy, scikit-learn, NetworkX, or scipy.sparse.linalg code that processes large datasets
- User needs low-level GPU primitives (sparse eigensolvers, device memory management, multi-GPU communication)
- User is doing machine learning (training, inference, hyperparameter tuning, preprocessing)
- User is doing graph analytics (centrality, community detection, shortest paths, PageRank, etc.)
- User is doing vector search, nearest neighbor search, similarity search, or building a RAG pipeline
- User has Faiss, Annoy, ScaNN, or sklearn NearestNeighbors code that could be GPU-accelerated
- User wants GPU-accelerated interactive dashboards, cross-filtering, or exploratory data analysis on large datasets
- User is doing geospatial analysis (point-in-polygon, spatial joins, trajectory analysis, distance calculations) with GeoPandas or shapely
- User is doing image processing, computer vision, or medical imaging (filtering, segmentation, morphology, feature detection) with scikit-image or OpenCV
- User is working with whole-slide images (WSI), digital pathology, microscopy, or remote sensing imagery
- User is loading large binary data files into GPU memory (numpy.fromfile → cupy, or Python open() → GPU array)
- User needs to read files from S3, HTTP, or WebHDFS directly into GPU memory
- User mentions GPUDirect Storage (GDS) or wants to bypass CPU-memory staging for file IO
- User is doing physics simulation (particles, cloth, fluids, rigid bodies) or differentiable simulation
- User needs mesh operations (ray casting, closest-point queries, signed distance fields) or geometry processing on GPU
- User is doing robotics (kinematics, dynamics, control) with transforms and quaternions
- User has Python simulation loops that could be JIT-compiled to GPU kernels
- User mentions NVIDIA Warp or wants differentiable GPU simulation integrated with PyTorch/JAX
- User is doing simulations, signal processing, financial modeling, bioinformatics, physics, or any compute-intensive work
- User wants to optimize existing code and GPU acceleration is the right answer

## Choose the Smallest Suitable Layer

Prefer a maintained library implementation over a custom kernel:

| Existing workload | Preferred path | Use for |
| --- | --- | --- |
| NumPy / SciPy | **CuPy** | arrays, sparse matrices, linear algebra, FFTs, signal processing |
| pandas | **cudf.pandas**, then **cuDF** | accelerator mode first; native API for more control |
| scikit-learn | **cuml.accel**, then **cuML** | accelerator mode first; native estimators as needed |
| NetworkX | **nx-cugraph**, then **cuGraph** | backend dispatch first; native graph API at scale |
| scikit-image | **cuCIM** | GPU image processing and whole-slide imaging |
| Faiss / Annoy / k-NN | **cuVS** | exact and approximate vector search |
| Raw or remote file I/O | **KvikIO** | GPU buffers and GPUDirect Storage |
| Custom array kernels | **Numba-CUDA-MLIR** for new work; **Numba-CUDA** for existing code | explicit SIMT kernels and shared memory |
| Spatial or differentiable kernels | **Warp** | geometry, simulation kernels, robotics, autodiff |
| High-level physics simulation | **Newton** | maintained engine that succeeds the removed `warp.sim` module |
| Low-level RAPIDS primitives | **RAFT** (`pylibraft`) | sparse eigensolvers, resources, multi-GPU building blocks |

Do not move code out of PyTorch, JAX, TensorFlow, or another GPU-native framework merely to use
one of these libraries. First remove CPU round trips and use the framework's compiler, profiler,
mixed-precision, and batching facilities.

Treat these as legacy-only:

| Project | Status | Guidance |
| --- | --- | --- |
| **cuxfilter** | Final release 26.06 | Maintain existing dashboards only. For new work, combine cuDF with HoloViews/hvPlot/Datashader and serve with Panel, Dash, Streamlit, or Bokeh. |
| **cuSpatial** | Archived at 25.04 | Use only in an isolated legacy environment. For new work, keep geometry in GeoPandas/Shapely and accelerate compatible tabular stages with cuDF. |

Full per-library guidance, including when each is the *wrong* choice and how to combine
them, is in [references/decision_framework.md](references/decision_framework.md).
Install commands and CUDA version selection are in
[references/installation.md](references/installation.md). Before/after conversions for
every library are in
[references/code_transformation_patterns.md](references/code_transformation_patterns.md).

## Optimization Workflow

### 1. Define the contract and baseline

- Capture a representative input, expected o