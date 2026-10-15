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

// Enhance the readable, anchor-linked workflow into a selectable flowchart.
document.querySelectorAll("[data-rv-flow]").forEach((flow) => {
  const path = flow.querySelector(".rv-flow-path");
  const steps = [...flow.querySelectorAll("[data-flow-step]")];
  const panels = [...flow.querySelectorAll("[data-flow-panel]")];
  const previous = flow.querySelector("[data-flow-previous]");
  const next = flow.querySelector("[data-flow-next]");
  const position = flow.querySelector("[data-flow-position]");
  const narrow = window.matchMedia("(max-width: 760px)");
  let selected = 0;

  function select(index, focus = false) {
    selected = (index + steps.length) % steps.length;
    steps.forEach((step, current) => {
      step.setAttribute("aria-selected", String(current === selected));
      step.tabIndex = current === selected ? 0 : -1;
      panels[current].hidden = current !== selected;
    });
    previous.disabled = selected === 0;
    next.textContent = selected === steps.length - 1 ? "Back to question \u21ba" : "Next step \u2192";
    position.textContent = `Step ${selected + 1} of ${steps.length}`;
    if (focus) steps[selected].focus();
  }

  function updateOrientation() {
    path.setAttribute("aria-orientation", narrow.matches ? "vertical" : "horizontal");
  }

  path.setAttribute("role", "tablist");
  updateOrientation();
  narrow.addEventListener("change", updateOrientation);
  steps.forEach((step, index) => {
    step.parentElement.setAttribute("role", "presentation");
    step.setAttribute("role", "tab");
    step.setAttribute("aria-controls", panels[index].id);
    panels[index].setAttribute("role", "tabpanel");
    panels[index].setAttribute("aria-labelledby", step.id);
    panels[index].tabIndex = 0;
    step.addEventListener("click", (event) => {
      event.preventDefault();
      select(index);
    });
    step.addEventListener("keydown", (event) => {
      let target;
      if (["ArrowRight", "ArrowDown"].includes(event.key)) target = index + 1;
      else if (["ArrowLeft", "ArrowUp"].includes(event.key)) target = index - 1;
      else if (event.key === "Home") target = 0;
      else if (event.key === "End") target = steps.length - 1;
      else if (event.key === " ") target = index;
      else return;
      event.preventDefault();
      select(target, true);
    });
  });
  previous.addEventListener("click", () => select(selected - 1));
  next.addEventListener("click", () => select(selected + 1));
  flow.dataset.enhanced = "";
  flow.querySelector(".rv-flow-controls").hidden = false;
  select(0);
});
