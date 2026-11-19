/* UI orchestration only: nutrient, yield and financial calculations run on the server. */
(() => {
  "use strict";
  const form = document.getElementById("agricultural-plan-form");
  if (!form) return;
  const el = (id) => document.getElementById(id);
  const products = el("plan-products"), saved = el("saved-plan"), comparisons = el("plan-comparisons");
  let plotId = null, planId = null, revision = 0, busy = false;
  function message(text, error = false) {
    el("plan-message").textContent = error ? "" : text;
    el("plan-error").textContent = error ? text : "";
    el("plan-error").hidden = !error;
  }
  async function api(url, method = "GET", data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const headers = {Accept: "application/json"};
      if (method !== "GET") {
        const tokenResponse = await fetch("/farmer/agricultural-plans/csrf", {credentials:"same-origin",cache:"no-store",redirect:"error",signal:controller.signal});
        if (!tokenResponse.ok) throw new Error("Your session could not be verified. Log in again.");
        const token = await tokenResponse.json();
        headers["X-CSRF-Token"] = token.csrf_token; headers["Content-Type"] = "application/json";
      }
      const response = await fetch(url, {method,headers,credentials:"same-origin",cache:"no-store",redirect:"error",
        signal:controller.signal,...(data === undefined ? {} : {body:JSON.stringify(data)})});
      const result = await response.json();
      if (!response.ok || result.ok !== true) {
        if (response.status === 401) throw new Error("Your session expired. Log in again.");
        if (response.status === 403) throw new Error("The request could not be verified. Try again.");
        if (response.status === 409) throw new Error("A plan already exists for this plot, year and season.");
        throw new Error(response.status === 400 ? (result.message || "Check the plan inputs.") : "The plan could not be loaded or saved. Refresh before retrying.");
      }
      return result.result;
    } finally { clearTimeout(timer); }
  }
  function clearComparisons() { comparisons.replaceChildren(); el("save-plan-choice").hidden = true; }
  function resetEditor() {
    planId = null; form.reset(); products.replaceChildren(); clearComparisons();
    form.elements.planting_year.value = String(new Date().getFullYear());
    form.elements.planting_year.disabled = false; form.elements.planting_season.disabled = false;
    el("plan-editor-title").textContent = "New seasonal plan";
    el("saved-selection").textContent = "";
    el("compare-plan").hidden = el("delete-plan").hidden = true;
  }
  function addProduct(product = {}) {
    const row = el("plan-product-template").content.firstElementChild.cloneNode(true);
    const input = (name) => row.querySelector(`[name="${name}"]`);
    input("product").value = product.product || "";
    input("quantity_kg").value = product.quantity_kg ?? "";
    for (const key of ["N","P2O5","K2O"]) input(key).value = product.grade?.[key] ?? "";
    const quote = product.pricing || {};
    const mode = ["price_kes_per_kg","price_kes_per_bag","total_cost_kes"].find((key) => Object.hasOwn(quote,key)) || "price_kes_per_kg";
    input("price_mode").value = mode; input("price").value = quote[mode] ?? "";
    input("bag_weight_kg").value = quote.bag_weight_kg ?? 50;
    const syncBag = () => { input("bag_weight_kg").disabled = input("price_mode").value !== "price_kes_per_bag"; };
    input("price_mode").addEventListener("change",syncBag); syncBag();
    row.querySelector(".remove-product").addEventListener("click",() => { if (!busy) row.remove(); });
    products.append(row);
  }
  async function loadPlans(id, version, selectedId = null) {
    const rows = await api(`/farmer/plots/${id}/agricultural-plans`);
    if (version !== revision) return;
    saved.replaceChildren(new Option("New seasonal plan", ""), ...rows.map((p) => new Option(`${p.planting_year} · ${p.planting_season} · ${p.variety}`, String(p.plan_id))));
    saved.value = selectedId ? String(selectedId) : "";
    if (selectedId) await loadPlan(selectedId,version);
  }
  async function loadPlan(id, version) {
    const p = await api(`/farmer/agricultural-plans/${id}`);
    if (version !== revision || p.plot_id !== plotId) return;
    resetEditor(); planId = p.plan_id;
    for (const name of ["planting_year","planting_season","variety","farmgate_price_kes_kg","seed_cost_kes","labour_cost_kes","other_cost_kes"]) form.elements[name].value = p[name];
    form.elements.planting_year.disabled = form.elements.planting_season.disabled = true;
    p.fertilizers.forEach(addProduct);
    el("plan-editor-title").textContent = "Edit saved seasonal plan";
    el("saved-selection").textContent = p.selection ? `Saved choice: ${p.selection.scenario_id} (${p.selection.scaling_percentage}%).` : "No preferred option saved.";
    el("compare-plan").hidden = el("delete-plan").hidden = false;
  }
  async function choosePlot(id) {
    revision++; plotId = id; resetEditor(); saved.replaceChildren(); message("");
    el("plan-workspace").hidden = !id;
    el("plan-plot-status").textContent = id ? `Plans for selected plot #${id}` : "Select one of your plots above to manage its plans.";
    if (id) try { await loadPlans(id,revision); } catch (error) { message(error.message,true); }
  }
  document.addEventListener("farmadvis:plot-selected",(event) => choosePlot(event.detail.plotId));
  document.addEventListener("farmadvis:plots-refreshed",() => { if (plotId && !busy) choosePlot(plotId); });
  document.getElementById("logout-button")?.addEventListener("click",() => choosePlot(null));
  el("add-plan-product").addEventListener("click",() => { if (!busy && products.children.length < 12) addProduct(); });
  saved.addEventListener("change",async () => {
    if (busy) return;
    revision++; clearComparisons();
    if (!saved.value) { resetEditor(); return; }
    try { await loadPlan(Number(saved.value),revision); } catch (error) { message(error.message,true); }
  });
  form.addEventListener("submit",async (event) => {
    event.preventDefault(); if (!plotId || busy) return;
    if (!form.reportValidity()) return;
    const id = plotId, editing = planId, version = revision;
    const data = Object.fromEntries(["planting_year","planting_season","variety","farmgate_price_kes_kg","seed_cost_kes","labour_cost_kes","other_cost_kes"].map((name) => [name,form.elements[name].value]));
    if (editing) { delete data.planting_year; delete data.planting_season; }
    data.fertilizers = []; data.prices = {};
    try {
      for (const row of products.children) {
        const value = (name) => row.querySelector(`[name="${name}"]`).value.trim();
        const product = value("product").toUpperCase(), mode = value("price_mode");
        const item = {product,quantity_kg:value("quantity_kg")};
        const grade = Object.fromEntries(["N","P2O5","K2O"].map((key) => [key,value(key)]));
        if (Object.values(grade).some(Boolean)) item.grade = grade;
        if (mode === "total_cost_kes") item.total_cost_kes = value("price");
        else {
          const quote = {[mode]:value("price"),...(mode === "price_kes_per_bag" ? {bag_weight_kg:value("bag_weight_kg")} : {})};
          if (data.prices[product] && JSON.stringify(data.prices[product]) !== JSON.stringify(quote)) throw new Error("Use a supplied total cost for repeated products with different quotes.");
          data.prices[product] = quote;
        }
        data.fertilizers.push(item);
      }
      busy = true; el("plan-fields").disabled = true;
      const result = await api(editing ? `/farmer/agricultural-plans/${editing}` : `/farmer/plots/${id}/agricultural-plans`,editing ? "PATCH" : "POST",data);
      if (version !== revision) return;
      message("Plan saved. Previous selections are cleared after an edit."); await loadPlans(id,version,result.plan_id);
    } catch (error) { if (version === revision) message(error.message,true); }
    finally { busy = false; el("plan-fields").disabled = false; }
  });
  el("compare-plan").addEventListener("click",async () => {
    if (!planId || busy) return;
    const id = planId, version = revision; busy = true; clearComparisons();
    try {
      const result = await api(`/farmer/agricultural-plans/${id}/comparisons`);
      if (version !== revision) return;
      for (const row of result.scenarios) {
        const card = document.createElement("article"); card.className = "scenario-card";
        const heading = document.createElement("h3"); heading.textContent = `${row.name} (${row.scaling_percentage}%)${row.is_current_plan ? " — current plan" : ""}`; card.append(heading);
        const productsText = document.createElement("p"); productsText.textContent = row.fertilizer.products.map((p) => `${p.product}: ${p.quantity_kg} kg`).join("; ") || "No fertilizer"; card.append(productsText);
        if (row.status !== "available") {
          const text = document.createElement("p"); text.textContent = `Unavailable: ${row.unavailable_reason}`; card.append(text);
        } else {
          const details = document.createElement("dl");
          for (const [label,key] of [["Yield (kg/ha)","predicted_yield_kg_ha"],["Production (kg)","expected_production_kg"],["Fertilizer cost (KES)","fertilizer_cost_kes"],["Total cost (KES)","total_cost_kes"],["Revenue (KES)","revenue_kes"],["Profit (KES)","net_profit_kes"],["ROI (%)","roi_percent"]]) {
            const group = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
            dt.textContent = label; dd.textContent = row[key] === null ? "Not defined (zero cost)" : Number(row[key]).toLocaleString(undefined,{maximumFractionDigits:2}); group.append(dt,dd); details.append(group);
          }
          card.append(details);
          const label = document.createElement("label"), radio = document.createElement("input");
          radio.type = "radio"; radio.name = "preferred-scenario"; radio.value = row.scenario_id;
          label.append(radio,document.createTextNode(" Choose this option")); card.append(label);
        }
        comparisons.append(card);
      }
      const note = document.createElement("p"); note.textContent = result.warnings.join(" "); comparisons.append(note);
      el("save-plan-choice").hidden = !result.scenarios.some((row) => row.status === "available");
      message("All evaluated options are shown. No option has been selected automatically.");
    } catch (error) { if (version === revision) message(error.message,true); }
    finally { busy = false; }
  });
  el("save-plan-choice").addEventListener("click",async () => {
    const choice = comparisons.querySelector('input[name="preferred-scenario"]:checked');
    if (!choice) { message("Choose an available option first.",true); return; }
    if (!planId || busy) return;
    const id = planId, version = revision; busy = true;
    try {
      const result = await api(`/farmer/agricultural-plans/${id}/selection`,"PUT",{scenario_id:choice.value});
      if (version === revision) { el("saved-selection").textContent = `Saved choice: ${result.scenario_id} (${result.scaling_percentage}%).`; message("Your choice was saved separately; the original plan is unchanged."); }
    } catch (error) { if (version === revision) message(error.message,true); }
    finally { busy = false; }
  });
  el("delete-plan").addEventListener("click",async () => {
    if (!planId || busy || !window.confirm("Delete this seasonal plan and its saved choice?")) return;
    const id = planId, plot = plotId, version = revision; busy = true;
    try { await api(`/farmer/agricultural-plans/${id}`,"DELETE",{}); if (version === revision) { resetEditor(); await loadPlans(plot,version); message("Seasonal plan deleted."); } }
    catch (error) { if (version === revision) message(error.message,true); }
    finally { busy = false; }
  });
  resetEditor();
})();
