"use strict";

// Real sample text; presentation-only boundaries and matches. No model calls.
document.querySelectorAll("[data-rv-retrieval]").forEach((explorer) => {
  const example = RAVEN_RETRIEVAL_EXAMPLE;
  const player = explorer.querySelector("[data-retrieval-player]");
  const screen = player.querySelector(".rv-film-screen");
  const viewport = player.querySelector("[data-film-viewport]");
  const playButton = player.querySelector("[data-film-play]");
  const previous = player.querySelector("[data-film-previous]");
  const next = player.querySelector("[data-film-next]");
  const seek = player.querySelector("[data-film-seek]");
  const modes = [...explorer.querySelectorAll("[data-retrieval-mode]")];
  const scopes = [...explorer.querySelectorAll("[data-retrieval-scope]")];
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const narrow = window.matchMedia("(max-width: 1000px)");
  const holdDuration = 1000;
  const titles = {
    embedded: "Embedded retrieval",
    "vector-conditioned": "Vector-conditioned retrieval",
    hierarchical: "Hierarchical retrieval",
    agreement: "Agreement-based retrieval",
  };
  let mode = "embedded";
  let scope = "local";
  let scenes = [];
  let index = 0;
  let holdElapsed = 0;
  let stepDuration = holdDuration;
  let playing = false;
  let completed = false;
  let frame;
  let lastTick;
  let camera;
  let holdVisual = false;

  const escape = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;" })[character]);

  function storyboard() {
    const global = scope === "global";
    const scene = (view, phase, title, caption, direction = "in") => ({ view, phase, title, caption, direction });
    const overview = scene("knowledge", "Search boundary", global ? "Start with the knowledge base" : "Start with Engineering", global ? "Engineering, Spacecraft, Narrative, and Medical contain different documents. Follow Engineering first; return to the others at the end." : "This local example searches Engineering, which contains Reaction_wheel.txt and pipeline_sample.txt.");
    const sections = scene("sections", "Start at the searchable text", "Begin with sections and chunks", "The search starts at chunk vectors, not by opening folders one at a time. Sections A, B, and C show the chunks and their parents together; follow the matching sources outward from here.");
    const matches = scene("matches", "Vector similarity", "Match the question to source text", `${global ? "Global retrieval goes directly to chunk vectors; this close-up shows Engineering only. " : "Search Engineering's chunk vectors directly. "}${mode === "vector-conditioned" ? "One chunk in Section B matches in this initial shortlist. Other chunks stay unhighlighted; ranking will add Section C later." : "One chunk in Section B and two in Section C pulse with the question. Not every chunk in a matching section is a vector hit."}`);
    const parents = scene("parents", "Resolve the source", "Highlight the parent sections", "Now highlight Sections B and C, which contain the matching chunks. Return each whole section once, including its surrounding content. Section A stays unselected.", "out");
    const sourceFile = scene("source-file", "Trace the source · file", "Find the file these sections belong to", "Zoom out to Reaction_wheel.txt and highlight it. The selected sections belong to this file; pipeline_sample.txt is nearby, but contributes nothing to the result.", "out");
    const sourceKnowledge = scene("source-knowledge", "Trace the source · knowledge", global ? "Each knowledge keeps its selected sections" : "Engineering supplies two source nodes", global ? "Engineering has two source cards, B and C. Spacecraft has one, D. All cards appear and fill in together beside their own knowledge; Narrative and Medical contribute none." : "Zoom out to Engineering. Two source cards appear together, one for Section B and one for Section C. Their fields fill in while the nodes settle into position; Section A has no card.", "out");
    const delivery = scene("delivery", "Connect the sources · model inputs", "Plug the source nodes into the LLM", global ? "The B, C, and D cards stay beside their knowledges. Curved edges attach their output pins to separate LLM input pins. The LLM is a model node, not a container for section cards." : "Both source cards stay on the canvas. Their output pins connect to the LLM's input pins with curved edges. The cards preserve the source details while the model receives their contents.", "out");
    const rankKnowledges = scene("shortlist-knowledges", "LLM relevance scoring", "Select Engineering", "The LLM ranks knowledge summaries against the question. Follow Engineering's attitude-control material here; Spacecraft's power-system material reappears at the end. full_retrieval skips this shortlist.");
    const metadata = scene("metadata", "Metadata, not chunk matching", "Read section metadata", "Section summaries, keywords, and other metadata give the LLM ranking context. This path does not need vector search.");
    const scoring = scene("scoring", "LLM relevance scoring", "Score sections against the question", "The LLM scores section metadata for relevance, in batches when needed. Higher-ranked sections are selected; these labels are illustrative, not actual scores.");
    if (mode === "embedded") return [sections, matches, parents, sourceFile, sourceKnowledge, delivery];
    if (mode === "hierarchical") return [...(global ? [overview, rankKnowledges] : []), metadata, scoring, { ...parents, caption: "Load the raw content of the sections selected by LLM metadata ranking. There is no chunk-to-section resolution step on this path." }, sourceFile, sourceKnowledge, delivery];
    if (mode === "vector-conditioned") return [
      sections, matches,
      scene(global ? "shortlist-knowledges" : "shortlist-files", "Narrow the search boundary", global ? "Keep Engineering in the shortlist" : "Keep Reaction_wheel.txt", global ? "Engineering appears in the vector results, so its sections are eligible for ranking. The Spacecraft shortlist is omitted from this close-up." : "The vector hit belongs to Reaction_wheel.txt. Score its other sections too, without scanning unrelated pipeline_sample.txt.", "out"),
      { ...scoring, caption: global ? "The LLM ranks Engineering's section metadata, including the battery requirements in Section C. Spacecraft's EPS sections also enter the global ranking; they reappear at the end." : "Ranking the shortlisted file's metadata selects Sections B and C, even though the initial vector example only matched a chunk in Section B." },
      scene("reranked", "Final section selection", "The final sections may differ", "Vector hits chose the search boundary, not the final answer context. LLM ranking may select sections that were not among the initial vector hits.", "out"),
      sourceFile, sourceKnowledge, delivery,
    ];
    return [
      { ...sections, phase: "First pass / Embedded" }, { ...matches, phase: "First pass / Embedded" }, { ...parents, phase: "First pass / Embedded" },
      ...(global ? [{ ...rankKnowledges, phase: "Second pass / Hierarchical" }] : []),
      { ...metadata, phase: "Second pass / Hierarchical" }, { ...scoring, phase: "Second pass / Hierarchical" },
      scene("comparison", "Compare the two outputs", "Both paths find the same sections", "Both passes select Sections B and C in this Engineering example: Strong Agreement returns one shared list. Different lists would be retained separately. Agreement is not a factual-correctness guarantee.", "out"),
      sourceFile, sourceKnowledge,
      { ...delivery, caption: global ? "One shared result connects to the LLM: B and C from Engineering, and D from Spacecraft. Each source node has its own curved edge into a model input pin; no duplicate cards are sent." : "Both passes selected Sections B and C. Their two source nodes remain visible and connect to the LLM once as a shared result. Section A has no card or connection." },
    ];
  }


  function updateControls() {
    player.dataset.playing = String(playing);
    playButton.setAttribute("aria-label", playing ? "Pause walkthrough" : "Play walkthrough");
    playButton.title = playing ? "Pause walkthrough" : "Play walkthrough";
    playButton.querySelector("[data-film-play-icon]").setAttribute("d", playing ? "M6 4h4v16H6Zm8 0h4v16h-4Z" : "M7 4v16l14-8Z");
    playButton.disabled = reduced.matches;
    previous.disabled = index === 0;
    next.disabled = index === scenes.length - 1;
    seek.max = String(scenes.length - 1);
    seek.value = String(index);
    seek.setAttribute("aria-valuetext", `${index + 1} of ${scenes.length}: ${scenes[index].title}`);
    player.querySelector("[data-film-position]").textContent = `${index + 1} / ${scenes.length}`;
    player.querySelector("[data-film-progress]").max = stepDuration;
    player.querySelector("[data-film-motion-note]").hidden = !reduced.matches;
  }

  function showExcerpts(view) {
    const panel = player.querySelector("[data-film-source-text]");
    const visible = ["sections", "matches", "metadata", "scoring", "parents", "source-file", "source-knowledge", "reranked", "comparison", "delivery"].includes(view);
    panel.hidden = !visible;
    if (!visible) return;
    const open = panel.querySelector("details")?.open || false;
    const selection = view === "matches" && mode === "vector-conditioned" ? example.vectorMatches : example.embedded;
    const selectSections = ["parents", "source-file", "source-knowledge", "scoring", "reranked", "comparison", "delivery"].includes(view);
    const sections = ["parents", "source-file", "source-knowledge", "reranked", "comparison", "delivery"].includes(view)
      ? example.sections.filter((section) => selection.includes(section.id)) : example.sections;
    const sources = sections.map((section) => `<div class="rv-film-excerpt" data-selected="${selectSections && selection.includes(section.id)}"><strong>Section ${escape(section.id)}</strong><small>${escape(section.knowledge)} / ${escape(section.file)}</small>${section.chunks.map((chunk) => `<blockquote>${escape(chunk)}</blockquote>`).join("")}</div>`);
    if (scope === "global" && view === "delivery") sources.push(`<div class="rv-film-excerpt" data-selected="true"><strong>Section ${example.power.id}</strong><small>${example.power.knowledge} / ${example.power.file}</small><blockquote>${escape(example.power.excerpt)}</blockquote></div>`);
    panel.innerHTML = `<details${open ? " open" : ""}><summary>Read the exact source excerpts</summary><div class="rv-film-excerpts">${sources.join("")}</div></details>`;
  }

  function goTo(target, transition = true) {
    index = Math.max(0, Math.min(scenes.length - 1, target));
    holdElapsed = 0;
    player.querySelector("[data-film-progress]").value = 0;
    completed = false;
    player.querySelector("[data-film-phase]").textContent = scenes[index].phase;
    player.querySelector("[data-film-title]").textContent = scenes[index].title;
    player.querySelector("[data-film-caption]").textContent = scenes[index].caption;
    showExcerpts(scenes[index].view);
    explorer.querySelector("[data-retrieval-status]").textContent = `${titles[mode]}, ${scope} scope. Scene ${index + 1} of ${scenes.length}: ${scenes[index].title}.`;
    holdVisual = false;
    camera.show(scenes[index].view, mode, transition && !reduced.matches, scenes[index].title);
    stepDuration = camera.remainingDuration + holdDuration;
    if (playing || camera.active) ensureFrame();
    updateControls();
  }

  function pause() {
    playing = false;
    holdVisual = true;
    cancelAnimationFrame(frame);
    frame = undefined;
    lastTick = undefined;
    updateControls();
  }

  function tick(now) {
    frame = undefined;
    const delta = lastTick === undefined ? 0 : Math.min(now - lastTick, 100);
    lastTick = now;
    const wasActive = camera.active;
    if (!holdVisual) camera.advance(delta);
    if (playing && !wasActive && !camera.active) holdElapsed += delta;
    player.querySelector("[data-film-progress]").value = Math.min(stepDuration, stepDuration - camera.remainingDuration - Math.max(0, holdDuration - holdElapsed));
    if (playing && !camera.active && holdElapsed >= holdDuration) {
      if (index === scenes.length - 1) {
        completed = true;
        pause();
        return;
      }
      goTo(index + 1);
    }
    if (playing || (!holdVisual && camera.active)) ensureFrame();
  }

  function ensureFrame() {
    if (frame === undefined) frame = requestAnimationFrame(tick);
  }

  function play() {
    if (reduced.matches) return;
    if (completed) goTo(0, false);
    playing = true;
    holdVisual = false;
    lastTick = undefined;
    updateControls();
    ensureFrame();
  }

  function configure() {
    cancelAnimationFrame(frame);
    frame = undefined;
    playing = false;
    lastTick = undefined;
    camera?.resetPulse();
    camera = new RavenRetrievalCamera(viewport, player.querySelector(".rv-film-query"), example, scope, narrow.matches);
    scenes = storyboard();
    player.querySelector("[data-film-query]").textContent = example.questions[scope];
    modes.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.retrievalMode === mode)));
    scopes.forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.retrievalScope === scope)));
    explorer.querySelector("#retrieval-diagram").setAttribute("aria-label", `${titles[mode]}, ${scope} scope walkthrough`);
    goTo(0, false);
  }

  modes.forEach((button) => {
    button.disabled = false;
    button.addEventListener("click", () => { mode = button.dataset.retrievalMode; configure(); });
  });
  scopes.forEach((button) => {
    button.disabled = false;
    button.addEventListener("click", () => { scope = button.dataset.retrievalScope; configure(); });
  });
  // Leaving the video dismisses controls without moving keyboard focus.
  screen.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") screen.dataset.controlsDismissed = "true";
  });
  screen.addEventListener("pointerenter", () => { delete screen.dataset.controlsDismissed; });
  screen.addEventListener("focusin", (event) => {
    if (event.target.matches(":focus-visible")) delete screen.dataset.controlsDismissed;
  });
  screen.addEventListener("keydown", () => { delete screen.dataset.controlsDismissed; });
  playButton.addEventListener("click", () => playing ? pause() : play());
  previous.addEventListener("click", () => { pause(); goTo(index - 1); });
  next.addEventListener("click", () => { pause(); goTo(index + 1); });
  seek.addEventListener("input", () => { const target = Number(seek.value); pause(); goTo(target); });
  player.querySelector("[data-film-replay]").addEventListener("click", () => { pause(); goTo(0, false); play(); });
  reduced.addEventListener("change", () => { pause(); goTo(index, false); });
  narrow.addEventListener("change", () => { pause(); camera.resetPulse(); camera = new RavenRetrievalCamera(viewport, player.querySelector(".rv-film-query"), example, scope, narrow.matches); goTo(index, false); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
  new IntersectionObserver((entries) => { if (!entries[0].isIntersecting && playing) pause(); }).observe(player);
  explorer.querySelector("[data-retrieval-fallback]").hidden = true;
  player.hidden = false;
  configure();
});
