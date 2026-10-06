# Herbenzo Pipeline (Stage B · Modernizer)

A runnable Stage B modernizer. Every ingredient is researched live. There is no ingredient registry, no approved-ingredient store, and no enrichment cache between requests.

```
name ──▶ live research ──▶ approval (this request or a compose draft)
              │                      │
              │                      ▼
              │              POST /modernize  { spec, approvals }
              │                      │
              └─────────────▶ ModernizedSKU + research_provenance
                              + classical_active_marker_gap when a marker is missing
```

**`/modernize` is deterministic.** It does not call an LLM, NCBI, or PubChem. Numerics on a marker come from the PubChem block already on the approval. The independent UI is at `http://127.0.0.1:8003/`.

## What is not stored

Research responses, approvals, and PubChem rows are not written to a reusable ingredient list. Two requests for the same herb each call NCBI and PubChem again. A compose draft may hold the approval JSON for that draft only. That file is not a registry.

Request-local ids look like `tax-43366`. They match `FormulationSpec.ingredient_id` to the approval on this request. They are not keys in a stored table.

## HTTP API + UI (port 8003)

```bash
cd ~/Desktop/herbenzo_pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# herbenzo-contracts: local path (see requirements.txt) or the shared package
# Optional. Fills the gitignored IMPPAT cache used as advisory context.
python scripts/fetch_imppat.py

uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003
```

Open `http://127.0.0.1:8003/`.

### Compose

Free text is the primary ingredient input. On top of that field, a dropdown queries live NCBI Taxonomy, GBIF vernacular names, and Wikidata. It is a suggestion helper, not a stored list and not an approval. Those calls are not cached.

1. Set formulation id, product name, finished form, market, servings, and confidence.
2. Type a scientific or common name. After two characters the field waits about 300 ms, shows “Searching NCBI, GBIF, and Wikidata…”, and calls `GET /research/suggest?q=`. NCBI is searched on scientific name, common name, and synonym. GBIF is searched with `qField=VERNACULAR`. Wikidata uses entity search and SPARQL for labels, aliases, and taxon common names (P1843) in English, Hindi, Sanskrit, and Telugu, then reads the scientific name (P225) and NCBI taxon id (P685). If Gemini is configured, it may add candidate binomials for a traditional name. Every binomial is reconciled to an NCBI **species** before it is listed. Rows are merged by NCBI tax id. Each row shows which source suggested it (`NCBI common name`, `GBIF vernacular`, `Wikidata`, `Gemini web research`). Nothing is selected until you click a row. A common name that maps to several plants, such as Shankhpushpi, stays a list. If GBIF or Wikidata is down, the NCBI rows are still returned and `source_notes` says so. An empty or failed lookup returns HTTP 200 with `suggestions: []`.
3. **Research** posts `POST /research` for the scientific name in the field. A picked row also sends `name_sources` and `name_query`, so the approval records which catalog matched the common name. **Approve** posts `{ "candidate": ... }` to `POST /research/approve`. That approval stays on this formulation (`window.herbenzoApproved`) and in the draft file if you save. It is the document modernize uses.
4. **Run Modernize** posts `{ "spec": FormulationSpec, "approvals": [...] }`. A bare FormulationSpec with no approvals returns **422** `not_approved`.

**Load ashwagandha** prefills `Withania somnifera`. **Load Triphala** prefills `Terminalia chebula`. Neither injects an approved row. You still research and approve.

**Advanced / Raw JSON** posts the same envelope. Extra keys on a bare spec are still `contract_validation`.

**Drafts** live in `herbenzo/data/compose_drafts/<id>.json` (`HERBENZO_COMPOSE_DRAFTS_DIR` overrides that). `complete: true` requires a valid spec and a matching approval for every ingredient: species-rank taxonomy, literature status `ok`, stored PubMed count at or above `HERBENZO_MIN_PUBMED_REFS`, and status `approved`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Compose form, raw JSON, review panel |
| `GET` | `/health` | `llm` is always `false` (modernize does not call a model). `llm_config` reports whether research can call Gemini |
| `GET`/`POST` | `/research/suggest` | Live NCBI, GBIF, and Wikidata suggestions. Failures stay HTTP 200 with an empty list and a note |
| `POST` | `/research` | Live research for one name. Not saved |
| `POST` | `/research/approve` | Stateless approval of the posted candidate |
| `POST` | `/research/marker` | Set or replace a PubChem-verified marker on an approval |
| `POST` | `/modernize` | `{spec, approvals, marker_overrides?}`. Deterministic |
| `POST` | `/suggest-formats` | Advisory format ranking from the posted approvals |
| `GET`/`POST` | `/drafts` | List or save a compose draft, including its approvals |
| `GET`/`DELETE` | `/drafts/{id}` | Load or delete one draft |

