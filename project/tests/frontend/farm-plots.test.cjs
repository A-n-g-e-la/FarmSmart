const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/farm-plots.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const listing = (plots = []) => response(200, { ok: true, plots });
const token = () => response(200, { ok: true, csrf_token: "scoped-token" });
const created = () => response(201, { ok: true, plot_id: 123 });
let nextPlotId = 1;
const plot = (area = "1.250000") => ({ plot_id: nextPlotId++, plot_name: "North plot", area_acres: area,
  location: null, soil_type: "Loam", farming_type: "Mixed practice" });

function element(tag = "div") {
  return {
    tag, value: "", hidden: false, disabled: false, textContent: "", attributes: {}, children: [], listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    focus() { this.focused = true; },
    scrollIntoView(options) { this.scrollOptions = options; },
    set innerHTML(value) { assert.fail("User data must never be interpreted as HTML"); },
  };
}

function setup(responses = []) {
  const names = ["plot_name", "area_acres", "location", "soil_type", "farming_type", "county", "sub_county", "ward"];
  const ids = ["plot-form", "plot-fields", "save-plot", "refresh-plots", "plot-list", "plot-count",
    "plot-total-area", "plots-empty", "plots-status", "plots-error", "plot-form-status", "plot-form-error",
    "plot-session", "plot-options", "plot_name", "area_acres", "plot_location", "soil_type", "farming_type", "county", "sub_county", "ward",
    "add-plot", "toggle-add-plot", "quick-add-plot",
    "plot-selection-status", "selected-plot-details",
    "edit-plot", "edit-plot-panel", "edit-plot-form", "edit-plot-fields", "save-plot-changes", "cancel-plot-edit",
    "edit-plot-status", "edit-plot-error", ...names.map((name) => "edit-" + name),
    ...names.map((name) => "edit-" + name + "-error"),
    ...names.map((name) => name + "-error")];
  const elements = Object.fromEntries(ids.map((id) => [id, element()]));
  elements["plot-options"].textContent = fs.readFileSync(path.join(__dirname, "../../app/data/plot_options.json"), "utf8");
  elements["plot-session"].hidden = true;
  const inputs = names.map((name) => elements[name === "location" ? "plot_location" : name]);
  elements["plot-form"].reset = () => inputs.forEach((input) => { input.value = ""; });
  elements["edit-plot-form"].reset = () => names.forEach((name) => { elements["edit-" + name].value = ""; });
  const calls = [];
  vm.runInNewContext(source, {
    document: { getElementById: (id) => elements[id], createElement: element },
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      if (!responses.length) assert.fail(`Unexpected request: ${url}`);
      const next = await responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  });
  return { elements, calls, responses,
    fill() {
      if (elements["add-plot"].hidden) elements["toggle-add-plot"].listeners.click();
      elements.plot_name.value = " North plot "; elements.area_acres.value = "1.250";
      elements.county.value = "Uasin Gishu";
      elements.sub_county.value = "Moiben";
      elements.sub_county.listeners.change();
      elements.ward.value = "Moiben";
    },
    submit: () => elements["plot-form"].listeners.submit({ preventDefault() {} }),
    refresh: () => elements["refresh-plots"].listeners.click(),
    edit: () => elements["edit-plot"].listeners.click(),
    cancelEdit: () => elements["cancel-plot-edit"].listeners.click(),
    saveEdit: () => elements["edit-plot-form"].listeners.submit({ preventDefault() {} }),
  };
}

function textTree(node) {
  return [node.textContent, ...node.children.map(textTree)].join(" ");
}

const cards = (app) => app.elements["plot-list"].children.map((item) => item.children[0]);

test("editing is available only after selection and prefills every field safely", async () => {
  const sample = { ...plot(), plot_name: '<img src=x onerror="alert(1)">' };
  const app = setup([listing([sample])]);
  await tick();
  assert.equal(app.elements["edit-plot"].hidden, true);
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  cards(app)[0].listeners.click(); app.edit();
  assert.equal(app.elements["edit-plot"].hidden, false);
  assert.equal(app.elements["edit-plot-panel"].hidden, false);
  assert.equal(app.elements["edit-plot"].attributes["aria-expanded"], "true");
  assert.equal(app.elements["edit-plot_name"].value, sample.plot_name);
  assert.equal(app.elements["edit-area_acres"].value, "1.25");
  assert.equal(app.elements["edit-location"].value, "");
  assert.equal(app.elements["edit-soil_type"].value, "Loam");
  assert.equal(app.elements["edit-farming_type"].value, "Mixed practice");
  assert.equal(app.elements["edit-plot_name"].focused, true);
});

