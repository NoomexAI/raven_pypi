/* Each illustration switches independently; no backend settings are changed. */
document.querySelectorAll("[data-retrieval-diagram]").forEach((figure) => {
  const controls = figure.querySelector(".retrieval-diagram-controls");
  const scopeLabel = figure.querySelector("[data-scope-label]");
  const localLabel = scopeLabel.textContent;

  controls.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-scope]");
    if (!button || !controls.contains(button)) return;

    const scope = button.dataset.scope;
    controls.querySelectorAll("button[data-scope]").forEach((item) => {
      item.setAttribute("aria-pressed", String(item.dataset.scope === scope));
    });
    figure.querySelectorAll("[data-diagram-view]").forEach((image) => {
      image.hidden = image.dataset.diagramView !== scope;
    });
    figure.querySelectorAll("[data-diagram-link]").forEach((link) => {
      link.hidden = link.dataset.diagramLink !== scope;
    });
    scopeLabel.textContent = localLabel.replace(/^Local /, scope === "global" ? "Global " : "Local ");
  });

  controls.hidden = false;
});
