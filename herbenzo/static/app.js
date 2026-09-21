/* Stage B independent UI — FormulationSpec → POST /modernize → ModernizedSKU */

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

const el = {
  input: document.getElementById("spec-input"),
  run: document.getElementById("run-btn"),
  loadSample: document.getElementById("load-sample"),
  file: document.getElementById("file-input"),
  statusPanel: document.getElementById("status-panel"),
  statusText: document.getElementById("status-text"),
  resultPanel: document.getElementById("result-panel"),
  resultMeta: document.getElementById("result-meta"),
  skuSummary: document.getElementById("sku-summary"),
  ingredientCards: document.getElementById("ingredient-cards"),
  rawJson: document.getElementById("raw-json"),
};

function setStatus(kind, message) {
  el.statusPanel.hidden = false;
  el.statusPanel.classList.remove("error", "ok");
  if (kind) el.statusPanel.classList.add(kind);
  el.statusText.textContent = message;
}

function clearResult() {
  el.resultPanel.hidden = true;
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

function renderSku(sku) {
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
    ["Confidence", pct(sku.confidence)],
    ["Inherited confidence", pct(sku.inherited_confidence)],
    ["Market", sku.target_market],
  ];
  const pt = sku.provenance_thread || {};
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
      const primary = (delivery.primary || "—").replaceAll("_", " ");
      const alts = Array.isArray(delivery.alternatives)
        ? delivery.alternatives.map((a) => a.replaceAll("_", " ")).join(", ")
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

  el.rawJson.textContent = JSON.stringify(sku, null, 2);
}

async function modernize() {
  clearResult();
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

  el.run.disabled = true;
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
      setStatus(
        "error",
        `Modernize failed (${res.status})\n${formatErrorDetail(detail)}`
      );
      return;
    }

    setStatus("ok", `Modernized · ${body.sku_id || "SKU"} · confidence ${pct(body.confidence)}`);
    renderSku(body);
  } catch (err) {
    setStatus(
      "error",
      `Network error: ${err.message}\nIs the API running on this origin (port 8003)?`
    );
  } finally {
    el.run.disabled = false;
  }
}

el.loadSample.addEventListener("click", () => {
  el.input.value = JSON.stringify(SAMPLE_SPEC, null, 2);
  setStatus("ok", "Loaded ashwagandha sample FormulationSpec.");
});

el.file.addEventListener("change", async () => {
  const file = el.file.files && el.file.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    JSON.parse(text); // validate early
    el.input.value = text;
    setStatus("ok", `Loaded ${file.name}`);
  } catch (err) {
    setStatus("error", `Could not load file: ${err.message}`);
  } finally {
    el.file.value = "";
  }
});

el.run.addEventListener("click", modernize);

el.input.value = JSON.stringify(SAMPLE_SPEC, null, 2);
