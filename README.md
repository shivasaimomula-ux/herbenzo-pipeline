# Herbenzo Pipeline (Stage B · Modernizer)

A runnable implementation of the reasoning core from the v2 target architecture:
**Component B (Modernizer)** plus the two shared services it depends on —
**Evidence Retrieval & Cache** and the **Citation Adjudication Service** — wired
together behind a CLI and an HTTP API on **port 8003**.

```
FormulationSpec ──▶ Component B ──▶ ModernizedSKU ──▶ adjudicated report
                        │                                    ▲
                        ├── Registries & Normalization        │
                        ├── Evidence Retrieval & Cache ───────┤
                        └── Citation Adjudication ────────────┘
```

**Deterministic — no LLM on the modernize path.** Independent B UI lives at
`http://127.0.0.1:8003/` (Task T10). Glue A→B→C is **Task T13**.

---

## HTTP API + UI (port 8003)

```bash
cd ~/Desktop/herbenzo_pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# herbenzo-contracts: local Desktop path (see requirements.txt) or
#   pip install "git+https://github.com/shivasaimomula-ux/herbenzo-contracts.git"
# Optional. Fills the gitignored IMPPAT cache used after NCBI Taxonomy.
python scripts/fetch_imppat.py

uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003
```

Open the **independent Stage B UI** in a browser:

```text
http://127.0.0.1:8003/
```

**Compose** (default tab) builds a FormulationSpec without hand-editing JSON. `herbenzo-contracts` `FormulationSpec.ingredients` is already a list, so a polyherbal formula (Triphala, Chyawanprash) is one product name plus many registry rows. This repo does not add a `role` field; the shared ingredient object has part, amount, extract ratio, and marker.

1. Set formulation id, product name (the formula, not a single herb), finished form (preset or any free text), market, servings per day, and confidence.
2. Search the registry and add every herb. The picker reads **only** `GET /ingredients` (stock rows plus approved overlay rows). Each selected row has its own part, amount (mg per serving), extract ratio, and standardization marker. Pending enrichment candidates are not in that list.
3. The **FormulationSpec preview** is the JSON that will be posted, including every selected `ingredient_id`.
4. **Run Modernize** calls the existing `POST /modernize` and renders the ModernizedSKU. Advisory flags on the response (including `classical_active_marker_gap`, if a response includes it) show as a banner. They are not errors. Any unknown ingredient id still returns **422** (`unknown_ingredient`) and the response names that id.

**Advanced / Raw JSON** is the previous paste-or-upload path.

**Drafts.** Name the form and **Save draft** before or after modernize. **Load** continues editing; **Delete** removes one. Drafts survive a page reload. Files live on the service box (no database):

```text
herbenzo/data/compose_drafts/<id>.json
```

Override that directory with `HERBENZO_COMPOSE_DRAFTS_DIR`. Incomplete specs can be saved. A draft reported `complete: true` has passed the FormulationSpec gate and uses registry ingredient ids, so it can be posted to `/modernize`.

This UI is Stage B only — not embedded in C/E. It does not change the Modernizer engine.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Independent B modernize UI (compose form + raw JSON) |
| `GET` | `/static/*` | UI assets (CSS/JS) |
| `GET` | `/health` | Stage B health JSON (`ui: available`) |
| `GET` | `/ingredients` | Stock registry rows the modernizer already uses |
| `GET` | `/drafts` | List named compose drafts |
| `POST` | `/drafts` | Create a draft, or update one when `id` is sent |
| `GET` | `/drafts/{id}` | Load one draft (`spec` is the saved form) |
| `DELETE` | `/drafts/{id}` | Delete a draft |
| `POST` | `/modernize` | Body: `FormulationSpec` (herbenzo-contracts) → `ModernizedSKU` |
| `POST` | `/enrich/propose` | Resolve a species into a **pending** evidence bundle. Not added to the registry |
| `GET` | `/enrich/candidates` | List candidates (`?status=pending\|approved\|rejected`) |
| `GET` | `/enrich/candidates/{id}` | Evidence bundle, including chemical taxonomy |
| `POST` | `/enrich/candidates/{id}/approve` | Promote one PubChem-backed marker to a curated `HB-*` overlay row |
| `POST` | `/enrich/candidates/{id}/reject` | Record a rejection. The id stays unknown to Stage B |

```bash
curl -s http://127.0.0.1:8003/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8003/modernize \
  -H 'content-type: application/json' \
  -d @examples/ashwagandha.json | python3 -m json.tool
```

Unknown fields / raised confidence floors → **422** with `herbenzo-contracts`
`validation_error_body`. Unknown registry ingredient IDs → **422**
(`unknown_ingredient`). A classical preparation with no registry marker does
**not** 422: `POST /modernize` returns 200 and, when any ingredient still has
a marker, the ModernizedSKU plus `classical_active_marker_gap`. When none do,
the body is `{ "sku": null, "classical_active_marker_gap": { ... } }`.
---

