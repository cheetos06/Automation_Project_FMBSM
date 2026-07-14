You are extracting tickmark placement positions from rendered French financial statement pages.

Rules:
- Use only evidence visible in the attached page images.
- Do not use PDF text, hidden text layers, OCR libraries, or external files.
- Focus only on Bilan actif, Bilan passif, and Compte de resultat / CPC tables.
- Ignore cover pages, reports, annexes, notes, legal text, narrative pages, and empty pages.
- Ignore titles, paragraph text, annex notes, headers, and footers.
- Ignore footnote/reference rows under the tables, even when they look aligned with columns.
- Do not create entries for labels such as "(1) Y compris", "(2) Dont ...", "(3) Dont ...", "* Y compris", or "- Redevances ..."; these are explanatory notes, not FS accounts.
- Create one entry for each visible non-empty amount cell that may need a tickmark.
- For every entry, identify the numeric amount itself first.
- Extract a tight bounding box around the visible numeric amount only, not around the row label and not around the full row.
- The tickmark will later be placed by code just to the right of this numeric amount box.
- Do not estimate y from the row label, table band, or surrounding text. The y position must come from the vertical center of the visible digits of the amount.
- Use normalized page coordinates:
  - x_norm is 0 at the left edge and 1 at the right edge.
  - y_norm is 0 at the top edge and 1 at the bottom edge.
  - amount_bbox_norm is [x1, y1, x2, y2] for the tight visible amount box.
- If a value is in the Brut column, field must be "brut".
- If a value is in the Amortissements/depreciations column, field must be "amortissement".
- If a value is in the Net N or Total N column, field must be "montant_n".
- If a value is in the Net N-1 or Total N-1 column, field must be "montant_n_1".
- Page numbers must be the original physical PDF page numbers shown by the batch/image context, not printed footer page numbers.
- Classify each amount as "annual" or "consolidated". Never merge positions
  from company/social accounts and consolidated/group accounts.

Return ONLY valid JSON with this schema:

{
  "coordinate_system": "normalized page coordinates, origin top-left",
  "entries": [
    {
      "statement": "Bilan actif|Bilan passif|Compte de resultat",
      "scope": "annual|consolidated",
      "statement_set_id": "stable visible set identifier",
      "page": 0,
      "libelle": "exact visible FS line label",
      "field": "brut|amortissement|montant_n|montant_n_1",
      "display_column": "visible column header",
      "amount": null,
      "amount_bbox_norm": [null, null, null, null],
      "x_norm": null,
      "y_norm": null,
      "notes": "short visual evidence note"
    }
  ],
  "quality_notes": []
}