`GET /ingredients` and the enrich routes are gone.

## Evidence gate

Approval needs both of these. A marker is not required.

- NCBI Taxonomy resolves the name to **species** rank. A miss or a genus hit is `identity_unresolved`.
- The relevance PubMed query stores at least `HERBENZO_MIN_PUBMED_REFS` articles that have a PMID and a title (default **1**). The count is the stored articles, not the raw hit count. Below that, or if PubMed is unavailable, the code is `insufficient_evidence`.

The literature query is the scientific name in Title/Abstract plus pharmacology, mechanism, pathway, constituents, or phytochemistry, with `sort=relevance`.

An ingredient that has not been approved on this request is `not_approved`. The detail includes `ingredient_ids` and a plain message. It does not wrap the id in extra quotes.

## Markers

Marker discovery runs after identity and literature. It uses constituent names from titles and abstracts, an advisory IMPPAT phytochemical list when that cache exists, and both a quoted and an unquoted `pccompound` search. For *Clitoria ternatea* the quoted term returns 0 and the unquoted term returns six FDA UNII ternatin CIDs.

Name lookup is not trusted blindly. Bare `ternatin` resolves to CID 5459184 (a methoxyflavone from other genera) and CID 192406 (a fungal cyclic peptide). Neither is attached. A name with several CIDs is kept only when exactly one of them has a PubChem taxonomy or LOTUS link to the target species. Otherwise the marker stays ambiguous and is not auto-selected. UNII-style titles such as `VW8J4G7G7W` are stored as candidates and are not auto-selected.

Useful *C. ternatea* markers when the approver names them, or when a single species-linked record is unambiguous:

- Ternatin A1, CID 16173494. LOTUS links it to *C. ternatea*. XLogP is missing. Missing XLogP does not block approval or modernization. The BCS note says XLogP was not reported and the solubility confidence is capped.
- Clitorin, CID 11592917. Physchem includes XLogP. Literature quantifies it at 764 mg/kg dry flower (PMID 39525386).

Delphinidin 3-glucoside (443650), rutin (5280805), and taraxerol (92097) occur in many taxa. They are not treated as a specific butterfly-pea marker.

`POST /research/marker` is the strict path. The name must resolve to exactly one CID that is linked to the species. Ambiguous or unlinked names return `marker_unverified`. The call appends a `marker_audit` entry on the approval document the caller sent. It does not write a store.

`/modernize` itself does not look up PubChem. A `marker_overrides` entry that is only a name, without a verified PubChem block (`pubchem_cid`, molecular weight, TPSA, HBD, HBA, rotatable bonds), returns `marker_unverified` and tells the caller to use `POST /research/marker` first. XLogP on that block is optional.

An approved ingredient with no marker is not a failure. `/modernize` still returns a full SKU. Each unmarked ingredient is `marker_status: pending` and unstandardized: marker properties, BCS class, and delivery technology stay empty rather than invented. The response adds `warnings` such as `marker_pending: <botanical name>` and `classical_active_marker_gap` (the code name is unchanged; it is no longer limited to classical dosage forms). The gap includes `release: requires marker before release` and how to add one: `POST /research/marker` with this approval and a specific `marker_name`, then modernize again. Downstream claims say that ingredient requires a marker before release. Those claims do not block SKU generation and do not lower confidence. Mixed formulas keep marker-backed chemistry and flag the rest on the same SKU.

`/suggest-formats` accepts marker-less approvals. Their solubility is `unknown`. Solubilizing formats are penalized only when every profile has solubility `high`.

The berberine P-gp efflux flag is a classifier rule. It is honored only when the approval snapshot already includes it. Live research does not invent that flag.

### Provenance

`research_provenance`, `warnings`, and `classical_active_marker_gap` sit beside the ModernizedSKU. `herbenzo-contracts` `ModernizedSKU` uses `extra="forbid"`, so those fields are not inside the shared model. The snapshot records taxonomy id, PMIDs, CIDs, URLs, retrieval times, PubChem numerics, the approval decision, and `name_match` (the query, scientific name, tax id, and which source produced the name). When the Ayush portal was on for that research, the same snapshot lists each hit's ARP id, PMID, DOI, URL, confidence, and review status. It is part of the response, not a database. A pending-marker SKU is still returned with that provenance.

