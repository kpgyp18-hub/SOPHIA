---
name: korean-text-in-images
description: Create or edit images containing Korean text without missing glyphs, encoding corruption, or generated pseudo-Hangul; use alongside image-editing when Korean copy must remain accurate and legible in a raster or layered visual.
---

# Korean Text in Images

Keep the approved Korean copy as text, not as model-generated pixels. This skill covers Korean text layers, raster rendering, and final-image verification; it does not perform OCR, speech recognition, or guarantee that a generative-image model can spell Korean correctly.

## Preflight

1. Obtain the authoritative copy as Unicode text and preserve it verbatim in a UTF-8 source, layer, or accompanying manifest. Confirm punctuation, spacing, line breaks, numbers, English words, and intended casing with the requester.
2. Normalize a working copy to Unicode NFC before comparison. Do not silently replace Hangul syllables with separately composed jamo, look-alike Latin/CJK characters, or a raster approximation.
3. Select a licensed font with the required Korean glyph coverage. Prefer a known Korean family such as Noto Sans KR/Noto Serif KR when its style is suitable; verify the exact installed font file, family, weight, and license rather than relying on a similarly named fallback.
4. Test-render the complete approved copy in the target application before compositing. Check every character at 100% for tofu boxes, fallback-font substitution, clipped 받침, separated jamo, broken ligatures, unexpected line wrapping, and missing symbols or emoji.

## Editing workflow

1. When a generated image needs Korean text, generate or edit only the background, subject, or intentionally blank text area. Add the approved Korean wording afterward as a real text layer or as a deterministic render from the approved font.
2. Keep text editable in the master file. Use separate layers for text, outline/stroke, shadow, background panel, and mask. Do not merge text into generated pixels until the approved export is ready.
3. Size and position Korean text using the target medium’s safe area. Inspect line height, tracking, word spacing, baseline alignment, and contrast against the actual background; Korean copy often needs different wrapping from an English placeholder.
4. If the delivery format cannot retain a text layer, retain the layered master and the font/license record. Convert to outlines only when required by the destination workflow and permitted by the font license; rasterize only in the delivery copy.
5. Never claim generated lettering is correct merely because it resembles Hangul. If the model produced text-like pixels, replace them with the approved text layer or mark the output as unsuitable.

## Verification

1. Compare the text layer or rendering input character-for-character with the approved NFC copy before export. For ordinary Hangul syllables, ensure the intended characters are present rather than decomposed into separate jamo; include any required punctuation and numerals in the comparison.
2. Export a delivery copy, reopen it, and inspect both full composition and 100% scale. Compare every visible line against the authoritative copy, preferably using a second reviewer for public-facing, legal, safety, price, or instructional content.
3. Check that the final result has no tofu boxes, replacement glyphs, substituted fonts, clipped characters, blurred small text, corrupted transparency around glyph edges, or an incorrect color/profile change.
4. Report the authoritative copy source, font family/file and license status, master and export paths, whether the delivery copy was flattened, and any remaining visual limitation. Do not report an image as text-accurate without the comparison evidence.

## Automation boundaries

For deterministic raster rendering, load an explicit TrueType/OpenType font by file path and retain the source string in Unicode. Do not rely on a platform default font or an editor fallback. Verify the installed application and font version before using batch automation; use a small representative proof before a bulk export.

## Reference implementations

Use these as implementation references, not as bundled dependencies or font assets:

- Noto CJK Korean font source and download guidance: <https://github.com/notofonts/noto-cjk>
- Noto font documentation and glyph-coverage guidance: <https://github.com/notofonts/noto-docs>
- Pillow OpenType/Unicode font rendering implementation: <https://github.com/python-pillow/Pillow/blob/main/src/PIL/ImageFont.py>
- Photoshop-class layer-preserving workflow: [image-editing](../image-editing/SKILL.md)
