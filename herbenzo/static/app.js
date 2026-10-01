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

const MARKETS = ["US", "EU", "NZ", "AU", "IN", "UK", "CA"];

const el = {
  tabCompose: document.getElementById("tab-compose"),
  tabRaw: document.getElementById("tab-raw"),
  composePanel: document.getElementById("compose-panel"),
  rawPanel: document.getElementById("raw-panel"),
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
  const compose = which === "compose";
  if (!compose && composeTouched && !rawDirty) {
    writeRawFromCompose();
  }
  el.composePanel.hidden = !compose;
  el.rawPanel.hidden = compose;
  el.tabCompose.setAttribute("aria-selected", compose ? "true" : "false");
  el.tabRaw.setAttribute("aria-selected", compose ? "false" : "true");
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
    html += `<option value="${escapeHtml(selectedId)}" selected data-botanical="${escapeHtml(saved.botanical_name || "")}" data-common="${escapeHtml(saved.common_name || "")}" data-part="${escapeHtml(saved.part_used || "")}">${escapeHtml(selectedId)} (not in registry)</option>`;
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
        Registry ingredient
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
  el.specPreview.textContent = JSON.stringify(buildSpec(), null, 2);
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
    if (!ing.ingredient_id) problems.push(`Ingredient ${n}: choose a registry ingredient.`);
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
  const res = await fetch("/ingredients", { headers: { accept: "application/json" } });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(formatErrorDetail(body && body.detail !== undefined ? body.detail : body));
  }
  registry = Array.isArray(body && body.ingredients) ? body.ingredients : [];
  registryById.clear();
  for (const row of registry) registryById.set(row.ingredient_id, row);
  const existing = el.ingredientEditors.querySelector(".ing-editor")
    ? readIngredients()
    : [blankIngredient()];
  renderEditors(existing.length ? existing : [blankIngredient()]);
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
    const forms = Array.isArray(gap.matched_forms) ? gap.matched_forms.join(", ") : "";
    const lines = (Array.isArray(gap.ingredients) ? gap.ingredients : [])
      .map(
        (row) =>
          `<li>${escapeHtml(row.ingredient_id || "ingredient")} — ${escapeHtml(row.reason || row.message || "")}</li>`
      )
      .join("");
    return `
      <div class="advisory-item">
        <strong>Advisory · classical preparation has no established active marker</strong>
        <p>${escapeHtml(gap.message || "classical_active_marker_gap")}</p>
        <p>Matched form: ${escapeHtml(forms || "—")}. This does not block modernization.</p>
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
      const markerName = marker.marker_name || "—";
      const proxy = marker.is_proxy ? " (proxy)" : "";
      const bcsClass = bcs.bcs_class != null ? `Class ${bcs.bcs_class}` : "—";
      const bcsBits = [
        bcs.solubility_call ? `solubility ${bcs.solubility_call}` : null,
        bcs.permeability_call ? `permeability ${bcs.permeability_call}` : null,
        bcs.evidence_basis ? `basis ${bcs.evidence_basis}` : null,
        typeof bcs.confidence === "number" ? `conf ${pct(bcs.confidence)}` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      const primary = String(delivery.primary || "—").replaceAll("_", " ");
      const alts = Array.isArray(delivery.alternatives)
        ? delivery.alternatives.map((a) => String(a).replaceAll("_", " ")).join(", ")
        : "";
      const rationale = Array.isArray(bcs.rationale) ? bcs.rationale : [];
      const delRationale = Array.isArray(delivery.rationale) ? delivery.rationale : [];

      return `
        <article class="ingredient" style="animation-delay: ${0.05 * idx}s">
          <h3>${escapeHtml(ing.ingredient_id || "ingredient")}</h3>
          <p class="sub">${escapeHtml(ing.botanical_name || "")}${
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
    el.resultMeta.textContent = hasAdvisory
      ? "Advisory only — no ModernizedSKU in the response"
      : "No ModernizedSKU in the response";
    el.skuSummary.innerHTML = `<div><dt>ModernizedSKU</dt><dd>—</dd></div>`;
    el.ingredientCards.innerHTML = "";
    el.rawJson.textContent = JSON.stringify(body, null, 2);
    setStatus(
      "ok",
      hasAdvisory
        ? "Modernize returned an advisory and no SKU. Modernization was not reported as an error."
        : "Modernize returned no SKU."
    );
    el.resultPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const advisoryNote = hasAdvisory ? " · advisory" : "";
  setStatus(
    "ok",
    `Modernized · ${sku.sku_id || "SKU"} · confidence ${pct(sku.confidence)}${advisoryNote}`
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
  await postModernize(spec, el.composeRun);
}

async function saveDraft() {
  const name = el.draftName.value.trim();
  if (!name) {
    setStatus("error", "Name the draft before saving.");
    el.draftName.focus();
    return;
  }
  const payload = { name, spec: buildSpec() };
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

el.loadSampleCompose.addEventListener("click", () => {
  currentDraftId = null;
  fillCompose(SAMPLE_SPEC);
  writeRawFromCompose();
  if (!el.draftName.value.trim()) el.draftName.value = "Ashwagandha sample";
  renderDraftList(lastDrafts);
  setStatus("ok", "Loaded ashwagandha sample into the compose form.");
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

async function boot() {
  try {
    await loadIngredients();
  } catch (err) {
    setStatus("error", `Could not load the ingredient registry.\n${err.message}`);
  }
  try {
    await loadDraftList();
  } catch (err) {
    setStatus("error", `Could not load drafts.\n${err.message}`);
  }
}

boot();
