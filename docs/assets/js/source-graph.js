"use strict";

document.querySelectorAll("[data-source-trail]").forEach((figure) => {
  const svg = figure.querySelector("svg");
  const nodes = [...svg.querySelectorAll("[data-node]")];
  const edges = [...svg.querySelectorAll("[data-from]")];
  const label = figure.querySelector("[data-trail-label]");
  const detail = figure.querySelector("[data-trail-detail]");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = matchMedia("(max-width: 700px)");
  const widePositions = [[90, 235], [280, 235], [485, 100], [485, 235], [485, 370], [715, 235], [965, 140], [965, 335]];
  const tallPositions = [[210, 45], [210, 155], [70, 310], [210, 310], [350, 310], [210, 440], [110, 605], [310, 605]];
  const sizes = nodes.map((node) => {
    const plane = node.querySelector(".rv-trail-plane");
    return [Number(plane.getAttribute("width")), Number(plane.getAttribute("height"))];
  });
  const content = nodes.map((node) => node.querySelector(".rv-trail-content"));
  const clamp = (value) => Math.max(0, Math.min(1, value));
  const mix = (from, to, amount) => from + (to - from) * amount;
  const stackYaw = -Math.PI / 3;
  const stackPitch = Math.PI / 6;
  let frame;
  let keyboardFocus = false;
  let pinned;
  let progress = 0;

  function inspect(node) {
    label.textContent = node?.dataset.label || "The source trail";
    detail.textContent = node?.dataset.detail || "Scroll to unfold. Hover or select a node to inspect how three source records become two reconstructed files.";
    const routes = node?.dataset.route?.split(" ") || [];
    const shared = node && !routes.length;
    nodes.forEach((item) => {
      const related = item.dataset.route?.split(" ").some((route) => routes.includes(route));
      item.dataset.active = String(item === node || related);
      item.setAttribute("aria-pressed", String(item === pinned));
    });
    edges.forEach((edge) => {
      const related = edge.dataset.route?.split(" ").some((route) => routes.includes(route));
      edge.dataset.active = String(Boolean(shared || related || (node && !edge.dataset.route)));
    });
    svg.querySelectorAll("[data-section]").forEach((section) => {
      section.dataset.active = String(routes.includes(section.dataset.section));
    });
  }

  function draw() {
    frame = undefined;
    const mobile = narrow.matches;
    svg.setAttribute("viewBox", mobile ? "0 0 420 730" : "0 0 1080 470");
    const bounds = svg.getBoundingClientRect();
    const center = (bounds.top + bounds.height / 2) / innerHeight;
    const pixelsPerUnit = bounds.width / (mobile ? 420 : 1080);
    const stackHeight = 200 * Math.abs(Math.sin(stackPitch) * Math.sin(stackYaw))
      + 110 * Math.cos(stackPitch)
      + (nodes.length - 1) * 20 * Math.abs(Math.sin(stackPitch) * Math.cos(stackYaw));
    const stackZoom = Math.min(mobile ? 1.45 : 1.8, innerHeight * .58 / (stackHeight * pixelsPerUnit));
    const stackRadius = stackHeight * stackZoom * pixelsPerUnit / (2 * innerHeight);
    // Leave the complete stack visible at both ends, with a viewport margin.
    const arrival = Math.min(.8, 1 - .06 - stackRadius);
    const departure = Math.max(.2, .06 + stackRadius);
    const unfolding = reduced.matches || keyboardFocus ? 1 : clamp(Math.min(
      (arrival - center) / (arrival - .56),
      (center - departure) / (.44 - departure),
    ));
    progress = unfolding * unfolding * (3 - 2 * unfolding);
    figure.dataset.progress = progress.toFixed(3);
    figure.dataset.unfolded = String(progress > .85);
    const fade = clamp((progress - .35) / .6);
    const positions = mobile ? tallPositions : widePositions;
    const matrices = new Map();
    const zoom = mix(stackZoom, 1, progress);
    const yaw = stackYaw * (1 - progress);
    const pitch = stackPitch * (1 - progress);

    nodes.forEach((node, index) => {
      const [width, height] = sizes[index];
      const scale = mobile ? (index >= 2 && index <= 4 ? .68 : .94) : 1;
      const layer = index - (nodes.length - 1) / 2;
      // XY planes, +Y up and +Z toward the viewer: rotate Y, then world X.
      // Project the rotated normal for layer spacing; negate Y for SVG.
      const depth = layer * 20 * zoom;
      const x = mix((mobile ? 210 : 540) + depth * Math.sin(stackYaw), positions[index][0], progress);
      const y = mix((mobile ? 365 : 235) + depth * Math.sin(stackPitch) * Math.cos(stackYaw), positions[index][1], progress);
      const planeWidth = mix(200, width * scale, progress) * zoom / width;
      const planeHeight = mix(110, height * scale, progress) * zoom / height;
      const a = planeWidth * Math.cos(yaw);
      const b = -planeWidth * Math.sin(pitch) * Math.sin(yaw);
      const c = 0;
      const d = planeHeight * Math.cos(pitch);
      node.setAttribute("transform", `matrix(${a} ${b} ${c} ${d} ${x} ${y})`);
      content[index].style.opacity = String(fade);
      matrices.set(node.dataset.node, { x, y, a, b, c, d, width, height });
    });

    const port = (matrix, outgoing) => {
      const x = mobile ? 0 : matrix.width / 2 * (outgoing ? 1 : -1);
      const y = mobile ? matrix.height / 2 * (outgoing ? 1 : -1) : 0;
      return [matrix.x + matrix.a * x + matrix.c * y, matrix.y + matrix.b * x + matrix.d * y];
    };
    edges.forEach((edge) => {
      const [x1, y1] = port(matrices.get(edge.dataset.from), true);
      const [x2, y2] = port(matrices.get(edge.dataset.to), false);
      const bend = mobile ? (y2 - y1) / 2 : (x2 - x1) / 2;
      edge.setAttribute("d", mobile
        ? `M${x1} ${y1}C${x1} ${y1 + bend} ${x2} ${y2 - bend} ${x2} ${y2}`
        : `M${x1} ${y1}C${x1 + bend} ${y1} ${x2 - bend} ${y2} ${x2} ${y2}`);
    });
    figure.querySelector(".rv-trail-edges").style.opacity = String(clamp((progress - .6) / .4));
  }

  function schedule() {
    if (frame === undefined) frame = requestAnimationFrame(draw);
  }

  nodes.forEach((node) => {
    node.addEventListener("pointerenter", (event) => {
      if (event.pointerType !== "touch" && progress > .85) inspect(node);
    });
    node.addEventListener("pointerleave", () => {
      inspect(pinned || (keyboardFocus ? svg.querySelector("[data-node]:focus") : undefined));
    });
    node.addEventListener("click", () => {
      pinned = pinned === node ? undefined : node;
      inspect(pinned);
    });
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        node.dispatchEvent(new MouseEvent("click"));
      }
    });
  });
  figure.addEventListener("focusin", (event) => {
    keyboardFocus = event.target.matches(":focus-visible");
    if (keyboardFocus) {
      draw();
      inspect(event.target.closest("[data-node]"));
    }
  });
  figure.addEventListener("focusout", (event) => {
    if (!figure.contains(event.relatedTarget)) {
      keyboardFocus = false;
      inspect(pinned);
      schedule();
    }
  });
  figure.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      pinned = undefined;
      inspect();
    }
  });
  document.addEventListener("click", (event) => {
    const node = event.target instanceof Element ? event.target.closest("[data-node]") : null;
    if (node && figure.contains(node)) return;
    pinned = undefined;
    keyboardFocus = false;
    inspect();
    if (figure.contains(document.activeElement)) document.activeElement.blur();
    schedule();
  });
  addEventListener("scroll", schedule, { passive: true });
  addEventListener("resize", schedule);
  reduced.addEventListener("change", schedule);
  narrow.addEventListener("change", schedule);
  figure.dataset.enhanced = "true";
  draw();
});
