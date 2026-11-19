const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/officer-auth.js"), "utf8");
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const token = () => response(200, { ok: true, csrf_token: "synthetic-token", identity: { user_id: 41, agricultural_officer_id: 91 } });
const success = () => response(200, { ok: true });

function setup(mode, responses = []) {
  const loginIds = ["officer-login-form", "officer-login-fields", "officer-login-button", "officer-password-toggle", "officer-login-error", "officer-login-status", "email", "password", "email-error", "password-error"];
  const logoutIds = ["officer-logout-button", "officer-logout-error", "officer-logout-status", "officer-login-again"];
  const elements = Object.fromEntries((mode === "login" ? loginIds : logoutIds).map((id) => [id, {
    id, value: "", type: id === "password" ? "password" : "text", disabled: true, hidden: true,
    textContent: "", attributes: {}, listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    addEventListener(name, listener) { this.listeners[name] = listener; },
    focus() { this.focused = true; },
    set innerHTML(value) { throw new Error("Unsafe DOM rendering"); },
  }]));
  if (mode === "login") {
    elements.email.value = " Officer@Example.COM ";
    elements.password.value = " synthetic-password ";
  }
  const calls = [], redirects = [];
  vm.runInNewContext(source, {
    document: { getElementById: (id) => elements[id] },
    window: { location: { assign: (url) => redirects.push(url) } },
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      const result = await responses.shift();
      if (result instanceof Error) throw result;
      return result;
    },
  });
  return { elements, calls, redirects, act: () => mode === "login"
    ? elements["officer-login-form"].listeners.submit({ preventDefault() {} })
    : elements["officer-logout-button"].listeners.click() };
}

test("login sends only email/password with scoped CSRF and navigates to the protected officer page", async () => {
  const app = setup("login", [token(), success()]);
  await app.act();
  assert.equal(app.calls[0].url, "/officer/login");
  assert.equal(app.calls[0].method, "GET");
  assert.equal(app.calls[1].url, "/officer/login");
  assert.equal(app.calls[1].method, "POST");
  assert.equal(app.calls[1].headers["X-CSRF-Token"], "synthetic-token");
  assert.equal(app.calls[1].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(app.calls[1].body), { email: "Officer@Example.COM", password: " synthetic-password " });
  assert.deepEqual(app.redirects, ["/officer/dashboard"]);
  assert.equal(app.elements.password.value, "");
  assert.equal(app.elements["officer-login-button"].disabled, true);
  assert.ok(app.calls.every((call) => call.credentials === "same-origin" && call.cache === "no-store" && call.redirect === "error"));
  await app.act();
  assert.equal(app.calls.length, 2);
});

test("required fields and length limits produce accessible errors without requests", async () => {
  for (const [email, password] of [["", ""], ["e".repeat(255), "password"], ["a@example.com", "p".repeat(1025)]]) {
    const app = setup("login");
    app.elements.email.value = email;
    app.elements.password.value = password;
    await app.act();
    assert.equal(app.calls.length, 0);
    assert.equal(app.elements["officer-login-error"].focused, true);
    assert.ok(app.elements.email.attributes["aria-invalid"] === "true" || app.elements.password.attributes["aria-invalid"] === "true");
  }
});

test("password visibility updates state and resets after a request", async () => {
  const app = setup("login", [token(), response(401, {})]);
  const toggle = app.elements["officer-password-toggle"];
  assert.equal(toggle.hidden, false);
  toggle.listeners.click();
  assert.equal(app.elements.password.type, "text");
  assert.equal(toggle.attributes["aria-pressed"], "true");
  assert.equal(toggle.attributes["aria-label"], "Hide password");
  await app.act();
  assert.equal(app.elements.password.type, "password");
  assert.equal(toggle.attributes["aria-pressed"], "false");
  assert.equal(app.elements.password.value, "");
});

for (const [status, pattern] of [[401, /Invalid email or password/], [400, /could not be verified/], [403, /could not be verified/], [503, /temporarily unavailable/], [500, /temporarily unavailable/]]) {
  test(`login HTTP ${status} displays a safe error without navigation`, async () => {
    const app = setup("login", [token(), response(status, { message: "private SQL password details" })]);
    await app.act();
    assert.match(app.elements["officer-login-error"].textContent, pattern);
    assert.ok(!app.elements["officer-login-error"].textContent.includes("private"));
    assert.deepEqual(app.redirects, []);
    assert.equal(app.elements.password.value, "");
    assert.equal(app.elements["officer-login-button"].disabled, false);
    assert.equal(app.elements["officer-login-fields"].disabled, false);
  });
}

test("CSRF rejection requires deliberate resubmission with a new GET token", async () => {
  const app = setup("login", [token(), response(400, {}), response(200, { csrf_token: "fresh-token" }), success()]);
  await app.act();
  assert.equal(app.calls.length, 2);
  app.elements.password.value = "synthetic-password";
  await app.act();
  assert.equal(app.calls[2].method, "GET");
  assert.equal(app.calls[3].headers["X-CSRF-Token"], "fresh-token");
  assert.deepEqual(app.redirects, ["/officer/dashboard"]);
});

