"use strict";

// Keep installation commands selectable if clipboard access is unavailable.
document.querySelectorAll("[data-rv-copy]").forEach((button) => {
  const status = button.parentElement.querySelector(".rv-copy-status");
  const originalTitle = button.title;
  button.addEventListener("click", async () => {
    const command = button.getAttribute("data-rv-copy");
    if (!command) return;

    button.disabled = true;
    if (status) status.textContent = "";
    try {
      await navigator.clipboard.writeText(command);
      button.dataset.copyState = "copied";
      button.title = "Copied";
      if (status) status.textContent = "Install command copied.";
    } catch {
      button.dataset.copyState = "failed";
      button.title = "Copy unavailable; select the command to copy manually.";
      if (status) status.textContent = button.title;
    } finally {
      window.setTimeout(() => {
        delete button.dataset.copyState;
        button.title = originalTitle;
        button.disabled = false;
      }, 1500);
    }
  });
});
