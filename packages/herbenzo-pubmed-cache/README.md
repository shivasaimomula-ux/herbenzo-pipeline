# herbenzo-pubmed-cache

Shared **PubMed / PMID disk cache** for the Herbenzo federated pipeline
(Audit Finding **#14** / Execution Plan **T16**).

```text
A / B / C / adjudication  ──read-through──►  HERBENZO_PUBMED_CACHE_DIR
                                              (pmid_<id>.json envelopes)
```

This package is a **shared library**, not a UI and not an NCBI client by itself.
Stage B remains the evidence-layer owner; this module extracts the cache
behaviour so other stages do not invent a second on-disk format.

## Why

Each of A, B, C, and the blue adjudication service was able to EFetch the same
PMID independently. Caching once (with a freshness stamp) cuts duplicate NCBI
spend and the LLM calls that follow from re-hydrating the same abstracts.

## Install

```bash
# editable (canonical tree lives inside Stage B)
pip install -e "/Users/shivasaimomula/Desktop/herbenzo_pipeline/packages/herbenzo-pubmed-cache[dev]"

# Desktop convenience copy (kept in sync for local discovery):
#   ~/Desktop/herbenzo-pubmed-cache

# or from GitHub (Stage B repo, subdirectory)
# pip install "herbenzo-pubmed-cache @ git+https://github.com/shivasaimomula-ux/herbenzo-pipeline.git#subdirectory=packages/herbenzo-pubmed-cache"
```

Requires Python ≥ 3.11. **No third-party dependencies** (stdlib only).

## Env

| Variable | Default | Meaning |
|----------|---------|---------|
| `HERBENZO_PUBMED_CACHE_DIR` | `<cwd>/cache/pubmed` | Shared directory all stages should share |
| `HERBENZO_PUBMED_CACHE_TTL_S` | `2592000` (30 days) | Freshness window; `0` = never expire |

Point every stage at the **same** absolute directory, for example:

```bash
export HERBENZO_PUBMED_CACHE_DIR="$HOME/Desktop/herbenzo_shared_cache/pubmed"
export HERBENZO_PUBMED_CACHE_TTL_S=2592000
```

## API

```python
from herbenzo_pubmed_cache import PmidDiskCache

cache = PmidDiskCache()  # respects env, or pass cache_dir= / ttl_seconds=

# miss
assert cache.get_pmid("37257749") is None

# after NCBI fetch elsewhere:
cache.put_pmid("37257749", {"pmid": "37257749", "title": "...", "abstract": "..."})

# hit
art = cache.get_pmid("37257749")

print(cache.stats.as_dict())  # hits / misses / expired / writes
```

On-disk file: `pmid_37257749.json` (same basename convention as Stage B).

Envelope (`herbenzo.pmid_cache/v1`):

```json
{
  "schema": "herbenzo.pmid_cache/v1",
  "cached_at": "2026-09-21T01:00:00+00:00",
  "ttl_seconds": 2592000,
  "payload": { "pmid": "37257749", "title": "..." }
}
```

Legacy bare article JSON files written by older Stage B clients still **hit**
when within TTL (freshness from file mtime).

## Wired consumers (T16)

| Stage | How |
|-------|-----|
| **B** `herbenzo_pipeline` | `PubMedClient` uses `PmidDiskCache` for search + fetch |
| **A** `ayurvedic-predictor` | `predictor.connectors.pubmed` read-through before EFetch |
| **Adjudication** `:8011` | `herbenzo_adjudication.pubmed.PubMedClient` read-through |

## Tests

```bash
cd ~/Desktop/herbenzo-pubmed-cache
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```
