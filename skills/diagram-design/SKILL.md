---
name: diagram-design
description: Design clear editorial diagrams as HTML, SVG, or an appropriate editable source; use when a visual must explain relationships, flow, hierarchy, or change more clearly than prose alone.
---

# Diagram Design

Make the diagram serve a specific reader decision. A diagram is not decoration and should not imply facts, certainty, or causal links that the source evidence does not support.

## Design brief

1. Identify the audience, question the diagram must answer, medium, dimensions, accessibility needs, and whether the output must remain editable.
2. Extract entities, relationships, direction, grouping, and uncertainty from the supplied evidence. Keep source facts distinct from proposed interpretation.
3. Select the smallest fitting visual grammar: flow for sequence, tree for hierarchy, map for dependencies, comparison for alternatives, and timeline for change. Do not force unrelated content into a generic box-and-arrow diagram.

## Build

1. Establish a reading order and clear visual hierarchy before styling. Use labels that retain the precise meaning of the source; avoid unexplained arrows, colors, icons, and abbreviations.
2. Show state, ownership, boundaries, feedback loops, and exceptions only when they materially affect understanding. Use a legend where encoding is not self-evident.
3. Prefer semantic HTML/SVG or the requested editable native format. Keep text selectable and preserve an editable source when the delivery format is flattened.
4. When redrawing Mermaid, draw.io, or Excalidraw, treat their layout/theme as input rather than authority; retain factual nodes and edges but recompute the presentation for the target audience.

## Verification

1. Render/open the output at intended dimensions and inspect readability, clipping, overlap, contrast, alignment, and arrow endpoints.
2. Trace representative paths from source evidence through the diagram. Check that every symbol has one meaning and that omitted detail does not reverse the conclusion.
3. Report source inputs, output and editable-source paths, visual grammar selected, and any inferred or simplified relationship.

## Reference implementation

- Diagram Design source repository and import safety approach: <https://github.com/cathrynlavery/diagram-design>