## CLI quick start

```bash
cd ~/Desktop/herbenzo_pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export NCBI_EMAIL="you@yourdomain.com"     # NCBI asks clients to identify themselves
export NCBI_API_KEY="..."                  # optional; raises 3 → 10 requests/second
# Or: cp .env.example .env  (Task T24 — never commit .env)

python -m herbenzo.cli run examples/ashwagandha.json -o out/report.json
```

### Commands

| Command | Purpose |
|---|---|
| `run <spec.json> [-o out.json] [--offline]` | Full pipeline; writes an auditable report |
| `adjudicate --pmid … --subject … --claim … [--domain safety]` | Adjudicate a single citation |
| `markers` | List registry ingredients (stock plus approved overlay), markers and PubChem CIDs |
| `enrich propose "<species or common name>" [--part root]` | Store a pending enrichment candidate |
| `enrich list [--status pending]` | List candidates |
| `enrich show <candidate-id>` | Print one evidence bundle |
| `enrich approve <candidate-id> [--marker NAME]` | Promote a candidate into the registry overlay |
| `enrich reject <candidate-id> [--reason TEXT]` | Reject a candidate |

`--offline` uses only cached descriptors and makes no network calls — use it for
CI and for reproducible golden-set runs.

### Tests

```bash
pytest -q
```

---

## What the pipeline actually enforces

These are the behaviours the architecture review identified as missing. Each is
implemented and tested, not asserted.

1. **Typed handoff contracts.** Pydantic v2 with `extra="forbid"`. A malformed
   handoff raises; it does not propagate. (`herbenzo/schemas/contracts.py`)
2. **Confidence floor.** Confidence may only fall: A → B → adjudication. A
   rejected or partial citation lowers the run's floor. Raising it is a
   `ConfidenceFloorViolation`.
3. **Citation adjudication on every claim.** Not "is this PMID real" but "does
   this study support *this* claim about *this* subject".
4. **Absence is a declared result.** A search returning zero records is recorded
   in `declared_gaps` and the claim is downgraded to `unsupported` — never
   silently dropped or filled in.
5. **Retraction blocking.** A retracted source is rejected outright.
6. **Per-run manifest.** Pipeline and engine version, retrieval dates,
   `literature_current_as_of`, and whether the run was offline.
7. **Stable claim IDs.** Every claim gets a deterministic `CLM-…` ID so a
   statement in a downstream document resolves back to its source and verdict.
8. **No uncited numbers.** `BioavailabilityEvidence` rejects a `fold_change`
   without PMID, evidence tier and model system.
9. **Classical preparations without an active marker are advisory.** If the
   dosage form or product name is a classical Ayurvedic preparation (decoction,
   lehya, bhasma, churna, and the other form names in
   `herbenzo/services/classical_marker_gap.py`) **and** an ingredient's registry
   row has no standardization marker, the pipeline report includes
   `classical_active_marker_gap` beside `sku`. The indicator does not raise,
   does not return 422, and does not lower the confidence floor. Marker-backed
   ingredients are still modernized and adjudicated. No marker chemistry is
   invented to fill the gap. `herbenzo-contracts` does not yet declare this
   field; it stays outside the validated ModernizedSKU.

---

## The adjudicator

`AdjudicationService.adjudicate(claim, subject, pmid, subject_aliases, claim_domain)`
returns a verdict with its reason. Fully deterministic — no model call in the
default path.

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

**A safety claim can never be auto-supported on an abstract-only mention.** A
record that genuinely reports tolerability declares it in the title or MeSH; if
it does not, the best available verdict is `partial` with manual review. This
gate exists because of a real observed failure — a crop-fertilizer study cited
as botanical oral-safety evidence, with a genuine PMID and a matching DOI.

Verified live against that case:

```
$ python -m herbenzo.cli adjudicate --pmid 37257749 \
    --subject "Terminalia bellirica" --alias bibhitaki \
    --claim "well tolerated on oral administration" --domain safety

  verdict: reject   reason_code: subject_absent
```

---

## Ingredient enrichment

The stock registry stays the curated `HB-*` table. Enrichment grows it only after a person approves a candidate.

