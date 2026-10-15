(() => {
  const button = document.getElementById("logout-button");
  if (!button) return;
  const token = document.getElementById("logout-csrf-token");
  const error = document.getElementById("logout-error");
  const status = document.getElementById("logout-status");
  let busy = false;

  function showError(message) {
    // Never display server diagnostics or imply an unconfirmed logout succeeded.
    error.textContent = message;
    error.hidden = false;
    error.focus();
  }

  button.disabled = false;
  button.addEventListener("click", async () => {
    if (busy) return;
    error.hidden = true;
    status.textContent = "";
    if (!token || !token.value) {
      showError("Logout could not be prepared. Please reload the page and try again.");
      return;
    }

    busy = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "Logging out...";
    status.textContent = "Logging out. Please wait.";
    let succeeded = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      // Send the scoped token only in a same-origin POST header, without retries.
      const response = await fetch("/farmer/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        redirect: "error", signal: controller.signal,
        headers: { Accept: "application/json", "X-CSRF-Token": token.value },
      });
      if (response.status === 400) {
        showError("Your logout request could not be verified. Please reload the page and try again.");
      } else if (response.status === 401) {
        showError("Your session could not be verified. Please reload the page before trying again.");
      } else if (!response.ok) {
        showError("Logout is temporarily unavailable. Please try again later.");
      } else {
        // Keep the timeout active until JSON confirms the session was cleared.
        const result = await response.json();
        if (!result || result.ok !== true) {
          showError("Unable to confirm logout. Please reload the page and try again.");
          return;
        }
        window.location.assign("/farmer/login-page");
        succeeded = true;
      }
    } catch {
      showError("Unable to confirm logout. Check your connection and try again.");
    } finally {
      clearTimeout(timer);
      busy = succeeded;
      button.disabled = succeeded;
      button.setAttribute("aria-busy", "false");
      if (!succeeded) button.textContent = "Logout";
      status.textContent = "";
    }
  });
})();
