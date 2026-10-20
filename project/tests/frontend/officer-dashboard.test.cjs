const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/officer-dashboard.js"), "utf8");
const authSource = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/officer-auth.js"), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const farmer = (id, count = 0) => ({ farmer_id: id, name: `Farmer ${id}`, phone_number: null, plot_count: count });
const listing = (farmers = []) => response(200, { ok: true, farmers });
const plot = (name = "North") => ({ plot_id: 11, plot_name: name, area_acres: "1.234000", location: null, soil_type: null, farming_type: null });
const details = (id, plots = []) => response(200, { ok: true, farmer_id: id, plots });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function node(tag = "div") {
  return {
    tag, hidden: false, disabled: false, attributes: {}, children: [], listeners: {}, _text: "",
    set textContent(value) { this._text = String(value); this.children = []; },
    get textContent() { return this._text; },
    get firstChild() { return this.children[0]; },
    get lastChild() { return this.children[this.children.length - 1]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; this._text = ""; },
    focus() { this.focused = true; },
    set innerHTML(value) { assert.fail("Unsafe HTML rendering"); },
  };
}
const trigger = (item, event = "click") => Promise.all((item.listeners[event] || []).map((listener) => listener()));
const textTree = (item) => [item.textContent, ...item.children.map(textTree)].join(" ");
const cards = (app) => app.el["assigned-farmers"].children.map((item) => item.firstChild);

function setup(responses = [], withLogout = false) {
  const ids = ["officer-dashboard", "assigned-farmers", "farmer-plots", "farmers-refresh", "plots-retry", "selected-farmer",
    "farmer-plots-error", "farmer-plots-panel", "assigned-count", "assigned-plot-count", "officer-data", "dashboard-session",
    "officer-login-again", "dashboard-reload", "farmer-plots-status", "farmers-status", "assigned-farmers-panel", "farmers-error",
    "officer-logout-button", "officer-logout-error", "officer-logout-status"];
  const el = Object.fromEntries(ids.map((id) => [id, node()]));
  for (const id of ["plots-retry", "dashboard-session", "officer-login-again", "dashboard-reload", "officer-logout-error"]) el[id].hidden = true;
  const calls = [], redirects = [];
  const context = vm.createContext({ document: { getElementById: (id) => el[id], createElement: node },
    AbortController, setTimeout, clearTimeout, window: { location: { assign: (url) => redirects.push(url) } },
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      assert.ok(responses.length, `Unexpected request ${url}`);
      const result = await responses.shift();
      if (result instanceof Error) throw result;
      return result;
    },
  });
  if (withLogout) vm.runInContext(authSource, context);
  vm.runInContext(source, context);
  return { el, calls, redirects, responses,
    select: (index) => trigger(cards({ el })[index]),
    refresh: () => trigger(el["farmers-refresh"]), retry: () => trigger(el["plots-retry"]),
    logout: () => trigger(el["officer-logout-button"]) };
}

test("load displays real summaries and accessible farmer cards without an automatic selection", async () => {
  const pending = deferred();
  const app = setup([pending.promise]);
  assert.match(app.el["farmers-status"].textContent, /Loading/);
  assert.equal(app.el["farmers-refresh"].disabled, true);
  pending.resolve(listing([farmer(101, 0), { ...farmer(102, 3), phone_number: "synthetic-phone" }]));
  await tick();
  assert.equal(app.el["assigned-count"].textContent, "2");
  assert.equal(app.el["assigned-plot-count"].textContent, "3");
  assert.equal(cards(app).length, 2);
  assert.match(textTree(cards(app)[0]), /Phone number not available/);
  assert.match(textTree(cards(app)[1]), /synthetic-phone/);
  assert.equal(cards(app)[0].tag, "button");
  assert.equal(cards(app)[0].type, "button");
  assert.equal(cards(app)[0].attributes["aria-pressed"], "false");
  assert.equal(app.calls.length, 1);
  assert.match(app.el["farmer-plots-status"].textContent, /Select a farmer/);
  assert.equal(app.calls[0].url, "/officer/api/farmers");
  assert.equal(app.calls[0].cache, "no-store");
  assert.equal(app.calls[0].credentials, "same-origin");
});