1. **Propose.** `POST /enrich/propose` with a species or common name. NCBI Taxonomy resolves the accepted Latin binomial first. If a local IMPPAT 3.0 cache is present, that binomial and the NCBI synonyms are looked up for Ayurvedic context (see below). The lookup is advisory and never fails the proposal. Candidate marker CIDs come from PubChem (E-utilities `pccompound` search, then PUG-REST properties). PubMed, Gene, and Protein supply citations and organism-linked records. Chemical taxonomy is attached per marker: ClassyFire/ChemOnt kingdom → superclass → class → subclass → direct parent (PubChem classification when it is complete, otherwise the keyless ClassyFire API by InChIKey) and NP Classifier pathway / superclass / class from GNPS when a SMILES string is present. A missing classification is stored as `unavailable`; it does not fail the proposal.
2. **Enrich.** Physicochemical numbers (molecular weight, XLogP, TPSA, H-bond counts, rotatable bonds, CID) are copied from PubChem with source URL and retrieval time. The optional LLM only ranks the PubChem marker names and writes a justification. If `HERBENZO_LLM_API_KEY` is unset, that step is `unavailable` and the candidate is still stored. Numeric keys in an LLM payload are discarded.
3. **Gate.** The bundle is a pending JSON file under `herbenzo/data/registry_overlay/candidates/` (`HERBENZO_REGISTRY_DIR` overrides the directory). It does not appear in `GET /ingredients`. Submitting its proposed id to `POST /modernize` returns **422** `unknown_ingredient`.
4. **Commit.** Approve assigns the proposed `HB-*` id and writes `approved/<id>.json`, including the PubChem descriptor record Stage B needs. A matched IMPPAT block is copied onto that row (Sanskrit/IAST name, synonyms, a single standardised part when the request did not name one, and AFI/API formulation context) with source and citation. A missing or ambiguous IMPPAT hit is not copied and does not block approval. Reject records the decision and does not create a row. After approval, Compose and Stage B treat the row like a stock ingredient.

NCBI E-utilities and PubChem PUG-REST work with no API key (3 requests/second). Set `NCBI_API_KEY` to use 10 requests/second. `NCBI_EMAIL` and `NCBI_TOOL` are sent when set. Copy `.env.example` to `.env` at the repo root; process environment variables win over that file. Do not commit `.env`.

### IMPPAT 3.0 (after NCBI, when the local cache exists)

Herbenzo Ayurvedic and Herbal Pvt Ltd is MSME-registered. The IMPPAT team confirmed by email on 6 October 2026 that an MSME entity can use IMPPAT without formal approval, so the lookup runs whenever `data/external/imppat/cache/` (or `HERBENZO_IMPPAT_DIR`) contains the batch files. Set `HERBENZO_IMPPAT_DIR=off` to skip it. Populate the cache with `python scripts/fetch_imppat.py`. The notice and citation list are in [data/external/imppat/](data/external/imppat/LICENSE_NOTICE.md).

Validation order: NCBI Taxonomy resolves the accepted binomial. Only then, and only when local files are available, IMPPAT adds Ayurvedic context to the enrichment candidate: Sanskrit/IAST ingredient names, original and standardised plant parts, AFI/API formulation context, family, common names, Latin synonyms, and linked phytochemical identifiers when that table is present. The block is stored on the candidate as `imppat` with `source` `IMPPAT 3.0`, the file name, and the file timestamp. The lookup is **non-blocking**. `matched`, `no_match`, `ambiguous`, and `unavailable` all leave the candidate pending and approvable. A missing hit does not return 422. Unknown ingredient ids submitted to `POST /modernize` still return 422, and `GET /ingredients` still lists stock rows plus approved overlay rows only.

On approval of a **matched** candidate, those Ayurvedic fields are copied onto the overlay row together with the CC BY-NC-ND 4.0 source and the three IMPPAT citations. Sanskrit/IAST becomes `sanskrit_name` (further names join synonyms). If the request did not name a plant part and IMPPAT names exactly one standardised part, that part is stored. Formulation context stays on the overlay `imppat` object. An ambiguous or missing hit is not copied.

Enable it by placing these TSVs in `data/external/imppat/cache/`:

- `Plant_Information_IMPPAT.tsv`
- `IMPPAT_SingleHerbalFormulations.tsv`
- `IMPPAT_PolyHerbalFormulations.tsv`
- `IMPPAT_Phytochemical_Plant_Association.tsv` (optional)

The plant table has no Sanskrit column. Sanskrit/IAST names are read from the API formulation title and the AFI ingredient title. Headers were checked against the 30 September 2026 batch files; the loader also accepts `standardized` spellings and ignores unknown columns. An empty, missing, or unreadable cache is `unavailable`.

IMPPAT is licensed **CC BY-NC-ND 4.0**. Attribute IMPPAT and cite the papers. Do not commit or redistribute the batch files (the cache directory is gitignored). The 6 October 2026 email covers use, not redistribution; keep it on file.

The **Review candidates** tab is separate from the Compose picker. It does not add unapproved herbs to the formulation.

## Layout

