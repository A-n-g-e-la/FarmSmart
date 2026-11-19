const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../app/static/javascript/officer-registration.js"), "utf8");
const names = ["first_name", "last_name", "email", "password", "password_confirmation", "region", "organization"];
const response = (status, data) => ({ status, ok: status >= 200 && status < 300, json: async () => data });
const token = () => response(200, { ok: true, csrf_token: "synthetic-officer-token" });
const success = () => response(201, { ok: true, status: "pending" });

function setup(responses = []) {
  let focused;
  const ids = [...names, ...names.map((name) => `${name}-error`), "password-toggle", "password_confirmation-toggle",
    "officer-registration-form", "officer-fields", "officer-submit", "officer-error", "officer-status", "officer-confirmation", "officer-form-content"];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    value: "", type: id.startsWith("password") ? "password" : "text", hidden: true, disabled: false,
    textContent: "", attributes: {}, listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, listener) { this.listeners[name] = listener; },
    focus() { focused = id; },
    set innerHTML(value) { throw new Error("Unsafe HTML rendering"); },
  }]));
  const values = { first_name: " Test ", last_name: " Officer ", email: " Officer@Example.COM ",
    password: " synthetic-password ", password_confirmation: " synthetic-password ", region: " Uasin Gishu ", organization: "" };
  Object.entries(values).forEach(([name, value]) => { elements[name].value = value; });
  elements["officer-form-content"].hidden = false;
  elements["officer-registration-form"].reset = () => names.forEach((name) => { elements[name].value = ""; });
  const calls = [];
  vm.runInNewContext(source, {
    document: { getElementById: (id) => elements[id] },
    AbortController, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push({ url, ...options });
      const result = await responses.shift();
      if (result instanceof Error) throw result;
      return result;
    },
  });
  return { elements, calls, get focused() { return focused; },
    submit: () => elements["officer-registration-form"].listeners.submit({ preventDefault() {} }) };
}

test("JSON registration uses the officer-scoped token and only supported fields, without redirecting or authenticating", async () => {
  const app = setup([token(), success()]);
  await app.submit();
  assert.equal(app.calls.length, 2);
  assert.ok(app.calls.every((call) => call.url === "/officer/register" && call.credentials === "same-origin" && call.cache === "no-store" && call.redirect === "error"));
  assert.equal(app.calls[0].method, "GET");
  assert.equal(app.calls[1].method, "POST");
  assert.equal(app.calls[1].headers["X-CSRF-Token"], "synthetic-officer-token");
  assert.equal(app.calls[1].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(app.calls[1].body), {
    first_name: "Test", last_name: "Officer", email: "Officer@Example.COM",
    password: " synthetic-password ", password_confirmation: " synthetic-password ", region: "Uasin Gishu", organization: "",
  });
  assert.equal(app.elements["officer-form-content"].hidden, true);
  assert.equal(app.elements["officer-confirmation"].hidden, false);
  assert.equal(app.focused, "officer-confirmation");
  names.forEach((name) => assert.equal(app.elements[name].value, ""));
  assert.equal(app.elements["officer-submit"].disabled, true);
  await app.submit();
  assert.equal(app.calls.length, 2);
});

test("required fields and password matching show accessible errors without requests", async () => {
  const app = setup();
  for (const name of names.filter((name) => name !== "organization")) app.elements[name].value = "";
  await app.submit();
  assert.equal(app.calls.length, 0);
  for (const name of names.filter((name) => name !== "organization")) {
    assert.equal(app.elements[`${name}-error`].hidden, false);
    assert.equal(app.elements[name].attributes["aria-invalid"], "true");
  }
  assert.equal(app.elements["organization-error"].hidden, true);
  assert.equal(app.focused, "officer-error");
  const mismatch = setup();
  mismatch.elements.password_confirmation.value = "different-password";
  await mismatch.submit();
  assert.match(mismatch.elements["password_confirmation-error"].textContent, /do not match/);
});

for (const [name, value] of [["first_name", "a".repeat(101)], ["last_name", "a".repeat(101)], ["region", "a".repeat(101)],
  ["organization", "a".repeat(151)], ["email", "a..b@example.com"], ["email", "a@-example.com"],
  ["email", "a@example"], ["email", "a".repeat(65) + "@example.com"], ["password", "short"], ["password", " ".repeat(8)], ["password", "a".repeat(1025)]]) {
  test(`invalid ${name} (${value.length} characters) blocks requests`, async () => {
    const app = setup();
    app.elements[name].value = value;
    await app.submit();
    assert.equal(app.calls.length, 0);
    assert.equal(app.elements[`${name}-error`].hidden, false);
  });
}

test("optional organization and maximum text and password boundaries are accepted", async () => {
  const app = setup([token(), success()]);
  for (const name of ["first_name", "last_name", "region"]) app.elements[name].value = "x".repeat(100);
  app.elements.organization.value = "x".repeat(150);
  app.elements.password.value = app.elements.password_confirmation.value = "x".repeat(1024);
  app.elements.email.value = "a".repeat(64) + "@" + "b".repeat(63) + "." + "c".repeat(63) + "." + "d".repeat(61);
  await app.submit();
  assert.equal(app.calls.length, 2);
  assert.equal(app.elements["officer-confirmation"].hidden, false);
});

