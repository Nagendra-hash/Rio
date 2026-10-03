---
name: neuropixels-analysis
description: Analyzes Neuropixels extracellular recordings end-to-end with SpikeInterface. Covers loading SpikeGLX/Open Ephys/NWB data, preprocessing, drift/motion correction, Kilosort4 (and CPU) spike sorting, quality metrics, and unit curation (threshold-based, model-based UnitRefine, and AI-assisted visual review). Use when working with Neuropixels 1.0/2.0 recordings, spike sorting, or extracellular electrophysiology analysis.
license: MIT license
compatibility: Requires Python 3.10+ with SpikeInterface, ProbeInterface, Neo, NumPy, SciPy, pandas, matplotlib, numba and scikit-learn. Optional sorters and models need separate dependencies and network access.
metadata: 
  version: 2.6
  last-reviewed: 2026-10-01
  skill-author: K-Dense Inc.
  openclaw: 
    primaryEnv: ANTHROPIC_API_KEY
    envVars: 
    - name: ANTHROPIC_API_KEY
      required: false
      description: For optional Claude API calls.
---

# Neuropixels Data Analysis

## Overview

Toolkit for analyzing Neuropixels high-density neural recordings using current best
practices from [SpikeInterface](https://spikeinterface.readthedocs.io/), the Allen
Institute, and the International Brain Laboratory (IBL). It covers the full workflow from
raw data to reviewed, curated units. Targets SpikeInterface 0.105.0,
ProbeInterface 0.4.0 and Neo 0.14.5 (reviewed 2026-10-01). Synthetic tests cover
recording contracts, preprocessing, analyzers, metrics and exports; real acquisition
files, native sorters, GPU execution and pretrained models remain illustrative.

All examples use the real SpikeInterface API (`spikeinterface.full as si`) plus the
companion curation module (`spikeinterface.curation as sc`). The skill ships runnable
scripts in `scripts/` and a copy-and-edit template in `assets/` that implement this
workflow directly on top of SpikeInterface — there is no separate package to install
beyond the dependencies listed under [Installation](#installation).

## When to Use This Skill

This skill should be used when:
- Working with Neuropixels recordings (`.ap.bin`, `.lf.bin`, `.meta` files)
- Loading data from SpikeGLX, Open Ephys, or NWB formats
- Preprocessing neural recordings (filtering, common reference, bad-channel detection)
- Detecting and correcting motion/drift
- Running spike sorting (Kilosort4, SpykingCircus2, Mountainsort5, Tridesclous2)
- Computing quality metrics (SNR, ISI violations, presence ratio, amplitude cutoff)
- Curating units (threshold-based, model-based, or AI-assisted)
- Creating visualizations and exporting to Phy or NWB

## Supported Hardware & Formats

| Probe | Electrodes | Channels | Notes |
|-------|-----------|----------|-------|
| Neuropixels 1.0 | 960 | 384 | Use acquisition ADC timing metadata |
| Neuropixels 2.0 (single) | 1280 | 384 | Verify part number and timing metadata |
| Neuropixels 2.0 (4-shank) | 5120 | 384 | Multi-region recording |

| Format | Extension | Reader |
|--------|-----------|--------|
| SpikeGLX | `.ap.bin`, `.lf.bin`, `.meta` | `si.read_spikeglx()` |
| Open Ephys | `.continuous`, `.oebin` | `si.read_openephys()` |
| NWB | `.nwb` | `si.read_nwb()` |

## Quick Start

### Import and configure parallel processing

```python
import spikeinterface.full as si

# Global job kwargs are reused by all parallelizable steps
si.set_global_job_kwargs(n_jobs=-1, chunk_duration="1s", progress_bar=True)
```

### Loading data

```python
# Inspect available streams first
stream_names, stream_ids = si.get_neo_streams("spikeglx", "/path/to/run_g0/")
print(stream_names)  # e.g. ['imec0.ap', 'imec0.lf', 'nidq']

# SpikeGLX (most common) — select the AP stream by name
recording = si.read_spikeglx("/path/to/run_g0/", stream_name="imec0.ap")

# Open Ephys
recording = si.read_openephys("/path/to/Record_Node_101/")

# For quick iteration, slice the first 60 s
fs = recording.get_sampling_frequency()
recording_sub = recording.frame_slice(0, min(int(60 * fs), recording.get_num_samples()))
```

### Full pipeline (bundled script)

The repository ships an end-to-end pipeline built on SpikeInterface:

```bash
python scripts/neuropixels_pipeline.py /path/to/spikeglx/data output/ --sorter kilosort4 --curation allen
```

It performs load → preprocess → drift check → optional motion correction → sorting →
postprocessing → quality metrics → curation → export. Phy and the report retain all
units for review with curation labels; `sorting_curated/` contains selected good units. Read the steps below to run them
interactively or customize the pipeline.

## Standard Analysis Workflow

### 1. Preprocessing

Validate AP stream, calibration, channel order, probe geometry and segment boundaries
first. This filter/reference chain is not full IBL destriping. Apply ADC timing
correction only from valid acquisition metadata and reference each shank separately:

```python
rec = si.highpass_filter(recording, freq_min=400.0)
bad_channel_ids, channel_labels = si.detect_bad_channels(rec)
rec = rec.remove_channels(bad_channel_ids)
rec = si.phase_shift(rec)  # Requires valid inter_sample_shift property.
rec = si.common_reference(rec, operator="median", reference="global")  # Single shank only.
```

For multiple shanks, use the bundled `reference_by_shank` helper described in
[PREPROCESSING.md](references/PREPROCESSING.md). Trace arrays are samples × channels;
`get_traces(return_in_uV=True)` needs calibrated gain/offset. The bundled commands
reject uncalibrated, empty or multisegment input instead of guessing.

Cache preprocessed data when storage and repeated use justify it:

```python
rec = rec.save(folder="preprocessed/", format="binary")
```

### 2. Check and correct drift

Always inspect drift before sorting:

```python
from spikeinterface.sortingcomponents.peak_detection import detect_peaks
from spikeinterface.sortingcomponents.peak_localization import localize_peaks

noise_levels = si.get_noise_levels(rec, return_in_uV=False)
peaks = detect_peaks(rec, method='locally_exclusive', method_kwargs={'noise_levels': noise_levels, 'detect_threshold': 5, 'radius_um': 50.0})
peak_locations = localize_peaks(rec, peaks, method="center_of_mass")

# Visualize the drift raster
si.plot_drift_raster_map(peaks=peaks, peak_locations=peak_locations,
                         recording=rec, clim=(-50, 50))
```

Apply correction if needed (presets: `rigid_fast`, `kilosort_like`,
`nonrigid_accurate`, `nonrigid_fast_and