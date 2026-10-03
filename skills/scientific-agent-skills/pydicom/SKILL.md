---
name: pydicom
description: Reads, inspects, writes, transforms, and preflights local DICOM datasets and pixel data. Applies to DICOM metadata, transfer syntaxes, compression plugins, frames, private elements, JSON, and bounded de-identification review.
license: MIT
compatibility: Python 3.12+ for the tested pixel stack; pydicom 3.0.2 alone needs Python 3.10+; optional pinned NumPy, Pillow, and pixel plugins. Helper CLIs are local-only and require authorized data.
metadata: 
  version: 1.4
  skill-author: K-Dense Inc.
  last-reviewed: 2026-10-01
---

# pydicom

Use pydicom for DICOM dataset I/O and pixel processing. Version 3.0.2 is the
current stable release reviewed here. It fixes CVE-2026-32711, a crafted
DICOMDIR path-traversal issue. pydicom 3.0.2 declares Python `>=3.10`; its
bundled DICOM dictionary is 2024c; the current DICOM Standard reviewed here is
2026d. No PACS, DIMSE, DICOMweb endpoint, or authenticated service is exercised
by this local-file skill.

## Mandatory safety boundary

- Work only with local data that the user is authorized to access.
- DICOM metadata, file names, private elements, overlays, structured content,
  and pixels may contain protected health information (PHI).
- Never print `Dataset`, export full metadata/JSON, or log element values by
  default. Use a documented allowlist and aggregate output.
- pydicom is a general DICOM framework, not a diagnostic viewer. Pixel output,
  validation, conversion, and plugin availability are not diagnostic claims.
- De-identification is profile-, purpose-, recipient-, jurisdiction-, and
  threat-context-specific. It requires privacy/DICOM expert verification.
- Never claim that a tag-removal script is DICOM PS3.15, HIPAA, GDPR, or other
  compliance. Preserve originals and audit derived outputs.
- Treat deterministic pseudonymization keys and UID maps as re-identification
  secrets: use least privilege and encrypted/managed secret storage, never
  commit, sync, log, or share them with derivatives, and define backup,
  rotation, revocation, and destruction procedures. A leaked key invalidates
  the intended separation; rotation also changes deterministic mappings.
- Set explicit input-file, file-count, frame-count, decoded-byte, and output
  limits before parsing untrusted or unusually large datasets.

## Installation

Create or activate an isolated environment, then install the reviewed release
(the pixel-stack pins below require Python 3.12+ because of NumPy):

```bash
uv pip install "pydicom==3.0.2"
```

Uncompressed pixel arrays and image rendering:

```bash
uv pip install "pydicom==3.0.2" "numpy==2.5.3" "Pillow==12.3.0"
```

Install only the transfer-syntax plugins required by the deployment:

```bash
# JPEG/JPEG-LS, JPEG 2000/HTJ2K, and faster RLE through pylibjpeg
uv pip install "numpy==2.5.3" "pylibjpeg==2.1.0" \
  "pylibjpeg-libjpeg==2.4.0" "pylibjpeg-openjpeg==2.6.0" \
  "pylibjpeg-rle==2.2.0"

# JPEG-LS encoder/decoder
uv pip install "numpy==2.5.3" "pyjpegls==1.5.1"

# Alternative decoder with platform-specific wheels
uv pip install "python-gdcm==3.2.6"
```

Plugin licenses and wheels differ by package/platform; review them before
deployment. Pillow has documented decoding limitations and pydicom cautions
that plugin output must be independently checked.

Native codec wheels widen the supply-chain and memory-safety boundary. For a
controlled deployment, resolve these exact pins on a trusted build host, lock
and verify wheel hashes/provenance, mirror approved artifacts internally, scan
them, and install with hash enforcement rather than resolving from the public
index at runtime.

## Choose the workflow

1. Need an aggregate overview: run `scripts/extract_metadata.py`.
2. Need bounded technical checks: run `scripts/dicom_inventory.py`.
3. Need codec deployment preflight: run
   `scripts/transfer_syntax_inspector.py`.
4. Need frame/memory planning: run `scripts/pixel_frame_planner.py`.
5. Need one non-diagnostic rendered frame: run
   `scripts/dicom_to_image.py`.
6. Need a pseudonymized derivative: read the de-identification section, create
   a site-reviewed action profile, then run `scripts/anonymize_dicom.py` and
   `scripts/deidentification_audit.py`.
7. Need to check a sensitive UID map: run
   `scripts/uid_mapping_validator.py`.

## Read datasets safely

`dcmread()` returns a `FileDataset`, a `Dataset` subclass with File Format
state such as `file_meta`, preamble, and original encoding.

```python
from pathlib import Path
import pydicom

path = Path("authorized/input.dcm")
ds = pydicom.dcmread(
    path,
    stop_before_pixels=True,
    specific_tags=[
        "SOPClassUID",
        "Modality",
        "Rows",
        "Columns",
        "NumberOfFrames",
    ],
)

technical = {
    "sop_class": ds.get("SOPClassUID"),
    "modality": ds.get("Modality"),
    "rows": ds.get("Rows"),
    "columns": ds.get("Columns"),
}
```

Use:

- `stop_before_pixels=True` for metadata-only work.
- `specific_tags=[...]` for a minimum allowlist.
- `defer_size="1 MiB"` when a later write must preserve large values.
- `force=False` (default). `force=True` only bypasses the File Format header
  check; it does not prove the bytes are valid DICOM.

Do not call `print(ds)`, `repr(ds)`, or iterate values into logs on clinical
data.

## Dataset, DataElement, and sequences

Access standard elements by keyword and check for absence:

```python
modality = ds.get("Modality", "UNSPECIFIED")
if "ReferencedImageSequence" in ds:
    for item in ds.ReferencedImageSequence:
        referenced_class = item.get("ReferencedSOPClassUID")
```

Tag access, such as `ds[0x0010, 0x0010]`, returns a `DataElement`; its `.value`
is separate. `Sequence` behaves like a list of nested `Dataset` items. Privacy
actions must recurse through every sequence item, not only the top level.

When creating a file, use `FileMetaDataset` for group `0002`, keep dataset and
file-meta SOP UIDs consistent, set a Transfer Syntax UID, and write in enforced
File Format:

```python
from pydicom import dcmwrite
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

meta = FileMetaDataset()
meta.MediaStorageSOPClassUID = CTImageStorage
meta.MediaStorageSOPInstanceUID = generate_uid()
meta.TransferSyntaxUID = ExplicitVRLittleEndian

ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" *