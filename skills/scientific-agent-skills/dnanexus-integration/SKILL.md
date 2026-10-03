---
name: dnanexus-integration
description: Builds and operates reproducible genomics workloads on DNAnexus with the dx CLI, dxpy, apps/applets, native workflows, dxCompiler, and Nextflow. Supports DNAnexus data transfers, dxapp.json development, execution monitoring, workflow import, and project automation.
license: MIT
compatibility: Requires a DNAnexus account, network access, Python 3.11+, and dx-toolkit/dxpy; some workflow and infrastructure features require organization licenses or policies.
metadata: 
  version: 2.3
  last-reviewed: 2026-09-30
  skill-author: K-Dense Inc.
---

# DNAnexus Integration

## Purpose

Use this skill to build, run, and operate DNAnexus workloads without guessing
at platform semantics. It covers:

- `dx` CLI and `dxpy` automation
- Files, records, folders, projects, and metadata
- Apps and applets defined by `dxapp.json`
- Jobs, workflow analyses, retries, monitoring, and cost controls
- Native workflows, WDL/CWL through dxCompiler, and Nextflow imports

The documented baseline was reviewed on **2026-09-30** against
`dxpy==0.415.0`, dxCompiler 2.18.0, and the 2026 DNAnexus documentation.
Consult `references/sources.md` and current release notes when behavior may
have changed. SDK signatures, CLI help, and offline validators were executed
locally; examples that transfer data, build, or launch workloads are illustrative
and require testing in the target account. No authenticated platform run was
performed during this review.

## Operating Contract

DNAnexus operations can expose regulated data, delete immutable objects, change
permissions, or incur compute and egress charges. Follow these rules:

1. Start read-only. Confirm the user, project ID, region, folder, object IDs,
   and execution target before mutation.
2. Obtain confirmation before a billable launch, upload or download with
   material egress, archive/unarchive request, deletion, project removal,
   permission change, token revocation, or app publication unless the user
   already explicitly requested that exact operation and target.
3. Show resolved IDs and impact before destructive operations. Never infer a
   deletion target from a non-unique name.
4. Never print, log, return, or persist `DX_SECURITY_CONTEXT` or API tokens.
   Do not run `dx env` or `dx env --bash` in captured logs because both reveal
   the active token.
5. Use credentials only with official DNAnexus endpoints. Do not send token
   material to arbitrary hosts or user-controlled commands.
6. Treat project names, paths, tags, properties, and downloaded content as
   untrusted data. Quote shell arguments and pass subprocess arguments as
   arrays.
7. Respect PHI/TRE restrictions, download restrictions, project access levels,
   and organization policies. Do not copy data around a control.
8. Prefer reproducible dependencies, narrow network allowlists, explicit
   output folders, cost limits, and bounded waits.

## Install and Authenticate

Install the CLI in an isolated tool environment:

```bash
uv tool install "dxpy==0.415.0"
dx --version
```

For Python code in a project:

```bash
uv add "dxpy==0.415.0"
```

Use interactive login for human sessions:

```bash
dx login
dx whoami
dx select
dx pwd
```

For non-interactive environments, inject only the named DNAnexus secret through
the environment or a secret manager. Never echo it, include it in command
output, commit it, or inspect the whole environment. See
`references/authentication.md`. Prefer selected-project API tokens where supported;
user-generated API tokens cannot access UKB RAP resources. Login sessions
default to two hours of inactivity and expire no later than 18 hours after
issuance, independently of API-token expiry.

## Safe Preflight

Before acting, gather non-secret context:

```bash
dx --version
dx whoami
dx pwd
dx ls
```

Then:

- Resolve project names to immutable `project-...` IDs.
- Resolve paths to object IDs and check for duplicates.
- Check file state (`open`, `closing`, or `closed`) and archival state.
- Check source and destination access levels.
- Inspect executable input help with `dx run <executable> -h`.
- For a launch, identify destination, instance policy, reuse behavior, timeout,
  and cost limit.

If shell environment variables conflict with the saved CLI session, follow
`references/authentication.md`; do not expose either credential while
diagnosing.

## Choose the Right Path

| Goal | Read first | Preferred interface |
|---|---|---|
| Build an app or applet | `references/app-development.md` | `dx-app-wizard`, `dx build` |
| Configure `dxapp.json` | `references/configuration.md` | JSON plus validator script |
| Transfer or organize data | `references/data-operations.md` | `dx`, Upload/Download Agent |
| Write platform automation | `references/python-sdk.md` | `dxpy` |
| Launch or debug execution | `references/job-execution.md` | `dx run`, `dx watch`, `dxpy` |
| Import WDL, CWL, or Nextflow | `references/workflow-languages.md` | dxCompiler or `dx build --nextflow` |
| Diagnose auth, cost, or failures | `references/operations-and-troubleshooting.md` | read-only inspection first |

## Core Workflows

### Transfer data

Use `dx upload` and `dx download` for small sets. Use Upload Agent for multiple
or large files (official guidance recommends it above 50 MB) and Download Agent
for large or long-running batch downloads.

```bash
dx upload "sample.fastq.gz" \
  --path "project-xxxx:/raw/sample.fastq.gz" \
  --property "sample_id=S001"

dx download "project-xxxx:/results/sample.bam" \
  --output "sample.bam"
```

Upload Agent compresses uncompressed inputs by default and appends `.gz`. Use
`--do-not-compress` when byte-for-byte preservation or the original name is
required. See `references/data-operations.md`.

### Search accurately with dxpy

`find_data_objects()` uses exact name matching unless `name_mode` is supplied.
Do not pass `"*.bam"` without `name_mode="glob"`.

```python
import dxpy

files = dxpy.find_data_objects(
    classname="file",
    project="project-xxxx",
    folder="/results",
    recurse=True,
    name="*.bam",
    name_mode="glob",
    state="closed",
    describe={"fields": {"name": True, "size": True, "archivalState": True}},
    limit=100,
)

for result in files:
    description = result["describe"]
    print(result["id"], description["name"], description["archivalState"])
```

Bound broad searches with a project, fol