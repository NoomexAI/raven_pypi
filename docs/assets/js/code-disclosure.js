/* Copy runnable scripts and keep long code blocks compact. */
(() => {
  const previewLines = 15;
  const copyIcon = '<svg class="rv-copy-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="9" y="9" width="12" height="12" rx="2"></rect><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"></path></svg>';
  const copiedIcon = '<svg class="rv-copied-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m5 12 4 4L19 6"></path></svg>';

  // Capture the original source before the shortened previews are created.
  document.querySelectorAll(".markdown-body .api-example").forEach((figure) => {
    const code = figure.querySelector("pre > code");
    if (!code || figure.querySelector(".code-copy")) return;
    const source = code.textContent;
    const filename = figure.querySelector(".code-snippet-filename")?.textContent.trim();
    const label = filename ? `Copy ${filename}` : "Copy script";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "code-copy";
    button.innerHTML = copyIcon;
    button.title = "Copy script";
    button.setAttribute("aria-label", label);

    const status = document.createElement("span");
    status.className = "code-copy-status";
    status.setAttribute("role", "status");
    let resetTimer;
    button.addEventListener("click", async () => {
      window.clearTimeout(resetTimer);
      button.disabled = true;
      status.textContent = "";
      try {
        await navigator.clipboard.writeText(source);
        button.innerHTML = copiedIcon;
        button.title = "Copied";
        button.setAttribute("aria-label", "Copied");
        status.textContent = filename ? `${filename} copied.` : "Script copied.";
      } catch {
        button.title = "Copy unavailable; expand the script and copy it manually.";
        button.setAttribute("aria-label", button.title);
        status.textContent = button.title;
      } finally {
        button.disabled = false;
        resetTimer = window.setTimeout(() => {
          button.innerHTML = copyIcon;
          button.title = "Copy script";
          button.setAttribute("aria-label", label);
        }, 1800);
      }
    });

    figure.classList.add("has-code-copy");
    figure.append(button, status);
  });

  document.querySelectorAll(".markdown-body pre > code").forEach((code, index) => {
    const pre = code.parentElement;
    if (pre.closest(".source-file, .code-disclosure")) return;

    const text = code.textContent.replace(/\n$/, "");
    const lines = text.split("\n");
    if (lines.length <= previewLines) return;

    // Clone only the first fifteen lines, retaining nested highlighting spans.
    let remaining = lines.slice(0, previewLines).join("\n").length;
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    range.setStart(code, 0);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (remaining <= node.textContent.length) {
        range.setEnd(node, remaining);
        break;
      }
      remaining -= node.textContent.length;
    }

    const preview = pre.cloneNode(false);
    preview.removeAttribute("id");
    const previewCode = code.cloneNode(false);
    previewCode.removeAttribute("id");
    previewCode.append(range.cloneContents());
    preview.append(previewCode);

    const container = document.createElement("div");
    container.className = "code-disclosure";
    pre.before(container);
    container.append(preview, pre);
    pre.id ||= `code-disclosure-${index}`;
    pre.hidden = true;

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "code-disclosure-toggle";
    toggle.setAttribute("aria-controls", pre.id);
    const label = pre.closest(".code-snippet")
      ?.querySelector(".code-snippet-filename")?.textContent.trim();

    function setExpanded(expanded) {
      pre.hidden = !expanded;
      preview.hidden = expanded;
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.textContent = expanded ? "Show less" : "Show more";
      toggle.setAttribute("aria-label", label
        ? `${toggle.textContent} — ${label}`
        : toggle.textContent);
    }

    toggle.addEventListener("click", () => {
      setExpanded(toggle.getAttribute("aria-expanded") !== "true");
    });
    container.append(toggle);
    setExpanded(false);
  });
})();