test("selection uses farmer ID, exposes selection, renders plots and avoids duplicate requests", async () => {
  const app = setup([listing([farmer(101, 1), farmer(102, 1)]), details(101, [plot()]), details(102, [plot("South")])]);
  await tick();
  await app.select(0);
  assert.equal(app.calls[1].url, "/officer/api/farmers/101/plots");
  assert.equal(cards(app)[0].attributes["aria-pressed"], "true");
  assert.equal(cards(app)[0].lastChild.hidden, false);
  assert.match(textTree(app.el["farmer-plots"]), /North.*Area \(acres\).*1.234000.*Location.*Not provided.*Soil type.*Not provided.*Farming type.*Not provided/);
  await app.select(0);
  assert.equal(app.calls.length, 2);
  await app.select(1);
  assert.equal(cards(app)[0].attributes["aria-pressed"], "false");
  assert.equal(app.el["selected-farmer"].textContent, "Farmer 102");
  assert.match(textTree(app.el["farmer-plots"]), /South/);
  assert.ok(app.calls.every((call) => call.method === "GET" && call.redirect === "error"));
});

test("empty farmer list shows honest zero summaries and no plot requests", async () => {
  const app = setup([listing()]);
  await tick();
  assert.equal(app.el["assigned-count"].textContent, "0");
  assert.equal(app.el["assigned-plot-count"].textContent, "0");
  assert.match(app.el["farmers-status"].textContent, /No farmers/);
  assert.equal(cards(app).length, 0);
  assert.equal(app.calls.length, 1);
});

test("selected farmer with no plots shows an empty state", async () => {
  const app = setup([listing([farmer(101)]), details(101)]);
  await tick(); await app.select(0);
  assert.match(app.el["farmer-plots-status"].textContent, /no registered farm plots/);
  assert.equal(app.el["farmer-plots"].children.length, 0);
});

test("malicious text is rendered literally and all optional plot fields are supported", async () => {
  const malicious = '<img src=x onerror="alert(1)">';
  const app = setup([listing([{ ...farmer(101, 1), name: malicious, phone_number: malicious }]), details(101, [{ ...plot(malicious), location: malicious, soil_type: malicious, farming_type: malicious }])]);
  await tick(); await app.select(0);
  assert.equal(app.el["selected-farmer"].textContent, malicious);
  assert.equal(cards(app)[0].firstChild.textContent, malicious);
  assert.equal(app.el["farmer-plots"].firstChild.firstChild.textContent, malicious);
  assert.ok(textTree(app.el["farmer-plots"]).includes(malicious));
});

test("list failures show safe errors and manual refresh recovers without retaining old totals", async () => {
  for (const result of [response(503, { message: "private SQL" }), new Error("private connection"), response(200, { ok: false }), response(404, {})]) {
    const app = setup([result, listing([farmer(101, 2)])]);
    await tick();
    assert.equal(app.el["farmers-error"].hidden, false);
    assert.ok(!app.el["farmers-error"].textContent.includes("private"));
    assert.equal(app.el["assigned-count"].textContent, "Not available");
    await app.refresh();
    assert.equal(app.el["assigned-plot-count"].textContent, "2");
    assert.equal(app.el["farmers-error"].hidden, true);
  }
});

test("plot errors clear old details, distinguish 404 and allow explicit retries for outages", async () => {
  for (const result of [response(404, { message: "private" }), response(503, { message: "private" }), new Error("private")]) {
    const app = setup([listing([farmer(101), farmer(102)]), details(101, [plot("Old")]), result, details(102, [plot("Recovered")])]);
    await tick(); await app.select(0); await app.select(1);
    assert.equal(app.el["farmer-plots"].children.length, 0);
    assert.equal(app.el["farmer-plots-error"].hidden, false);
    assert.ok(!app.el["farmer-plots-error"].textContent.includes("private"));
    if (result.status === 404) {
      assert.match(app.el["farmer-plots-error"].textContent, /no longer available/);
      assert.equal(app.el["plots-retry"].hidden, true);
    } else {
      assert.equal(app.el["plots-retry"].hidden, false);
      await app.retry();
      assert.match(textTree(app.el["farmer-plots"]), /Recovered/);
    }
  }
});

