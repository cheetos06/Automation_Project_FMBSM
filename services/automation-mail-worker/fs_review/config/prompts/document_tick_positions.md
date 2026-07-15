You are refining annotation positions on one rendered page of a French
financial report. Use only the attached page image and the supplied candidate
JSON. Do not use PDF text, hidden text layers, OCR libraries, filenames, or
external files. Do not change the review decisions and do not invent or remove
candidates.

The image may contain a pale coordinate grid added by the pipeline. Its lines
and small decimal labels are measurement guides at normalized 0.05 intervals;
they are not part of the financial report and must not be reviewed as content.

For every candidate, locate the requested visual object accurately:

- `present_text`: the candidate label identifies the subject or start of the
  reviewed block; it does not always define the full extent. Use
  `review_comment` and `review_evidence` to determine what was reviewed. If the
  decision covers prose below a heading, `bbox_norm` must enclose the heading
  and all related paragraph lines up to the next heading. If it reviews only a
  title, tightly enclose only that title. A wrapped paragraph is one block.
  Return an `anchor_norm` immediately to the right of the complete block and
  vertically centered on the full block, not aligned with its first line. For
  a title, the anchor must be just to the right of the actual last visible
  character.
- `amount_cell`: `bbox_norm` must tightly enclose only the visible numeric cell.
  Return an anchor immediately to its right and vertically centered.
- `evidence_region`: `bbox_norm` must tightly enclose the relevant visible
  table or disclosure region. Return an anchor just outside its right edge and
  vertically centered.
- `missing_prior`: the described text is absent from the current page. Do not
  pretend that it has a visible bbox. Use the original position only as a rough
  hint, then return `bbox_norm: null` and an `anchor_norm` in clear whitespace
  at the logical insertion level between the surrounding current blocks. The
  point must not overlap, cover, or sit inside any visible title, paragraph,
  table, number, header, or footer. Prefer open space to the right of the
  logical insertion level.

Coordinates are normalized `[0,1]` with origin at the top-left. Keep every
anchor inside the page with enough room for a small 10-point check or cross.
Return each supplied `id` exactly once. If a present object cannot be located
reliably, retain its original bbox and say so in `quality_note`.

Return ONLY valid JSON:

{
  "page": 0,
  "placements": [
    {
      "id": "candidate id",
      "bbox_norm": [0, 0, 0, 0],
      "anchor_norm": [0, 0],
      "quality_note": ""
    }
  ],
  "quality_notes": []
}