test("Cancel and Escape discard edits without requests, preserve add-form draft and restore focus", async () => {
  const app = setup([listing([plot()])]);
  await tick(); cards(app)[0].listeners.click(); app.fill(); app.edit();
  app.elements["edit-plot_name"].value = "Unsaved";
  app.cancelEdit();
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.elements["edit-plot_name"].value, "");
  assert.equal(app.elements["edit-plot"].focused, true);
  assert.equal(app.elements.plot_name.value, " North plot ");
  app.edit();
  assert.equal(app.elements["edit-plot_name"].value, "North plot");
  let prevented = false;
  app.elements["edit-plot-panel"].listeners.keydown({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.calls.length, 1);
});

test("PATCH sends changed fields and null clears, refreshes reordered selected details, and confirms", async () => {
  const selected = { ...plot(), plot_id: 123, location: "Moiben" };
  const other = plot();
  const app = setup([listing([selected, other]), token(), response(200, { ok: true, plot_id: 123 }),
    listing([other, { ...selected, plot_name: "Renamed", location: null }])]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Renamed";
  app.elements["edit-location"].value = " ";
  await app.saveEdit();
  assert.equal(app.calls[1].url, "/farmer/plots/csrf-token");
  assert.equal(app.calls[2].url, "/farmer/plots/123");
  assert.equal(app.calls[2].method, "PATCH");
  assert.equal(app.calls[2].credentials, "same-origin");
  assert.equal(app.calls[2].cache, "no-store");
  assert.equal(app.calls[2].headers["X-CSRF-Token"], "scoped-token");
  assert.deepEqual(JSON.parse(app.calls[2].body), { plot_name: "Renamed", location: null });
  assert.equal(cards(app)[1].attributes["aria-pressed"], "true");
  assert.match(textTree(app.elements["selected-plot-details"]), /Renamed/);
  assert.match(app.elements["edit-plot-status"].textContent, /updated successfully/);
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.elements["edit-plot"].focused, true);
});