test("missing or failed CSRF acquisition never sends credentials", async () => {
  for (const result of [response(200, {}), response(200, null), response(503, {}), response(200, { csrf_token: 123 })]) {
    const app = setup("login", [result]);
    await app.act();
    assert.equal(app.calls.length, 1);
    assert.match(app.elements["officer-login-error"].textContent, /could not be prepared/);
    assert.equal(app.elements.password.value, "");
  }
});

test("login network errors, timeouts and malformed or unconfirmed responses remain safe", async () => {
  const timeout = new Error("private timeout");
  timeout.name = "AbortError";
  for (const results of [[new Error("private")], [token(), timeout], [token(), response(200, null)],
    [token(), response(200, { ok: false, redirect: "https://untrusted.example" })],
    [token(), { status: 200, ok: true, json: async () => { throw new Error("private response"); } }]]) {
    const app = setup("login", results);
    await app.act();
    assert.equal(app.elements["officer-login-error"].hidden, false);
    assert.ok(!app.elements["officer-login-error"].textContent.includes("private"));
    assert.deepEqual(app.redirects, []);
    assert.equal(app.elements.password.value, "");
    assert.equal(app.elements["officer-login-button"].disabled, false);
  }
});

test("logout fetches the current home token then POSTs without rendering identifiers", async () => {
  const app = setup("logout", [token(), success()]);
  assert.equal(app.calls.length, 0);
  await app.act();
  assert.deepEqual(app.calls.map((call) => [call.url, call.method]), [["/officer/home", "GET"], ["/officer/logout", "POST"]]);
  assert.equal(app.calls[1].headers["X-CSRF-Token"], "synthetic-token");
  assert.deepEqual(app.redirects, ["/officer/login-page"]);
  assert.equal(app.elements["officer-logout-button"].disabled, true);
  assert.ok(Object.values(app.elements).every((element) => !element.textContent.includes("user_id") && !element.textContent.includes("agricultural_officer_id")));
  assert.ok(app.calls.every((call) => call.credentials === "same-origin" && call.cache === "no-store" && call.redirect === "error"));
});

for (const phase of ["token", "logout"]) {
  test(`session expiry during ${phase} shows a login link without claiming logout succeeded`, async () => {
    const app = setup("logout", phase === "token" ? [response(401, {})] : [token(), response(401, {})]);
    await app.act();
    assert.match(app.elements["officer-logout-error"].textContent, /session has expired/);
    assert.equal(app.elements["officer-login-again"].hidden, false);
    assert.equal(app.elements["officer-logout-button"].disabled, true);
    assert.deepEqual(app.redirects, []);
    assert.equal(app.calls.length, phase === "token" ? 1 : 2);
  });
}

test("logout token errors prevent POST and permit explicit retry", async () => {
  for (const result of [response(503, {}), response(200, {}), response(200, null), response(200, { ok: false, csrf_token: "invalid" })]) {
    const app = setup("logout", [result]);
    await app.act();
    assert.equal(app.calls.length, 1);
    assert.equal(app.elements["officer-logout-button"].disabled, false);
    assert.deepEqual(app.redirects, []);
  }
});

for (const status of [400, 403, 500, 503]) {
  test(`logout HTTP ${status} never displays diagnostics or claims success`, async () => {
    const app = setup("logout", [token(), response(status, { message: "private SQL" })]);
    await app.act();
    assert.equal(app.elements["officer-logout-error"].focused, true);
    assert.ok(!app.elements["officer-logout-error"].textContent.includes("private"));
    assert.equal(app.elements["officer-logout-button"].disabled, false);
    assert.deepEqual(app.redirects, []);
  });
}

test("logout CSRF retry reacquires its token without automatic POST retries", async () => {
  const app = setup("logout", [token(), response(400, {}), response(200, { ok: true, csrf_token: "fresh-token" }), success()]);
  await app.act();
  assert.equal(app.calls.length, 2);
  await app.act();
  assert.equal(app.calls[2].url, "/officer/home");
  assert.equal(app.calls[3].headers["X-CSRF-Token"], "fresh-token");
});

test("unconfirmed logout, network errors, and malformed JSON never redirect", async () => {
  for (const results of [[new Error("private")], [token(), new Error("private")], [token(), response(200, {})],
    [token(), { status: 200, ok: true, json: async () => { throw new Error("private"); } }]]) {
    const app = setup("logout", results);
    await app.act();
    assert.deepEqual(app.redirects, []);
    assert.equal(app.elements["officer-logout-button"].disabled, false);
    assert.ok(!app.elements["officer-logout-error"].textContent.includes("private"));
  }
});

for (const mode of ["login", "logout"]) {
  test(`${mode} blocks duplicates while token acquisition and POST are pending`, async () => {
    let resolveToken, resolvePost;
    const app = setup(mode, [new Promise((resolve) => { resolveToken = resolve; }), new Promise((resolve) => { resolvePost = resolve; })]);
    const first = app.act();
    await app.act();
    assert.equal(app.calls.length, 1);
    assert.equal(app.elements[`officer-${mode}-button`].disabled, true);
    resolveToken(token());
    await new Promise((resolve) => setImmediate(resolve));
    await app.act();
    assert.equal(app.calls.length, 2);
    resolvePost(success());
    await first;
  });
}

test("script exits harmlessly on unrelated pages", () => {
  vm.runInNewContext(source, { document: { getElementById: () => null }, fetch: () => assert.fail("Unexpected request") });
});
