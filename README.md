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

uvicorn herbenzo.api:app --host 0.0.0.0 --port 8003
```

Open the **independent Stage B UI** in a browser:

```text
http://127.0.0.1:8003/
```

**Compose** (default tab) builds a FormulationSpec without hand-editing JSON:

1. Set formulation id, product name, finished form (preset or any free text), market, servings per day, and confidence.
2. Pick one or more registry ingredients. Botanical name, common name, and part fill from that row. Enter **amount as mg per serving** (`quantity_mg`). Extract ratio (`10:1`) and a standardization marker + percent are optional.
3. The **FormulationSpec preview** is the JSON that will be posted.
4. **Run Modernize** calls the existing `POST /modernize` and renders the ModernizedSKU. Advisory flags on the response (including `classical_active_marker_gap`, if a response includes it) show as a banner. They are not errors. Unknown ingredient ids still return **422**.

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
| `POST` | `/suggest-formats` | Advisory finished-format ranking for a list of ingredient ids. Does not change `/modernize` |

```bash
curl -s http://127.0.0.1:8003/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8003/modernize \
  -H 'content-type: application/json' \
  -d @examples/ashwagandha.json | python3 -m json.tool
```

Unknown fields / raised confidence floors → **422** with `herbenzo-contracts`
`validation_error_body`. Unknown registry ingredient IDs → **422**
(`unknown_ingredient`). The same unknown-id gate applies to `POST /suggest-formats`.

### Suggested modern formats (advisory)

`POST /suggest-formats` ranks finished formats for one or more registry ingredient
ids. The body is `ingredient_ids` (or `ingredients`) plus optional `audience`,
`dosage_form`, `product_name`, and `quantities_mg`. Ranking is a curated catalog
(`herbenzo/data/modern_formats.json`) plus fixed rules. It does not call an LLM
and it does not change BCS, delivery, or the ModernizedSKU.

The suggestion is **not** added to the `/modernize` body. `herbenzo-contracts`
models reject unknown fields (`extra="forbid"`), so an extra key on that
response would fail any consumer that re-validates the payload as a
ModernizedSKU. Use the separate endpoint.

Catalog references include static regulatory citations checked on 6 Oct 2026:
the FSSAI Nutra Regulations 2022 direction (clause 5(1)) on gummy, jelly,
chewable, mouth-dissolving strip, and bar formats; FDA nanomaterials guidance
and the EMA nanomedicines hub on nanoemulsion and conventional emulsion; and
the FDA Inactive Ingredient Database for excipient precedent. When ashwagandha
or turmeric is selected, each suggestion also cites that herb's EMA HMPC
monograph. No monograph was verified for the other registry herbs. Each
suggestion also carries prebuilt evidence-search URLs for Europe PMC, the NIH
DSLD `search-filter` query, and the Health Canada LNHPD advanced-search page.
Those URLs are strings only. Ranking does not call them. LNHPD has no
documented free-text ingredient query, so that link is not prefilled.

For a Chyawanprash-like or other classical name (`chyawanprash`, `avaleha`,
`lehya`, `churna`, and the other tokens in `CLASSICAL_TOKENS`), nanoemulsion
stays in the ranked list. It is strongly down-ranked and carries an explicit
owner caution; gummy and soft chew receive a preference bonus.

The Compose tab shows a **Suggested modern formats** panel. A card click fills
the free-string finished-form field. Advanced / Raw JSON is unchanged.

```bash
python -m herbenzo.cli suggest-formats HB-AMLA HB-PIPL HB-ASHW \
  --dosage-form avaleha --product-name Chyawanprash --audience adults
```
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
| `markers` | List registry ingredients, markers and PubChem CIDs |
| `suggest-formats <id>…` | Advisory finished-format ranking (does not modernize) |

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

## Layout

| Path | Role |
|---|---|
| `herbenzo/schemas/contracts.py` | Handoff contracts, confidence floor, citation guards |
| `herbenzo/clients/pubmed.py` | NCBI E-utilities — search, fetch; shared PMID cache (`herbenzo-pubmed-cache`) |
| `herbenzo/clients/pubchem.py` | PubChem PUG-REST — CID resolution and descriptors |
| `herbenzo/services/evidence.py` | Evidence store + manifest stamps over the shared cache |
| `herbenzo/services/adjudication.py` | Citation adjudication service |
| `herbenzo/services/registries.py` | Ingredient identity, markers, dose normalization |
| `herbenzo/services/live_registries.py` | Cached descriptors with live PubChem fallback |
| `herbenzo/components/modernizer/` | BCS classifier, delivery recommender, orchestrator |
| `herbenzo/pipeline.py` | End-to-end runner and report builder |
| `herbenzo/cli.py` | Command line |
| `herbenzo/api.py` | FastAPI: UI at `/`, `GET /health`, `GET /ingredients`, `/drafts`, `POST /modernize`, `POST /suggest-formats` on `:8003` |
| `herbenzo/format_suggestions.py` | Advisory format catalog and ranker (does not change ModernizedSKU) |
| `herbenzo/data/modern_formats.json` | Finished-format catalog, including checked market pages and static regulatory citations |
| `herbenzo/static/` | Independent B UI (compose form, drafts, raw JSON, format panel) |
| `herbenzo/data/compose_drafts/` | On-disk compose drafts (gitignored; created on save) |
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
