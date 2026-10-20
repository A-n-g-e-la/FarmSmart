(() => {
  "use strict";
  const root = document.getElementById("officer-dashboard");
  if (!root) return;
  const el = (id) => document.getElementById(id);
  const list = el("assigned-farmers"), plots = el("farmer-plots");
  const refresh = el("farmers-refresh"), retry = el("plots-retry");
  const controllers = new Set();
  let farmers = [], selected = null, selectionVersion = 0, loadingList = false, stopped = false;
  let plotController;

  function node(tag, value, className) {
    const item = document.createElement(tag);
    if (value !== undefined) item.textContent = value;
    if (className) item.className = className;
    return item;
  }
  function message(id, value) { el(id).textContent = value; el(id).hidden = !value; }
  function clearDetails() {
    selectionVersion += 1;
    if (plotController) plotController.abort();
    plots.replaceChildren();
    el("selected-farmer").textContent = "";
    message("farmer-plots-error", "");
    retry.hidden = true;
    el("farmer-plots-panel").setAttribute("aria-busy", "false");
  }
  function stop(expired) {
    stopped = true;
    controllers.forEach((controller) => controller.abort());
    clearDetails();
    selected = null;
    farmers = [];
    list.replaceChildren();
    el("assigned-count").textContent = "Not available";
    el("assigned-plot-count").textContent = "Not available";
    el("officer-data").hidden = true;
    refresh.disabled = true;
    message("dashboard-session", expired
      ? "Your session has expired or access is no longer available. Please log in again."
      : "Dashboard data cleared for logout. Reload the dashboard if logout does not complete.");
    el(expired ? "officer-login-again" : "dashboard-reload").hidden = false;
  }
  async function read(url, controller) {
    controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, { method: "GET", credentials: "same-origin", cache: "no-store",
        redirect: "error", headers: { Accept: "application/json" }, signal: controller.signal });
      if (response.status === 401 && !stopped) stop(true);
      return { status: response.status, data: response.ok ? await response.json() : null };
    } finally {
      clearTimeout(timer);
      controllers.delete(controller);
    }
  }
  function renderFarmers() {
    list.replaceChildren();
    for (const farmer of farmers) {
      const button = node("button", undefined, "officer-button officer-farmer");
      button.type = "button";
      const active = farmer.farmer_id === selected;
      button.setAttribute("aria-pressed", String(active));
      button.setAttribute("aria-controls", "farmer-plots-panel");
      button.append(node("span", farmer.name), node("span", farmer.phone_number || "Phone number not available", "farmer-contact"),
        node("span", `${farmer.plot_count} registered plots`, "farmer-count"));
      const indicator = node("span", "Selected farmer", "farmer-selected");
      indicator.hidden = !active;
      button.append(indicator);
      button.addEventListener("click", () => selectFarmer(farmer));
      const item = node("li");
      item.append(button);
      list.append(item);
    }
  }
  function renderPlots(records) {
    plots.replaceChildren();
    for (const plot of records) {
      const card = node("article", undefined, "officer-plot");
      card.append(node("h3", plot.plot_name));
      const details = node("dl");
      for (const [label, value] of [["Area (acres)", plot.area_acres], ["Location", plot.location],
        ["Soil type", plot.soil_type], ["Farming type", plot.farming_type]]) {
        const row = node("div");
        row.append(node("dt", label), node("dd", value === null || value === undefined || value === "" ? "Not provided" : value));
        details.append(row);
      }
      card.append(details);
      plots.append(card);
    }
  }
  async function selectFarmer(farmer, force = false) {
    if (stopped || loadingList || (!force && selected === farmer.farmer_id)) return;
    selected = farmer.farmer_id;
    clearDetails();
    const version = selectionVersion;
    // Update state without replacing focused native buttons during selection.
    Array.from(list.children).forEach((item, index) => {
      const active = farmers[index].farmer_id === selected;
      item.firstChild.setAttribute("aria-pressed", String(active));
      item.firstChild.lastChild.hidden = !active;
    });
    el("selected-farmer").textContent = farmer.name;
    el("farmer-plots-status").textContent = "Loading farm plots…";
    el("farmer-plots-panel").setAttribute("aria-busy", "true");
    plotController = new AbortController();
    try {
      const { status, data } = await read(`/officer/api/farmers/${farmer.farmer_id}/plots`, plotController);
      if (stopped || version !== selectionVersion) return;
      if (status === 404) {
        message("farmer-plots-error", "This farmer is no longer available to you. Refresh the farmer list.");
      } else if (status !== 200) {
        message("farmer-plots-error", "Farm plots are temporarily unavailable. Please try again.");
        retry.hidden = false;
      } else {
        if (!data || data.ok !== true || data.farmer_id !== selected || !Array.isArray(data.plots)) throw new Error("Invalid response");
        renderPlots(data.plots);
        el("farmer-plots-status").textContent = data.plots.length ? "Registered farm plots" : "This farmer has no registered farm plots.";
      }
    } catch {
      if (stopped || version !== selectionVersion) return;
      plots.replaceChildren();
      message("farmer-plots-error", "Could not load farm plots. Check your connection and try again.");
      retry.hidden = false;
    } finally {
      if (!stopped && version === selectionVersion) {
        el("farmer-plots-panel").setAttribute("aria-busy", "false");
        if (!el("farmer-plots-error").hidden) el("farmer-plots-status").textContent = "";
      }
    }
  }
  async function loadFarmers() {
    if (stopped || loadingList) return;
    loadingList = true;
    refresh.disabled = true;
    clearDetails();
    selected = null;
    farmers = [];
    list.replaceChildren();
    el("assigned-count").textContent = "Not available";
    el("assigned-plot-count").textContent = "Not available";
    el("farmer-plots-status").textContent = "Select a farmer to view their registered farm plots.";
    el("farmers-status").textContent = "Loading assigned farmers…";
    el("assigned-farmers-panel").setAttribute("aria-busy", "true");
    message("farmers-error", "");
    try {
      const { status, data } = await read("/officer/api/farmers", new AbortController());
      if (stopped) return;
      if (status !== 200) {
        message("farmers-error", "Assigned farmers are temporarily unavailable. Please refresh the list.");
        return;
      }
      if (!data || data.ok !== true || !Array.isArray(data.farmers) || !data.farmers.every((farmer) =>
        Number.isSafeInteger(farmer.farmer_id) && farmer.farmer_id > 0 && typeof farmer.name === "string"
        && Number.isSafeInteger(farmer.plot_count) && farmer.plot_count >= 0)) throw new Error("Invalid response");
      const total = data.farmers.reduce((sum, farmer) => sum + farmer.plot_count, 0);
      if (!Number.isSafeInteger(total)) throw new Error("Invalid total");
      farmers = data.farmers;
      el("assigned-count").textContent = String(farmers.length);
      el("assigned-plot-count").textContent = String(total);
      renderFarmers();
      el("farmers-status").textContent = farmers.length ? "Select a farmer to view farm plots." : "No farmers are currently assigned to you.";
    } catch {
      if (!stopped) message("farmers-error", "Could not load assigned farmers. Check your connection and refresh the list.");
    } finally {
      loadingList = false;
      if (!stopped) {
        refresh.disabled = false;
        el("assigned-farmers-panel").setAttribute("aria-busy", "false");
        if (!el("farmers-error").hidden) el("farmers-status").textContent = "";
      }
    }
  }
  refresh.addEventListener("click", loadFarmers);
  retry.addEventListener("click", () => {
    const farmer = farmers.find((item) => item.farmer_id === selected);
    if (farmer && el("farmer-plots-panel").getAttribute("aria-busy") !== "true") return selectFarmer(farmer, true);
  });
  // Keep the existing logout implementation; discard dashboard data and pending
  // reads as soon as logout is requested, even if a response arrives late.
  el("officer-logout-button").addEventListener("click", () => stop(false));
  loadFarmers();
})();