test("backend field validation uses textContent and ignores unknown error keys", async () => {
  const message = "<img src=x onerror=alert(1)>";
  const app = setup([token(), response(400, { code: "validation_error", errors: { region: message, request: "private SQL", unknown: "private SQL" } })]);
  await app.submit();
  assert.equal(app.elements["region-error"].textContent, message);
  assert.equal(app.elements.region.attributes["aria-invalid"], "true");
  assert.equal(app.elements["officer-confirmation"].hidden, true);
  assert.equal(app.elements["officer-form-content"].hidden, false);
  assert.equal(app.elements.password.value, "");
  assert.equal(app.elements.password_confirmation.value, "");
  assert.ok(!app.elements["officer-error"].textContent.includes("private SQL"));
});

for (const [code, pattern] of [[409, /email address/], [403, /expired/], [503, /temporarily unavailable/], [500, /temporarily unavailable/]]) {
  test(`HTTP ${code} is safe and allows an explicit retry`, async () => {
    const app = setup([token(), response(code, { message: "private SQL credentials" })]);
    await app.submit();
    assert.match(app.elements["officer-error"].textContent, pattern);
    assert.ok(!app.elements["officer-error"].textContent.includes("private SQL"));
    assert.equal(app.elements["officer-submit"].disabled, false);
    assert.equal(app.elements["officer-fields"].disabled, false);
    assert.equal(app.elements.password.value, "");
    assert.equal(app.elements.password_confirmation.value, "");
    if (code === 409) assert.equal(app.elements["email-error"].hidden, false);
  });
}

test("expired CSRF requires an explicit retry with a fresh GET token", async () => {
  const app = setup([token(), response(403, {}), response(200, { csrf_token: "fresh-token" }), success()]);
  await app.submit();
  assert.equal(app.calls.length, 2);
  app.elements.password.value = app.elements.password_confirmation.value = "synthetic-password";
  await app.submit();
  assert.equal(app.calls[2].method, "GET");
  assert.equal(app.calls[3].headers["X-CSRF-Token"], "fresh-token");
  assert.equal(app.elements.email.attributes["aria-invalid"], "false");
});

test("missing token or failed token request prevents POST", async () => {
  for (const result of [response(200, {}), response(503, { message: "private" }), response(200, null)]) {
    const app = setup([result]);
    await app.submit();
    assert.equal(app.calls.length, 1);
    assert.match(app.elements["officer-error"].textContent, /Could not verify/);
  }
});

test("network errors and malformed JSON reveal no internal details and never retry automatically", async () => {
  for (const responses of [[new Error("private")], [token(), new Error("private")],
    [token(), { status: 201, json: async () => { throw new Error("private HTML response"); } }]]) {
    const app = setup(responses);
    await app.submit();
    assert.match(app.elements["officer-error"].textContent, /connection/);
    assert.ok(!app.elements["officer-error"].textContent.includes("private"));
    assert.equal(app.elements["officer-submit"].disabled, false);
    assert.equal(app.elements["officer-confirmation"].hidden, true);
    assert.equal(app.elements.password.value, "");
    if (app.calls.length === 2) assert.match(app.elements["officer-error"].textContent, /may have been received/);
  }
});

test("pending requests disable fields and prevent duplicate submissions through token GET and POST", async () => {
  let resolveToken, resolvePost;
  const app = setup([new Promise((resolve) => { resolveToken = resolve; }), new Promise((resolve) => { resolvePost = resolve; })]);
  const first = app.submit();
  assert.equal(app.elements["officer-submit"].disabled, true);
  assert.equal(app.elements["officer-fields"].disabled, true);
  assert.equal(app.elements["officer-registration-form"].attributes["aria-busy"], "true");
  await app.submit();
  assert.equal(app.calls.length, 1);
  resolveToken(token());
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.calls.length, 2);
  await app.submit();
  assert.equal(app.calls.length, 2);
  resolvePost(success());
  await first;
  assert.equal(app.elements["officer-registration-form"].attributes["aria-busy"], "false");
});

test("both password visibility controls update accessible state and reset after submission", async () => {
  const app = setup([token(), response(503, {})]);
  for (const name of ["password", "password_confirmation"]) {
    const toggle = app.elements[`${name}-toggle`];
    assert.equal(toggle.hidden, false);
    toggle.listeners.click();
    assert.equal(app.elements[name].type, "text");
    assert.equal(toggle.attributes["aria-pressed"], "true");
    assert.match(toggle.attributes["aria-label"], /^Hide/);
  }
  await app.submit();
  for (const name of ["password", "password_confirmation"]) {
    assert.equal(app.elements[name].type, "password");
    assert.equal(app.elements[`${name}-toggle`].attributes["aria-pressed"], "false");
  }
});
