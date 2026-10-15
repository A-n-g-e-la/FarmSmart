const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/login.js"), "utf8");

function setup(responses) {
  const ids = ["login-form", "phone_number", "password", "login-button", "password-toggle", "login-error", "login-status", "phone-error", "password-error"];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    value: "", type: id === "password" ? "password" : "text", hidden: true,
    disabled: true, textContent: "", attributes: {}, listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    focus() { this.focused = true; },
  }]));
  elements.phone_number.value = "0712345678";
  elements.password.value = "synthetic-password";
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
  return { elements, calls, redirects, submit: () => elements["login-form"].listeners.submit({ preventDefault() {} }) };
}

const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const token = () => response(200, { csrf_token: "synthetic-token" });

test("success uses same-origin cookies and CSRF then redirects without retaining password", async () => {
  const app = setup([token(), response(200, { ok: true, identity: { user_id: 1 }, csrf_token: "logout-token" })]);
  await app.submit();
  assert.deepEqual(app.redirects, ["/farmer/home"]);
  assert.equal(app.calls.length, 2);
  assert.equal(app.calls[0].method, "GET");
  assert.equal(app.calls[1].method, "POST");
  assert.ok(app.calls.every((call) => call.credentials === "same-origin"));
  assert.equal(app.calls[1].headers["X-CSRF-Token"], "synthetic-token");
  assert.equal(JSON.parse(app.calls[1].body).phone_number, "0712345678");
  assert.equal(app.elements.password.value, "");
  assert.equal(app.elements["login-button"].disabled, true);
});

test("required fields produce accessible errors without any request", async () => {
  const app = setup([]);
  app.elements.phone_number.value = "";
  app.elements.password.value = "";
  await app.submit();
  assert.equal(app.calls.length, 0);
  assert.equal(app.elements["phone-error"].hidden, false);
  assert.equal(app.elements["password-error"].hidden, false);
  assert.equal(app.elements["login-error"].focused, true);
});

for (const [status, expected] of [[401, "Invalid mobile number"], [400, "could not be verified"], [503, "temporarily unavailable"], [500, "temporarily unavailable"]]) {
  test(`HTTP ${status} shows a safe error and clears password`, async () => {
    const app = setup([token(), response(status, { message: "private-server-details" })]);
    await app.submit();
    assert.ok(app.elements["login-error"].textContent.includes(expected));
    assert.ok(!app.elements["login-error"].textContent.includes("private-server-details"));
    assert.equal(app.elements.password.value, "");
    assert.equal(app.elements["login-button"].disabled, false);
    assert.equal(app.redirects.length, 0);
  });
}

test("network failure is safe and allows another attempt", async () => {
  const app = setup([new Error("private-network-details")]);
  await app.submit();
  assert.ok(app.elements["login-error"].textContent.includes("Check your connection"));
  assert.equal(app.elements.password.value, "");
  assert.equal(app.elements["login-button"].disabled, false);
});

test("missing token prevents credentials from being submitted", async () => {
  const app = setup([response(200, {})]);
  await app.submit();
  assert.equal(app.calls.length, 1);
  assert.equal(app.elements.password.value, "");
});

test("password toggle updates accessibility state", () => {
  const app = setup([]);
  app.elements["password-toggle"].listeners.click();
  assert.equal(app.elements.password.type, "text");
  assert.equal(app.elements["password-toggle"].attributes["aria-pressed"], "true");
  app.elements["password-toggle"].listeners.click();
  assert.equal(app.elements.password.type, "password");
});

test("expired-CSRF retry obtains a fresh token without automatic resubmission", async () => {
  const app = setup([token(), response(400, {}), response(200, { csrf_token: "fresh-token" }), response(200, { ok: true })]);
  await app.submit();
  assert.equal(app.calls.length, 2);
  app.elements.password.value = "synthetic-password";
  await app.submit();
  assert.equal(app.calls[2].method, "GET");
  assert.equal(app.calls[3].headers["X-CSRF-Token"], "fresh-token");
});

test("submission shows loading state and blocks duplicate clicks", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const app = setup([pending, response(200, { ok: true })]);
  const first = app.submit();
  assert.equal(app.elements["login-button"].disabled, true);
  assert.equal(app.elements["login-form"].attributes["aria-busy"], "true");
  await app.submit();
  assert.equal(app.calls.length, 1);
  resolve(token());
  await first;
  assert.equal(app.calls.length, 2);
});

test("malformed server JSON never exposes parser or server details", async () => {
  const app = setup([{ status: 200, ok: true, json: async () => { throw new Error("private-server-content"); } }]);
  await app.submit();
  assert.equal(app.redirects.length, 0);
  assert.ok(!app.elements["login-error"].textContent.includes("private-server-content"));
  assert.equal(app.elements.password.value, "");
  assert.equal(app.elements["login-button"].disabled, false);
});
