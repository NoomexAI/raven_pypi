"use strict";

// Keep installation commands selectable if clipboard access is unavailable.
document.querySelectorAll("[data-rv-copy]").forEach((button) => {
  button.setAttribute("aria-live", "polite");
  button.addEventListener("click", async () => {
    const command = button.getAttribute("data-rv-copy");
    if (!command) return;

    button.disabled = true;
    const originalLabel = button.textContent;
    try {
      await navigator.clipboard.writeText(command);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select to copy";
    } finally {
      window.setTimeout(() => {
        button.textContent = originalLabel;
        button.disabled = false;
      }, 1500);
    }
  });
});
