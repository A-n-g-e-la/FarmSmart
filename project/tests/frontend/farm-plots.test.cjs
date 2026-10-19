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
const plot = (area = "1.250000") => ({ plot_id: 1, plot_name: "North plot", area_acres: area,
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
  const names = ["plot_name", "area_acres", "location", "soil_type", "farming_type"];
  const ids = ["plot-form", "plot-fields", "save-plot", "refresh-plots", "plot-list", "plot-count",
    "plot-total-area", "plots-empty", "plots-status", "plots-error", "plot-form-status", "plot-form-error",
    "plot-session", "plot_name", "area_acres", "plot_location", "soil_type", "farming_type",
    "add-plot", "toggle-add-plot", "quick-add-plot",
    ...names.map((name) => name + "-error")];
  const elements = Object.fromEntries(ids.map((id) => [id, element()]));
  elements["plot-session"].hidden = true;
  const inputs = names.map((name) => elements[name === "location" ? "plot_location" : name]);
  elements["plot-form"].reset = () => inputs.forEach((input) => { input.value = ""; });
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
    },
    submit: () => elements["plot-form"].listeners.submit({ preventDefault() {} }),
    refresh: () => elements["refresh-plots"].listeners.click(),
  };
}

function textTree(node) {
  return [node.textContent, ...node.children.map(textTree)].join(" ");
}

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
  assert.match(textTree(app.elements["plot-list"]), /Not provided/);
  assert.match(textTree(app.elements["plot-list"]), /Mixed practice/);
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
  assert.equal(app.elements["plot-list"].children[0].children[0].textContent, malicious);
  assert.match(textTree(app.elements["plot-list"]), /onerror/);
});

test("valid submission fetches CSRF, posts allowlisted string fields, resets, and refreshes", async () => {
  const app = setup([listing(), token(), created(), listing([plot()])]);
  await tick();
  app.fill();
  app.elements.farming_type.value = "Mixed practice";
  await app.submit();
  assert.deepEqual(app.calls.map((request) => request.url), ["/farmer/plots", "/farmer/plots/csrf-token", "/farmer/plots", "/farmer/plots"]);
  const request = app.calls[2];
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.headers["X-CSRF-Token"], "scoped-token");
  assert.equal(request.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(request.body), { plot_name: "North plot", area_acres: "1.250",
    location: "", soil_type: "", farming_type: "Mixed practice" });
  assert.equal(app.elements.plot_name.value, "");
  assert.match(app.elements["plot-form-status"].textContent, /registered successfully/);
  assert.equal(app.elements["plot-count"].textContent, "1");
  assert.equal(app.elements["plot-fields"].disabled, false);
  assert.equal(app.elements["add-plot"].hidden, true);
  assert.equal(app.elements["toggle-add-plot"].attributes["aria-expanded"], "false");
  assert.equal(app.elements["toggle-add-plot"].focused, true);
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
