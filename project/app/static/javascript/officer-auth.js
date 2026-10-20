(() => {
  "use strict";
  const form = document.getElementById("officer-login-form");
  const logout = document.getElementById("officer-logout-button");
  if (!form && !logout) return;

  function showError(element, message) {
    element.textContent = message;
    element.hidden = false;
    element.focus();
  }
  async function api(url, options) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {
        ...options, credentials: "same-origin", cache: "no-store",
        redirect: "error", signal: controller.signal,
      });
      return { response, data: response.ok ? await response.json() : null };
    } finally {
      clearTimeout(timer);
    }
  }

  if (form) {
    const email = document.getElementById("email");
    const password = document.getElementById("password");
    const button = document.getElementById("officer-login-button");
    const fields = document.getElementById("officer-login-fields");
    const toggle = document.getElementById("officer-password-toggle");
    const error = document.getElementById("officer-login-error");
    const status = document.getElementById("officer-login-status");
    let busy = false;
    button.disabled = false;
    toggle.hidden = false;
    function passwordVisibility(show) {
      password.type = show ? "text" : "password";
      toggle.textContent = show ? "Hide" : "Show";
      toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
      toggle.setAttribute("aria-pressed", String(show));
    }
    toggle.addEventListener("click", () => passwordVisibility(password.type === "password"));
    function fieldError(input, message) {
      const target = document.getElementById(`${input.id}-error`);
      target.textContent = message;
      target.hidden = !message;
      input.setAttribute("aria-invalid", String(Boolean(message)));
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      status.textContent = "";
      fieldError(email, !email.value.trim() ? "Enter your registered email address." : email.value.trim().length > 254 ? "Use at most 254 characters." : "");
      fieldError(password, !password.value ? "Enter your password." : password.value.length > 1024 ? "Use at most 1024 characters." : "");
      if (email.getAttribute("aria-invalid") === "true" || password.getAttribute("aria-invalid") === "true") {
        showError(error, "Please check the highlighted fields.");
        return;
      }
      const credentials = { email: email.value.trim(), password: password.value };
      busy = true;
      button.disabled = true;
      fields.disabled = true;
      form.setAttribute("aria-busy", "true");
      button.textContent = "Logging in...";
      status.textContent = "Verifying your account. Please wait.";
      let succeeded = false;
      try {
        const token = await api("/officer/login", { method: "GET", headers: { Accept: "application/json" } });
        if (!token.response.ok || !token.data || typeof token.data.csrf_token !== "string" || !token.data.csrf_token) {
          showError(error, "Login could not be prepared. Please try again.");
          return;
        }
        const { response, data } = await api("/officer/login", {
          method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": token.data.csrf_token },
          body: JSON.stringify(credentials),
        });
        if (response.status === 401) showError(error, "Invalid email or password. Please try again.");
        else if (response.status === 400 || response.status === 403) showError(error, "Your request could not be verified. Enter your password and try again.");
        else if (!response.ok) showError(error, "Login is temporarily unavailable. Please try again later.");
        else if (!data || data.ok !== true) showError(error, "Login could not be confirmed. Please try again.");
        else {
          window.location.assign("/officer/dashboard");
          succeeded = true;
        }
      } catch {
        showError(error, "Unable to reach the login service. Check your connection and try again.");
      } finally {
        password.value = "";
        credentials.password = "";
        passwordVisibility(false);
        busy = succeeded;
        button.disabled = succeeded;
        fields.disabled = succeeded;
        form.setAttribute("aria-busy", "false");
        if (!succeeded) button.textContent = "Log in";
        status.textContent = succeeded ? "Login successful. Opening your officer home." : "";
      }
    });
  }

  if (logout) {
    const error = document.getElementById("officer-logout-error");
    const status = document.getElementById("officer-logout-status");
    let busy = false;
    logout.disabled = false;
    logout.addEventListener("click", async () => {
      if (busy) return;
      busy = true;
      logout.disabled = true;
      logout.setAttribute("aria-busy", "true");
      logout.textContent = "Logging out...";
      error.hidden = true;
      status.textContent = "Logging out. Please wait.";
      let finished = false;
      function expired() {
        finished = true;
        document.getElementById("officer-login-again").hidden = false;
        showError(error, "Your session has expired or access is no longer available. Please log in again.");
      }
      try {
        // Fetch a current logout token from the existing protected JSON endpoint.
        // Its identity fields are neither rendered nor persisted in the browser.
        const token = await api("/officer/home", { method: "GET", headers: { Accept: "application/json" } });
        if (token.response.status === 401) { expired(); return; }
        if (!token.response.ok || !token.data || token.data.ok !== true || typeof token.data.csrf_token !== "string" || !token.data.csrf_token) {
          showError(error, "Logout could not be prepared. Please try again later.");
          return;
        }
        const { response, data } = await api("/officer/logout", {
          method: "POST", headers: { Accept: "application/json", "X-CSRF-Token": token.data.csrf_token },
        });
        if (response.status === 401) expired();
        else if (response.status === 400 || response.status === 403) showError(error, "Your logout request could not be verified. Please try again.");
        else if (!response.ok) showError(error, "Logout is temporarily unavailable. Please try again later.");
        else if (!data || data.ok !== true) showError(error, "Unable to confirm logout. Please try again.");
        else {
          window.location.assign("/officer/login-page");
          finished = true;
        }
      } catch {
        showError(error, "Unable to confirm logout. Check your connection and try again.");
      } finally {
        busy = finished;
        logout.disabled = finished;
        logout.setAttribute("aria-busy", "false");
        logout.textContent = "Logout";
        status.textContent = "";
      }
    });
  }
})();
