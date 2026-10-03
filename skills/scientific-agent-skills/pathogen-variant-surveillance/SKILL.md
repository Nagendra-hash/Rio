---
name: pathogen-variant-surveillance
description: Queries public GenSpectrum LAPIS data for pathogen genomic surveillance, current lineage nomenclature, weekly sequence proportions, reporting delays, and descriptive mutation frequencies. Use for variant surveillance, Pango lineage validation, dominant submitted lineages, Nextclade assignment provenance, SARS-CoV-2, influenza/H5N1 clades, RSV, mpox, measles, dengue, or LAPIS queries. Distinguishes sequence prevalence from infection prevalence, clades from genotypes, missing calls from reference matches, and sampling changes from biological growth advantage.
license: MIT
compatibility: Requires Python 3.11+. Scripts use only the standard library. Needs network access to public LAPIS deployments on lapis.cov-spectrum.org, lapis.genspectrum.org, lapis.pathoplexus.org and raw.githubusercontent.com for pango-designation. No credentials for these public queries.
allowed-tools: Read Write Edit Bash
metadata: 
  version: 1.3
  skill-author: K-Dense Inc.
  last-reviewed: 2026-10-01
---

# Pathogen Variant Surveillance

## When to use

Use current data when a question depends on which lineages appear in submitted sequences,
what a lineage name currently means, or how the submitted sequence distribution changed.
Never answer a current circulation question from remembered lineage names or old examples.

This skill supports descriptive surveillance research. Counts describe sequences submitted to
one database under stated filters; they are not case counts, infection prevalence, clinical
interpretations, outbreak recommendations, or evidence of enhanced pathogen function.

## Verified scope

Reviewed on 2026-10-01 against official documentation, live schemas for all 15 registered
instances, and small public queries. SARS-CoV-2 served LAPIS 0.8.7/SILO 0.14.3; the other
registered deployments served LAPIS 0.8.0/SILO 0.11.0. Do not assume identical feature support.
Bundled standard-library scripts have synthetic regression tests and bounded live smoke checks.
Nextclade, GenoFLU, authenticated APIs, and sequence-level assay validation are not executed here.

| Instance | Host | Common lineage field |
| --- | --- | --- |
| `sars-cov-2` | lapis.cov-spectrum.org/open/v2 | `pangoLineage` (indexed) |
| `h5n1` | lapis.genspectrum.org/h5n1 | `clade` (unindexed) |
| `h3n2`, `h1n1pdm` | lapis.genspectrum.org/<name> | `cladeHA` / `cladeNA` (unindexed) |
| `influenza-a` | lapis.genspectrum.org/influenza-a | `subtypeHA` / `subtypeNA` |
| `rsv-a`, `rsv-b`, `mpox`, `measles`, `dengue`, `west-nile`, `hmpv`, `ebola-zaire`, `ebola-sudan`, `cchf` | lapis.pathoplexus.org/<name> | inspect the schema |

The scripts read `/sample/databaseConfig`. A lineage-index value is currently an identifier
string, not necessarily a boolean. Only indexed fields support descendant `NAME*` queries.
Use `--lineage-field` deliberately when several naming systems coexist. Unknown unindexed
values can return zero; that does not verify the name or prove biological absence.

When available, defaults select `versionStatus=LATEST_VERSION`, `isRevocation=false`, and
`dataUseTerms=OPEN`. These are printed with the result and can be overridden explicitly with
`--where`. Open access to an endpoint is not a blanket data-use license; preserve source
attribution and the applicable [Pathoplexus terms](https://pathoplexus.org/about/terms-of-use).

## Workflow

```bash
cd skills/pathogen-variant-surveillance/scripts
```

1. Inspect the instance and choose collection date, lineage system, geography, host and data
   inclusion rules. Country fields differ: SARS-CoV-2/GenSpectrum use `country`; Pathoplexus
   uses `geoLocCountry`. Inspect actual categories before choosing a value.
2. Review reporting delays before choosing the prevalence window.
3. Discover common labels in that window, then verify names in the relevant nomenclature.
4. Report counts, denominators, intervals, snapshot version, dates and exclusions together.

### Describe observed reporting delay

```bash
python3 reporting_lag.py --where country=USA --cohorts 6 --skip-months 3
```

This groups by **both collection and submission/release dates**, calculates each date difference,
and reports `mean_observed`, `min_observed`, `max_observed` and contributing cohort count.
It excludes missing dates, unequal collection-date range bounds, negative lags and submissions
after `--until`. Long offsets use only cohorts old enough to contribute that follow-up.

The result is a CDF conditional on records visible now. It cannot establish eventual completeness,
a trustworthy date, or when a record first appeared in LAPIS. `--until` sets an analysis anchor;
it does not retrieve an earlier database snapshot. Cohorts receive equal weight, not weight
proportional to sequence count. The contributing cohort set can vary by offset.

### Discover and describe weekly proportions

```bash
python3 lineage_prevalence.py --top 5 --where country=USA --weeks 12
```

Discovery ranks exact nonempty labels; `unassigned` remains a real category. The denominator
includes all selected records, including unassigned/null lineage calls. Overlapping descendant
queries must not be summed. Explicit lineage examples below illustrate syntax, not current dominance:

```bash
python3 lineage_prevalence.py "XFG*" --where country=USA --weeks 16 --growth --lag-days 90
```

Here `90` is an illustrative user-selected exclusion horizon, not a universal measured lag.
The window expands to whole ISO weeks and the output states the expanded dates. Weeks ending
within `--lag-days` of today, the current partial week, zero-count weeks, and weeks below the
chosen older-half count threshold are flagged `low`. Other weeks are not certified complete.
Growth fits exclude flagged weeks unless `--include-incomplete` is explicit.

For collection fields ending `RangeLower`, weekly and lag analyses require the corresponding
`RangeUpper` and exclude unequal bounds. The exclusion count covers returned records; date
range filters can already exclude null dates, so it is not a database-wide missing-date count.
Upstream imputation or inaccurate metadata cannot be detected from declared date types alone.

Proportions use Wilson intervals for binomial sampling uncertainty only. `--growth` fits a
weighted descriptive log-odds slope, with at least five observed sequences in three nonempty
weeks and dispersion floor