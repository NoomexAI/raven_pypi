"use strict";

// One persistent SVG hierarchy. Only its camera and emphasis change between steps.
class RavenRetrievalCamera {
  static clipCounter = 0;


  constructor(viewport, question, example, scope, compact) {
    this.viewport = viewport;
    this.question = question;
    this.example = example;
    this.scope = scope;
    this.compact = compact;
    this.width = compact ? 420 : 960;
    this.height = compact ? 290 : 440;
    this.rooms = new Map();
    this.measure = document.createElement("canvas").getContext("2d");
    this.build();
  }


  escape(value) {
    return String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[character]);
  }


  label(section) {
    return `Section ${section.id}`;
  }


  rect(box, attributes = "") {
    return `<rect x="${box.x}" y="${box.y}" width="${box.w}" height="${box.h}" rx="8" class="rv-world-frame" ${attributes}/>`;
  }


  folder(box) {
    const { x, y, w, h } = box;
    const tab = Math.min(80, w * .38);
    return `<path class="rv-world-frame" data-knowledge-border d="M${x + 8} ${y}H${x + tab}L${x + tab + 16} ${y + 16}H${x + w - 8}Q${x + w} ${y + 16} ${x + w} ${y + 24}V${y + h - 8}Q${x + w} ${y + h} ${x + w - 8} ${y + h}H${x + 8}Q${x} ${y + h} ${x} ${y + h - 8}V${y + 8}Q${x} ${y} ${x + 8} ${y}Z"/>`;
  }


  text(x, y, value, width, size, attributes = "") {
    this.measure.font = `${size}px ${getComputedStyle(this.viewport).fontFamily}`;
    const lines = [];
    let current = "";
    for (const word of String(value).split(/\s+/)) {
      if (this.measure.measureText(`${current}${current ? " " : ""}${word}`).width <= width) {
        current += `${current ? " " : ""}${word}`;
      } else {
        if (current) lines.push(current);
        current = "";
        for (const character of word) {
          if (current && this.measure.measureText(current + character).width > width) { lines.push(current); current = ""; }
          current += character;
        }
      }
    }
    if (current) lines.push(current);
    return `<text class="rv-world-text" font-size="${size}" aria-label="${this.escape(value)}" ${attributes}>${lines.map((part, row) => `<tspan x="${x}" y="${y + row * (size + 3)}">${this.escape(part)}</tspan>`).join("")}</text>`;
  }


  room(parent, box, id, contents) {
    const scale = Math.min(box.w / this.width, box.h / this.height);
    const x = box.x + (box.w - this.width * scale) / 2;
    const y = box.y + (box.h - this.height * scale) / 2;
    const room = { id, x: parent.x + x * parent.scale, y: parent.y + y * parent.scale, scale: parent.scale * scale };
    this.rooms.set(id, room);
    return `<g data-room="${id}" transform="translate(${x} ${y}) scale(${scale})">${contents(room)}</g>`;
  }


  sectionSlots() {
    return Array.from({ length: 3 }, (_, item) => this.compact
      ? { x: 12 + item * 136, y: 24, w: 124, h: 242 }
      : { x: 45 + item * 310, y: 40, w: 270, h: 360 });
  }


  chunks(section, sectionBox) {
    const inset = this.compact ? 8 : 14;
    const top = this.compact ? 44 : 60;
    const gap = this.compact ? 10 : 16;
    const height = (sectionBox.h - top - inset - gap) / 2;
    return section.chunks.map((quote, item) => {
      const box = { x: sectionBox.x + inset, y: sectionBox.y + top + item * (height + gap), w: sectionBox.w - inset * 2, h: height };
      return `<g class="rv-world-chunk" data-chunk-section="${section.id}" data-chunk-index="${item}" data-quote="${this.escape(quote)}" data-matched="false">${this.rect(box, 'data-chunk-border')}${this.text(box.x + inset, box.y + (this.compact ? 22 : 34), quote, box.w - inset * 2, this.compact ? 12 : 22)}</g>`;
    }).join("");
  }


  sections() {
    return this.sectionSlots().map((box, item) => {
      const section = this.example.sections[item];
      return `<g data-section="${section.id}" data-selected="false"><title>Section ${section.id}</title>${this.rect(box, 'data-section-border')}
        ${this.text(box.x + 12, box.y + 28, this.label(section), box.w - 24, this.compact ? 16 : 22)}
        <g data-section-metadata>${this.text(box.x + 12, box.y + 62, section.summary, box.w - 24, this.compact ? 14 : 18)}${this.text(box.x + 12, box.y + box.h - 16, section.keywords.split(" · ")[0], box.w - 24, this.compact ? 13 : 18)}</g>
        <g data-section-chunks>${this.chunks(section, box)}</g></g>`;
    }).join("");
  }


  files(parent) {
    const boxes = this.compact
      ? [{ x: 24, y: 66, w: 172, h: 156 }, { x: 224, y: 66, w: 172, h: 156 }]
      : [{ x: 145, y: 94, w: 300, h: 260 }, { x: 515, y: 94, w: 300, h: 260 }];
    return boxes.map((box, item) => {
      const file = this.example.knowledges[0].files[item];
      return `<g data-film-paper="${file}">${this.rect(box)}${this.text(box.x + 12, box.y + 28, file, box.w - 24, this.compact ? 16 : 22)}
        <g${item === 0 ? ' data-metadata-for="sections"' : ""}>${this.text(box.x + 12, box.y + (this.compact ? 82 : 100), item === 0 ? "Spacecraft fault responses" : "Ollama and vector search", box.w - 24, this.compact ? 14 : 18)}</g>
        ${item === 0 ? this.room(parent, { x: box.x + 10, y: box.y + (this.compact ? 55 : 70), w: box.w - 20, h: box.h - (this.compact ? 65 : 85) }, "sections", () => this.sections()) : ""}</g>`;
    }).join("");
  }


  comparison() {
    const boxes = this.compact ? [{ x: 14, y: 42, w: 188, h: 190 }, { x: 218, y: 42, w: 188, h: 190 }] : [{ x: 80, y: 55, w: 360, h: 280 }, { x: 520, y: 55, w: 360, h: 280 }];
    return boxes.map((box, item) => this.rect(box) + this.text(box.x + 16, box.y + 32, item ? "Hierarchical" : "Embedded", box.w - 32, this.compact ? 20 : 26) +
      this.example.embedded.map((id, row) => this.text(box.x + 20, box.y + 95 + row * 55, this.label({ id }), box.w - 40, this.compact ? 18 : 26)).join("")).join("") +
      this.text(30, this.height - 20, "Same sections → one shared list", this.width - 60, this.compact ? 20 : 26);
  }


  sourceRecord(section, box, order) {
    const fields = [["Knowledge", section.knowledge], ["File", section.file], ["Section", this.label(section)]];
    return `<g class="rv-world-packet" data-source-record="${section.id}" data-record-knowledge="${section.knowledge}" data-order="${order}" opacity="1">${this.rect(box)}${this.text(box.x + 14, box.y + 28, "Source record", box.w - 28, 22)}${fields.map(([name, value], item) => {
      const y = box.y + 55 + item * 48;
      return this.text(box.x + 14, y, name, box.w - 28, 13) + this.text(box.x + 14, y + 26, value, box.w - 28, 24, `data-source-value data-record-order="${order}" data-field-order="${item}"`);
    }).join("")}</g>`;
  }


  pin(x, y, attributes) {
    return `<circle cx="${x}" cy="${y}" r="6" class="rv-graph-pin" ${attributes}/>`;
  }


  buildGraph() {
    const global = this.scope === "global";
    this.graphProgress = 0;
    this.graphBounds = { x: 0, y: global ? -70 : 0, w: 1100, h: global ? 680 : 560 };
    const anchors = global ? [[25, 145, 180], [25, 385, 180], [20, 20, 78], [125, 35, 78]] : [[25, 175, 160]];
    this.knowledgeTargets = anchors.map(([x, y, width], item) => ({ x, y, scale: width / this.knowledgeBoxes[item].w }));
    const selected = this.example.sections.filter((section) => this.example.embedded.includes(section.id));
    if (global) selected.push(this.example.power);
    const boxes = global ? [[260, 40], [570, 80], [310, 360]] : [[260, 35], [570, 300]];
    const llm = { x: 900, y: global ? 385 : 170, w: 150, h: 120 };
    this.graphRecords = selected.map((section, item) => {
      const box = { x: boxes[item][0], y: boxes[item][1], w: 250, h: 210 };
      const input = { x: box.x, y: box.y + box.h / 2 };
      const output = global && item === 0 ? { x: box.x + box.w / 2, y: box.y + box.h } : { x: box.x + box.w, y: box.y + box.h / 2 };
      const sink = { x: llm.x, y: llm.y + 30 + item * 30 };
      const controls = global && item === 0 ? [output.x, 340, 800, sink.y] : item === 1 ? [1070, output.y, sink.x - 120, sink.y] : [output.x + 120, output.y, sink.x - 120, sink.y];
      return { section, box, input, output, sink, controls, owner: this.example.knowledges.findIndex((knowledge) => knowledge.name === section.knowledge), dx: 0, dy: 0 };
    });
    const ownership = this.graphRecords.map(({ section }) => `<path class="rv-graph-owner" data-owner-edge="${section.id}" data-knowledge="${section.knowledge}"/>`).join("");
    const edges = this.graphRecords.map(({ section, output, sink, controls }, item) => `<path class="rv-graph-wire" data-llm-edge="${section.id}" data-order="${item}" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1" d="M${output.x} ${output.y}C${controls.join(" ")} ${sink.x} ${sink.y}"/>`).join("");
    const records = this.graphRecords.map(({ section, box, input, output }, item) => this.sourceRecord(section, box, item).replace("</g>", `${this.pin(input.x, input.y, `data-source-input="${section.id}"`)}${this.pin(output.x, output.y, `data-source-output="${section.id}"`)}</g>`)).join("");
    const inputs = this.graphRecords.map(({ section, sink }) => this.pin(sink.x, sink.y, `data-llm-input="${section.id}"`)).join("");
    return `<g data-source-graph opacity="0">${ownership}<g data-wire-layer opacity="0">${edges}</g>${records}<g data-llm-node opacity="0">${this.rect(llm)}${this.text(llm.x + 25, llm.y + 55, "LLM", 100, 34)}${this.text(llm.x + 25, llm.y + 82, "model", 100, 18)}${inputs}</g></g>`;
  }


  build() {
    const root = { x: 0, y: 0, scale: 1 };
    const knowledgeBoxes = this.scope === "local" ? [this.compact ? { x: 116, y: 93, w: 188, h: 104 } : { x: 375, y: 105, w: 210, h: 230 }] : this.example.knowledges.map((_, item) => this.compact
      ? { x: 14 + item % 2 * 204, y: 48 + Math.floor(item / 2) * 140, w: 188, h: 104 }
      : { x: 28 + item % 2 * 560, y: 30 + Math.floor(item / 2) * 300, w: 300, h: 240 });
    this.knowledgeBoxes = knowledgeBoxes;
    this.primaryBox = knowledgeBoxes[0];
    this.overviewBounds = { x: 0, y: 0, w: this.width, h: this.scope === "global" ? (this.compact ? 310 : 590) : this.height };
    const sources = knowledgeBoxes.map((box, item) => {
      const knowledge = this.example.knowledges[item];
      return `<g data-film-knowledge="${knowledge.name}">${this.folder(box)}${this.text(box.x + 12, box.y + 44, knowledge.name, box.w - 24, this.compact ? 20 : 30)}
        <g${item === 0 ? ' data-metadata-for="files"' : ""} data-knowledge-preview>${this.text(box.x + 12, box.y + 70, knowledge.files[0], box.w - 24, this.compact ? 14 : 18)}</g>
        <g data-source-selection opacity="0">${this.rect({ x: box.x + box.w - 42, y: box.y + box.h - 33, w: 24, h: 20 })}${this.text(box.x + box.w - 38, box.y + box.h - 17, "✓", 20, 16)}</g>
        ${this.pin(box.x + box.w, box.y + box.h / 2, 'data-knowledge-pin opacity="0"')}
        ${item === 0 ? this.room(root, { x: box.x + 10, y: box.y + 50, w: box.w - 20, h: box.h - 62 }, "files", (room) => this.files(room)) : ""}</g>`;
    }).join("");
    const graph = this.buildGraph();
    const compare = this.room(root, { x: this.width * 1.45, y: this.height * 1.2, w: this.width * .7, h: this.height * .7 }, "comparison", () => this.comparison());
    const clipId = `rv-camera-window-${++RavenRetrievalCamera.clipCounter}`;
    this.viewport.innerHTML = `<div class="rv-film-layer"><svg viewBox="0 0 ${this.width} ${this.height}" preserveAspectRatio="xMinYMid meet" role="img"><title></title><defs><clipPath id="${clipId}"><rect width="${this.width}" height="${this.height}"/></clipPath></defs><g clip-path="url(#${clipId})"><g class="rv-camera-world">${sources}${compare}${graph}</g></g></svg></div>`;
    this.svg = this.viewport.querySelector("svg");
    this.world = this.viewport.querySelector(".rv-camera-world");
    this.sourceValues = [...this.world.querySelectorAll("[data-source-value]")].map((node) => ({ node, lines: [...node.querySelectorAll("tspan")].map((span) => ({ span, text: span.textContent })) }));
    for (const room of this.rooms.values()) room.node = this.world.querySelector(`[data-room="${room.id}"]`);
    this.camera = this.fit(this.scope === "global" ? { x: 0, y: 0, w: this.width, h: this.height } : this.primaryBox);
    this.paint();
  }


  fit(box) {
    return { x: box.x + box.w / 2, y: box.y + box.h / 2, w: Math.max(box.w, box.h * this.width / this.height) * 1.04 };
  }


  target(view) {
    if (["source-knowledge", "delivery"].includes(view)) return this.fit(this.graphBounds);
    if (view === "knowledge") return this.fit(this.scope === "global" ? this.overviewBounds : { x: this.primaryBox.x + this.primaryBox.w / 2 - this.width / 2, y: this.primaryBox.y + this.primaryBox.h / 2 - this.height / 2, w: this.width, h: this.height });
    if (view === "shortlist-knowledges") return this.fit(this.primaryBox);
    const id = ["files", "source-file", "shortlist-files"].includes(view) ? "files" : view === "comparison" ? "comparison" : "sections";
    const room = this.rooms.get(id);
    return this.fit({ x: room.x, y: room.y, w: this.width * room.scale, h: this.height * room.scale });
  }


  show(view, mode, animate, title) {
    this.resetPulse();
    this.view = view;
    this.showMetadata = mode === "hierarchical" || ["metadata", "scoring", "reranked"].includes(view);
    this.viewport.dataset.filmScene = view;
    this.motion = null;
    this.pulse = null;
    this.reveal = null;
    this.delivery = null;
    this.pending = null;
    this.svg.setAttribute("aria-label", title);
    this.svg.querySelector("title").textContent = title;
    const evidence = ["matches", "parents", "source-file", "source-knowledge", "scoring", "reranked", "comparison", "delivery"].includes(view);
    const selectSections = ["parents", "source-file", "source-knowledge", "scoring", "reranked", "comparison", "delivery"].includes(view);
    const selection = mode === "vector-conditioned" && ["matches", "shortlist-files", "shortlist-knowledges"].includes(view) ? this.example.vectorMatches : this.example.embedded;
    this.world.querySelectorAll("[data-chunk-section]").forEach((node) => {
      const match = this.example.chunkMatches.find((item) => item.section === node.dataset.chunkSection);
      node.dataset.matched = String(evidence && !this.showMetadata && selection.includes(node.dataset.chunkSection) && Boolean(match?.chunks.includes(Number(node.dataset.chunkIndex))));
    });
    this.world.querySelectorAll("[data-section]").forEach((node) => node.dataset.selected = String(selectSections && selection.includes(node.dataset.section)));
    this.world.querySelectorAll("[data-film-paper]").forEach((node) => node.dataset.selectedFile = String(["source-file", "source-knowledge", "delivery"].includes(view) && node.dataset.filmPaper === "Reaction_wheel.txt"));
    this.world.querySelectorAll("[data-film-knowledge]").forEach((node) => {
      const knowledge = this.example.knowledges.find((item) => item.name === node.dataset.filmKnowledge);
      const selected = ["source-knowledge", "delivery"].includes(view) && knowledge.relevant;
      node.classList.toggle("film-dim", ["source-knowledge", "delivery"].includes(view) && !knowledge.relevant);
      node.querySelector("[data-source-selection]").setAttribute("opacity", selected ? "1" : "0");
      if (selected) node.dataset.selectedSource = knowledge.name;
      else delete node.dataset.selectedSource;
    });
    this.world.querySelector("[data-llm-node]").setAttribute("opacity", view === "delivery" ? "1" : "0");
    this.world.querySelector("[data-wire-layer]").setAttribute("opacity", view === "delivery" ? "1" : "0");
    this.paintNodes(view === "delivery" ? 1200 : 0);
    this.paintWires(0);
    const target = this.target(view);
    const graphTarget = ["source-knowledge", "delivery"].includes(view) ? 1 : 0;
    const distance = Math.abs(Math.log(target.w / this.camera.w));
    const changed = distance > .001 || Math.hypot(target.x - this.camera.x, target.y - this.camera.y) > .001 || Math.abs(graphTarget - this.graphProgress) > .001;
    if (animate && changed) this.motion = { from: { ...this.camera }, to: target, graphFrom: this.graphProgress, graphTo: graphTarget, elapsed: 0, duration: (1400 + Math.min(600, distance * 280)) * (target.w > this.camera.w ? .7 : 1) };
    else { this.camera = target; this.graphProgress = graphTarget; }
    if (view === "matches") this.pending = { type: "pulse" };
    if (view === "source-knowledge") this.pending = { type: "reveal" };
    if (view === "delivery") this.pending = { type: "delivery" };
    if (!animate) {
      this.pending = null;
      this.paintNodes(1200);
      this.paintWires(view === "delivery" ? 1600 : 0);
      this.viewport.dataset.matchPulse = view === "matches" ? "settled" : "none";
    } else if (!this.motion) this.startEffect();
    this.paint();
  }


  startEffect() {
    if (this.pending?.type === "pulse") { this.pulse = { elapsed: 0 }; this.viewport.dataset.matchPulse = "active"; }
    if (this.pending?.type === "reveal") this.reveal = { elapsed: 0 };
    if (this.pending?.type === "delivery") this.delivery = { elapsed: 0 };
    this.pending = null;
  }


  paint() {
    const scale = this.width / this.camera.w;
    this.world.setAttribute("transform", `matrix(${scale} 0 0 ${scale} ${this.width / 2 - this.camera.x * scale} ${this.height / 2 - this.camera.y * scale})`);
    const reveal = (value) => { const t = Math.max(0, Math.min(1, (value - .24) / .42)); return t * t * (3 - 2 * t); };
    for (const room of this.rooms.values()) room.node.setAttribute("opacity", String(reveal(room.scale * scale) * (1 - this.graphProgress)));
    this.world.querySelectorAll("[data-metadata-for]").forEach((node) => node.setAttribute("opacity", String(1 - reveal(this.rooms.get(node.dataset.metadataFor).scale * scale))));
    this.world.querySelectorAll("[data-section-metadata]").forEach((node) => node.setAttribute("opacity", this.showMetadata ? "1" : "0"));
    this.world.querySelectorAll("[data-section-chunks]").forEach((node) => node.setAttribute("opacity", this.showMetadata ? "0" : "1"));
    this.world.querySelectorAll("[data-knowledge-preview]").forEach((node) => {
      if (this.graphProgress) node.setAttribute("opacity", String(1 - this.graphProgress));
      else if (!node.dataset.metadataFor) node.setAttribute("opacity", "1");
    });
    this.world.querySelector("[data-source-graph]").setAttribute("opacity", String(this.graphProgress));
    this.world.querySelectorAll("[data-film-knowledge]").forEach((node, item) => {
      const box = this.knowledgeBoxes[item], target = this.knowledgeTargets[item], t = this.graphProgress;
      const x = box.x + (target.x - box.x) * t, y = box.y + (target.y - box.y) * t, size = 1 + (target.scale - 1) * t;
      node.setAttribute("transform", `translate(${x} ${y}) scale(${size}) translate(${-box.x} ${-box.y})`);
      node.querySelector("[data-knowledge-pin]").setAttribute("opacity", this.example.knowledges[item].relevant ? String(t) : "0");
    });
    for (const record of this.graphRecords) {
      const box = this.knowledgeBoxes[record.owner], target = this.knowledgeTargets[record.owner], t = this.graphProgress;
      const size = 1 + (target.scale - 1) * t;
      const x = box.x + (target.x - box.x) * t + box.w * size, y = box.y + (target.y - box.y) * t + box.h * size / 2;
      const endX = record.input.x + record.dx, endY = record.input.y + record.dy;
      const topRoute = this.scope === "global" && record.section.id === "C";
      const lowRoute = this.scope === "local" && record.section.id === "C";
      const controls = topRoute ? [x + 50, -140, endX - 70, -140] : lowRoute ? [x + 70, 455, endX - 80, 475] : [x + 70, y, endX - 70, endY];
      this.world.querySelector(`[data-owner-edge="${record.section.id}"]`).setAttribute("d", `M${x} ${y}C${controls.join(" ")} ${endX} ${endY}`);
    }
    this.viewport.dataset.cameraState = this.motion ? "moving" : "settled";
  }


  paintPulse(amount) {
    const rgb = getComputedStyle(document.documentElement).getPropertyValue("--site-accent-rgb").trim();
    this.question.style.boxShadow = amount ? `inset 0 0 0 ${amount * 2}px rgba(${rgb}, ${amount * .7}), 0 0 ${amount * 9}px rgba(${rgb}, ${amount * .25})` : "";
    this.question.querySelector("[data-film-query]").style.textShadow = amount ? `0 0 ${amount * 8}px rgba(${rgb}, ${amount * .6})` : "";
    this.world.querySelectorAll('[data-chunk-section][data-matched="true"] [data-chunk-border]').forEach((node) => {
      node.style.strokeWidth = String(2.2 + amount * 2);
      node.style.filter = `drop-shadow(0 0 ${2 + amount * 5}px rgba(${rgb}, ${.22 + amount * .4}))`;
    });
  }


  resetPulse() {
    this.question.style.boxShadow = "";
    this.question.querySelector("[data-film-query]").style.textShadow = "";
    this.world.querySelectorAll("[data-chunk-border]").forEach((node) => { node.style.strokeWidth = ""; node.style.filter = ""; });
    this.viewport.dataset.matchPulse = "none";
  }


  paintNodes(elapsed) {
    const t = Math.min(1, elapsed / 1200), eased = t * t * (3 - 2 * t);
    for (const [item, record] of this.graphRecords.entries()) {
      record.dx = (item % 2 ? -14 : 14) * (1 - eased);
      record.dy = (item % 2 ? 10 : -10) * (1 - eased);
      this.world.querySelector(`[data-source-record="${record.section.id}"]`).setAttribute("transform", `translate(${record.dx} ${record.dy})`);
    }
    for (const { node, lines } of this.sourceValues) {
      const local = elapsed - Number(node.dataset.fieldOrder) * 400;
      const length = lines.reduce((total, line) => total + line.text.length, 0);
      let remaining = Math.floor(Math.max(0, Math.min(1, local / 400)) * length);
      for (const { span, text } of lines) { span.textContent = text.slice(0, Math.max(0, remaining)); remaining -= text.length; }
    }
    if (this.view === "source-knowledge") this.viewport.dataset.recordPhase = t === 1 ? "ready" : "writing";
  }


  paintWires(elapsed) {
    this.world.querySelectorAll("[data-llm-edge]").forEach((node) => {
      const t = Math.max(0, Math.min(1, (elapsed - Number(node.dataset.order) * 160) / 1000));
      node.setAttribute("stroke-dashoffset", String(1 - t * t * (3 - 2 * t)));
      const id = node.dataset.llmEdge;
      node.dataset.connected = String(t === 1);
      this.world.querySelector(`[data-source-output="${id}"]`).dataset.connected = String(t === 1);
      this.world.querySelector(`[data-llm-input="${id}"]`).dataset.connected = String(t === 1);
    });
    if (this.view === "delivery") this.viewport.dataset.recordPhase = elapsed >= 1600 ? "complete" : "connecting";
  }


  advance(delta) {
    if (this.motion) {
      const motion = this.motion;
      motion.elapsed += delta;
      const t = Math.min(1, motion.elapsed / motion.duration);
      const eased = (1 - Math.cos(Math.PI * t)) / 2;
      this.camera = { x: motion.from.x + (motion.to.x - motion.from.x) * eased, y: motion.from.y + (motion.to.y - motion.from.y) * eased, w: Math.exp(Math.log(motion.from.w) + Math.log(motion.to.w / motion.from.w) * eased) };
      this.graphProgress = motion.graphFrom + (motion.graphTo - motion.graphFrom) * eased;
      if (t === 1) { this.camera = motion.to; this.motion = null; this.startEffect(); }
      this.paint();
    }
    if (this.pulse) {
      this.pulse.elapsed += delta;
      const t = Math.min(1, this.pulse.elapsed / 1100);
      this.paintPulse(Math.sin(Math.PI * t) ** 2);
      if (t === 1) { this.resetPulse(); this.pulse = null; this.viewport.dataset.matchPulse = "settled"; }
    }
    if (this.delivery) {
      this.delivery.elapsed += delta;
      this.paintWires(this.delivery.elapsed);
      if (this.delivery.elapsed >= 1600) this.delivery = null;
    }
    if (this.reveal) {
      this.reveal.elapsed += delta;
      this.paintNodes(this.reveal.elapsed);
      this.paint();
      if (this.reveal.elapsed >= 1200) this.reveal = null;
    }
  }


  get active() {
    return Boolean(this.motion || this.pulse || this.reveal || this.delivery);
  }


  get remainingDuration() {
    return (this.motion ? this.motion.duration - this.motion.elapsed : 0) +
      (this.pending?.type === "pulse" ? 1100 : this.pending?.type === "reveal" ? 1200 : this.pending?.type === "delivery" ? 1600 : 0) +
      (this.pulse ? 1100 - this.pulse.elapsed : 0) +
      (this.reveal ? Math.max(0, 1200 - this.reveal.elapsed) : 0) +
      (this.delivery ? Math.max(0, 1600 - this.delivery.elapsed) : 0);
  }
}
