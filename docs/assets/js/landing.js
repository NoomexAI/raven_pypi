"use strict";

// Inspect each card in place by fading only the layers in front of it.
document.querySelectorAll("[data-application-deck]").forEach((deck, deckIndex) => {
  const cards = [...deck.querySelectorAll(".rv-card")];
  let pinned = null;
  const buttons = cards.map((card, index) => {
    const content = document.createElement("div");
    content.className = "rv-layer-content";
    content.id = `application-deck-${deckIndex}-${index}`;
    content.append(...card.childNodes);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rv-layer-handle";
    button.setAttribute("aria-controls", content.id);
    const label = document.createElement("span");
    label.textContent = content.querySelector("h3").textContent;
    button.append(label);
    card.append(button, content);
    return button;
  });

  function select(index) {
    deck.dataset.locked = String(pinned !== null);
    deck.style.setProperty("--pinned-layer", String(pinned === null ? 0 : cards.length - 1 - pinned));
    cards.forEach((card, current) => {
      const active = current === index;
      card.dataset.active = String(active);
      card.dataset.muted = String(current < index);
      card.dataset.pinned = String(current === pinned);
      buttons[current].setAttribute("aria-expanded", String(active));
      buttons[current].setAttribute("aria-pressed", String(current === pinned));
      card.querySelector(".rv-layer-content").setAttribute("aria-hidden", String(!active));
    });
  }

  buttons.forEach((button, index) => {
    cards[index].addEventListener("pointerenter", (event) => {
      const keyboardFocused = buttons.some((control) => control === document.activeElement && control.matches(":focus-visible"));
      if (event.pointerType !== "touch" && pinned === null && !keyboardFocused) select(index);
    });
    button.addEventListener("focus", () => { if (pinned === null) select(index); });
    cards[index].addEventListener("click", () => {
      pinned = index;
      select(index);
    });
  });
  deck.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "touch" || pinned !== null) return;
    const focused = buttons.findIndex((button) => button === document.activeElement && button.matches(":focus-visible"));
    select(focused < 0 ? 0 : focused);
  });
  deck.addEventListener("focusout", (event) => {
    if (pinned === null && !deck.contains(event.relatedTarget)) select(0);
  });
  document.addEventListener("click", (event) => {
    const card = event.target instanceof Element ? event.target.closest(".rv-card") : null;
    if (cards.includes(card)) return;
    pinned = null;
    select(0);
    if (deck.contains(document.activeElement)) document.activeElement.blur();
  });
  deck.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      pinned = null;
      select(0);
      buttons[0].focus({ preventScroll: true });
    }
  });
  deck.dataset.enhanced = "true";
  select(0);
});

// Flip the conversation-memory cards to reveal their full descriptions.
document.querySelectorAll("[data-memory-flip]").forEach((button) => {
  const front = button.querySelector("[data-memory-front]");
  const back = button.querySelector("[data-memory-back]");
  const title = front.querySelector("b").textContent;

  button.addEventListener("click", () => {
    const expanded = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(expanded));
    button.setAttribute("aria-label", `${expanded ? "Show summary of" : "Show details for"} ${title}`);
    front.setAttribute("aria-hidden", String(expanded));
    front.inert = expanded;
    back.setAttribute("aria-hidden", String(!expanded));
    back.inert = !expanded;
  });
});

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

// Scroll over the workflow cards to inspect each step.
document.querySelectorAll("[data-rv-flow]").forEach((flow) => {
  const panels = [...flow.querySelectorAll("[data-flow-panel]")];
  const details = flow.querySelector(".rv-flow-details");
  const position = flow.querySelector("[data-flow-position]");
  let selected = 0;
  let drag = null;
  let pendingClickPanel = null;
  let suppressClick = false;

  function setFlipped(panel, flipped) {
    const card = panel.querySelector("[data-flow-flip]");
    const front = panel.querySelector("[data-flow-front]");
    const back = panel.querySelector("[data-flow-back]");
    panel.dataset.flipped = String(flipped);
    card.dataset.flipped = String(flipped);
    panel.setAttribute("aria-expanded", String(flipped));
    panel.setAttribute("aria-label", `${front.querySelector("h3").textContent}; ${flipped ? "details" : "summary"}. Press Enter to flip.`);
    front.setAttribute("aria-hidden", String(flipped));
    front.inert = flipped;
    back.setAttribute("aria-hidden", String(!flipped));
    back.inert = !flipped;
  }

  function select(index, focus = false) {
    selected = (index + panels.length) % panels.length;
    panels.forEach((panel, current) => {
      let offset = (current - selected + panels.length) % panels.length;
      if (offset === panels.length - 1) offset = -1;
      panel.hidden = offset === 2;
      panel.dataset.position = String(offset);
      panel.setAttribute("aria-hidden", String(current !== selected));
      panel.tabIndex = current === selected ? 0 : -1;
      panel.setAttribute("role", "group");
      const flipped = panel.dataset.flipped === "true";
      panel.setAttribute("aria-expanded", String(flipped));
      panel.setAttribute("aria-label", `${panel.querySelector("[data-flow-front] h3").textContent}; ${flipped ? "details" : "summary"}. Press Enter to flip.`);
    });
    position.textContent = `Step ${selected + 1} of ${panels.length}`;
    if (focus) panels[selected].focus();
  }

  function finishDrag(event, advance) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const distance = event.clientX - drag.startX;
    const moved = drag.moved;
    const panel = drag.panel;
    const pointerId = drag.pointerId;
    drag = null;
    flow.classList.remove("is-dragging");
    details.style.removeProperty("--flow-drag-x");
    if (details.hasPointerCapture(pointerId)) details.releasePointerCapture(pointerId);
    if (advance && Math.abs(distance) >= 48) {
      select(selected + (distance < 0 ? 1 : -1));
    }
    pendingClickPanel = advance && !moved ? panel : null;
    suppressClick = advance && moved;
  }

  details.addEventListener("pointerdown", (event) => {
    if (!event.isPrimary || event.button !== 0 || !event.target.closest("[data-flow-panel]")) return;
    suppressClick = false;
    pendingClickPanel = null;
    drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      moved: false,
      panel: event.target.closest("[data-flow-panel]"),
    };
    details.setPointerCapture(event.pointerId);
    flow.classList.add("is-dragging");
  });
  details.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const distance = event.clientX - drag.startX;
    if (Math.abs(distance) >= 8) drag.moved = true;
    details.style.setProperty("--flow-drag-x", `${distance}px`);
  });
  details.addEventListener("pointerup", (event) => finishDrag(event, true));
  details.addEventListener("pointercancel", (event) => finishDrag(event, false));
  details.addEventListener("lostpointercapture", (event) => finishDrag(event, false));
  details.addEventListener("click", (event) => {
    const targetPanel = event.target instanceof Element ? event.target.closest("[data-flow-panel]") : null;
    const panel = targetPanel || pendingClickPanel;
    pendingClickPanel = null;
    if (!panel) return;
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    setFlipped(panel, panel.dataset.flipped !== "true");
  });
  panels.forEach((panel) => {
    panel.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      setFlipped(panel, panel.dataset.flipped !== "true");
    });
  });
  details.addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? panels.length - 1 : selected + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1);
    select(next, true);
  });

  flow.dataset.enhanced = "";
  select(0);
});
