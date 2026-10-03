---
name: tamarind
description: Provides access to a collection of open-source molecular design and structural biology tools on the Tamarind Bio platform, via its REST API or MCP server — no local GPUs required. Tamarind bundles popular open-source models for structure prediction (AlphaFold, Boltz, Chai, ESMFold), protein, binder, and de novo design (RFdiffusion, ProteinMPNN, BoltzGen), antibody and nanobody design and developability, protein-ligand docking (DiffDock, Autodock Vina), binding-affinity prediction, MSA generation, and molecular dynamics. Use when the user mentions Tamarind or tamarind.bio, wants to run any of these open-source tools in the cloud, references app.tamarind.bio/api or the x-api-key header, or needs to submit batches of sequences for structural or biophysical characterization.
license: MIT
compatibility: Requires Python 3.10+, a Tamarind Bio account, and an API key from app.tamarind.bio. Uses the `requests` library against the public REST API (these recipes use HTTP directly). Network access required. Optional MCP server at mcp.tamarind.bio/mcp for agent hosts.
metadata: 
  version: 1.3
  last-reviewed: 2026-09-30
  skill-author: Tamarind Bio
  trigger-keywords: protein structure prediction, AlphaFold, Boltz, Chai, ESMFold, protein design, binder design, de novo design, antibody design, nanobody, protein-ligand docking, DiffDock, Autodock Vina, binding affinity, MSA generation, inverse folding, ProteinMPNN, RFdiffusion, BoltzGen, cloud GPU biology, structure prediction API, x-api-key, developability, adme, enzyme, peptide, protein language models, molecular design
  openclaw: 
    primaryEnv: TAMARIND_API_KEY
    envVars: 
    - name: TAMARIND_API_KEY
      required: true
      description: Tamarind Bio API key sent as the x-api-key header.
---

# Tamarind Bio

Tamarind runs molecular-design and structural-biology tools on managed compute:
structure prediction, protein and antibody design, docking, binding-affinity
prediction, MSA generation, and molecular dynamics. Use it when the user requests
Tamarind, its REST API/MCP server, or cloud execution of these scientific tools.
For local sequence processing or molecular descriptors, use a local library.

## Sources and review scope

The REST contracts and public catalog were reviewed on **2026-09-30**. Examples are
illustrative until validated against the user's account; this review did not run
authenticated jobs or establish scientific accuracy for any model.

- [API index](https://app.tamarind.bio/llms.txt) and
  [complete guide](https://app.tamarind.bio/llms-full.txt).
- [Current OpenAPI](https://app.tamarind.bio/api/openapi.json) includes discovery,
  validation, jobs, files, and newer pipeline/custom-tool surfaces.
  [openapi.yaml](https://app.tamarind.bio/openapi.yaml) is also available. Check that
  needed paths exist: the merged spec can return HTTP 200 with only its classic
  surface when the backend spec cannot be fetched.
- [Public catalog](https://app.tamarind.bio/tools.json) needs no key; query `?type=`
  or `?tag=`. It documents public tools and conditional required settings, not
  every optional parameter or account entitlement.
- [Product documentation index](https://docs.tamarind.bio/llms.txt) links to
  Markdown pages, including the [MCP guide](https://docs.tamarind.bio/tamarind/mcp-server.md).

Fetch the account's current schemas before composing a run. Where the prose guide
and OpenAPI differ, prefer the operation/schema for field shapes, and record any
unresolved difference rather than guessing.

## Access

1. Use the user's Tamarind deployment. The shared base is
   `https://app.tamarind.bio/api`; a dedicated organization deployment has its own
   host and account data. Every relative REST path below is under `/api`.
2. Obtain a key from the deployment's API settings and read it from
   `TAMARIND_API_KEY`; send it as `x-api-key`. Keep keys out of files and logs.
3. Check the account's current allowance and compute budget before scaling up.
   Free usage is a monthly allowance, not an unconditional promise of ten jobs
   forever; billing and entitlements can change.

```bash
# Public discovery requires no credential.
curl --fail-with-body 'https://app.tamarind.bio/tools.json?type=alphafold'
# Account-scoped discovery:
curl --fail-with-body 'https://app.tamarind.bio/api/tools' \
  -H "x-api-key: $TAMARIND_API_KEY"
```

For REST examples install `requests` in the execution environment. The official
CLI distribution is **`tamarind-cli`**, and its Custom Tools Python client imports
as `from tamarind import Tamarind`; the unrelated package named `tamarind` is not
this client. See the [SDK reference](https://app.tamarind.bio/api-docs/custom-tools-sdk-reference).
Core job recipes below use HTTP directly.

## Workflow

1. **Discover.** Read `GET /tools` and match the user's scientific task to the tool
   description. Built-ins return an array; `?custom=true` lists legacy custom
   tools only. Current custom deployments can be missing from this list: use the
   known deployed name and its schema before concluding that it is unavailable.
2. **Read the schema.** `GET /tools/{name}/schema` returns a JSON Schema for the
   `settings` object. `GET /tools` also supplies a trimmed `settings` parameter
   list. Check task-dependent fields, file extensions, list values, and defaults.
3. **Validate.** Send `POST /validate-job` with `type`, `settings`, and optional
   `jobName`. Check HTTP status first, then JSON `valid`. On success, inspect and
   use `normalized` as the settings to submit. Address `unrecognized_settings`
   if returned, even alongside `valid: true`: an optional-field typo can otherwise
   silently leave the default in effect. Validation checks fields, not all submit
   policies, queue limits, or deployment readiness.
4. **Submit once.** `POST /submit-job` takes `jobName`, `type`, `settings`, optional
   `version` for a custom-tool build, and optional/organization-required
   `projectTag`. Persist the submitted name and settings. A successful response
   is plain text, not a JSON receipt. Use the returned stored name.
5. **Poll.** `GET /jobs?jobName=...` returns a row directly. Single-job terminal
   states are `Complete`, `Stopped`, and `Failed`; handle legacy `Deleted` or an
   exact-lookup error without looping indefinitely. Poll batch parents using
   `batchStatus`, and poll newer pip