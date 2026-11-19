(() => {
  "use strict";
  const form = document.getElementById("officer-registration-form");
  if (!form) return;
  const names = ["first_name", "last_name", "email", "password", "password_confirmation", "region", "organization"];
  const fields = Object.fromEntries(names.map((name) => [name, document.getElementById(name)]));
  const submit = document.getElementById("officer-submit");
  const fieldset = document.getElementById("officer-fields");
  const error = document.getElementById("officer-error");
  const status = document.getElementById("officer-status");
  let busy = false;
  let completed = false;

  function resetPassword(name) {
    const button = document.getElementById(`${name}-toggle`);
    fields[name].type = "password";
    button.textContent = "Show";
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", name === "password" ? "Show password" : "Show confirm password");
  }
  for (const name of ["password", "password_confirmation"]) {
    const button = document.getElementById(`${name}-toggle`);
    button.hidden = false;
    button.addEventListener("click", () => {
      if (fields[name].type === "text") resetPassword(name);
      else {
        fields[name].type = "text";
        button.textContent = "Hide";
        button.setAttribute("aria-pressed", "true");
        button.setAttribute("aria-label", name === "password" ? "Hide password" : "Hide confirm password");
      }
    });
  }

  function fieldError(name, message) {
    const target = document.getElementById(`${name}-error`);
    target.textContent = message;
    target.hidden = !message;
    fields[name].setAttribute("aria-invalid", message ? "true" : "false");
  }
  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    error.focus();
  }
  function validEmail(value) {
    if (value.length > 254 || !/^[\x00-\x7F]+$/.test(value)) return false;
    const parts = value.toLowerCase().split("@");
    if (parts.length !== 2 || parts[0].length > 64 || !/^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$/.test(parts[0])) return false;
    const labels = parts[1].split(".");
    return labels.length >= 2 && labels.every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label));
  }
  function validate(data) {
    const errors = {};
    for (const name of ["first_name", "last_name", "region"]) {
      if (!data[name]) errors[name] = "This field is required.";
      else if (data[name].length > 100) errors[name] = "Use at most 100 characters.";
    }
    if (!validEmail(data.email)) errors.email = "Provide a valid email address of at most 254 characters.";
    if (!data.password.trim()) errors.password = "A password is required.";
    else if (data.password.length < 8) errors.password = "Use at least 8 characters.";
    else if (data.password.length > 1024) errors.password = "Use at most 1024 characters.";
    if (!data.password_confirmation) errors.password_confirmation = "Confirm your password.";
    else if (data.password_confirmation !== data.password) errors.password_confirmation = "Passwords do not match.";
    if (data.organization.length > 150) errors.organization = "Use at most 150 characters.";
    return errors;
  }
  async function request(options) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/officer/register", {
        credentials: "same-origin", cache: "no-store", redirect: "error",
        ...options, signal: controller.signal,
      });
      return { response, data: await response.json() };
    } finally {
      clearTimeout(timer);
    }
  }

  submit.disabled = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || completed) return;
    error.hidden = true;
    error.textContent = "";
    status.textContent = "";
    names.forEach((name) => fieldError(name, ""));
    const payload = Object.fromEntries(names.map((name) => [name,
      name.startsWith("password") ? fields[name].value : fields[name].value.trim(),
    ]));
    const errors = validate(payload);
    if (Object.keys(errors).length) {
      Object.entries(errors).forEach(([name, message]) => fieldError(name, message));
      showError("Please check the highlighted fields.");
      return;
    }
    busy = true;
    submit.disabled = true;
    fieldset.disabled = true;
    form.setAttribute("aria-busy", "true");
    status.textContent = "Submitting your registration…";
    let sent = false;
    try {
      const token = await request({ method: "GET", headers: { Accept: "application/json" } });
      if (!token.response.ok || !token.data || typeof token.data.csrf_token !== "string" || !token.data.csrf_token) {
        showError("Could not verify this registration. Please try again.");
        return;
      }
      sent = true;
      const { response, data } = await request({
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": token.data.csrf_token },
        body: JSON.stringify(payload),
      });
      if (response.status === 201 && data && data.ok === true && data.status === "pending") {
        completed = true;
        form.reset();
        document.getElementById("officer-form-content").hidden = true;
        const confirmation = document.getElementById("officer-confirmation");
        confirmation.hidden = false;
        confirmation.focus();
      } else if (response.status === 400 && data && data.code === "validation_error") {
        for (const name of names) {
          if (data.errors && typeof data.errors[name] === "string") fieldError(name, data.errors[name]);
        }
        showError("Please check the highlighted fields and enter your passwords again.");
      } else if (response.status === 409) {
        fieldError("email", "Registration cannot be completed with this email address.");
        showError("Please check your email address and enter your passwords again.");
      } else if (response.status === 403) {
        showError("Your registration could not be verified or the security token has expired. Enter your passwords and try again.");
      } else {
        showError("Registration is temporarily unavailable. Please try again later.");
      }
    } catch {
      showError(sent
        ? "We could not confirm your registration. Check your connection before trying again. Your registration may have been received."
        : "Could not connect. Check your connection and try again.");
    } finally {
      for (const name of ["password", "password_confirmation"]) {
        fields[name].value = "";
        payload[name] = "";
        resetPassword(name);
      }
      busy = false;
      fieldset.disabled = completed;
      submit.disabled = completed;
      form.setAttribute("aria-busy", "false");
      status.textContent = "";
    }
  });
})();
