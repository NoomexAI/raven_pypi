/* Color command words without changing executable text or Rouge markup. */
document.querySelectorAll(".site-content code").forEach((code) => {
  const words = [];
  for (const match of code.textContent.matchAll(/\bpip(\s+)install\b/g)) {
    words.push({ start: match.index, end: match.index + 3, className: "command-pip" });
    const start = match.index + 3 + match[1].length;
    words.push({ start, end: start + 7, className: "command-install" });
  }
  if (!words.length) return;

  const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let offset = 0;
  while (walker.nextNode()) {
    const node = walker.currentNode;
    nodes.push({ node, start: offset, end: offset + node.textContent.length });
    offset += node.textContent.length;
  }

  for (const { node, start, end } of nodes) {
    const overlaps = words.filter((word) => word.start < end && word.end > start);
    if (!overlaps.length) continue;
    const fragment = document.createDocumentFragment();
    const text = node.textContent;
    let cursor = 0;
    for (const word of overlaps) {
      const from = Math.max(start, word.start) - start;
      const to = Math.min(end, word.end) - start;
      fragment.append(document.createTextNode(text.slice(cursor, from)));
      const span = document.createElement("span");
      span.className = word.className;
      span.textContent = text.slice(from, to);
      fragment.append(span);
      cursor = to;
    }
    fragment.append(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  }
});