## Ayush Research Portal

Off by default (`HERBENZO_AYUSH_PORTAL_ENABLED=false`). When enabled, research adds bibliographic hits from [arp.ayush.gov.in](https://arp.ayush.gov.in) beside the PubMed block. Abstracts and email addresses are dropped. The client is throttled, retries only on HTTP 5xx, and does not use the disk cache. See `data/external/ayush_portal/LICENSE_NOTICE.md`.

`POST /enrich/ayush/{arp_id}/accept` writes a reviewer note to `ayush_reviews.json` under `HERBENZO_REGISTRY_DIR`. That file is an accept note, not an ingredient registry.

```bash
python -m herbenzo.cli ayush search "Withania somnifera" --system ayurveda --limit 5
python -m herbenzo.cli ayush record ARP_AYU030864
python -m herbenzo.cli ayush accept ARP_AYU030906 --note "journal checked"
```

## Research front door

`POST /research` goes through `ResearchFrontDoor` when Gemini is configured. The model plans tool calls. The candidate is assembled only from tool results.

Tools: NCBI Taxonomy search, PubMed search/summary, PubChem name→CID, properties, and taxonomy links, species compound search (quoted and unquoted), IMPPAT lookup when the local cache exists, an optional web search/fetch, and `ayush_portal_search`. `web_fetch` only opens URLs that `web_search` returned in the same session. If `HERBENZO_WEB_SEARCH_ENDPOINT` is unset, web search returns `unavailable` and research continues. `ayush_portal_search` runs only when `HERBENZO_AYUSH_PORTAL_ENABLED=true` (default false). A disabled switch omits `ayush_portal` from the candidate. A portal failure is `status: unavailable` and PubMed research continues.

Guardrails:

- The tool loop stops at `HERBENZO_RESEARCH_MAX_STEPS` (default 8) or `HERBENZO_RESEARCH_TIMEOUT_S` (default 60).
- PMIDs, CIDs, and URLs in the narrative that were not in a tool response are dropped and listed in `ignored_claims`.
- Physicochemical numbers in model text are stripped. Stored descriptors come from PubChem tool results.
- The model does not approve anything. Approval is a separate call.
- A missing key, a bad Gemini URL or model, a timeout, or an exception falls back to the deterministic NCBI/PubChem path. `research_path` is `deterministic`, `gemini`, or `deterministic_fallback`.

`GET /health` keeps `"llm": false` so callers can see that modernize does not call a model. `llm_config.status` is `ok`, `unconfigured`, or `invalid`, with a message. A Gemini host with no path (`https://generativelanguage.googleapis.com`) is invalid because that URL returns 404. The OpenAI-compatible base is `https://generativelanguage.googleapis.com/v1beta/openai`. `gemini-4.0-argon` is not a known model. `gemini-2.5-flash` is.

## Butterfly pea

*Clitoria ternatea* (NCBI taxid 43366, species, Fabaceae) is the worked example. Quoted `pccompound` is empty, so approval does not depend on a marker. With at least one stored PubMed article the candidate can be approved with `marker_status: pending`. Modernize then returns a full SKU with that ingredient `marker_status: pending`, `warnings: ["marker_pending: Clitoria ternatea"]`, the gap flag, and provenance. Naming Ternatin A1 on `POST /research/marker` attaches CID 16173494, clears the pending flag, and a later modernize emits a chemistry-backed SKU. Missing XLogP on that record does not block it. Naming bare `ternatin` does not attach a marker.

Mechanism references used in the fixtures: PMID 26120869 (ternatins inhibit NF-κB/iNOS), 18926895, 14568080, 34975979, 21214440, 12490229.

Tests, all mocked, no live network:

- `test_clitoria_quoted_search_is_empty_and_bare_ternatin_is_not_attached`
- `test_clitoria_pending_marker_modernizes_with_provenance_and_gap`
- `test_suggestion_still_has_to_pass_the_evidence_gate`
- `test_gemini_front_door_strips_hallucinations_and_uses_tool_numbers`
- `test_withania_is_researched_not_loaded_from_stock`
- `test_suggest_is_live_and_empty_on_failure`
- `test_unapproved_research_does_not_modernize`
- `test_compose_approve_posts_the_candidate_and_modernizes_it`
- `test_butterfly_pea_suggests_every_taxon_and_picks_none`
- `test_shankhpushpi_lists_every_species_and_picks_none`
- `test_name_source_outage_keeps_the_other_results`
- `test_non_species_and_unreconcilable_names_are_dropped`
- `test_gemini_binomials_are_kept_only_after_ncbi_species_confirmation`
- `test_gemini_loop_cap_and_fallback`

## Trade-offs

Live research adds latency and depends on NCBI, PubChem, GBIF, Wikidata, and optionally Gemini. NCBI rate limits apply (3 requests/second without `NCBI_API_KEY`, 10 with a key). GBIF and Wikidata are called without an API key, with a short gap between requests and an 8 second timeout (6 seconds for Wikidata SPARQL). If one of those hosts is down, suggestions still return the sources that answered, plus `source_notes`. Evidence can differ from run to run because PubMed ranking and tool choices are not frozen. The provenance snapshot on the modernize response records what that request actually used, including which source matched the name, so a SKU can be audited without a registry. Research HTTP clients do not write the shared response cache (`cache_enabled=False`), and common-name lookups are not cached either. Adjudication still uses the PubMed PMID cache.

## CLI

```bash
export NCBI_EMAIL="you@yourdomain.com"
export NCBI_API_KEY="..."     # optional
python -m herbenzo.cli research "Clitoria ternatea"
python -m herbenzo.cli suggest "butterfly pea"
python -m herbenzo.cli research-approve candidate.json --marker "Ternatin A1"
python -m herbenzo.cli run envelope.json -o out/report.json
```

`run` accepts `{ "spec", "approvals", "marker_overrides" }` or a bare FormulationSpec. A bare spec exits 2 with `not_approved`. `--offline` skips literature network calls during adjudication only.

`suggest-formats` takes an approvals JSON file first, then ingredient ids:

```bash
python -m herbenzo.cli suggest-formats approvals.json tax-43366 \
  --dosage-form capsule --product-name "Butterfly pea"
```

| Command | Purpose |
|---|---|
| `research "<name>"` | Live research. Prints JSON. Does not save it |
| `research-approve <file>` | Approve that document. Optional `--marker` |
| `suggest "<text>"` | Live taxonomy suggestions |
| `run <envelope.json>` | Modernize, then adjudicate |
| `adjudicate` | One claim against one PMID |
| `suggest-formats <approvals.json> <id>…` | Advisory format ranking |
| `ayush search "<query>"` | Bibliographic ARP search. Disabled unless the portal switch is on |
| `ayush record <ARP_ID>` | One ARP record page |
| `ayush accept <ARP_ID>` | Store a reviewer accept note. Does not create an ingredient |

## Contracts impact

Shared `herbenzo-contracts` models are unchanged. `FormulationSpec.ingredient_id` stays a string. `FormulationSpec` and `ModernizedSKU` still use `extra="forbid"`, so the modernize body is a pipeline-local envelope:

```json
{ "spec": { "...FormulationSpec..." }, "approvals": [], "marker_overrides": [] }
```

`research_provenance`, `warnings`, and `classical_active_marker_gap` are siblings of the SKU, not fields on the shared model. Ayush hits, when the portal was enabled, live on that provenance snapshot. No change to `FormulationSpec`, `IngredientSpec`, or `ModernizedSKU` was required.

A SKU whose every marker is resolved still passes the shared `ModernizedSKU` gate. Pipeline-local annotations (`marker_status`, `standardization`) are stripped before that check, then `marker_status: resolved` is added back on the HTTP body. A SKU with any pending marker is pipeline-local and is not forced through the shared descriptor schema: the shared model requires a PubChem marker, a BCS class, and a delivery technology, and those stay empty on purpose when no marker exists.

## What the pipeline still enforces

1. Typed handoff contracts (`herbenzo/schemas/contracts.py` and the shared package).
2. Confidence may only fall.
3. Citation adjudication on every claim in the CLI report.
4. A search that returns nothing is a declared gap, not a silent fill.
5. Retracted sources are rejected.
6. Per-run manifest with retrieval dates.
7. Stable `CLM-…` claim ids.
8. No uncited numeric fold-change.
9. A missing marker is advisory. `/modernize` still returns the SKU. `classical_active_marker_gap` and `warnings` name each pending ingredient. Release claims say a marker is required. The gap does not 422 and it does not lower confidence.

## The adjudicator

`AdjudicationService.adjudicate(claim, subject, pmid, subject_aliases, claim_domain)` returns a verdict with its reason. The default path does not call a model.

| Reason code | Meaning |
|---|---|
| `subject_absent` | Record is real but is not about this subject → **reject** |
| `wrong_claim_domain` | Cited as safety, but reports no tolerability/toxicity data → **reject** |
| `safety_signal_weak` | Safety words in the abstract body only, not title/MeSH → **partial** |
| `retracted_source` | → **reject** |
| `claim_topic_absent` | Right subject, none of the claim's terms → **reject** |
| `partial_subject_only` | Weak topical overlap → **partial** |
| `no_abstract` | Cannot assess automatically → **partial**, manual review |
| `supported` | Subject and claim terms both present → **support** |

A safety claim is never auto-supported on an abstract-only mention.

```bash
python -m herbenzo.cli adjudicate --pmid 37257749 \
    --subject "Terminalia bellirica" --alias bibhitaki \
    --claim "well tolerated on oral administration" --domain safety
```

## IMPPAT 3.0

IMPPAT stays advisory. It never blocks identity or approval. After NCBI Taxonomy resolves a species, a local cache can add Sanskrit/IAST names, plant parts, formulation context, and phytochemical identifiers. Phytochemical public ids are not sent to PubChem name lookup.

On approval of a **matched** hit, Sanskrit, synonyms, a single standardised part (when the request did not name one), license `CC BY-NC-ND 4.0`, and the three citations are copied onto that approval document. They are not written to an overlay file. `no_match` does not invent a Sanskrit name.

Set `HERBENZO_IMPPAT_DIR=off` to skip the lookup. Populate the cache with `python scripts/fetch_imppat.py`. The notice is in [data/external/imppat/](data/external/imppat/LICENSE_NOTICE.md). Do not commit the TSVs.

## Layout

| Path | Role |
|---|---|
| `herbenzo/services/research.py` | Deterministic live research and approval |
| `herbenzo/services/gemini_research.py` | Gemini tool loop and fallback |
| `herbenzo/services/records.py` | Request snapshot, provenance, marker override checks |
| `herbenzo/services/research_extract.py` | Constituent and pathway extraction, citation stripping |
| `herbenzo/services/llm_config.py` | Gemini URL and model checks |
| `herbenzo/research_api.py` | `/research` routes |
| `herbenzo/services/imppat.py` | Optional local IMPPAT context |
| `herbenzo/components/modernizer/` | BCS, delivery, orchestrator. Reads the approval snapshot only |
| `herbenzo/services/classical_marker_gap.py` | Gap flag for any approved ingredient with no marker |
| `herbenzo/format_suggestions.py` | Advisory format ranking from approvals |
| `herbenzo/static/` | Compose UI: free text, live suggest, research, approve |
| `tests/fixtures/` | Former stock rows and marker properties, for chemistry tests only |
| `tests/legacy_snapshot.py` | Builds a snapshot from those fixtures. Runtime does not load them |
| `herbenzo/data/compose_drafts/` | Drafts, including approvals (gitignored) |

Removed from the runtime path: `herbenzo/services/registries.py`, `overlay.py`, `candidate_store.py`, `live_registries.py`, `enrichment.py`, `herbenzo/enrich_api.py`, and `herbenzo/data/marker_properties.json` (copied to `tests/fixtures/`).

## Tests

```bash
pytest -q
```

Network is mocked. Chemistry cases for the old stock herbs go through `tests/legacy_snapshot.py`. Runtime code does not read `tests/fixtures/`.

## Known limitations

- PubChem `503 PUGREST.ServerBusy` can still happen on a live call. Parsing is tested offline.
- Claim overlap is lexical. It will not catch a paper that reports the opposite result.
- Descriptor BCS misses some lattice-limited solubilities. Ellagic acid is the known example.
- Berberine Class IV in the fixture tests depends on an efflux flag stored on the fixture snapshot. Live research does not set that flag.
- Ternatin records have no XLogP and a molecular weight above 500, so solubility is called low and confidence is capped. They are identity markers, not a claim of systemic absorption.
- The API Aparajita monograph covers *C. ternatea* root. Flower markers (ternatin A1, clitorin) are the blue-petal standardisation choice, not that root monograph.

## Not included

Components A, C, and E, authentication, and per-country regulatory content. Compose drafts are local JSON for this form only.
