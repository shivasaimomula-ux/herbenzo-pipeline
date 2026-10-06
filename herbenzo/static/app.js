/* Stage B UI — compose or paste a FormulationSpec, POST /modernize, read the SKU. */

const SAMPLE_SPEC = {
  formulation_id: "F-ASHW-001",
  product_name: "Ashwagandha Root Extract 500 mg",
  dosage_form: "capsule",
  target_market: "US",
  servings_per_day: 1,
  confidence: 0.85,
  ingredients: [
    {
      ingredient_id: "HB-ASHW",
      botanical_name: "Withania somnifera",
      common_name: "Ashwagandha",
      part_used: "root",
      quantity_mg: 500.0,
      extract_ratio: "10:1",
      standardized_marker: "Withaferin A",
      standardized_percent: 5.0,
    },
  ],
};

const TRIPHALA_SPEC = {
  formulation_id: "F-TRIP-001",
  product_name: "Triphala Churna",
  dosage_form: "powder",
  target_market: "EU",
  servings_per_day: 2,
  confidence: 0.8,
  ingredients: [
    {
      ingredient_id: "HB-HARI",
      botanical_name: "Terminalia chebula",
      common_name: "Haritaki",
      part_used: "pericarp of fruit",
      quantity_mg: 1000,
    },
    {
      ingredient_id: "HB-BIBH",
      botanical_name: "Terminalia bellirica",
      common_name: "Bibhitaki",
      part_used: "pericarp of fruit",
      quantity_mg: 1000,
    },
    {
      ingredient_id: "HB-AMLA",
      botanical_name: "Phyllanthus emblica",
      common_name: "Amalaki",
      part_used: "fruit",
      quantity_mg: 1000,
    },
  ],
};

const MARKETS = ["US", "EU", "NZ", "AU", "IN", "UK", "CA"];

const el = {
  tabCompose: document.getElementById("tab-compose"),
  tabRaw: document.getElementById("tab-raw"),
  tabReview: document.getElementById("tab-review"),
  composePanel: document.getElementById("compose-panel"),
  rawPanel: document.getElementById("raw-panel"),
  reviewPanel: document.getElementById("review-panel"),
  ingredientSearch: document.getElementById("ingredient-search"),
  ingredientSuggest: document.getElementById("ingredient-suggest"),
  ingredientChips: document.getElementById("ingredient-chips"),
  loadTriphala: document.getElementById("load-triphala"),
  enrichQuery: document.getElementById("enrich-query"),
  enrichPart: document.getElementById("enrich-part"),
  enrichPropose: document.getElementById("enrich-propose"),
  enrichRefresh: document.getElementById("enrich-refresh"),
  candidateList: document.getElementById("candidate-list"),
  candidateDetail: document.getElementById("candidate-detail"),
  draftName: document.getElementById("draft-name"),
  saveDraft: document.getElementById("save-draft"),
  newDraft: document.getElementById("new-draft"),
  draftList: document.getElementById("draft-list"),
  formulationId: document.getElementById("formulation-id"),
  productName: document.getElementById("product-name"),
  dosageForm: document.getElementById("dosage-form"),
  market: document.getElementById("target-market"),
  servings: document.getElementById("servings"),
  confidence: document.getElementById("confidence"),
  ingredientEditors: document.getElementById("ingredient-editors"),
  addIngredient: document.getElementById("add-ingredient"),
  loadSampleCompose: document.getElementById("load-sample-compose"),
  specPreview: document.getElementById("spec-preview"),
  composeRun: document.getElementById("compose-run"),
  editJson: document.getElementById("edit-json"),
  input: document.getElementById("spec-input"),
  run: document.getElementById("run-btn"),
  loadSample: document.getElementById("load-sample"),
  file: document.getElementById("file-input"),
  statusPanel: document.getElementById("status-panel"),
  statusText: document.getElementById("status-text"),
  resultPanel: document.getElementById("result-panel"),
  resultMeta: document.getElementById("result-meta"),
  advisory: document.getElementById("advisory-banner"),
  skuSummary: document.getElementById("sku-summary"),
  ingredientCards: document.getElementById("ingredient-cards"),
  rawJson: document.getElementById("raw-json"),
};

let registry = [];
const registryById = new Map();
let currentDraftId = null;
// Raw JSON starts as the sample. Copy the compose form over it only after the
// form changes, and stop copying once the textarea itself has been edited.
let composeTouched = false;
let rawDirty = false;

function setStatus(kind, message) {
  el.statusPanel.hidden = false;
  el.statusPanel.classList.remove("error", "ok");
  if (kind) el.statusPanel.classList.add(kind);
  el.statusText.textContent = message;
}

function clearResult() {
  el.resultPanel.hidden = true;
  el.advisory.hidden = true;
  el.advisory.innerHTML = "";
  el.skuSummary.innerHTML = "";
  el.ingredientCards.innerHTML = "";
  el.rawJson.textContent = "";
  el.resultMeta.textContent = "";
}

function formatErrorDetail(detail) {
  if (detail == null) return "Request failed.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
        const msg = item.msg || item.message || JSON.stringify(item);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("\n");
  }
  if (typeof detail === "object") {
    const code = detail.error || detail.code;
    const msg = detail.message || detail.detail || detail.msg;
    const parts = [];
    if (code) parts.push(String(code));
    if (msg) parts.push(String(msg));
    const errList = detail.details || detail.errors;
    if (Array.isArray(errList)) {
      parts.push(
        errList
          .map((e) => {
            if (typeof e === "string") return `  • ${e}`;
            const loc = Array.isArray(e.loc) ? e.loc.join(".") : e.field || "";
            const m = e.msg || e.message || JSON.stringify(e);
            return loc ? `  • ${loc}: ${m}` : `  • ${m}`;
          })
          .join("\n")
      );
    } else if (errList && typeof errList === "object") {
      parts.push(JSON.stringify(errList, null, 2));
    }
    if (Array.isArray(detail.unknown_ids) && detail.unknown_ids.length) {
      parts.push(`Unknown ingredient id: ${detail.unknown_ids.join(", ")}`);
    }
    if (parts.length) return parts.join("\n");
    return JSON.stringify(detail, null, 2);
  }
  return String(detail);
}