test("unchanged six-place acreage is preserved when editing another field", async () => {
  const sample = { ...plot("0.002471"), plot_id: 123 };
  const app = setup([listing([sample]), token(), response(200, { ok: true, plot_id: 123 }), listing([sample])]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-soil_type"].value = "Clay";
  await app.saveEdit();
  assert.deepEqual(JSON.parse(app.calls[2].body), { soil_type: "Clay" });
});

test("unchanged form sends no request and changed values obey registration validation", async () => {
  const app = setup([listing([plot()])]);
  await tick(); cards(app)[0].listeners.click(); app.edit(); await app.saveEdit();
  assert.match(app.elements["edit-plot-status"].textContent, /No changes/);
  for (const value of ["0", "-1", "0.0001", "1.2345", "1.250000", "1000000", "NaN", "1e3"]) {
    app.elements["edit-area_acres"].value = value;
    await app.saveEdit();
    assert.equal(app.elements["edit-area_acres-error"].hidden, false);
    assert.equal(app.elements["edit-plot-panel"].hidden, false);
  }
  app.elements["edit-area_acres"].value = "1.25";
  app.elements["edit-plot_name"].value = "";
  app.elements["edit-location"].value = "x".repeat(151);
  app.elements["edit-farming_type"].value = "x".repeat(101);
  await app.saveEdit();
  for (const name of ["plot_name", "location", "farming_type"]) assert.equal(app.elements[`edit-${name}-error`].hidden, false);
  assert.equal(app.elements["edit-plot-error"].focused, true);
  assert.equal(app.calls.length, 1);
});

test("backend validation remains visible without displaying diagnostics", async () => {
  const app = setup([listing([plot()]), token(), response(400, { errors: { plot_name: "private SQL", area_acres: "<script>" } })]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  await app.saveEdit();
  assert.equal(app.elements["edit-plot-panel"].hidden, false);
  assert.equal(app.elements["edit-plot_name"].value, "Changed");
  assert.equal(app.elements["edit-plot_name-error"].hidden, false);
  assert.doesNotMatch(app.elements["edit-plot_name-error"].textContent, /private/);
  assert.equal(app.elements["edit-plot-fields"].disabled, false);
});

test("CSRF rejection permits a deliberate retry with a fresh token", async () => {
  const sample = { ...plot(), plot_id: 123 };
  const app = setup([listing([sample]), token(), response(403, {}), token(), response(200, { ok: true, plot_id: 123 }), listing([sample])]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  await app.saveEdit();
  assert.equal(app.calls.length, 3);
  assert.equal(app.elements["edit-plot-panel"].hidden, false);
  assert.match(app.elements["edit-plot-error"].textContent, /fresh security token/);
  await app.saveEdit();
  assert.equal(app.calls[3].url, "/farmer/plots/csrf-token");
});

for (const failure of [response(503, {}), new Error("private timeout"), response(200, { ok: false })]) {
  test("unconfirmed PATCH preserves the draft and asks to check saved details", async () => {
    const app = setup([listing([plot()]), token(), failure]);
    await tick(); cards(app)[0].listeners.click(); app.edit();
    app.elements["edit-plot_name"].value = "Changed";
    await app.saveEdit();
    assert.match(app.elements["edit-plot-error"].textContent, /check the saved details/);
    assert.equal(app.elements["edit-plot_name"].value, "Changed");
    assert.equal(app.elements["edit-plot-panel"].hidden, false);
    assert.equal(app.elements["edit-plot-status"].textContent, "");
  });
}

for (const duringPatch of [false, true]) {
  test("expired sessions disable editing and show the login prompt", async () => {
    const app = setup([listing([plot()]), ...(duringPatch ? [token()] : []), response(401, {})]);
    await tick(); cards(app)[0].listeners.click(); app.edit();
    app.elements["edit-plot_name"].value = "Changed";
    await app.saveEdit();
    assert.equal(app.elements["edit-plot-fields"].disabled, true);
    assert.equal(app.elements["edit-plot-panel"].hidden, true);
    assert.equal(app.elements["edit-plot"].hidden, true);
    assert.equal(app.elements["plot-session"].hidden, false);
    assert.equal(app.elements["edit-plot_name"].value, "");
  });
}

test("switching selected plots closes and discards the edit draft", async () => {
  const first = plot(); const second = { ...plot(), plot_name: "South plot" };
  const app = setup([listing([first, second])]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Unsaved";
  cards(app)[1].listeners.click();
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.elements["edit-plot_name"].value, "");
  app.edit();
  assert.equal(app.elements["edit-plot_name"].value, "South plot");
  assert.equal(app.calls.length, 1);
});

test("pending edit blocks duplicate saves, add submits and refresh", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const sample = { ...plot(), plot_id: 123 };
  const app = setup([listing([sample]), token(), pending, listing([sample])]);
  await tick(); cards(app)[0].listeners.click(); app.fill(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  const saving = app.saveEdit(); await tick();
  await app.saveEdit(); await app.submit(); await app.refresh();
  assert.equal(app.calls.length, 3);
  assert.equal(app.elements["edit-plot-fields"].disabled, true);
  assert.equal(app.elements["plot-fields"].disabled, true);
  assert.equal(app.elements["edit-plot-form"].attributes["aria-busy"], "true");
  resolve(response(200, { ok: true, plot_id: 123 })); await saving;
  assert.equal(app.elements["plot-fields"].disabled, false);
  assert.equal(app.elements.plot_name.value, " North plot ");
});

test("switching before token acquisition prevents PATCH to a discarded draft", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const app = setup([listing([plot(), plot()]), pending]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  const saving = app.saveEdit(); cards(app)[1].listeners.click();
  resolve(token()); await saving;
  assert.equal(app.calls.length, 2);
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
});

test("an in-flight PATCH cannot overwrite the newly selected plot's editor", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const first = { ...plot(), plot_id: 123 }; const second = { ...plot(), plot_name: "South" };
  const app = setup([listing([first, second]), token(), pending, listing([first, second])]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  const saving = app.saveEdit(); await tick(); cards(app)[1].listeners.click();
  resolve(response(200, { ok: true, plot_id: 123 })); await saving;
  assert.equal(cards(app)[1].attributes["aria-pressed"], "true");
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.elements["edit-plot-status"].textContent, "");
  app.edit(); assert.equal(app.elements["edit-plot_name"].value, "South");
});

test("404 closes editing and refreshes the selection without leaking server details", async () => {
  const app = setup([listing([plot()]), token(), response(404, {}), listing()]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  await app.saveEdit();
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
  assert.equal(app.elements["edit-plot"].hidden, true);
  assert.match(app.elements["edit-plot-error"].textContent, /no longer available/);
  assert.equal(app.elements["edit-plot-error"].hidden, false);
  assert.equal(app.elements["edit-plot-error"].focused, true);
});

for (const switchBack of [false, true]) {
  test(`404 refresh does not show or focus stale feedback after switching plots${switchBack ? " and switching back" : ""}`, async () => {
    let resolveRefresh;
    const pendingRefresh = new Promise((resolve) => { resolveRefresh = resolve; });
    const first = { ...plot(), plot_id: 123, plot_name: "Plot A" };
    const second = { ...plot(), plot_id: 456, plot_name: "Plot B" };
    const app = setup([listing([first, second]), token(), response(404, {}), pendingRefresh]);
    await tick(); cards(app)[0].listeners.click(); app.edit();
    app.elements["edit-plot_name"].value = "Updated A";
    const saving = app.saveEdit();
    await tick();
    assert.equal(app.calls.at(-1).url, "/farmer/plots");
    assert.equal(app.elements["refresh-plots"].disabled, true);
    cards(app)[1].listeners.click();
    if (switchBack) cards(app)[0].listeners.click();
    resolveRefresh(listing(switchBack ? [first, second] : [second]));
    await saving;
    assert.equal(cards(app)[0].attributes["aria-pressed"], "true");
    assert.match(textTree(app.elements["selected-plot-details"]), switchBack ? /Plot A/ : /Plot B/);
    assert.equal(app.elements["edit-plot-error"].hidden, true);
    assert.notEqual(app.elements["edit-plot-error"].focused, true);
    assert.equal(app.elements["edit-plot-panel"].hidden, true);
  });
}

test("a confirmed update remains confirmed if its refresh fails", async () => {
  const sample = { ...plot(), plot_id: 123 };
  const app = setup([listing([sample]), token(), response(200, { ok: true, plot_id: 123 }), response(503, {})]);
  await tick(); cards(app)[0].listeners.click(); app.edit();
  app.elements["edit-plot_name"].value = "Changed";
  await app.saveEdit();
  assert.match(app.elements["edit-plot-status"].textContent, /updated successfully/);
  assert.equal(app.elements["plots-error"].hidden, false);
  assert.equal(app.elements["edit-plot-panel"].hidden, true);
});

test("plot cards use native buttons and start without an automatic selection", async () => {
  const app = setup([listing([{ ...plot(), plot_id: 11, location: "Moiben" }, { ...plot(), plot_id: 22 }])]);
  await tick();
  assert.equal(app.elements["selected-plot-details"].hidden, true);
  assert.match(app.elements["plot-selection-status"].textContent, /Select a farm plot/);
  for (const button of cards(app)) {
    assert.equal(button.tag, "button");
    assert.equal(button.type, "button");
    assert.equal(button.attributes["aria-pressed"], "false");
    assert.equal(button.attributes["aria-controls"], "selected-plot-details");
    assert.equal(button.children.at(-1).hidden, true);
  }
  assert.equal(cards(app)[0].attributes["data-plot-id"], "11");
  assert.match(textTree(cards(app)[0]), /Moiben/);
});

test("selecting and switching same-named plots updates indicators and all details by ID", async () => {
  const first = { ...plot(), plot_id: 11, location: "Moiben" };
  const second = { ...plot("2.500000"), plot_id: 22, location: null, soil_type: "Clay", farming_type: "Irrigated" };
  const app = setup([listing([first, second])]);
  await tick();
  cards(app)[0].listeners.click();
  assert.equal(cards(app)[0].attributes["aria-pressed"], "true");
  assert.equal(cards(app)[0].children.at(-1).hidden, false);
  assert.equal(app.elements["selected-plot-details"].hidden, false);
  assert.match(textTree(app.elements["selected-plot-details"]), /Moiben/);
  cards(app)[1].listeners.click();
  assert.equal(cards(app)[0].attributes["aria-pressed"], "false");
  assert.equal(cards(app)[0].children.at(-1).hidden, true);
  assert.equal(cards(app)[1].attributes["aria-pressed"], "true");
  assert.equal(cards(app)[1].children.at(-1).hidden, false);
  for (const value of ["North plot", "2.500000 acres", "Not provided", "Clay", "Irrigated"]) {
    assert.ok(textTree(app.elements["selected-plot-details"]).includes(value));
  }
  assert.equal(app.calls.length, 1); // Selection uses the authenticated list already retrieved.
});

test("refresh preserves ID selection through reorder and updates changed details", async () => {
  const first = { ...plot(), plot_id: 11 };
  const second = { ...plot(), plot_id: 22 };
  const app = setup([listing([first, second]), listing([{ ...second, plot_name: "Renamed", area_acres: "3.000000" }, first])]);
  await tick(); cards(app)[1].listeners.click();
  await app.refresh();
  assert.equal(cards(app)[0].attributes["data-plot-id"], "22");
  assert.equal(cards(app)[0].attributes["aria-pressed"], "true");
  assert.equal(cards(app)[1].attributes["aria-pressed"], "false");
  assert.match(textTree(app.elements["selected-plot-details"]), /Renamed/);
  assert.match(textTree(app.elements["selected-plot-details"]), /3.000000 acres/);
});

test("refresh removing the selected ID clears selection even if another plot shares its name", async () => {
  const first = { ...plot(), plot_id: 11 };
  const second = { ...plot(), plot_id: 22 };
  const app = setup([listing([first, second]), listing([second])]);
  await tick(); cards(app)[0].listeners.click(); await app.refresh();
  assert.equal(cards(app)[0].attributes["aria-pressed"], "false");
  assert.equal(app.elements["selected-plot-details"].hidden, true);
  assert.equal(app.elements["selected-plot-details"].children.length, 0);
  assert.match(app.elements["plot-selection-status"].textContent, /Select a farm plot/);
});

test("empty refresh clears selection and leaves registration available", async () => {
  const app = setup([listing([plot()]), listing()]);
  await tick(); cards(app)[0].listeners.click(); await app.refresh();
  assert.equal(cards(app).length, 0);
  assert.equal(app.elements["plots-empty"].hidden, false);
  assert.equal(app.elements["selected-plot-details"].hidden, true);
  assert.match(app.elements["plot-selection-status"].textContent, /Select a farm plot/);
  app.elements["toggle-add-plot"].listeners.click();
  assert.equal(app.elements["add-plot"].hidden, false);
});

test("failed refresh clears stale details but can restore selection on successful retry", async () => {
  const selected = plot();
  const app = setup([listing([selected]), response(503, {}), listing([selected])]);
  await tick(); cards(app)[0].listeners.click(); await app.refresh();
  assert.equal(app.elements["selected-plot-details"].hidden, true);
  assert.equal(app.elements["selected-plot-details"].children.length, 0);
  await app.refresh();
  assert.equal(cards(app)[0].attributes["aria-pressed"], "true");
});

test("expired sessions discard selected details and old card actions", async () => {
  const app = setup([listing([plot()]), response(401, {})]);
  await tick();
  const oldCard = cards(app)[0];
  oldCard.listeners.click(); await app.refresh(); oldCard.listeners.click();
  assert.equal(app.elements["selected-plot-details"].hidden, true);
  assert.equal(app.elements["selected-plot-details"].children.length, 0);
  assert.match(app.elements["plot-selection-status"].textContent, /log in again/);
});

test("form starts collapsed while plot retrieval and summaries remain available", async () => {
  const app = setup([listing([plot()])]);
  await tick();
  assert.equal(app.elements["add-plot"].hidden, true);
  assert.equal(app.elements["toggle-add-plot"].attributes["aria-expanded"], "false");
  assert.equal(app.elements["toggle-add-plot"].textContent, "+ Add Farm Plot");
  assert.equal(app.elements["toggle-add-plot"].disabled, false);
  assert.equal(app.elements["plot-count"].textContent, "1");
});

test("toggle opens with input focus and closes without losing entered values", async () => {
  const app = setup([listing()]);
  await tick();
  const toggle = app.elements["toggle-add-plot"];
  toggle.listeners.click();
  assert.equal(app.elements["add-plot"].hidden, false);
  assert.equal(toggle.attributes["aria-expanded"], "true");
  assert.equal(app.elements.plot_name.focused, true);
  app.elements.plot_name.value = "Unsaved plot";
  toggle.listeners.click();
  assert.equal(app.elements["add-plot"].hidden, true);
  assert.equal(toggle.focused, true);
  assert.equal(app.elements.plot_name.value, "Unsaved plot");
  toggle.listeners.click();
  let prevented = false;
  app.elements["add-plot"].listeners.keydown({ key: "Escape", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(app.elements["add-plot"].hidden, true);
  assert.equal(toggle.attributes["aria-expanded"], "false");
});

test("Quick Actions opens and focuses the form before scrolling, even when already open", async () => {
  const app = setup([listing()]);
  await tick();
  let prevented = 0;
  for (let index = 0; index < 2; index++) {
    app.elements["quick-add-plot"].listeners.click({ preventDefault() { prevented++; } });
    assert.equal(app.elements["add-plot"].hidden, false);
    assert.equal(app.elements["toggle-add-plot"].attributes["aria-expanded"], "true");
    assert.equal(app.elements.plot_name.focused, true);
    assert.equal(app.elements["add-plot"].scrollOptions.block, "start");
  }
  assert.equal(prevented, 2);
});

test("Quick Actions focuses the panel while inputs are disabled during loading", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const app = setup([pending]);
  app.elements["quick-add-plot"].listeners.click({ preventDefault() {} });
  assert.equal(app.elements["add-plot"].hidden, false);
  assert.equal(app.elements["add-plot"].focused, true);
  resolve(listing()); await tick();
  assert.equal(app.elements["plot-fields"].disabled, false);
});

test("initial retrieval uses private same-origin GET and shows actual counts and precise totals", async () => {
  const app = setup([listing([plot("0.100"), plot("0.200")])]);
  await tick();
  assert.equal(app.calls[0].url, "/farmer/plots");
  assert.equal(app.calls[0].credentials, "same-origin");
  assert.equal(app.calls[0].cache, "no-store");
  assert.equal(app.calls[0].redirect, "error");
  assert.equal(app.elements["plot-count"].textContent, "2");
  assert.equal(app.elements["plot-total-area"].textContent, "0.300000 acres");
  assert.equal(app.elements["plots-empty"].hidden, true);
  app.elements["plot-list"].children[0].children[0].listeners.click();
  assert.match(textTree(app.elements["selected-plot-details"]), /Not provided/);
  assert.match(textTree(app.elements["selected-plot-details"]), /Mixed practice/);
});

test("empty plots show an honest empty state and zero totals only after success", async () => {
  const app = setup([listing()]);
  await tick();
  assert.equal(app.elements["plot-count"].textContent, "0");
  assert.equal(app.elements["plot-total-area"].textContent, "0.000000 acres");
  assert.equal(app.elements["plots-empty"].hidden, false);
});

test("six-place stored acreage is displayed and summed exactly across large plots", async () => {
  const app = setup([listing([plot("999999.999000"), plot("0.002471"), plot("0.000001")])]);
  await tick();
  assert.equal(app.elements["plot-total-area"].textContent, "1000000.001472 acres");
  assert.match(textTree(app.elements["plot-list"]), /999999.999000 acres/);
});

test("acreage input boundaries remain three-place decimals despite six-place storage", async () => {
  const app = setup([listing()]);
  await tick(); app.fill();
  for (const value of ["0.000999", "1.250000", "999999.999001"]) {
    app.elements.area_acres.value = value;
    await app.submit();
    assert.equal(app.elements["area_acres-error"].hidden, false);
  }
  assert.equal(app.calls.length, 1);
  for (const value of ["0.001", "999999.999", "100000"]) {
    app.responses.push(token(), created(), listing());
    app.fill(); app.elements.area_acres.value = value;
    await app.submit();
    assert.equal(app.elements["area_acres-error"].hidden, true);
    assert.equal(JSON.parse(app.calls[app.calls.length - 2].body).area_acres, value);
  }
});

test("user-supplied strings are rendered as text nodes without HTML interpretation", async () => {
  const malicious = '<img src=x onerror="alert(1)">';
  const app = setup([listing([{ ...plot(), plot_name: malicious, location: malicious,
    soil_type: malicious, farming_type: malicious }])]);
  await tick();
  assert.equal(app.elements["plot-list"].children[0].children[0].children[0].textContent, malicious);
  assert.match(textTree(app.elements["plot-list"]), /onerror/);
  app.elements["plot-list"].children[0].children[0].listeners.click();
  assert.match(textTree(app.elements["selected-plot-details"]), /onerror/);
});

test("valid submission fetches CSRF, posts allowlisted string fields, resets, and refreshes", async () => {
  const app = setup([listing(), token(), created(), listing([plot()])]);
  await tick();
  app.fill();
  app.elements.farming_type.value = "Rain-fed";
  await app.submit();
  assert.deepEqual(app.calls.map((request) => request.url), ["/farmer/plots", "/farmer/plots/csrf-token", "/farmer/plots", "/farmer/plots"]);
  const request = app.calls[2];
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.headers["X-CSRF-Token"], "scoped-token");
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), { plot_name: "North plot", area_acres: "1.250",
    location: "", soil_type: "", farming_type: "Rain-fed",
    county: "Uasin Gishu", sub_county: "Moiben", ward: "Moiben" });
  assert.equal(app.elements.plot_name.value, "");
  assert.match(app.elements["plot-form-status"].textContent, /registered successfully/);
  assert.equal(app.elements["plot-count"].textContent, "1");
  assert.equal(app.elements["plot-fields"].disabled, false);
  assert.equal(app.elements["add-plot"].hidden, true);
  assert.equal(app.elements["toggle-add-plot"].attributes["aria-expanded"], "false");
  assert.equal(app.elements["toggle-add-plot"].focused, true);
});

test("sub-county selection resets dependent wards and excludes other counties", async () => {
  const app = setup([listing()]);
  await tick();
  assert.equal(app.elements.county.value, "Uasin Gishu");
  assert.deepEqual(app.elements.sub_county.children.map((option) => option.value),
    ["", "Ainabkoi", "Kapseret", "Kesses", "Moiben", "Soy", "Turbo"]);
  assert.equal(app.elements.ward.disabled, true);
  app.fill();
  app.elements.sub_county.value = "Kapseret";
  app.elements.sub_county.listeners.change();
  assert.equal(app.elements.ward.value, "");
  assert.deepEqual(app.elements.ward.children.map((option) => option.value),
    ["", "Simat/Kapseret", "Langas", "Ngeria", "Megun", "Kipkenyo"]);
  app.elements.ward.value = "Moiben";
  await app.submit();
  assert.equal(app.calls.length, 1);
  assert.equal(app.elements["ward-error"].hidden, false);
});

test("editing a legacy plot preserves old text and saves a complete new administrative location", async () => {
  const saved = { ...plot(), farming_type: "Mixed practice", soil_type: "Old soil description" };
  const app = setup([listing([saved]), token(), response(200, { ok: true }), listing([saved])]);
  await tick();
  app.elements["plot-list"].children[0].children[0].listeners.click();
  app.elements["edit-plot"].listeners.click();
  assert.equal(app.elements["edit-county"].value, "");
  assert.equal(app.elements["edit-farming_type"].value, "Mixed practice");
  assert.match(textTree(app.elements["edit-soil_type"]), /Previously entered: Old soil description/);
  app.elements["edit-sub_county"].value = "Turbo";
  app.elements["edit-sub_county"].listeners.change();
  app.elements["edit-ward"].value = "Kamagut";
  await app.elements["edit-plot-form"].listeners.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(app.calls[2].body), {
    county: "Uasin Gishu", sub_county: "Turbo", ward: "Kamagut",
  });
});

