(() => {
  const form = document.getElementById("plot-form");
  if (!form) return;
  const fields = {
    plot_name: document.getElementById("plot_name"),
    area_acres: document.getElementById("area_acres"),
    location: document.getElementById("plot_location"),
    soil_type: document.getElementById("soil_type"),
    farming_type: document.getElementById("farming_type"),
  };
  const fieldset = document.getElementById("plot-fields");
  const save = document.getElementById("save-plot");
  const refresh = document.getElementById("refresh-plots");
  const list = document.getElementById("plot-list");
  const count = document.getElementById("plot-count");
  const total = document.getElementById("plot-total-area");
  const empty = document.getElementById("plots-empty");
  const loadStatus = document.getElementById("plots-status");
  const loadError = document.getElementById("plots-error");
  const formStatus = document.getElementById("plot-form-status");
  const formError = document.getElementById("plot-form-error");
  const panel = document.getElementById("add-plot");
  const toggle = document.getElementById("toggle-add-plot");
  const quickAdd = document.getElementById("quick-add-plot");
  let saving = false;
  let loading = false;
  let expired = false;

  function setExpanded(expanded, moveFocus = false) {
    panel.hidden = !expanded;
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = expanded ? "- Hide form" : "+ Add Farm Plot";
    if (moveFocus) {
      if (!expanded) toggle.focus();
      else if (fieldset.disabled) panel.focus({ preventScroll: true });
      else fields.plot_name.focus({ preventScroll: true });
    }
  }

  toggle.addEventListener("click", () => {
    if (!saving) setExpanded(panel.hidden, true);
  });
  quickAdd.addEventListener("click", (event) => {
    event.preventDefault();
    setExpanded(true, true);
    panel.scrollIntoView({ block: "start" });
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !saving) {
      event.preventDefault();
      setExpanded(false, true);
    }
  });
  setExpanded(false);
  toggle.disabled = false;

  async function api(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        ...options, credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: controller.signal,
        headers: { Accept: "application/json", ...options.headers },
      });
      if (response.status === 401) {
        expired = true;
        fieldset.disabled = true;
        refresh.disabled = true;
        list.replaceChildren();
        count.textContent = total.textContent = "Not available yet";
        empty.hidden = true;
        document.getElementById("plot-session").hidden = false;
      }
      // Read validation JSON only; other errors never render server diagnostics.
      const data = response.ok || response.status === 400 ? await response.json() : null;
      return { response, data };
    } finally {
      clearTimeout(timer);
    }
  }

  function showError(element, message, focus = false) {
    element.textContent = message;
    element.hidden = false;
    if (focus) element.focus();
  }

  function fieldError(name, message) {
    const error = document.getElementById(`${name}-error`);
    fields[name].setAttribute("aria-invalid", String(Boolean(message)));
    error.textContent = message;
    error.hidden = !message;
  }

  // The database stores six decimal places; sum millionths of an acre exactly.
  function millionths(value) {
    if (typeof value !== "string" || !/^[0-9]{1,6}(\.[0-9]{1,6})?$/.test(value)) throw new Error();
    const [whole, fraction = ""] = value.split(".");
    const amount = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
    if (amount <= 0n || amount > 999999999999n) throw new Error();
    return amount;
  }

  function renderPlots(plots) {
    if (!Array.isArray(plots)) throw new Error();
    let area = 0n;
    const items = plots.map((plot) => {
      area += millionths(plot.area_acres);
      if (typeof plot.plot_name !== "string") throw new Error();
      const item = document.createElement("li");
      const title = document.createElement("h3");
      title.textContent = plot.plot_name;
      const details = document.createElement("dl");
      for (const [label, value] of [
        ["Area", `${plot.area_acres} acres`], ["Location", plot.location],
        ["Soil type", plot.soil_type], ["Farming type", plot.farming_type],
      ]) {
        if (value !== null && typeof value !== "string") throw new Error();
        const row = document.createElement("div");
        const term = document.createElement("dt");
        const description = document.createElement("dd");
        term.textContent = label;
        description.textContent = value || "Not provided";
        row.append(term, description);
        details.append(row);
      }
      item.append(title, details);
      return item;
    });
    if (expired) return;
    list.replaceChildren(...items);
    count.textContent = String(plots.length);
    total.textContent = `${area / 1000000n}.${String(area % 1000000n).padStart(6, "0")} acres`;
    empty.hidden = plots.length !== 0;
  }

  async function loadPlots() {
    if (loading || expired) return;
    loading = true;
    fieldset.disabled = true;
    refresh.disabled = true;
    loadError.hidden = true;
    loadStatus.textContent = "Loading farm plots...";
    list.setAttribute("aria-busy", "true");
    try {
      const { response, data } = await api("/farmer/plots");
      if (!response.ok || !data || data.ok !== true) throw new Error();
      renderPlots(data.plots);
      loadStatus.textContent = expired ? "" : "Farm plots updated.";
    } catch {
      list.replaceChildren();
      count.textContent = total.textContent = "Not available yet";
      empty.hidden = true;
      loadStatus.textContent = "";
      showError(loadError, expired ? "Please log in again to view your plots." : "Farm plots could not be loaded. Try Refresh plots when your connection or the service is available.");
    } finally {
      loading = false;
      fieldset.disabled = expired || saving;
      refresh.disabled = expired || saving;
      list.setAttribute("aria-busy", "false");
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || loading || expired || panel.hidden) return;
    formError.hidden = true;
    formStatus.textContent = "";
    const data = Object.fromEntries(Object.entries(fields).map(([name, input]) => [name, input.value.trim()]));
    const errors = {};
    for (const [name, limit] of [["plot_name", 100], ["location", 150], ["soil_type", 100], ["farming_type", 100]]) {
      if (Array.from(data[name]).length > limit) errors[name] = `Use at most ${limit} characters.`;
    }
    if (!data.plot_name) errors.plot_name = "Enter a plot name.";
    if (data.area_acres.length > 32 || !/^[0-9]+(\.[0-9]{1,3})?$/.test(data.area_acres)
        || Number(data.area_acres) < 0.001 || Number(data.area_acres) > 999999.999) {
      errors.area_acres = "Use acres from 0.001 to 999999.999 with at most 3 decimal places.";
    }
    for (const name of Object.keys(fields)) fieldError(name, errors[name] || "");
    if (Object.keys(errors).length) {
      showError(formError, "Please check the highlighted fields.", true);
      return;
    }
    saving = true;
    toggle.disabled = true;
    fieldset.disabled = true;
    refresh.disabled = true;
    form.setAttribute("aria-busy", "true");
    save.textContent = "Registering...";
    let submitted = false;
    let registered = false;
    try {
      const tokenResult = await api("/farmer/plots/csrf-token");
      if (expired) throw new Error();
      if (!tokenResult.response.ok || !tokenResult.data || tokenResult.data.ok !== true
          || typeof tokenResult.data.csrf_token !== "string" || !tokenResult.data.csrf_token) {
        showError(formError, "Plot registration could not be prepared. Please try again later.", true);
        return;
      }
      submitted = true;
      const { response, data: result } = await api("/farmer/plots", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": tokenResult.data.csrf_token },
        body: JSON.stringify(data),
      });
      if (expired) throw new Error();
      if (response.status === 400) {
        const invalid = result && result.errors;
        // Map only known fields to fixed messages; never render server HTML/diagnostics.
        for (const name of Object.keys(fields)) {
          if (invalid && Object.hasOwn(invalid, name)) fieldError(name, name === "area_acres"
            ? "Use acres from 0.001 to 999999.999 with at most 3 decimal places."
            : `Check this field (maximum ${name === "location" ? 150 : 100} characters).`);
        }
        showError(formError, "Please check your plot details and the highlighted fields.", true);
      } else if (response.status === 403) {
        showError(formError, "Your request could not be verified. Please submit again to obtain a fresh security token.", true);
      } else if (!response.ok || !result || result.ok !== true || !Number.isInteger(result.plot_id) || result.plot_id <= 0) {
        showError(formError, "Unable to confirm registration. Refresh plots and check whether the plot was saved before trying again.", true);
      } else {
        form.reset();
        formStatus.textContent = "Farm plot registered successfully.";
        registered = true;
        await loadPlots();
      }
    } catch {
      showError(formError, expired ? "Your session could not be verified. Please log in again."
        : submitted ? "Unable to confirm registration. Refresh plots and check whether the plot was saved before trying again."
          : "Unable to prepare registration. Check your connection and try again.", true);
    } finally {
      saving = false;
      toggle.disabled = false;
      fieldset.disabled = expired;
      refresh.disabled = expired || loading;
      form.setAttribute("aria-busy", "false");
      save.textContent = "Register farm plot";
      if (registered) setExpanded(false, true);
    }
  });
  refresh.addEventListener("click", () => { if (!saving) return loadPlots(); });
  fieldset.disabled = false;
  loadPlots();
})();
