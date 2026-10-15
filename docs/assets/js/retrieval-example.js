"use strict";

// Public presentation fixture. Quotes are verbatim fragments of test_docs files;
// knowledge assignments, section labels, boundaries, metadata, and matches are illustrative.
const RAVEN_RETRIEVAL_EXAMPLE = {
  questions: {
    local: "What triggers spacecraft safe-hold, and what battery charge is required?",
    global: "What triggers spacecraft safe-hold, and how does EPS respond to low bus voltage?",
  },
  knowledges: [
    { name: "Engineering", files: ["Reaction_wheel.txt", "pipeline_sample.txt"], summary: "Attitude control, spacecraft faults, and local-model tooling.", relevant: true },
    { name: "Spacecraft", files: ["EPS.txt"], summary: "Battery protection, bus voltage, and load shedding.", relevant: true },
    { name: "Narrative", files: ["short_story.txt"], summary: "Elias discovers a mysterious clock and a silver street.", relevant: false },
    { name: "Medical", files: ["Clinical.txt"], summary: "DKA classification and potassium safeguards.", relevant: false },
  ],
  sections: [
    {
      id: "A", file: "Reaction_wheel.txt", knowledge: "Engineering",
      summary: "Calibration before thruster ignition.", keywords: "540 s · star tracker",
      chunks: ["five hundred and forty seconds", "eighty-eight percent baseline efficiency threshold"],
    },
    {
      id: "B", file: "Reaction_wheel.txt", knowledge: "Engineering",
      summary: "Anomaly triggers safe-hold.", keywords: "0.35 N·m·s · 95 ms",
      chunks: ["zero-point-three-five Newton-meter-seconds", "ninety-five milliseconds"],
    },
    {
      id: "C", file: "Reaction_wheel.txt", knowledge: "Engineering",
      summary: "Battery enables recovery coils.", keywords: "above 14% · magnetic coils",
      chunks: ["fourteen percent voltage sag limit", "magnetic de-tumbling coils"],
    },
  ],
  power: {
    id: "D", file: "EPS.txt", knowledge: "Spacecraft",
    excerpt: "If the main bus voltage drops below 24.2V for a consecutive period exceeding 8 seconds, the EPS telemetry module triggers a Level 1 Load Shedding event.",
  },
  // Chunk-level matches are independent of whole-section selection.
  chunkMatches: [{ section: "B", chunks: [0] }, { section: "C", chunks: [0, 1] }],
  embedded: ["B", "C"],
  hierarchical: ["B", "C"],
  vectorMatches: ["B"],
};