test("client validation prevents requests and provides accessible field errors", async () => {
  const app = setup([listing()]);
  await tick();
  for (const area of ["", "0", "-1", "1.2345", "1000000", "NaN", "1e3", "1,2"]) {
    app.fill();
    app.elements.area_acres.value = area;
    await app.submit();
    assert.equal(app.elements["area_acres-error"].hidden, false);
    assert.equal(app.elements.area_acres.attributes["aria-invalid"], "true");
  }
  app.fill();
  app.elements.plot_name.value = " ";
  app.elements.plot_location.value = "x".repeat(151);
  app.elements.soil_type.value = "x".repeat(101);
  app.elements.farming_type.value = "x".repeat(101);
  await app.submit();
  for (const name of ["plot_name", "location", "soil_type", "farming_type"]) {
    assert.equal(app.elements[name + "-error"].hidden, false);
  }
  assert.equal(app.elements["plot-form-error"].focused, true);
  assert.equal(app.elements["add-plot"].hidden, false);
  assert.equal(app.calls.length, 1);
});

test("backend validation is mapped beside known fields without exposing diagnostics", async () => {
  const app = setup([listing(), token(), response(400, { ok: false, errors: {
    plot_name: "private diagnostics", area_acres: "<script>server</script>", unknown: "private",
  } })]);
  await tick(); app.fill(); await app.submit();
  assert.equal(app.elements["plot_name-error"].hidden, false);
  assert.equal(app.elements["area_acres-error"].hidden, false);
  assert.doesNotMatch(app.elements["plot_name-error"].textContent, /private/);
  assert.equal(app.elements.plot_name.value, " North plot ");
  assert.equal(app.elements["add-plot"].hidden, false);
  assert.equal(app.elements["toggle-add-plot"].attributes["aria-expanded"], "true");
});