function pct(n) {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function writeRawFromCompose() {
  el.input.value = JSON.stringify(buildSpec(), null, 2);
  composeTouched = false;
  rawDirty = false;
}

function showTab(which) {
  if (which === "raw" && composeTouched && !rawDirty) {
    writeRawFromCompose();
  }
  el.composePanel.hidden = which !== "compose";
  el.rawPanel.hidden = which !== "raw";
  el.reviewPanel.hidden = which !== "review";
  el.tabCompose.setAttribute("aria-selected", which === "compose" ? "true" : "false");
  el.tabRaw.setAttribute("aria-selected", which === "raw" ? "true" : "false");
  el.tabReview.setAttribute("aria-selected", which === "review" ? "true" : "false");
  if (which === "review") loadCandidates();
}

function blankIngredient() {
  return {
    ingredient_id: "",
    botanical_name: "",
    common_name: "",
    part_used: "",
    quantity_mg: "",
    extract_ratio: "",
    standardized_marker: "",
    standardized_percent: "",
  };
}

function markerNames(rec) {
  if (!rec || !Array.isArray(rec.markers)) return [];
  return rec.markers.map((marker) => marker.marker_name).filter(Boolean);
}

function markerRationale(rec, markerName) {
  if (!rec || !markerName || !Array.isArray(rec.markers)) return "";
  const found = rec.markers.find((marker) => marker.marker_name === markerName);
  return found && found.rationale ? found.rationale : "";
}

function ingredientOptions(selectedId, ing) {
  const known = new Set(registry.map((row) => row.ingredient_id));
  const saved = ing || {};
  let html = `<option value="">Select ingredient</option>`;
  html += registry
    .map((row) => {
      const selected = row.ingredient_id === selectedId ? " selected" : "";
      const label = `${row.common_name || row.botanical_name} — ${row.botanical_name} (${row.ingredient_id})`;
      return `<option value="${escapeHtml(row.ingredient_id)}"${selected}>${escapeHtml(label)}</option>`;
    })
    .join("");
  if (selectedId && !known.has(selectedId)) {
    html += `<option value="${escapeHtml(selectedId)}" selected data-botanical="${escapeHtml(saved.botanical_name || "")}" data-common="${escapeHtml(saved.common_name || "")}" data-part="${escapeHtml(saved.part_used || "")}">${escapeHtml(selectedId)} (not approved in this session)</option>`;
  }
  return html;
}

function markerOptions(rec, selected) {
  const names = markerNames(rec);
  let html = `<option value="">Not standardized</option>`;
  for (const name of names) {
    const selectedAttr = name === selected ? " selected" : "";
    html += `<option value="${escapeHtml(name)}"${selectedAttr}>${escapeHtml(name)}</option>`;
  }
  if (selected && !names.includes(selected)) {
    html += `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)}</option>`;
  }
  return html;
}

function cardHtml(ing) {
  const row = ing || blankIngredient();
  const rec = registryById.get(row.ingredient_id);
  const botanical = rec ? rec.botanical_name : row.botanical_name || "";
  const common = rec ? rec.common_name || "" : row.common_name || "";
  const part = rec ? rec.part_used || "" : row.part_used || "";
  const marker = row.standardized_marker || "";
  const qty = row.quantity_mg == null ? "" : String(row.quantity_mg);
  const ratio = row.extract_ratio || "";
  const percent = row.standardized_percent == null ? "" : String(row.standardized_percent);
  const hint = markerRationale(rec, marker);
  return `
    <article class="ing-editor" data-botanical="${escapeHtml(botanical)}" data-common="${escapeHtml(common)}" data-part="${escapeHtml(part)}">
      <div class="ing-head">
        <h3>Ingredient</h3>
        <button type="button" class="ghost remove-ing">Remove</button>
      </div>
      <label class="field">
        Approved ingredient
        <select class="ing-id">${ingredientOptions(row.ingredient_id || "", row)}</select>
      </label>
      <p class="identity"><span class="botanical">${escapeHtml(botanical || "—")}</span> · <span class="common">${escapeHtml(common || "—")}</span> · <span class="part">${escapeHtml(part || "—")}</span></p>
      <div class="field-grid">
        <label class="field">
          Amount (mg / serving)
          <input class="qty" type="number" min="0" step="any" inputmode="decimal" placeholder="500" value="${escapeHtml(qty)}" />
        </label>
        <label class="field">
          Extract ratio
          <input class="ratio" type="text" inputmode="decimal" placeholder="10:1" spellcheck="false" value="${escapeHtml(ratio)}" />
        </label>
        <label class="field">
          Standardized marker
          <select class="marker">${markerOptions(rec, marker)}</select>
        </label>
        <label class="field">
          Standardized percent
          <input class="pct" type="number" min="0" max="100" step="any" inputmode="decimal" placeholder="optional" value="${escapeHtml(percent)}" />
        </label>
      </div>
      <p class="marker-hint hint">${escapeHtml(hint)}</p>
    </article>`;
}

function renumberCards() {
  const cards = el.ingredientEditors.querySelectorAll(".ing-editor");
  cards.forEach((card, index) => {
    const title = card.querySelector("h3");
    if (title) title.textContent = `Ingredient ${index + 1}`;
  });
}

function renderEditors(ingredients) {
  const rows = Array.isArray(ingredients) && ingredients.length ? ingredients : [blankIngredient()];
  el.ingredientEditors.innerHTML = rows.map((row) => cardHtml(row)).join("");
  renumberCards();
  updatePreview();
}

function paintIdentity(card) {
  card.querySelector(".botanical").textContent = card.dataset.botanical || "—";
  card.querySelector(".common").textContent = card.dataset.common || "—";
  card.querySelector(".part").textContent = card.dataset.part || "—";
}

function paintMarkerHint(card) {
  const rec = registryById.get(card.querySelector(".ing-id").value);
  const marker = card.querySelector(".marker").value;
  card.querySelector(".marker-hint").textContent = markerRationale(rec, marker);
}

function readNumber(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return undefined;
  const n = Number(text);
  return Number.isFinite(n) ? n : text;
}

function readIngredients() {
  return [...el.ingredientEditors.querySelectorAll(".ing-editor")].map((card) => {
    const ing = {
      ingredient_id: card.querySelector(".ing-id").value.trim(),
      botanical_name: (card.dataset.botanical || "").trim(),
    };
    const common = (card.dataset.common || "").trim();
    const part = (card.dataset.part || "").trim();
    if (common) ing.common_name = common;
    if (part) ing.part_used = part;
    const qty = readNumber(card.querySelector(".qty").value);
    if (qty !== undefined) ing.quantity_mg = qty;
    const ratio = card.querySelector(".ratio").value.trim();
    if (ratio) ing.extract_ratio = ratio;
    const marker = card.querySelector(".marker").value.trim();
    if (marker) ing.standardized_marker = marker;
    const percent = readNumber(card.querySelector(".pct").value);
    if (percent !== undefined) ing.standardized_percent = percent;
    return ing;
  });
}

function buildSpec() {
  const spec = {
    formulation_id: el.formulationId.value.trim(),
    product_name: el.productName.value.trim(),
    dosage_form: el.dosageForm.value.trim(),
    target_market: el.market.value,
    ingredients: readIngredients(),
  };
  const servings = readNumber(el.servings.value);
  if (servings !== undefined) {
    spec.servings_per_day = typeof servings === "number" && Number.isInteger(servings) ? servings : servings;
  }
  const confidence = readNumber(el.confidence.value);
  if (confidence !== undefined) spec.confidence = confidence;
  return spec;
}

function updatePreview() {
  renumberCards();
  renderChips();
  el.specPreview.textContent = JSON.stringify(buildSpec(), null, 2);
}

function selectedIngredientIds() {
  return new Set(readIngredients().map((ing) => ing.ingredient_id).filter(Boolean));
}

function filteredRegistry(query) {
  const q = query.trim().toLowerCase();
  const selected = selectedIngredientIds();
  return registry
    .filter((row) => {
      if (selected.has(row.ingredient_id)) return false;
      if (!q) return true;
      const hay = [
        row.ingredient_id,
        row.botanical_name,
        row.common_name,
        row.sanskrit_name,
        ...(row.synonyms || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    })
    .slice(0, 8);
}

function renderSuggestions() {
  if (!el.ingredientSuggest || !el.ingredientSearch) return;
  const query = el.ingredientSearch.value.trim();
  const status = document.getElementById("ingredient-suggest-status");
  window.clearTimeout(suggestTimer);
  if (query.length < 2) {
    el.ingredientSuggest.hidden = true;
    el.ingredientSuggest.innerHTML = "";
    if (status) status.textContent = "";
    return;
  }
  if (status) status.textContent = "Searching NCBI, GBIF, and Wikidata…";
  const serial = ++suggestSerial;
  suggestTimer = window.setTimeout(async () => {
    try {
      const res = await fetch(`/research/suggest?q=${encodeURIComponent(query)}`, {
        headers: { accept: "application/json" },
      });
      const body = await res.json().catch(() => ({ suggestions: [] }));
      if (serial !== suggestSerial) return;
      const rows = Array.isArray(body.suggestions) ? body.suggestions : [];
      if (!rows.length) {
        el.ingredientSuggest.hidden = true;
        el.ingredientSuggest.innerHTML = "";
        if (status) {
          const notes = Array.isArray(body.source_notes) ? body.source_notes.filter(Boolean) : [];
          const lead = body.error ? "No suggestions (taxonomy lookup failed)." : "No matching species.";
          status.textContent = [lead, ...notes].join(" ");
        }
        return;
      }
      el.ingredientSuggest.hidden = false;
      el.ingredientSuggest.innerHTML = rows
        .map((row) => {
          const commons = (row.common_names || []).concat(row.synonyms || []).join(", ");
          const sources = Array.isArray(row.sources) && row.sources.length ? row.sources.join(", ") : "NCBI Taxonomy";
          const label = `${row.scientific_name} · ${row.rank || "rank unknown"} · tax ${row.tax_id} · ${sources}${commons ? " · " + commons : ""}`;
          const sourceAttr = (Array.isArray(row.sources) ? row.sources : []).join("|");
          return `<li><button type="button" data-scientific-name="${escapeHtml(row.scientific_name)}" data-tax-id="${escapeHtml(row.tax_id)}" data-sources="${escapeHtml(sourceAttr)}">${escapeHtml(label)}</button></li>`;
        })
        .join("");
      if (status) status.textContent = suggestionStatus(body, rows);
    } catch (err) {
      if (serial !== suggestSerial) return;
      el.ingredientSuggest.hidden = true;
      if (status) status.textContent = "No suggestions (taxonomy lookup failed).";
    }
  }, 300);
}

function renderChips() {
  if (!el.ingredientChips) return;
  const rows = readIngredients().filter((ing) => ing.ingredient_id);
  if (!rows.length) {
    el.ingredientChips.innerHTML = `<p class="draft-empty">No herbs selected yet. Type a name, research it, then approve.</p>`;
    return;
  }
  el.ingredientChips.innerHTML = rows
    .map((ing) => {
      const rec = registryById.get(ing.ingredient_id);
      const label = rec ? rec.common_name || rec.botanical_name : ing.ingredient_id;
      return `<button type="button" class="chip" data-chip="${escapeHtml(ing.ingredient_id)}">${escapeHtml(label)} <span aria-hidden="true">×</span></button>`;
    })
    .join("");
}

function addRegistryIngredient(id) {
  const rec = registryById.get(id);
  if (!rec) return;
  if (selectedIngredientIds().has(id)) {
    setStatus("ok", `${rec.common_name || id} is already in this formulation.`);
    return;
  }
  const cards = [...el.ingredientEditors.querySelectorAll(".ing-editor")];
  const onlyBlank = cards.length === 1 && !cards[0].querySelector(".ing-id").value;
  if (onlyBlank) cards[0].remove();
  el.ingredientEditors.insertAdjacentHTML(
    "beforeend",
    cardHtml({
      ingredient_id: id,
      botanical_name: rec.botanical_name,
      common_name: rec.common_name,
      part_used: rec.part_used,
      quantity_mg: "",
    })
  );
  composeTouched = true;
  updatePreview();
  const last = el.ingredientEditors.querySelector(".ing-editor:last-child .qty");
  if (last) last.focus();
}

function removeIngredient(id) {
  const cards = [...el.ingredientEditors.querySelectorAll(".ing-editor")];
  for (const card of cards) {
    if (card.querySelector(".ing-id").value === id) card.remove();
  }
  if (!el.ingredientEditors.querySelector(".ing-editor")) {
    el.ingredientEditors.insertAdjacentHTML("beforeend", cardHtml(blankIngredient()));
  }
  composeTouched = true;
  updatePreview();
}

function specProblems(spec) {
  const problems = [];
  if (!spec.formulation_id) problems.push("Formulation ID is required.");
  if (!spec.product_name) problems.push("Product name is required.");
  if (!spec.dosage_form) problems.push("Finished form is required.");
  if (!MARKETS.includes(spec.target_market)) problems.push("Choose a target market.");
  if (!Number.isInteger(spec.servings_per_day) || spec.servings_per_day <= 0) {
    problems.push("Servings per day must be an integer greater than 0.");
  }
  if (typeof spec.confidence !== "number" || spec.confidence < 0 || spec.confidence > 1) {
    problems.push("Confidence must be a number from 0 to 1.");
  }
  if (!Array.isArray(spec.ingredients) || spec.ingredients.length < 1) {
    problems.push("Add at least one ingredient.");
  }
  const ids = [];
  (spec.ingredients || []).forEach((ing, index) => {
    const n = index + 1;
    if (!ing.ingredient_id) problems.push(`Ingredient ${n}: research and approve a species first.`);
    if (!ing.botanical_name) problems.push(`Ingredient ${n}: botanical name is missing.`);
    if (typeof ing.quantity_mg !== "number" || !(ing.quantity_mg > 0)) {
      problems.push(`Ingredient ${n}: amount (mg per serving) must be greater than 0.`);
    }
    if (ing.extract_ratio) {
      const match = /^([^:]+):([^:]+)$/.exec(ing.extract_ratio);
      const native = match && Number(match[1]);
      const extract = match && Number(match[2]);
      if (!match || !(native > 0) || !(extract > 0)) {
        problems.push(`Ingredient ${n}: extract ratio must look like 10:1.`);
      }
    }
    if (ing.standardized_percent != null && !ing.standardized_marker) {
      problems.push(`Ingredient ${n}: name the marker before setting a percent.`);
    }
    if (
      ing.standardized_percent != null &&
      (typeof ing.standardized_percent !== "number" ||
        ing.standardized_percent < 0 ||
        ing.standardized_percent > 100)
    ) {
      problems.push(`Ingredient ${n}: standardized percent must be between 0 and 100.`);
    }
    if (ing.ingredient_id) ids.push(ing.ingredient_id);
  });
  const dupes = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
  if (dupes.length) problems.push(`Duplicate ingredient: ${dupes.join(", ")}.`);
  return problems;
}

function fillCompose(spec) {
  const data = spec && typeof spec === "object" ? spec : {};
  el.formulationId.value = data.formulation_id || "";
  el.productName.value = data.product_name || "";
  el.dosageForm.value = data.dosage_form || "";
  el.market.value = MARKETS.includes(data.target_market) ? data.target_market : "US";
  el.servings.value = data.servings_per_day == null ? "1" : String(data.servings_per_day);
  el.confidence.value = data.confidence == null ? "0.85" : String(data.confidence);
  const ingredients = Array.isArray(data.ingredients) && data.ingredients.length
    ? data.ingredients
    : [blankIngredient()];
  renderEditors(ingredients);
}

function resetCompose() {
  currentDraftId = null;
  el.draftName.value = "";
  fillCompose({
    target_market: "US",
    servings_per_day: 1,
    confidence: 0.85,
    ingredients: [blankIngredient()],
  });
  renderDraftList(lastDrafts);
}

function formatWhen(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

let lastDrafts = [];
const approvedById = new Map();
window.herbenzoApproved = approvedById;
let suggestTimer = 0;
let suggestSerial = 0;
let pickedName = "";
let pickedQuery = "";
let pickedSources = [];

function suggestionStatus(body, rows) {
  const notes = Array.isArray(body.source_notes) ? body.source_notes.filter(Boolean) : [];
  let lead = "";
  if (rows.length > 1) {
    lead = "Several taxa match. Pick a scientific name and tax id. Nothing is selected for you.";
  } else if (body.ambiguous) {
    lead = "This name is not the scientific name. Pick the row. Nothing is selected for you.";
  }
  return [lead, ...notes].filter(Boolean).join(" ");
}
let lastCandidate = null;

function renderDraftList(drafts) {
  lastDrafts = Array.isArray(drafts) ? drafts : [];
  if (!lastDrafts.length) {
    el.draftList.innerHTML = `<p class="draft-empty">No drafts saved yet.</p>`;
    return;
  }
  el.draftList.innerHTML = lastDrafts
    .map((draft) => {
      const current = draft.id === currentDraftId ? " is-current" : "";
      const ready = draft.complete ? "ready" : "";
      const label = draft.complete ? "Ready" : "Incomplete";
      const count = draft.ingredient_count == null ? "" : `${draft.ingredient_count} ingredient${draft.ingredient_count === 1 ? "" : "s"}`;
      return `
        <div class="draft-row${current}" data-draft-id="${escapeHtml(draft.id)}">
          <div class="draft-copy">
            <strong>${escapeHtml(draft.name || "Untitled")}</strong>
            <span class="pill ${ready}">${label}</span>
            <span>${escapeHtml(count)}</span>
            <span>${escapeHtml(formatWhen(draft.updated_at))}</span>
          </div>
          <div class="actions">
            <button type="button" class="ghost load-draft" data-id="${escapeHtml(draft.id)}">Load</button>
            <button type="button" class="ghost delete-draft" data-id="${escapeHtml(draft.id)}">Delete</button>
          </div>
        </div>`;
    })
    .join("");
}

async function loadDraftList() {
  const res = await fetch("/drafts", { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(formatErrorDetail(body && body.detail !== undefined ? body.detail : body));
  }
  renderDraftList((body && body.drafts) || []);
}

async function loadIngredients() {
  registry = [...registryById.values()];
}

function rememberApproval(approval) {
  const ingredient = approval.ingredient || {};
  const id = ingredient.ingredient_id || approval.ingredient_id;
  if (!id) return;
  approvedById.set(id, approval);
  const row = {
    ingredient_id: id,
    botanical_name: ingredient.botanical_name,
    common_name: ingredient.common_name,
    sanskrit_name: ingredient.sanskrit_name,
    synonyms: ingredient.synonyms || [],
    part_used: ingredient.part_used,
    markers: ingredient.markers || [],
  };
  registryById.set(id, row);
  registry = [...registryById.values()];
}

function selectedTaxonMatches(query) {
  if (!el.ingredientSuggest || el.ingredientSuggest.hidden) return true;
  const buttons = [...el.ingredientSuggest.querySelectorAll("[data-scientific-name]")];
  if (!buttons.length) return true;
  const typed = query.trim().toLowerCase();
  const exact = buttons.filter((button) => (button.dataset.scientificName || "").toLowerCase() === typed);
  return exact.length === 1;
}

async function researchCurrentName() {
  const query = el.ingredientSearch.value.trim();
  if (!query) {
    setStatus("error", "Type a species or common name first.");
    return;
  }
  if (!selectedTaxonMatches(query)) {
    setStatus(
      "error",
      "This name matches more than one taxon, or it is not the scientific name. Pick a row in the list. Nothing is selected for you."
    );
    return;
  }
  setStatus(null, "Researching…");
  const payload = { query };
  if (pickedName && query.toLowerCase() === pickedName.toLowerCase() && pickedSources.length) {
    payload.name_sources = pickedSources;
    payload.name_query = pickedQuery;
  }
  const res = await fetch("/research", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    setStatus("error", `Research failed (${res.status})\n${formatErrorDetail(body && body.detail)}`);
    return;
  }
  lastCandidate = body;
  const evidence = document.getElementById("research-evidence");
  const articles = (body.literature && body.literature.articles) || [];
  const marker = body.marker_status || "pending";
  if (evidence) {
    evidence.textContent = [
      `${body.taxonomy && body.taxonomy.scientific_name} (${body.taxonomy && body.taxonomy.rank}, tax ${body.taxonomy && body.taxonomy.tax_id})`,
      `PubMed refs: ${articles.length}. Marker: ${marker}.`,
      articles.slice(0, 3).map((article) => `PMID ${article.pmid}`).join(", "),
    ].filter(Boolean).join(" ");
  }
  setStatus("ok", "Research finished. Approve to hold it on this formulation.");
}

async function approveLastCandidate() {
  if (!lastCandidate) {
    setStatus("error", "Research an ingredient before approving it.");
    return;
  }
  const res = await fetch("/research/approve", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ candidate: lastCandidate }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    setStatus("error", `Approval failed (${res.status})\n${formatErrorDetail(body && body.detail)}`);
    return;
  }
  rememberApproval(body);
  const id = body.ingredient_id;
  const ingredient = body.ingredient || {};
  const name = ingredient.botanical_name || id;
  addRegistryIngredient(id);
  const evidence = document.getElementById("research-evidence");
  if (evidence) {
    const marker = body.marker_status || "pending";
    evidence.textContent = `Approved ${name} (${id}) for this formulation. Marker: ${marker}. Run Modernize sends this approval with the spec.`;
  }
  setStatus("ok", `Approved ${name} (${id}) for this formulation. Run Modernize uses this approval.`);
}

function responseSku(body) {
  if (!body || typeof body !== "object") return null;
  if (body.sku_id) return body;
  if (body.sku && typeof body.sku === "object" && body.sku.sku_id) return body.sku;
  return null;
}

function collectAdvisories(body) {
  const found = [];
  const seen = new Set();
  const consider = (node) => {
    if (!node || typeof node !== "object") return;
    if (node.classical_active_marker_gap) {
      const key = "classical_active_marker_gap";
      if (!seen.has(key)) {
        seen.add(key);
        found.push({ code: key, value: node.classical_active_marker_gap });
      }
    }
    for (const key of ["advisory", "advisories", "advisory_flags", "flags"]) {
      const value = node[key];
      if (value == null || value === false || value === "") continue;
      const id = `${key}:${JSON.stringify(value)}`;
      if (seen.has(id)) continue;
      seen.add(id);
      found.push({ code: key, value });
    }
  };
  consider(body);
  if (body && body.sku && typeof body.sku === "object") consider(body.sku);
  return found;
}

function summarizeAdvisory(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") return item.message || item.code || JSON.stringify(item);
        return String(item);
      })
      .join("\n");
  }
  if (typeof value === "object") {
    if (typeof value.message === "string" && value.message) return value.message;
    const bits = [];
    for (const [key, item] of Object.entries(value)) {
      if (item == null || item === false || item === "") continue;
      if (typeof item === "string" || typeof item === "number") bits.push(`${key}: ${item}`);
    }
    if (bits.length) return bits.join("\n");
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function renderOneAdvisory(item) {
  if (item.code === "classical_active_marker_gap" && item.value && typeof item.value === "object") {
    const gap = item.value;
    const lines = (Array.isArray(gap.ingredients) ? gap.ingredients : [])
      .map(
        (row) =>
          `<li>${escapeHtml(row.ingredient_id || "ingredient")} — ${escapeHtml(row.reason || row.message || "")}</li>`
      )
      .join("");
    return `
      <div class="advisory-item">
        <strong>Marker pending — this ingredient stays on the SKU, unstandardized</strong>
        <p>${escapeHtml(gap.message || "marker pending")}</p>
        <p>Release requires a marker. QC, specification, and label fields that need a marker stay pending. POST /research/marker with this approval and a specific marker_name, then modernize again. This does not block the SKU.</p>
        ${lines ? `<ul>${lines}</ul>` : ""}
      </div>`;
  }
  return `
    <div class="advisory-item">
      <strong>Advisory · ${escapeHtml(item.code)}</strong>
      <p>${escapeHtml(summarizeAdvisory(item.value))}</p>
    </div>`;
}

function renderAdvisoryBanner(body) {
  const items = collectAdvisories(body);
  if (!items.length) {
    el.advisory.hidden = true;
    el.advisory.innerHTML = "";
    return false;
  }
  el.advisory.hidden = false;
  el.advisory.innerHTML = items.map((item) => renderOneAdvisory(item)).join("");
  return true;
}

function renderSku(sku, rawBody) {
  el.resultPanel.hidden = false;
  el.resultMeta.textContent = [
    sku.engine_version || "",
    sku.source_stage || "",
    sku.generated_at ? `at ${sku.generated_at}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const summaryRows = [
    ["SKU", sku.sku_id],
    ["Source formulation", sku.source_formulation_id],
    ["Product", sku.product_name],
    ["Finished form", sku.dosage_form],
    ["Confidence", pct(sku.confidence)],
    ["Inherited confidence", pct(sku.inherited_confidence)],
    ["Market", sku.target_market],
    ["Servings / day", sku.servings_per_day],
  ];
  const pt = sku.provenance_thread || {};
  const stages = Array.isArray(pt.stages)
    ? pt.stages.join(",")
    : pt.stages || "A,B";
  const cReady = !!(sku.sku_id && (pt.sku_id || sku.sku_id));
  summaryRows.push([
    "Handoff",
    cReady
      ? `→C ready · stages=[${stages}]`
      : `→C blocked · stages=[${stages}]`,
  ]);
  if (pt.spec_id || pt.formulation_id || pt.sku_id) {
    summaryRows.push([
      "Provenance",
      [
        pt.spec_id ? `spec_id=${pt.spec_id}` : null,
        pt.formulation_id ? `formulation_id=${pt.formulation_id}` : null,
        pt.sku_id ? `sku_id=${pt.sku_id}` : null,
      ]
        .filter(Boolean)
        .join(" → "),
    ]);
  }
  const warnings = rawBody && Array.isArray(rawBody.warnings) ? rawBody.warnings.filter(Boolean) : [];
  if (warnings.length) {
    summaryRows.push(["Warnings", warnings.join("; ")]);
  }
  el.skuSummary.innerHTML = summaryRows
    .map(
      ([k, v]) =>
        `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v ?? "—")}</dd></div>`
    )
    .join("");

  const ingredients = Array.isArray(sku.ingredients) ? sku.ingredients : [];
  el.ingredientCards.innerHTML = ingredients
    .map((ing, idx) => {
      const marker = ing.marker || {};
      const bcs = ing.bcs || {};
      const delivery = ing.delivery || {};
      const pending =
        ing.marker_status === "pending" || marker.marker_status === "pending";
      const markerName = pending ? "unstandardized" : marker.marker_name || "—";
      const proxy = !pending && marker.is_proxy ? " (proxy)" : "";
      const flag = pending ? `<span class="marker-flag">marker pending</span>` : "";
      const bcsClass = pending
        ? "unstandardized"
        : bcs.bcs_class != null
          ? `Class ${bcs.bcs_class}`
          : "—";
      const bcsBits = pending
        ? "no class until a marker is set"
        : [
            bcs.solubility_call ? `solubility ${bcs.solubility_call}` : null,
            bcs.permeability_call ? `permeability ${bcs.permeability_call}` : null,
            bcs.evidence_basis ? `basis ${bcs.evidence_basis}` : null,
            typeof bcs.confidence === "number" ? `conf ${pct(bcs.confidence)}` : null,
          ]
            .filter(Boolean)
            .join(" · ");
      const primary = pending
        ? "unstandardized — requires a marker before release"
        : String(delivery.primary || "—").replaceAll("_", " ");
      const alts = Array.isArray(delivery.alternatives)
        ? delivery.alternatives.map((a) => String(a).replaceAll("_", " ")).join(", ")
        : "";
      const rationale = Array.isArray(bcs.rationale) ? bcs.rationale : [];
      const delRationale = Array.isArray(delivery.rationale) ? delivery.rationale : [];

      return `
        <article class="ingredient" style="animation-delay: ${0.05 * idx}s">
          <h3>${escapeHtml(ing.botanical_name || ing.ingredient_id || "ingredient")}${flag}</h3>
          <p class="sub">${escapeHtml(ing.ingredient_id || "")}${
            ing.quantity_mg != null ? ` · ${escapeHtml(ing.quantity_mg)} mg` : ""
          }${
            ing.marker_dose_mg != null
              ? ` · marker dose ${escapeHtml(ing.marker_dose_mg)} mg`
              : ""
          }</p>
          <div class="facts">
            <div class="fact">
              <h4>Marker</h4>
              <p>${escapeHtml(markerName)}${escapeHtml(proxy)}</p>
              ${
                marker.rationale
                  ? `<ul><li>${escapeHtml(marker.rationale)}</li></ul>`
                  : ""
              }
            </div>
            <div class="fact">
              <h4>BCS</h4>
              <p>${escapeHtml(bcsClass)}${bcsBits ? ` · ${escapeHtml(bcsBits)}` : ""}</p>
              ${
                rationale.length
                  ? `<ul>${rationale
                      .map((r) => `<li>${escapeHtml(r)}</li>`)
                      .join("")}</ul>`
                  : ""
              }
            </div>
            <div class="fact">
              <h4>Delivery</h4>
              <p>${escapeHtml(primary)}${
                delivery.advisory_only ? " · advisory" : ""
              }</p>
              ${alts ? `<ul><li>Alternatives: ${escapeHtml(alts)}</li></ul>` : ""}
              ${
                delRationale.length
                  ? `<ul>${delRationale
                      .map((r) => `<li>${escapeHtml(r)}</li>`)
                      .join("")}</ul>`
                  : ""
              }
            </div>
          </div>
        </article>`;
    })
    .join("");

  el.rawJson.textContent = JSON.stringify(rawBody || sku, null, 2);
}

function showModernizeSuccess(body) {
  const sku = responseSku(body);
  const hasAdvisory = renderAdvisoryBanner(body);
  if (!sku) {
    el.resultPanel.hidden = false;
    el.resultMeta.textContent = "No ModernizedSKU in the response";
    el.skuSummary.innerHTML = `<div><dt>ModernizedSKU</dt><dd>missing</dd></div>`;
    el.ingredientCards.innerHTML = "";
    el.rawJson.textContent = JSON.stringify(body, null, 2);
    setStatus("error", "Modernize did not return a SKU.");
    el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const warnings = Array.isArray(body && body.warnings) ? body.warnings.filter(Boolean) : [];
  const pendingNote = warnings.length
    ? ` · ${warnings.join("; ")}`
    : hasAdvisory
      ? " · marker pending"
      : "";
  setStatus(
    "ok",
    `Modernized · ${sku.sku_id || "SKU"} · confidence ${pct(sku.confidence)}${pendingNote}`
  );
  renderSku(sku, body);
  el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function postModernize(payload, button) {
  clearResult();
  if (button) button.disabled = true;
  setStatus(null, "Calling POST /modernize…");
  try {
    const res = await fetch("/modernize", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(payload),
    });
    let body;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (!res.ok) {
      const detail = body && body.detail !== undefined ? body.detail : body;
      setStatus("error", `Modernize failed (${res.status})\n${formatErrorDetail(detail)}`);
      return;
    }
    showModernizeSuccess(body);
  } catch (err) {
    setStatus(
      "error",
      `Network error: ${err.message}\nIs the API running on this origin (port 8003)?`
    );
  } finally {
    if (button) button.disabled = false;
  }
}

async function modernizeRaw() {
  let payload;
  try {
    payload = JSON.parse(el.input.value);
  } catch (err) {
    setStatus("error", `Invalid JSON: ${err.message}`);
    return;
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    setStatus("error", "FormulationSpec must be a JSON object.");
    return;
  }
  await postModernize(payload, el.run);
}

async function modernizeCompose() {
  const spec = buildSpec();
  updatePreview();
  const problems = specProblems(spec);
  if (problems.length) {
    clearResult();
    setStatus("error", problems.join("\n"));
    return;
  }
  const approvals = readIngredients()
    .map((ing) => approvedById.get(ing.ingredient_id))
    .filter(Boolean);
  await postModernize({ spec, approvals }, el.composeRun);
}

async function saveDraft() {
  const name = el.draftName.value.trim();
  if (!name) {
    setStatus("error", "Name the draft before saving.");
    el.draftName.focus();
    return;
  }
  const payload = {
    name,
    spec: buildSpec(),
    approvals: [...approvedById.values()],
  };
  if (currentDraftId) payload.id = currentDraftId;
  el.saveDraft.disabled = true;
  try {
    const res = await fetch("/drafts", {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body && body.detail !== undefined ? body.detail : body;
      setStatus("error", `Could not save draft (${res.status})\n${formatErrorDetail(detail)}`);
      return;
    }
    currentDraftId = body.id;
    const ready = body.complete ? "ready to modernize" : "incomplete — saved anyway";
    setStatus("ok", `Saved draft “${body.name}” (${ready}).`);
    await loadDraftList();
  } catch (err) {
    setStatus("error", `Could not save draft: ${err.message}`);
  } finally {
    el.saveDraft.disabled = false;
  }
}

async function loadDraft(id) {
  const res = await fetch(`/drafts/${encodeURIComponent(id)}`, {
    headers: { accept: "application/json" },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail !== undefined ? body.detail : body;
    setStatus("error", `Could not load draft (${res.status})\n${formatErrorDetail(detail)}`);
    return;
  }
  currentDraftId = body.id;
  el.draftName.value = body.name || "";
  approvedById.clear();
  for (const approval of body.approvals || []) rememberApproval(approval);
  fillCompose(body.spec || {});
  writeRawFromCompose();
  showTab("compose");
  renderDraftList(lastDrafts);
  const ready = body.complete ? "Ready to modernize." : "Incomplete — finish the form before running.";
  setStatus("ok", `Loaded draft “${body.name}”. ${ready}`);
}

async function deleteDraft(id, name) {
  const label = name || "this draft";
  if (!window.confirm(`Delete draft “${label}”?`)) return;
  const res = await fetch(`/drafts/${encodeURIComponent(id)}`, { method: "DELETE" });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail !== undefined ? body.detail : body;
    setStatus("error", `Could not delete draft (${res.status})\n${formatErrorDetail(detail)}`);
    return;
  }
  if (currentDraftId === id) currentDraftId = null;
  setStatus("ok", `Deleted draft “${label}”.`);
  await loadDraftList();
}

el.tabCompose.addEventListener("click", () => showTab("compose"));
el.tabRaw.addEventListener("click", () => showTab("raw"));

el.ingredientEditors.addEventListener("input", () => {
  composeTouched = true;
  updatePreview();
});
el.ingredientEditors.addEventListener("change", (event) => {
  composeTouched = true;
  const card = event.target.closest(".ing-editor");
  if (!card) return;
  if (event.target.classList.contains("ing-id")) {
    const rec = registryById.get(event.target.value);
    const markerSelect = card.querySelector(".marker");
    const names = markerNames(rec);
    const keep = names.includes(markerSelect.value) ? markerSelect.value : "";
    if (rec) {
      card.dataset.botanical = rec.botanical_name || "";
      card.dataset.common = rec.common_name || "";
      card.dataset.part = rec.part_used || "";
    } else {
      const option = event.target.selectedOptions && event.target.selectedOptions[0];
      card.dataset.botanical = (option && option.dataset.botanical) || "";
      card.dataset.common = (option && option.dataset.common) || "";
      card.dataset.part = (option && option.dataset.part) || "";
    }
    markerSelect.innerHTML = markerOptions(rec, keep);
    paintIdentity(card);
    paintMarkerHint(card);
  }
  if (event.target.classList.contains("marker")) paintMarkerHint(card);
  updatePreview();
});
el.ingredientEditors.addEventListener("click", (event) => {
  const button = event.target.closest(".remove-ing");
  if (!button) return;
  const card = button.closest(".ing-editor");
  if (card) card.remove();
  if (!el.ingredientEditors.querySelector(".ing-editor")) {
    el.ingredientEditors.insertAdjacentHTML("beforeend", cardHtml(blankIngredient()));
  }
  composeTouched = true;
  updatePreview();
});

for (const node of [el.formulationId, el.productName, el.dosageForm, el.market, el.servings, el.confidence]) {
  node.addEventListener("input", () => {
    composeTouched = true;
    updatePreview();
  });
  node.addEventListener("change", () => {
    composeTouched = true;
    updatePreview();
  });
}

el.addIngredient.addEventListener("click", () => {
  el.ingredientEditors.insertAdjacentHTML("beforeend", cardHtml(blankIngredient()));
  composeTouched = true;
  updatePreview();
  const cards = el.ingredientEditors.querySelectorAll(".ing-editor");
  const last = cards[cards.length - 1];
  const select = last && last.querySelector(".ing-id");
  if (select) select.focus();
});

el.ingredientSearch.addEventListener("input", () => {
  const typed = el.ingredientSearch.value.trim().toLowerCase();
  if (!pickedName || typed !== pickedName.toLowerCase()) {
    pickedName = "";
    pickedQuery = "";
    pickedSources = [];
  }
  renderSuggestions();
});
el.ingredientSearch.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  researchCurrentName();
});
el.ingredientSuggest.addEventListener("mousedown", (event) => {
  const button = event.target.closest("[data-scientific-name]");
  if (!button) return;
  event.preventDefault();
  pickedQuery = el.ingredientSearch.value.trim();
  pickedName = button.dataset.scientificName || "";
  pickedSources = (button.dataset.sources || "").split("|").map((item) => item.trim()).filter(Boolean);
  el.ingredientSearch.value = pickedName;
  el.ingredientSuggest.hidden = true;
  const status = document.getElementById("ingredient-suggest-status");
  if (status) status.textContent = "Suggestion selected. Research still has to pass identity and evidence.";
});
document.getElementById("ingredient-research").addEventListener("click", researchCurrentName);
document.getElementById("ingredient-approve").addEventListener("click", approveLastCandidate);
el.ingredientChips.addEventListener("click", (event) => {
  const chip = event.target.closest("[data-chip]");
  if (!chip) return;
  removeIngredient(chip.dataset.chip);
});

el.loadTriphala.addEventListener("click", () => {
  el.ingredientSearch.value = "Terminalia chebula";
  renderSuggestions();
  setStatus("ok", "Triphala is three herbs. Research each name, starting with Terminalia chebula.");
});

el.tabReview.addEventListener("click", () => showTab("review"));
el.enrichPropose.addEventListener("click", proposeCandidate);
el.enrichRefresh.addEventListener("click", () => {
  lastCandidate = null;
  if (el.candidateDetail) el.candidateDetail.innerHTML = "";
  loadCandidates();
});
el.candidateList.addEventListener("click", (event) => {
  const approve = event.target.closest("[data-approve-candidate]");
  const reject = event.target.closest("[data-reject-candidate]");
  if (approve) decideCandidate(approve.dataset.approveCandidate, "approve");
  if (reject) decideCandidate(reject.dataset.rejectCandidate, "reject");
});
el.candidateDetail.addEventListener("click", (event) => {
  const approve = event.target.closest("[data-approve-candidate]");
  const reject = event.target.closest("[data-reject-candidate]");
  if (approve) decideCandidate(approve.dataset.approveCandidate, "approve");
  if (reject) decideCandidate(reject.dataset.rejectCandidate, "reject");
});

el.loadSampleCompose.addEventListener("click", () => {
  el.ingredientSearch.value = "Withania somnifera";
  renderSuggestions();
  setStatus("ok", "Search prefilled with Withania somnifera. Research and approve before modernize.");
});

el.editJson.addEventListener("click", () => {
  writeRawFromCompose();
  showTab("raw");
  setStatus("ok", "Copied the compose form into the raw JSON editor.");
});

el.composeRun.addEventListener("click", modernizeCompose);
el.saveDraft.addEventListener("click", saveDraft);
el.newDraft.addEventListener("click", () => {
  resetCompose();
  clearResult();
  setStatus("ok", "Started a new draft. Saved drafts were not deleted.");
});

el.draftList.addEventListener("click", (event) => {
  const load = event.target.closest(".load-draft");
  const remove = event.target.closest(".delete-draft");
  if (load) {
    loadDraft(load.dataset.id);
    return;
  }
  if (remove) {
    const row = remove.closest(".draft-row");
    const name = row ? row.querySelector("strong")?.textContent : "";
    deleteDraft(remove.dataset.id, name);
  }
});

el.loadSample.addEventListener("click", () => {
  el.input.value = JSON.stringify(SAMPLE_SPEC, null, 2);
  rawDirty = true;
  composeTouched = false;
  setStatus("ok", "Loaded ashwagandha sample FormulationSpec.");
});

el.file.addEventListener("change", async () => {
  const file = el.file.files && el.file.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    JSON.parse(text);
    el.input.value = text;
    rawDirty = true;
    composeTouched = false;
    setStatus("ok", `Loaded ${file.name}`);
  } catch (err) {
    setStatus("error", `Could not load file: ${err.message}`);
  } finally {
    el.file.value = "";
  }
});

el.run.addEventListener("click", modernizeRaw);
el.input.addEventListener("input", () => {
  rawDirty = true;
});
el.input.value = JSON.stringify(SAMPLE_SPEC, null, 2);

function imppatLine(block) {
  if (!block) return "IMPPAT 3.0: not attached. A missing lookup does not block approval.";
  const status = block.status || "unavailable";
  const files = (block.files || [])
    .map((file) => file.name)
    .filter(Boolean)
    .join(", ");
  const sanskrit = (block.sanskrit_names || []).slice(0, 6).join(", ");
  const forms = (block.formulations || [])
    .slice(0, 4)
    .map((row) => row.formulation_name || row.formulation_id)
    .filter(Boolean)
    .join(", ");
  const parts = [
    `IMPPAT 3.0: ${status} (does not block approval${status === "matched" ? "; copied onto the approved row with citation" : ""})`,
    files ? `files ${files}` : "",
    block.retrieved_at ? `retrieved ${block.retrieved_at}` : "",
    sanskrit ? `Sanskrit/IAST ${sanskrit}` : "",
    forms ? `formulations ${forms}` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

function taxonomyLine(block) {
  if (!block) return "No classification retrieved.";
  if (block.status === "unavailable") return `Unavailable${block.error ? ` — ${block.error}` : ""}`;
  const levels = ["kingdom", "superclass", "class", "subclass", "direct_parent"]
    .map((key) => block[key])
    .filter(Boolean);
  const when = block.retrieved_at ? ` · retrieved ${block.retrieved_at}` : "";
  const source = block.source ? `${block.source}${when}` : when;
  return `${levels.join(" > ") || "No levels"}${source ? ` (${source})` : ""}`;
}

function npLine(block) {
  if (!block) return "No NPClassifier result.";
  if (block.status === "unavailable") return `Unavailable${block.error ? ` — ${block.error}` : ""}`;
  const parts = [
    (block.pathway || []).join(", "),
    (block.superclass || []).join(", "),
    (block.class || []).join(", "),
  ].filter(Boolean);
  const when = block.retrieved_at ? ` · retrieved ${block.retrieved_at}` : "";
  return `${parts.join(" > ") || "No labels"} (${block.source || "NPClassifier"}${when})`;
}

function safeHttps(url) {
  const text = String(url || "");
  return /^https:\/\//i.test(text) ? text : "";
}

function ayushEvidence(block) {
  if (!block || typeof block !== "object") return "";
  const heading = "Ayush Research Portal";
  if (block.status !== "ok") {
    const reason = block.reason ? ` — ${block.reason}` : "";
    return `
      <article class="taxonomy-block" data-ayush-portal>
        <h4>${heading}</h4>
        <p>${escapeHtml(block.status || "unavailable")}${escapeHtml(reason)}</p>
      </article>`;
  }
  const rows = Array.isArray(block.records) && block.records.length ? block.records : block.hits || [];
  const items = rows
    .slice(0, 5)
    .map((hit) => {
      const href = safeHttps(hit.record_url);
      const label = escapeHtml(hit.arp_id || "record");
      const link = href ? `<a href="${escapeHtml(href)}" rel="noreferrer">${label}</a>` : label;
      const cite = hit.citation && hit.citation.url ? safeHttps(hit.citation.url) : "";
      const citeLink = cite ? ` · <a href="${escapeHtml(cite)}" rel="noreferrer">${escapeHtml(hit.citation.source || "citation")}</a>` : "";
      return `<li>${link} ${escapeHtml(hit.title || "")} · ${escapeHtml(hit.journal || "")} · ${escapeHtml(hit.confidence || "")} / ${escapeHtml(hit.review_status || "")}${citeLink}</li>`;
    })
    .join("");
  const attribution = rows[0] && rows[0].attribution ? `<p>${escapeHtml(rows[0].attribution)}</p>` : "";
  return `
    <article class="taxonomy-block" data-ayush-portal>
      <h4>${heading}</h4>
      <p>${escapeHtml(block.source || heading)} · ${escapeHtml(block.retrieved_at || "")}</p>
      <ul>${items || "<li>No portal hits.</li>"}</ul>
      ${attribution}
    </article>`;
}

function renderCandidateDetail(doc) {
  const markers = Array.isArray(doc.markers) ? doc.markers : [];
  const markerHtml = markers
    .map((marker) => {
      const chem = marker.chemical_taxonomy || {};
      const pubchem = marker.pubchem || {};
      return `
        <article class="taxonomy-block">
          <h4>${escapeHtml(marker.name || "marker")}</h4>
          <p>PubChem CID ${escapeHtml(pubchem.cid ?? "—")} · MW ${escapeHtml(pubchem.molecular_weight ?? "—")} · XLogP ${escapeHtml(pubchem.xlogp ?? "—")}</p>
          <p>Source: ${escapeHtml(pubchem.source || "PubChem")} · ${escapeHtml(pubchem.retrieved_at || "")}</p>
          <p>ClassyFire: ${escapeHtml(taxonomyLine(chem.classyfire))}</p>
          <p>NP Classifier: ${escapeHtml(npLine(chem.npclassifier))}</p>
          ${marker.llm_rationale ? `<p>${escapeHtml(marker.llm_rationale)}</p>` : ""}
        </article>`;
    })
    .join("");
  const justification = doc.justification || {};
  const pending = doc.status === "pending";
  el.candidateDetail.innerHTML = `
    <article class="candidate-card">
      <h3>${escapeHtml(doc.query || "candidate")} · ${escapeHtml(doc.status || "")}</h3>
      <p>${escapeHtml((doc.taxonomy && doc.taxonomy.scientific_name) || "")} · ${escapeHtml(doc.ingredient_id || "no id yet")} · approval stays on this page, not in a stored list</p>
      <p>${escapeHtml(imppatLine(doc.imppat))}</p>
      <p>${escapeHtml(justification.status === "ok" ? justification.narrative || "" : "LLM justification unavailable.")}</p>
      ${pending ? `<div class="actions"><button type="button" class="primary" data-approve-candidate="local">Approve for this formulation</button><button type="button" class="ghost" data-reject-candidate="local">Discard</button></div>` : ""}
    </article>
    ${ayushEvidence(doc.ayush_portal)}
    ${markerHtml}`;
}

function renderCandidateList() {
  el.candidateList.innerHTML = `<p class="draft-empty">Nothing is stored between requests. Research a name to inspect it here.</p>`;
}

function loadCandidates() {
  renderCandidateList();
}

async function proposeCandidate() {
  const query = el.enrichQuery.value.trim();
  if (!query) {
    setStatus("error", "Enter a species or common name to research.");
    return;
  }
  const payload = { query };
  const part = el.enrichPart.value.trim();
  if (part) payload.part_used = part;
  setStatus(null, "Researching…");
  const res = await fetch("/research", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    setStatus("error", `Research failed (${res.status})\n${formatErrorDetail(body && body.detail)}`);
    return;
  }
  lastCandidate = body;
  renderCandidateDetail(body);
  setStatus("ok", "Research finished. Approve to hold it on this formulation.");
}

async function decideCandidate(_id, action) {
  if (action !== "approve") {
    lastCandidate = null;
    el.candidateDetail.innerHTML = "";
    setStatus("ok", "Discarded. Nothing was stored.");
    return;
  }
  if (!lastCandidate) {
    setStatus("error", "Research an ingredient before approving it.");
    return;
  }
  const res = await fetch("/research/approve", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({ candidate: lastCandidate }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    setStatus("error", `Approval failed (${res.status})\n${formatErrorDetail(body && body.detail)}`);
    return;
  }
  rememberApproval(body);
  addRegistryIngredient(body.ingredient_id);
  renderCandidateDetail(body);
  setStatus("ok", `Approved ${(body.ingredient && body.ingredient.botanical_name) || body.ingredient_id} (${body.ingredient_id}) for this formulation. Run Modernize uses this approval.`);
}

async function boot() {
  try {
    await loadIngredients();
  } catch (err) {
    setStatus("error", `Could not prepare the form.\n${err.message}`);
  }
  try {
    await loadDraftList();
  } catch (err) {
    setStatus("error", `Could not load drafts.\n${err.message}`);
  }
}

boot();
