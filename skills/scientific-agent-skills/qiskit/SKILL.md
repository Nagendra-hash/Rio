---
name: qiskit
description: Builds, simulates, transpiles, and executes quantum circuits with Qiskit and IBM Quantum Runtime. Use for Qiskit 2.x circuits and operators, V2 Sampler or Estimator primitives, target-aware transpilation, local or noisy simulation, IBM QPU execution, Runtime sessions or batches, error mitigation, and Qiskit ecosystem packages.
license: Apache-2.0
compatibility: Python 3.10+ on a supported 64-bit platform. Local SDK workflows need qiskit; noisy simulation needs qiskit-aer; IBM QPU access needs qiskit-ibm-runtime, network access, an IBM Quantum Platform account, and an API key.
metadata: 
  version: 2.3
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
---

# Qiskit

Use current Qiskit 2.x APIs to build circuits, prepare hardware-compatible instruction set architecture (ISA) circuits, and execute them through V2 primitives.

Reviewed **2026-10-01** with local tests on `qiskit==2.5.2`, `qiskit-ibm-runtime==0.50.0`, and `qiskit-aer==0.17.2`. IBM account, QPU, session, and batch examples are illustrative and documentation/source-checked; no remote workloads were submitted. See [references/sources.md](references/sources.md) for the verification boundary.

Runtime 0.50 deprecates the top-level `SamplerV2`/`EstimatorV2` implementations. New code imports `Sampler` from `qiskit_ibm_runtime.executor_sampler` and `Estimator` from `qiskit_ibm_runtime.executor_estimator`. These still implement the V2 PUB interface, using client-side processing and `Executor`. Use option models from `qiskit_ibm_runtime.options_models`.

## Choose the Right Path

| Goal | Recommended interface |
|---|---|
| Ideal evolution with finite-shot sampling | `qiskit.primitives.StatevectorSampler` |
| Exact local expectation values | `qiskit.primitives.StatevectorEstimator` |
| High-performance or noisy simulation | Qiskit Aer |
| IBM QPU sampling | `qiskit_ibm_runtime.executor_sampler.Sampler` |
| IBM QPU expectation values and mitigation | `qiskit_ibm_runtime.executor_estimator.Estimator` |
| Backend without native primitives | `BackendSamplerV2` or `BackendEstimatorV2` |
| Open-system or master-equation dynamics | Prefer QuTiP |
| Differentiable quantum machine learning | Prefer PennyLane unless Qiskit integration is required |

## Installation

Create an isolated environment and install only the components needed:

```bash
uv venv --python 3.13
source .venv/bin/activate

# Core SDK plus plotting support
uv pip install "qiskit[visualization]==2.5.2"

# Add only when needed
uv pip install "qiskit-ibm-runtime==0.50.0"
uv pip install "qiskit-aer==0.17.2"
```

Do not install `qiskit-terra`; it was superseded by the `qiskit` distribution. Qiskit Runtime, Aer, Nature, Machine Learning, Optimization, and Algorithms are separate distributions.

For IBM account setup, CI-safe credential handling, optional packages, and environment repair, read [references/setup.md](references/setup.md).

## Core Workflow

Follow this sequence for every hardware-oriented workload:

1. **Map** the problem to a circuit and, for Estimator, one or more observables.
2. **Optimize** the parameterized circuit once for the selected backend.
3. **Apply the layout** to every observable.
4. **Execute** ISA circuits through a V2 primitive using Primitive Unified Blocs (PUBs).
5. **Analyze** register-aware results, metadata, uncertainty, and resource usage.

Do not bind and retranspile a parameterized circuit inside every optimizer iteration. Transpile the parameterized circuit once, then pass parameter arrays in PUBs.

## Quick Local Sampling

```python
from qiskit import QuantumCircuit
from qiskit.primitives import StatevectorSampler

circuit = QuantumCircuit(2)
circuit.h(0)
circuit.cx(0, 1)
circuit.measure_all()  # creates the classical register named "meas"

sampler = StatevectorSampler(seed=7)
pub_result = sampler.run([circuit], shots=1024).result()[0]
counts = pub_result.data.meas.get_counts()
print(counts)
```

Sampler V2 preserves shots and classical-register structure. Access the register by its actual name; `measure_all()` uses `meas`.

For circuits with multiple classical registers, each register’s counts are a marginal distribution. Preserve shot alignment when computing cross-register correlations; multiplying marginal frequencies destroys those correlations. Use `SamplerPubResult.join_data` with an explicit register order for joint bitstrings and record that order in the result labels. Qiskit 2.5.2 puts the first joined `BitArray` register in the least-significant bits, contrary to the current docstring; verify with an asymmetric state. See [references/primitives.md](references/primitives.md).

## Quick Local Estimation

```python
import numpy as np
from qiskit import QuantumCircuit
from qiskit.circuit import Parameter
from qiskit.primitives import StatevectorEstimator
from qiskit.quantum_info import SparsePauliOp

theta = Parameter("theta")
circuit = QuantumCircuit(2)
circuit.ry(theta, 0)
circuit.cx(0, 1)

observable = SparsePauliOp.from_list([("ZZ", 1.0), ("XX", 0.5)])
parameter_values = [[0.0], [np.pi / 4], [np.pi / 2]]

estimator = StatevectorEstimator(seed=7)
pub = (circuit, observable, parameter_values)
pub_result = estimator.run([pub]).result()[0]
print(pub_result.data.evs)
```

Estimator circuits should not contain final measurements. PUB arrays broadcast; verify circuit parameter order before constructing large sweeps.

## IBM QPU Sampling

This example assumes credentials were saved securely as described in [references/setup.md](references/setup.md). It never embeds or prints an API key.

```python
from qiskit import QuantumCircuit
from qiskit.transpiler import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService
from qiskit_ibm_runtime.executor_sampler import Sampler

service = QiskitRuntimeService()
backend = service.least_busy(
    operational=True,
    simulator=False,
    min_num_qubits=2,
)

circuit = QuantumCircuit(2)
circuit.h(0)
circuit.cx(0, 1)
circuit.measure_all()

pass_manager = generate_preset_pass_manager(
    backend=backend,
    optimization_level=1,
    seed_transpiler=7,
)
isa_circuit = pass_manager.run(circuit)

sampler = Sampler(mode=backend)
job = sampler.run([isa_circuit], shots=1024)
print("job_id:", job.job_id())
counts = job.result()[0].data.meas.get_counts()
```

Save the job ID before waiting for results so the job can be retrieved later.

##