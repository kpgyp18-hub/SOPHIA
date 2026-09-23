---
name: image-editing
description: Edit raster images and layered compositions for Photoshop-class work, including selections, masks, retouching, color correction, compositing, and export validation; use when the result must preserve editable structure where possible.
---

# Image Editing

Produce the requested visual change while preserving source fidelity, editability, and the intended output use.

## Preflight

1. Identify the source file, desired deliverable, intended medium (web, print, social, UI, or archive), target dimensions, and whether the work needs to remain editable.
2. Preserve the original. For layered files (`.psd`, `.psb`, `.xcf`, `.kra`), retain layer groups, masks, adjustment layers, text, smart objects, and blend modes unless the request specifically changes them.
3. Inspect at working scale and 100% before changing pixels. Confirm the active selection, layer, mask, color mode, bit depth, and transparency state; do not assume a flattened preview is the editable source.

## Editing workflow

1. Prefer non-destructive operations: adjustment layers for tonal/color work, masks for hide/reveal, smart objects or duplicated source layers for transforms and filters, and separate retouch layers for healing or cloning.
2. For selections and cutouts, refine edges against the actual destination background. Check hair, semi-transparent materials, shadows, halos, and color fringing at 100%; use masks rather than permanent erasure whenever editability matters.
3. For compositing, match perspective, scale, light direction, color temperature, contrast, grain/noise, and depth of field before flattening. Keep the inserted subject and its shadow/reflection on separable layers.
4. For retouching, remove only the requested defects. Keep texture and natural detail; do not introduce broad beauty, identity, or factual changes without explicit instruction.
5. Use generative edits only when requested and supported by the selected tool. Treat generated pixels as a distinct layer or variant, preserve the pre-generation source, and disclose that the result was generated.

## Export and verification

1. Save an editable master when relevant, then export a separate delivery copy. Do not overwrite the only layered master with a flattened format.
2. Verify crop, dimensions, resolution, color profile, alpha handling, file size, and format against the intended use. Use sRGB for ordinary web delivery unless another profile is explicitly required.
3. Reopen the exported file and inspect the altered area at 100%, plus the full composition. Check for mask seams, missing layers, clipping, banding, unexpected color shifts, and lost transparency.
4. Report the source preserved, output path and format, substantive edits, and any fidelity limitation or unverified editor-specific feature.

## Automation boundaries

Use Photoshop UXP automation only for repeatable, authorized edits and run document-changing actions in the editor's appropriate modal operation. Do not bulk-apply destructive actions without a duplicate, a small representative validation, and user approval for the final scope. GIMP and Krita scripting APIs differ by version; verify the installed version before relying on an automation workflow.

## Reference implementations

Use these as implementation references, not as bundled dependencies:

- Adobe Photoshop UXP plugin samples: <https://github.com/AdobeDocs/uxp-photoshop-plugin-samples>
- Adobe Photoshop UXP API documentation: <https://github.com/AdobeDocs/uxp-photoshop>
- GIMP source and plug-in APIs: <https://github.com/GNOME/gimp>
- Krita scripting and plug-in APIs: <https://github.com/KDE/krita>