test("403 allows a deliberate retry with a fresh token and never retries automatically", async () => {
  const app = setup([listing(), token(), response(403, {}), token(), created(), listing([plot()])]);
  await tick(); app.fill(); await app.submit();
  assert.equal(app.calls.length, 3);
  assert.match(app.elements["plot-form-error"].textContent, /fresh security token/);
  assert.equal(app.elements["add-plot"].hidden, false);
  await app.submit();
  assert.equal(app.calls[3].url, "/farmer/plots/csrf-token");
  assert.equal(app.elements["plot-count"].textContent, "1");
});

for (const failing of [response(503, {}), new Error("private network"),
  { ok: true, status: 200, json: async () => { throw new Error("private parser"); } }]) {
  test("unavailable or malformed listing shows no invented zero or stale stats", async () => {
    const app = setup([listing([plot()]), failing]);
    await tick(); await app.refresh();
    assert.equal(app.elements["plot-count"].textContent, "Not available yet");
    assert.equal(app.elements["plot-list"].children.length, 0);
    assert.equal(app.elements["plots-empty"].hidden, true);
    assert.equal(app.elements["plots-error"].hidden, false);
    assert.doesNotMatch(app.elements["plots-error"].textContent, /private/);
    assert.equal(app.elements["refresh-plots"].disabled, false);
  });
}