test("HTTP 401 clears sensitive data and provides a login link from either API", async () => {
  for (const plotExpiry of [false, true]) {
    const app = setup(plotExpiry ? [listing([farmer(101)]), response(401, {})] : [response(401, {})]);
    await tick(); if (plotExpiry) await app.select(0);
    assert.equal(app.el["officer-data"].hidden, true);
    assert.equal(app.el["officer-login-again"].hidden, false);
    assert.match(app.el["dashboard-session"].textContent, /expired/);
    assert.equal(cards(app).length, 0);
    assert.equal(app.el["farmer-plots"].children.length, 0);
    const count = app.calls.length;
    await app.refresh();
    assert.equal(app.calls.length, count);
  }
});

test("rapid switching ignores late success, 404, 503 and network failures without stale focus", async () => {
  for (const late of [details(101, [plot("Stale")]), response(404, {}), response(503, {}), new Error("private")]) {
    const pending = deferred();
    const app = setup([listing([farmer(101), farmer(102)]), pending.promise, details(102, [plot("Current")])]);
    await tick();
    const first = app.select(0);
    await app.select(1);
    assert.equal(app.calls[1].signal.aborted, true);
    pending.resolve(late); await first;
    assert.equal(app.el["selected-farmer"].textContent, "Farmer 102");
    assert.match(textTree(app.el["farmer-plots"]), /Current/);
    assert.ok(!textTree(app.el["farmer-plots"]).includes("Stale"));
    assert.equal(app.el["farmer-plots-error"].hidden, true);
    assert.equal(app.el["farmer-plots-panel"].attributes["aria-busy"], "false");
  }
});

test("switching back to the first farmer cannot revive an earlier interaction", async () => {
  const pending = deferred();
  const app = setup([listing([farmer(101), farmer(102)]), pending.promise, details(102), details(101, [plot("Newest")])]);
  await tick(); const old = app.select(0); await app.select(1); await app.select(0);
  pending.resolve(details(101, [plot("Oldest")])); await old;
  assert.match(textTree(app.el["farmer-plots"]), /Newest/);
});

test("list refresh invalidates pending selection responses and blocks duplicate refresh", async () => {
  const old = deferred(), listingPending = deferred();
  const app = setup([listing([farmer(101)]), old.promise, listingPending.promise]);
  await tick(); const select = app.select(0); const refresh = app.refresh();
  await app.refresh(); assert.equal(app.calls.length, 3);
  listingPending.resolve(listing()); await refresh;
  old.resolve(details(101, [plot("Old")])); await select;
  assert.equal(app.el["assigned-count"].textContent, "0");
  assert.equal(app.el["farmer-plots"].children.length, 0);
  assert.equal(app.el["selected-farmer"].textContent, "");
});

test("logout reuses the existing token and POST flow while invalidating late dashboard data", async () => {
  const pending = deferred();
  const app = setup([listing([farmer(101)]), pending.promise, response(200, { ok: true, csrf_token: "scoped-token" }), response(200, { ok: true })], true);
  await tick(); const select = app.select(0); await app.logout();
  pending.resolve(details(101, [plot("Late private data")])); await select;
  assert.equal(app.el["officer-data"].hidden, true);
  assert.equal(app.el["farmer-plots"].children.length, 0);
  assert.deepEqual(app.calls.slice(-2).map((call) => [call.url, call.method]), [["/officer/home", "GET"], ["/officer/logout", "POST"]]);
  assert.equal(app.calls.at(-1).headers["X-CSRF-Token"], "scoped-token");
  assert.deepEqual(app.redirects, ["/officer/login-page"]);
});

test("failed logout leaves dashboard data cleared and offers reload without claiming success", async () => {
  const app = setup([listing([farmer(101)]), response(503, {})], true);
  await tick(); await app.logout();
  assert.equal(app.el["dashboard-reload"].hidden, false);
  assert.equal(app.el["officer-data"].hidden, true);
  assert.deepEqual(app.redirects, []);
  assert.equal(app.el["officer-logout-error"].hidden, false);
});

test("malformed list and mismatched plot responses fail safely", async () => {
  const bad = setup([listing([{ ...farmer(101), farmer_id: "../other" }])]);
  await tick(); assert.equal(cards(bad).length, 0);
  const app = setup([listing([farmer(101)]), details(102, [plot("Wrong farmer")])]);
  await tick(); await app.select(0);
  assert.equal(app.el["farmer-plots"].children.length, 0);
  assert.equal(app.el["farmer-plots-error"].hidden, false);
});

test("script exits on unrelated pages", () => {
  vm.runInNewContext(source, { document: { getElementById: () => null }, fetch: () => assert.fail("Unexpected fetch") });
});