| Path | Role |
|---|---|
| `herbenzo/schemas/contracts.py` | Handoff contracts, confidence floor, citation guards |
| `herbenzo/clients/pubmed.py` | NCBI E-utilities — search, fetch; shared PMID cache (`herbenzo-pubmed-cache`) |
| `herbenzo/clients/pubchem.py` | PubChem PUG-REST — CID resolution and descriptors |
| `herbenzo/clients/eutils.py` | Enrichment E-utilities client (taxonomy, PubMed, gene, protein, pccompound) with NCBI rate limits |
| `herbenzo/clients/chemclass.py` | ClassyFire/ChemOnt and NP Classifier lookups, cached |
| `herbenzo/clients/llm.py` | Optional OpenAI-compatible justification. Skipped when no key is set |
| `herbenzo/services/enrichment.py` | Propose → enrich → approve/reject |
| `herbenzo/services/imppat.py` | Optional local IMPPAT 3.0 context after NCBI Taxonomy |
| `herbenzo/config.py` | Loads repo-root `.env` without overriding existing environment variables |
| `herbenzo/services/evidence.py` | Evidence store + manifest stamps over the shared cache |
| `herbenzo/services/adjudication.py` | Citation adjudication service |
| `herbenzo/services/registries.py` | Ingredient identity, markers, dose normalization |
| `herbenzo/services/live_registries.py` | Cached descriptors with live PubChem fallback |
| `herbenzo/components/modernizer/` | BCS classifier, delivery recommender, orchestrator |
| `herbenzo/pipeline.py` | End-to-end runner and report builder |
| `herbenzo/cli.py` | Command line |
| `herbenzo/api.py` | FastAPI: UI at `/`, `GET /health`, `GET /ingredients`, `/drafts`, `POST /modernize` on `:8003` |
| `herbenzo/static/` | Independent B UI (compose form, drafts, raw JSON) |
| `herbenzo/data/compose_drafts/` | On-disk compose drafts (gitignored; created on save) |
| `herbenzo/data/registry_overlay/` | Pending candidates and approved `HB-*` rows (gitignored; `HERBENZO_REGISTRY_DIR` overrides) |
| `herbenzo/contract_gate.py` | Shared-package FormulationSpec / ModernizedSKU gates |
| `cache/` | On-disk response cache — delete to force re-retrieval |

Both API clients cache every response to `cache/`, rate-limit themselves, and
retry `429`/`5xx` with exponential backoff. A repeated run issues no network
traffic at all while entries remain within TTL.

### Shared PubMed / PMID cache (Task T16)

Stage B is the evidence-layer owner. PMID files use the shared
[`herbenzo-pubmed-cache`](packages/herbenzo-pubmed-cache) envelope
(`pmid_<id>.json` + freshness stamp). Point **A / B / adjudication** at the
same directory to cut duplicate NCBI spend:

```bash
export HERBENZO_PUBMED_CACHE_DIR="$HOME/Desktop/herbenzo_shared_cache/pubmed"
export HERBENZO_PUBMED_CACHE_TTL_S=2592000   # 30 days; 0 = never expire
```

Default (env unset): `<cwd>/cache/pubmed` — same relative layout as before.

---

## Known issues and limitations

- **PubChem returned `503 PUGREST.ServerBusy` throughout development.** The
  client's retry/backoff was added in response and the parsing path is tested
  offline, but the live PubChem call has **not** been verified end-to-end.
  Run `python -m herbenzo.cli markers` on your machine to confirm; all ten
  registry markers are already cached, so this only affects markers you add.
- **`model_system` detection is keyword-based** and returns `unknown` when an
  abstract gives no clear signal (a bacteriology study, for example). `unknown`
  is deliberate — better than a confident wrong tier.
- **Claim-term overlap is lexical, not semantic.** It catches wrong-subject and
  wrong-domain citations reliably; it will not catch a citation that is on-topic
  but reports the *opposite* result. Result-direction detection is the natural
  next addition, and is where an LLM call earns its place — `AdjudicationService`
  is the single seam to add it behind.
- **Descriptor-based BCS misses lattice-limited solubility.** Ellagic acid
  classifies as Class I and is poorly absorbed in reality. The measured-solubility
  override path exists and is tested; the values are not curated.
- **Berberine's Class IV call depends on a curated P-gp override** flagged
  `override_requires_citation=True`. It needs a retrieved PMID before release.

## Requires domain review before production use

The ingredient identity table and marker assignments in
`herbenzo/services/registries.py` are modelling decisions, not lookups. So is the
berberine efflux override. Review them before anything ships.

## Not included

Components A, C, E; A→B dose/identity adapter (**T11**); C consuming ModernizedSKU
(**T12**); A→B→C glue (**T13**); authentication; per-country
regulatory content. Compose drafts are local JSON files for the Stage B form
only — not a product database. The modernize HTTP surface and independent B UI
are live on `:8003`; the full CLI report path (evidence + adjudication) remains
available via `python -m herbenzo.cli`.

IMPPAT 3.0 local cache and MSME usage basis: [data/external/imppat/](data/external/imppat/README.md).
