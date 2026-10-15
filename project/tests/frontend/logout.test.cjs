const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/dashboard.js"), "utf8");

function setup(responses) {
  const ids = ["logout-button", "logout-csrf-token", "logout-error", "logout-status"];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    value: "", hidden: true, disabled: true, textContent: "", attributes: {}, listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() { this.focused = true; },
  }]));
  elements["logout-csrf-token"].value = "synthetic-logout-token";
  const calls = [];
  const redirects = [];
  vm.runInNewContext(source, {
    document: { getElementById: (id) => elements[id] },
    window: { location: { assign: (url) => redirects.push(url) } },
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      const response = responses.shift();
      if (response instanceof Error) throw response;
      return response;
    },
  });
  return { elements, calls, redirects, click: () => elements["logout-button"].listeners.click() };
}

const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });

test("confirmed logout sends same-origin POST with CSRF and opens the login page", async () => {
  const app = setup([response(200, { ok: true })]);
  await app.click();
  assert.deepEqual(app.redirects, ["/farmer/login-page"]);
  assert.equal(app.calls.length, 1);
  const request = app.calls[0];
  assert.equal(request.url, "/farmer/logout");
  assert.equal(request.method, "POST");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.cache, "no-store");
  assert.equal(request.redirect, "error");
  assert.equal(request.headers["X-CSRF-Token"], "synthetic-logout-token");
  assert.equal(app.elements["logout-button"].disabled, true);
});

for (const [status, expected] of [[400, "request could not be verified"], [401, "session could not be verified"], [503, "temporarily unavailable"], [500, "temporarily unavailable"]]) {
  test(`HTTP ${status} announces a safe failure without redirecting`, async () => {
    const app = setup([response(status, { message: "private-server-details" })]);
    await app.click();
    const error = app.elements["logout-error"];
    assert.ok(error.textContent.includes(expected));
    assert.ok(!error.textContent.includes("private-server-details"));
    assert.equal(error.hidden, false);
    assert.equal(error.focused, true);
    assert.deepEqual(app.redirects, []);
    assert.equal(app.elements["logout-button"].disabled, false);
    assert.equal(app.elements["logout-status"].textContent, "");
    assert.equal(app.calls.length, 1);
  });
}

test("network or timeout failure never claims logout succeeded", async () => {
  for (const name of ["TypeError", "AbortError"]) {
    const failure = new Error("private-network-details");
    failure.name = name;
    const app = setup([failure]);
    await app.click();
    assert.ok(app.elements["logout-error"].textContent.includes("Unable to confirm logout"));
    assert.ok(!app.elements["logout-error"].textContent.includes("private-network-details"));
    assert.deepEqual(app.redirects, []);
    assert.equal(app.elements["logout-button"].disabled, false);
  }
});

test("unconfirmed or malformed JSON does not redirect", async () => {
  for (const result of [{ ok: false }, {}, null, { ok: "true" }]) {
    const app = setup([response(200, result)]);
    await app.click();
    assert.deepEqual(app.redirects, []);
    assert.ok(app.elements["logout-error"].textContent.includes("Unable to confirm logout"));
  }
  const app = setup([{ status: 200, ok: true, json: async () => { throw new Error("private-response-body"); } }]);
  await app.click();
  assert.deepEqual(app.redirects, []);
  assert.ok(!app.elements["logout-error"].textContent.includes("private-response-body"));
});

test("missing token prevents a request", async () => {
  const app = setup([]);
  app.elements["logout-csrf-token"].value = "";
  await app.click();
  assert.equal(app.calls.length, 0);
  assert.ok(app.elements["logout-error"].textContent.includes("reload the page"));
});

test("loading state blocks duplicate requests and permits a deliberate retry after failure", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const app = setup([pending, response(200, { ok: true })]);
  const first = app.click();
  assert.equal(app.elements["logout-button"].disabled, true);
  assert.equal(app.elements["logout-button"].attributes["aria-busy"], "true");
  assert.equal(app.elements["logout-status"].textContent, "Logging out. Please wait.");
  await app.click();
  assert.equal(app.calls.length, 1);
  resolve(response(503, {}));
  await first;
  assert.equal(app.elements["logout-button"].attributes["aria-busy"], "false");
  await app.click();
  assert.equal(app.calls.length, 2);
  assert.deepEqual(app.redirects, ["/farmer/login-page"]);
});
