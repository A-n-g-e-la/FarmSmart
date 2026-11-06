(() => {
  const form = document.getElementById("plot-form");
  if (!form) return;
  const options = JSON.parse(document.getElementById("plot-options").textContent);
  const fields = {
    plot_name: document.getElementById("plot_name"),
    area_acres: document.getElementById("area_acres"),
    location: document.getElementById("plot_location"),
    soil_type: document.getElementById("soil_type"),
    farming_type: document.getElementById("farming_type"),
    county: document.getElementById("county"),
    sub_county: document.getElementById("sub_county"),
    ward: document.getElementById("ward"),
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
  const selectionStatus = document.getElementById("plot-selection-status");
  const selectedDetails = document.getElementById("selected-plot-details");
  const editButton = document.getElementById("edit-plot");
  const editPanel = document.getElementById("edit-plot-panel");
  const editForm = document.getElementById("edit-plot-form");
  const editFieldset = document.getElementById("edit-plot-fields");
  const editSave = document.getElementById("save-plot-changes");
  const editCancel = document.getElementById("cancel-plot-edit");
  const editStatus = document.getElementById("edit-plot-status");
  const editError = document.getElementById("edit-plot-error");
  const editFields = Object.fromEntries(Object.keys(fields).map((name) => [name, document.getElementById(`edit-${name}`)]));
  const administrative = ["county", "sub_county", "ward"];

  function selectOptions(select, choices, value = "", legacy = false) {
    const entries = [["", "Select / not provided"], ...choices.map((item) => [item, item])];
    if (legacy && value && !choices.includes(value)) entries.push([value, `Previously entered: ${value}`]);
    select.replaceChildren(...entries.map(([key, label]) => {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = label;
      return option;
    }));
    select.value = choices.includes(value) || legacy ? value : "";
  }

  function locationOptions(inputs, plot = null) {
    inputs.county.value = plot ? plot.county || "" : options.county;
    selectOptions(inputs.sub_county, Object.keys(options.sub_counties), plot?.sub_county || "");
    const wards = Object.hasOwn(options.sub_counties, inputs.sub_county.value)
      ? options.sub_counties[inputs.sub_county.value] : [];
    selectOptions(inputs.ward, wards, plot?.ward || "");
    inputs.ward.disabled = !wards.length;
    selectOptions(inputs.soil_type, options.soil_types, plot?.soil_type || "", Boolean(plot));
    selectOptions(inputs.farming_type, options.farming_types, plot?.farming_type || "", Boolean(plot));
  }
  locationOptions(fields);
  locationOptions(editFields, {});
  for (const inputs of [fields, editFields]) inputs.sub_county.addEventListener("change", () => {
    inputs.county.value = options.county;
    const wards = Object.hasOwn(options.sub_counties, inputs.sub_county.value)
      ? options.sub_counties[inputs.sub_county.value] : [];
    selectOptions(inputs.ward, wards);
    inputs.ward.disabled = !wards.length;
  });
  let editPlotId = null;
  let editBaseline = {};
  let editRevision = 0;
  let editSaving = false;
  let selectedPlotId = null;
  let selectionRevision = 0;
  let availablePlots = new Map();
  let plotControls = new Map();
  let saving = false;
  let loading = false;
  let expired = false;

  function syncControls() {
    const busy = loading || saving || editSaving;
    fieldset.disabled = expired || busy;
    editFieldset.disabled = expired || busy;
    refresh.disabled = expired || busy;
    editButton.disabled = expired || busy;
    toggle.disabled = saving;
  }

  function closeEdit(moveFocus = false) {
    editRevision++;
    editPlotId = null;
    editBaseline = {};
    editForm.reset();
    editPanel.hidden = true;
    editButton.setAttribute("aria-expanded", "false");
    editError.hidden = true;
    for (const name of Object.keys(editFields)) fieldError(name, "", editFields, "edit-");
    if (moveFocus && !editButton.hidden && !editButton.disabled) editButton.focus();
  }

  editButton.addEventListener("click", () => {
    if (expired || loading || saving || editSaving || !availablePlots.has(selectedPlotId)) return;
    if (!editPanel.hidden) { closeEdit(true); return; }
    const plot = availablePlots.get(selectedPlotId);
    locationOptions(editFields, plot);
    editPlotId = selectedPlotId;
    editRevision++;
    editStatus.textContent = "";
    editError.hidden = true;
    for (const name of Object.keys(editFields)) {
      if (administrative.includes(name)) {
        editBaseline[name] = editFields[name].value;
        continue;
      }
      let value = plot[name] ?? "";
      // Strip only insignificant zeroes; never round a stored six-place area.
      if (name === "area_acres" && value.includes(".")) value = value.replace(/0+$/, "").replace(/\.$/, "");
      editFields[name].value = value;
      editBaseline[name] = value;
    }
    editPanel.hidden = false;
    editButton.setAttribute("aria-expanded", "true");
    editFields.plot_name.focus();
  });
  editCancel.addEventListener("click", () => { if (!editSaving) closeEdit(true); });
  editPanel.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !editSaving) { event.preventDefault(); closeEdit(true); }
  });

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
        selectedPlotId = null;
        availablePlots.clear();
        plotControls.clear();
        resetSelectionView("Please log in again to select a farm plot.");
        count.textContent = total.textContent = "Not available yet";
        empty.hidden = true;
        document.getElementById("plot-session").hidden = false;
        syncControls();
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

  function fieldError(name, message, inputs = fields, prefix = "") {
    const error = document.getElementById(`${prefix}${name}-error`);
    inputs[name].setAttribute("aria-invalid", String(Boolean(message)));
    error.textContent = message;
    error.hidden = !message;
  }

  function validatePlotInputs(data) {
    const errors = {};
    for (const [name, limit] of [["plot_name", 100], ["location", 150], ["soil_type", 100], ["farming_type", 100]]) {
      if (Object.hasOwn(data, name) && Array.from(data[name]).length > limit) errors[name] = `Use at most ${limit} characters.`;
    }
    if (Object.hasOwn(data, "plot_name") && !data.plot_name) errors.plot_name = "Enter a plot name.";
    for (const [name, choices] of [["soil_type", options.soil_types], ["farming_type", options.farming_types]]) {
      if (Object.hasOwn(data, name) && data[name] && !choices.includes(data[name])) errors[name] = "Select a listed option or Unknown.";
    }
    if (administrative.some((name) => Object.hasOwn(data, name))) {
      if (data.county !== options.county) errors.county = "This prototype supports Uasin Gishu only.";
      const wards = Object.hasOwn(options.sub_counties, data.sub_county) ? options.sub_counties[data.sub_county] : [];
      if (!wards.length) errors.sub_county = "Select a sub-county.";
      if (!wards.includes(data.ward)) errors.ward = "Select a ward belonging to this sub-county.";
    }
    if (Object.hasOwn(data, "area_acres") && (data.area_acres.length > 32 || !/^[0-9]+(\.[0-9]{1,3})?$/.test(data.area_acres)
        || Number(data.area_acres) < 0.001 || Number(data.area_acres) > 999999.999)) {
      errors.area_acres = "Use acres from 0.001 to 999999.999 with at most 3 decimal places.";
    }
    return errors;
  }

  // The database stores six decimal places; sum millionths of an acre exactly.
  function millionths(value) {
    if (typeof value !== "string" || !/^[0-9]{1,6}(\.[0-9]{1,6})?$/.test(value)) throw new Error();
    const [whole, fraction = ""] = value.split(".");
    const amount = BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
    if (amount <= 0n || amount > 999999999999n) throw new Error();
    return amount;
  }

  function resetSelectionView(message) {
    closeEdit();
    editButton.hidden = true;
    selectedDetails.replaceChildren();
    selectedDetails.hidden = true;
    selectionStatus.textContent = message;
  }

  function updateSelection() {
    const selected = availablePlots.get(selectedPlotId);
    if (!selected) selectedPlotId = null;
    for (const [id, control] of plotControls) {
      const active = id === selectedPlotId;
      control.button.setAttribute("aria-pressed", String(active));
      control.indicator.hidden = !active;
    }
    if (!selected) {
      resetSelectionView("Select a farm plot to view its details.");
      return;
    }
    const rows = [
      ["Plot name", selected.plot_name], ["Area", `${selected.area_acres} acres`],
      ["County", selected.county], ["Sub-county", selected.sub_county], ["Ward", selected.ward],
      ["Location details", selected.location], ["Predominant soil texture", selected.soil_type],
      ["Water supply method", selected.farming_type],
    ].map(([label, value]) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value || "Not provided";
      row.append(term, description);
      return row;
    });
    selectedDetails.replaceChildren(...rows);
    selectedDetails.hidden = false;
    editButton.hidden = false;
    syncControls();
    selectionStatus.textContent = `Selected plot: ${selected.plot_name}.`;
  }

  function renderPlots(plots) {
    if (!Array.isArray(plots)) throw new Error();
    let area = 0n;
    const nextPlots = new Map();
    const nextControls = new Map();
    const items = plots.map((plot) => {
      area += millionths(plot.area_acres);
      if (!Number.isSafeInteger(plot.plot_id) || plot.plot_id <= 0 || nextPlots.has(plot.plot_id)
          || typeof plot.plot_name !== "string") throw new Error();
      for (const value of [plot.location, plot.soil_type, plot.farming_type, plot.county ?? null, plot.sub_county ?? null, plot.ward ?? null]) {
        if (value !== null && typeof value !== "string") throw new Error();
      }
      nextPlots.set(plot.plot_id, plot);
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "plot-select";
      button.setAttribute("data-plot-id", String(plot.plot_id));
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-controls", "selected-plot-details");
      const title = document.createElement("span");
      title.className = "plot-card-name";
      title.textContent = plot.plot_name;
      const acreage = document.createElement("span");
      acreage.textContent = `${plot.area_acres} acres`;
      button.append(title, acreage);
      const address = [plot.county, plot.sub_county, plot.ward, plot.location].filter(Boolean).join(" / ");
      if (address) {
        const location = document.createElement("span");
        location.textContent = address;
        button.append(location);
      }
      const indicator = document.createElement("span");
      indicator.className = "plot-card-indicator";
      indicator.textContent = "Selected";
      indicator.hidden = true;
      button.append(indicator);
      button.addEventListener("click", () => {
        if (expired || !availablePlots.has(plot.plot_id)) return;
        if (selectedPlotId !== plot.plot_id) {
          selectionRevision++;
          closeEdit();
          editStatus.textContent = "";
        }
        selectedPlotId = plot.plot_id;
        updateSelection();
      });
      nextControls.set(plot.plot_id, { button, indicator });
      item.append(button);
      return item;
    });
    if (expired) return;
    list.replaceChildren(...items);
    availablePlots = nextPlots;
    plotControls = nextControls;
    updateSelection();
    count.textContent = String(plots.length);
    total.textContent = `${area / 1000000n}.${String(area % 1000000n).padStart(6, "0")} acres`;
    empty.hidden = plots.length !== 0;
  }

  async function loadPlots() {
    if (loading || expired) return;
    loading = true;
    syncControls();
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
      availablePlots.clear();
      plotControls.clear();
      resetSelectionView(expired ? "Please log in again to select a farm plot."
        : "Refresh farm plots to view their details.");
      count.textContent = total.textContent = "Not available yet";
      empty.hidden = true;
      loadStatus.textContent = "";
      showError(loadError, expired ? "Please log in again to view your plots." : "Farm plots could not be loaded. Try Refresh plots when your connection or the service is available.");
    } finally {
      loading = false;
      syncControls();
      list.setAttribute("aria-busy", "false");
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || editSaving || loading || expired || panel.hidden) return;
    formError.hidden = true;
    formStatus.textContent = "";
    const data = Object.fromEntries(Object.entries(fields).map(([name, input]) => [name, input.value.trim()]));
    const errors = validatePlotInputs(data);
    for (const name of Object.keys(fields)) fieldError(name, errors[name] || "");
    if (Object.keys(errors).length) {
      showError(formError, "Please check the highlighted fields.", true);
      return;
    }
    saving = true;
    syncControls();
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
        locationOptions(fields);
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
      syncControls();
      form.setAttribute("aria-busy", "false");
      save.textContent = "Register farm plot";
      if (registered) setExpanded(false, true);
    }
  });
  editForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (editSaving || saving || loading || expired || editPanel.hidden || editPlotId !== selectedPlotId) return;
    editError.hidden = true;
    editStatus.textContent = "";
    const changes = Object.fromEntries(Object.entries(editFields)
      .map(([name, input]) => [name, input.value.trim()])
      .filter(([name, value]) => value !== editBaseline[name]));
    if (administrative.some((name) => Object.hasOwn(changes, name))) {
      for (const name of administrative) changes[name] = editFields[name].value;
    }
    const errors = validatePlotInputs(changes);
    for (const name of Object.keys(editFields)) fieldError(name, errors[name] || "", editFields, "edit-");
    if (Object.keys(errors).length) { showError(editError, "Please check the highlighted fields.", true); return; }
    if (!Object.keys(changes).length) { editStatus.textContent = "No changes to save."; return; }
    for (const name of ["location", "soil_type", "farming_type"]) {
      if (changes[name] === "") changes[name] = null;
    }
    const targetId = editPlotId;
    const revision = editRevision;
    const currentEditor = () => editPlotId === targetId && editRevision === revision;
    let submitted = false;
    let updated = false;
    editSaving = true;
    syncControls();
    editForm.setAttribute("aria-busy", "true");
    editSave.textContent = "Saving...";
    try {
      const tokenResult = await api("/farmer/plots/csrf-token");
      if (expired || !currentEditor()) return;
      if (!tokenResult.response.ok || !tokenResult.data || tokenResult.data.ok !== true
          || typeof tokenResult.data.csrf_token !== "string" || !tokenResult.data.csrf_token) {
        showError(editError, "Editing could not be prepared. Please try again later.", true); return;
      }
      submitted = true;
      const { response, data } = await api(`/farmer/plots/${targetId}`, {
        method: "PATCH", headers: { "Content-Type": "application/json", "X-CSRF-Token": tokenResult.data.csrf_token },
        body: JSON.stringify(changes),
      });
      if (expired) return;
      if (response.ok && data && data.ok === true && data.plot_id === targetId) {
        updated = true;
        if (currentEditor()) { closeEdit(); editStatus.textContent = "Farm plot updated successfully."; }
        await loadPlots();
      } else if (currentEditor()) {
        if (response.status === 400) {
          for (const name of Object.keys(editFields)) {
            if (data && data.errors && Object.hasOwn(data.errors, name)) fieldError(name, name === "area_acres"
              ? "Use acres from 0.001 to 999999.999 with at most 3 decimal places."
              : `Check this field (maximum ${name === "location" ? 150 : 100} characters).`, editFields, "edit-");
          }
          showError(editError, "Please check the highlighted plot details.", true);
        } else if (response.status === 403) {
          showError(editError, "Your request could not be verified. Save again to obtain a fresh security token.", true);
        } else if (response.status === 404) {
          const failureSelectionRevision = selectionRevision;
          closeEdit();
          await loadPlots();
          // Refresh may clear a missing plot; only a user switch invalidates its feedback.
          if (!expired && selectionRevision === failureSelectionRevision
              && (selectedPlotId === targetId || selectedPlotId === null)) {
            showError(editError, "This plot is no longer available. Check the refreshed plots before editing again.", true);
          }
        } else {
          showError(editError, "Unable to confirm the update. Refresh plots and check the saved details before trying again.", true);
        }
      }
    } catch {
      if (currentEditor() && !expired) showError(editError, submitted
        ? "Unable to confirm the update. Refresh plots and check the saved details before trying again."
        : "Unable to prepare editing. Check your connection and try again.", true);
    } finally {
      editSaving = false;
      syncControls();
      editForm.setAttribute("aria-busy", "false");
      editSave.textContent = "Save Changes";
      if (updated && selectedPlotId === targetId && !editButton.hidden) editButton.focus();
    }
  });
  editPanel.hidden = true;
  editButton.hidden = true;
  refresh.addEventListener("click", () => { if (!saving && !editSaving) return loadPlots(); });
  fieldset.disabled = false;
  loadPlots();
})();
