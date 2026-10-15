(() => {
  const form = document.getElementById("login-form");
  if (!form) return;
  const phone = document.getElementById("phone_number");
  const password = document.getElementById("password");
  const button = document.getElementById("login-button");
  const toggle = document.getElementById("password-toggle");
  const error = document.getElementById("login-error");
  const status = document.getElementById("login-status");
  let busy = false;

  button.disabled = false;
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    toggle.textContent = show ? "Hide" : "Show";
    toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
    toggle.setAttribute("aria-pressed", String(show));
  });

  function showError(message) {
    // Use fixed messages and textContent; never render raw server diagnostics.
    error.textContent = message;
    error.hidden = false;
    error.focus();
  }

  function fieldError(input, id, message) {
    input.setAttribute("aria-invalid", String(Boolean(message)));
    const element = document.getElementById(id);
    element.textContent = message;
    element.hidden = !message;
  }

  async function api(options) {
    // Bound stalled requests without automatically retrying credential submissions.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/farmer/login", {
        ...options, credentials: "same-origin", cache: "no-store",
        redirect: "error", signal: controller.signal,
      });
      // Keep the timeout active while reading successful JSON response bodies too.
      const data = response.ok ? await response.json() : null;
      return { response, data };
    } finally {
      clearTimeout(timer);
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    error.hidden = true;
    status.textContent = "";
    fieldError(phone, "phone-error", phone.value.trim() ? "" : "Enter your Kenyan mobile number.");
    fieldError(password, "password-error", password.value ? "" : "Enter your password.");
    if (!phone.value.trim() || !password.value) {
      showError("Please complete the highlighted fields.");
      return;
    }
    if (phone.value.length > 40 || password.value.length > 1024) {
      showError("Please check your mobile number and password.");
      password.value = "";
      return;
    }

    busy = true;
    button.disabled = true;
    form.setAttribute("aria-busy", "true");
    button.textContent = "Logging in…";
    status.textContent = "Verifying your account. Please wait.";
    let succeeded = false;
    try {
      // Get a fresh session-bound token for each attempt; never persist credentials.
      const { response: tokenResponse, data: tokenData } = await api({ method: "GET", headers: { Accept: "application/json" } });
      if (!tokenResponse.ok) {
        showError("Login is temporarily unavailable. Please try again later.");
        return;
      }
      if (typeof tokenData.csrf_token !== "string" || !tokenData.csrf_token) {
        showError("Login could not be prepared. Please reload the page and try again.");
        return;
      }
      const { response, data: result } = await api({
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": tokenData.csrf_token },
        body: JSON.stringify({ phone_number: phone.value.trim(), password: password.value }),
      });
      if (response.status === 401) {
        showError("Invalid mobile number or password. Please try again.");
      } else if (response.status === 400) {
        // The next attempt fetches a fresh token; do not resubmit a password silently.
        showError("Your request could not be verified. Enter your password and try again.");
      } else if (!response.ok) {
        showError("Login is temporarily unavailable. Please try again later.");
      } else {
        if (result.ok !== true) {
          showError("Login could not be completed. Please try again.");
          return;
        }
        succeeded = true;
        status.textContent = "Login successful. Opening your farmer home.";
        // The protected destination renders the farmer dashboard after revalidation.
        window.location.assign("/farmer/home");
      }
    } catch {
      showError("Unable to reach the login service. Check your connection and try again.");
    } finally {
      password.value = "";
      password.type = "password";
      toggle.textContent = "Show";
      toggle.setAttribute("aria-label", "Show password");
      toggle.setAttribute("aria-pressed", "false");
      busy = succeeded;
      button.disabled = succeeded;
      form.setAttribute("aria-busy", "false");
      if (!succeeded) {
        button.textContent = "Log in →";
        status.textContent = "";
      }
    }
  });
})();