test("initial session expiry disables registration and offers existing login route", async () => {
  const app = setup([response(401, {})]);
  await tick(); app.fill(); await app.submit();
  assert.equal(app.elements["plot-session"].hidden, false);
  assert.equal(app.elements["plot-fields"].disabled, true);
  assert.equal(app.calls.length, 1);
});

for (const atPost of [false, true]) {
  test("session expiry while obtaining token or submitting clears private plot data", async () => {
    const app = setup([listing([plot()]), ...(atPost ? [token()] : []), response(401, {})]);
    await tick(); app.fill(); await app.submit();
    assert.equal(app.elements["plot-fields"].disabled, true);
    assert.equal(app.elements["plot-list"].children.length, 0);
    assert.equal(app.elements["plot-count"].textContent, "Not available yet");
    assert.match(app.elements["plot-form-error"].textContent, /log in again/);
  });
}

test("missing token prevents POST and preserves the farmer's input", async () => {
  const app = setup([listing(), response(200, { ok: true })]);
  await tick(); app.fill(); await app.submit();
  assert.equal(app.calls.length, 2);
  assert.match(app.elements["plot-form-error"].textContent, /could not be prepared/);
  assert.equal(app.elements.plot_name.value, " North plot ");
});

for (const failure of [response(503, {}), new Error("private timeout"), response(201, { ok: false })]) {
  test("unconfirmed writes preserve input and ask to check plots before retrying", async () => {
    const app = setup([listing(), token(), failure]);
    await tick(); app.fill(); await app.submit();
    assert.match(app.elements["plot-form-error"].textContent, /check whether the plot was saved/);
    assert.equal(app.elements["plot-form-status"].textContent, "");
    assert.equal(app.elements["plot-fields"].disabled, false);
    assert.equal(app.elements["add-plot"].hidden, false);
    assert.equal(app.calls.length, 3);
  });
}

