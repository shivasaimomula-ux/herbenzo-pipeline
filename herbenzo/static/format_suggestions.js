/* Advisory format panel. Reads the compose selection and POSTs /suggest-formats.
   A card click only fills the free-string finished-form field. */

(function () {
  const panel = document.getElementById("format-suggestions");
  const cards = document.getElementById("format-suggestion-cards");
  const refresh = document.getElementById("refresh-formats");
  const audience = document.getElementById("format-audience");
  const editors = document.getElementById("ingredient-editors");
  const dosageForm = document.getElementById("dosage-form");
  const productName = document.getElementById("product-name");
  if (!panel || !cards || !editors) return;

  let timer = 0;
  let requestSerial = 0;

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function selection() {
    const ids = [];
    const quantities = {};
    const marked = editors.querySelectorAll("[data-ingredient-id]");
    const cardsInForm = [...editors.querySelectorAll(".ing-editor")];
    if (!marked.length && cardsInForm.length) {
      for (const card of cardsInForm) {
        const select = card.querySelector(".ing-id, select");
        const id = select && select.value.trim();
        if (!id) continue;
        if (!ids.includes(id)) ids.push(id);
        const qty = card.querySelector(".qty");
        if (!qty || !qty.value.trim()) continue;
        const amount = Number(qty.value);
        if (Number.isFinite(amount) && amount > 0) quantities[id] = amount;
      }
      return { ids, quantities };
    }
    marked.forEach((node) => {
      const id = (node.dataset.ingredientId || "").trim();
      if (id && !ids.includes(id)) ids.push(id);
    });
    return { ids, quantities };
  }

  function fillDosageForm(label) {
    if (!dosageForm) return;
    dosageForm.value = label;
    dosageForm.dispatchEvent(new Event("input", { bubbles: true }));
    dosageForm.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function renderEmpty(message) {
    cards.innerHTML = `<p class="format-empty">${escapeHtml(message)}</p>`;
  }

  function renderError(message) {
    cards.innerHTML = `<p class="format-error">${escapeHtml(message)}</p>`;
  }

  function renderSuggestions(body) {
    const rows = Array.isArray(body.suggestions) ? body.suggestions : [];
    if (!rows.length) {
      renderEmpty("No formats were ranked for this selection.");
      return;
    }
    const note = body.classical_like
      ? "Classical or Chyawanprash-like name detected. Nanoemulsion stays in the list with an owner caution."
      : "Advisory ranking only.";
    const html = rows
      .map((row) => {
        const reasons = (row.fit_reasons || []).slice(0, 2);
        const cautions = row.cautions || [];
        const refs = row.references || [];
        const refHtml = refs
          .map((ref) => {
            const label = `${ref.brand || "Reference"} — ${ref.category || ""}`.trim();
            const url = ref.url || "";
            if (!url) return "";
            return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a></li>`;
          })
          .join("");
        return `
          <button type="button" class="format-card" data-dosage-form="${escapeHtml(row.dosage_form_label || row.name || "")}">
            <span class="format-card-head">
              <strong>${escapeHtml(row.name || row.format_id || "")}</strong>
              <span class="format-score">${escapeHtml(row.score)} · ${escapeHtml(row.category || "")}</span>
            </span>
            <p class="format-kicker">${escapeHtml(row.format_id || "")}</p>
            ${reasons.map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
            ${cautions.slice(0, 2).map((line) => `<p>${escapeHtml(line)}</p>`).join("")}
            ${refHtml ? `<ul>${refHtml}</ul>` : ""}
          </button>`;
      })
      .join("");
    cards.innerHTML = `<p class="format-empty">${escapeHtml(note)}</p>${html}`;
  }

  async function refreshSuggestions() {
    const { ids, quantities } = selection();
    if (!ids.length) {
      renderEmpty("Select an ingredient to see format suggestions.");
      return;
    }
    const serial = ++requestSerial;
    const payload = { ingredient_ids: ids };
    if (audience && audience.value) payload.audience = audience.value;
    if (dosageForm && dosageForm.value.trim()) payload.dosage_form = dosageForm.value.trim();
    if (productName && productName.value.trim()) payload.product_name = productName.value.trim();
    if (Object.keys(quantities).length) payload.quantities_mg = quantities;
    try {
      const res = await fetch("/suggest-formats", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => null);
      if (serial !== requestSerial) return;
      if (!res.ok) {
        const detail = body && body.detail !== undefined ? body.detail : body;
        const message = detail && detail.message ? detail.message : "Could not rank formats.";
        renderError(message);
        return;
      }
      renderSuggestions(body || {});
    } catch (err) {
      if (serial !== requestSerial) return;
      renderError(err.message || "Could not rank formats.");
    }
  }

  function schedule() {
    window.clearTimeout(timer);
    timer = window.setTimeout(refreshSuggestions, 250);
  }

  cards.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (link) return;
    const card = event.target.closest(".format-card");
    if (!card) return;
    fillDosageForm(card.dataset.dosageForm || "");
  });

  if (refresh) refresh.addEventListener("click", refreshSuggestions);
  if (audience) audience.addEventListener("change", schedule);
  editors.addEventListener("change", schedule);
  editors.addEventListener("input", schedule);
  if (dosageForm) {
    dosageForm.addEventListener("change", schedule);
    dosageForm.addEventListener("input", schedule);
  }
  if (productName) {
    productName.addEventListener("change", schedule);
    productName.addEventListener("input", schedule);
  }
  // Sample load and draft load replace the ingredient editors without a change event.
  const observer = new MutationObserver(schedule);
  observer.observe(editors, { childList: true, subtree: true });

  renderEmpty("Select an ingredient to see format suggestions.");
  window.setTimeout(refreshSuggestions, 400);
})();
