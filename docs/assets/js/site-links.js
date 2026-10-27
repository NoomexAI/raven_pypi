/* Open page and resource links in new tabs; keep in-page navigation in place. */
(() => {
  const current = new URL(window.location.href);

  function prepareLink(link) {
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#")) return;

    let destination;
    try {
      destination = new URL(href, current);
    } catch {
      return;
    }
    if (!["http:", "https:"].includes(destination.protocol)) return;

    const inPage = destination.origin === current.origin &&
      destination.pathname === current.pathname &&
      destination.search === current.search && destination.hash;
    if (inPage) return;

    link.setAttribute("target", "_blank");
    const relations = new Set((link.getAttribute("rel") || "").split(/\s+/).filter(Boolean));
    relations.add("noopener");
    relations.add("noreferrer");
    link.setAttribute("rel", [...relations].join(" "));
  }

  document.querySelectorAll("a[href]").forEach(prepareLink);

  function prepareClickedLink(event) {
    const link = event.target instanceof Element
      ? event.target.closest("a[href]")
      : null;
    if (link) prepareLink(link);
  }

  // Also cover links inserted or changed by page-specific scripts.
  document.addEventListener("click", prepareClickedLink, true);
  document.addEventListener("auxclick", prepareClickedLink, true);
})();