test("pending write blocks duplicate submits and refresh until completion", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const app = setup([listing(), token(), pending, listing([plot()])]);
  await tick(); app.fill();
  const first = app.submit();
  await tick();
  await app.submit(); await app.refresh();
  assert.equal(app.calls.length, 3);
  assert.equal(app.elements["plot-fields"].disabled, true);
  assert.equal(app.elements["plot-form"].attributes["aria-busy"], "true");
  assert.equal(app.elements["toggle-add-plot"].disabled, true);
  app.elements["toggle-add-plot"].listeners.click();
  app.elements["add-plot"].listeners.keydown({ key: "Escape", preventDefault() {} });
  assert.equal(app.elements["add-plot"].hidden, false);
  resolve(created()); await first;
  assert.equal(app.elements["plot-fields"].disabled, false);
});

test("confirmed creation stays confirmed if subsequent listing fails", async () => {
  const app = setup([listing(), token(), created(), response(503, {})]);
  await tick(); app.fill(); await app.submit();
  assert.match(app.elements["plot-form-status"].textContent, /registered successfully/);
  assert.equal(app.elements["plots-error"].hidden, false);
  assert.equal(app.elements.plot_name.value, "");
  assert.equal(app.elements["add-plot"].hidden, true);
});

test("invalid response data never produces partial totals", async () => {
  const app = setup([listing([plot(), plot("NaN")])]);
  await tick();
  assert.equal(app.elements["plot-count"].textContent, "Not available yet");
  assert.equal(app.elements["plot-list"].children.length, 0);
});
