/* Reveal the source panel before following an in-page source reference. */
(() => {
  function revealSource(hash, focus = false) {
    let id;
    try {
      id = decodeURIComponent(hash.slice(1));
    } catch {
      return;
    }

    const panel = document.getElementById(id);
    if (!panel || !panel.matches("details.source-file")) return;

    panel.open = true;
    if (focus) panel.querySelector("summary").focus({ preventScroll: true });
    panel.scrollIntoView({ block: "start" });
  }

  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey ||
        event.metaKey || event.shiftKey || event.altKey) return;

    const link = event.target instanceof Element
      ? event.target.closest("a[href]")
      : null;
    if (!link) return;

    const target = new URL(link.href, window.location.href);
    if (target.origin !== window.location.origin ||
        target.pathname !== window.location.pathname ||
        target.search !== window.location.search) return;

    revealSource(target.hash, true);
  });

  window.addEventListener("hashchange", () => revealSource(window.location.hash));
  revealSource(window.location.hash);
})();
