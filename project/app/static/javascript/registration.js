// Reveal controls only when JavaScript is available; form submission stays native.
document.querySelectorAll("[data-password-target]").forEach((button) => {
  const input = document.getElementById(button.dataset.passwordTarget);
  button.hidden = false;
  button.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? "Hide" : "Show";
    button.setAttribute("aria-pressed", String(show));
    button.setAttribute("aria-label", `${show ? "Hide" : "Show"} ${input.id === "password" ? "password" : "confirm password"}`);
  });
});

// Move keyboard/screen-reader focus to server-rendered errors after a failed POST.
const errorSummary = document.getElementById("error-summary");
if (errorSummary) errorSummary.focus();
