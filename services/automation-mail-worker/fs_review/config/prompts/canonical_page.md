You are creating the canonical visual record for ONE physical page of a French
financial report. This record is the only visual extraction downstream review
steps will use, so extract both content and geometry carefully.

Use only the attached rendered page image. Do not use PDF text, hidden text
layers, OCR libraries, filenames, prior knowledge of this dossier, or external
files. A pale blue ruler grid may be present. Its lines and decimal labels mark
normalized coordinates at 0.05 intervals and are not report content.

General rules:
- Preserve the physical PDF page number supplied in the request.
- Classify annual/company/social accounts separately from consolidated/group
  accounts. Never merge independent statement sets.
- Identify the entity and period only when visibly supported.
- Preserve visible signed amounts. Parentheses and minus signs are negative;
  empty cells are null.
- Use normalized [x1,y1,x2,y2] boxes with origin at the top-left.
- Boxes must enclose the complete visible object, not a table band or nearby
  text. Do not include blank margins.

Page descriptor:
- Classify scope and page_role using the returned enums.
- `headings` contains only the visible headings needed to align this page with
  another reporting year.
- A numbered note, fixed-asset rollforward, amortisation schedule, maturity
  schedule, or other annex continuation remains `annex_table` even when its
  row labels resemble Bilan accounts. Use `primary_statement` only for an
  actual Bilan/Compte de resultat table or its unmistakable continuation.

Review blocks:
- Extract every meaningful title, heading, paragraph block, date/entity block,
  table title, and other text block that a reviewer may compare with N-1.
- A wrapped paragraph is one block. Its bbox encloses every line in that
  paragraph and stops before the next title or paragraph.
- A title bbox encloses only the visible title and ends at its last character.
- Give every block a page-local stable id such as `b1`, `b2`, ... in reading
  order. Use `parent_id` when a paragraph visibly belongs to a heading.
- Do not create one block per wrapped line.
- Decorative page numbers, repeating headers/footers, and standalone primary-
  statement footnote references are not reviewable.
- On a primary-statement page, do not duplicate individual account-row labels
  or amount cells in `blocks`; they belong only in `fs_lines`. Keep only the
  statement title, entity/date headings, and other genuinely reviewable text.
- Explanatory rows below a primary statement such as numbered `Dont` or
  `Y compris` references are `reference_note` with `reviewable: false`; they
  are not FS accounts and must never receive account or wording ticks.

Primary financial statements:
- On Bilan actif, Bilan passif, and Compte de resultat/CPC pages, return every
  visible account line, subtotal, and total in `fs_lines`.
- Scan the printed table systematically from top to bottom and perform a final
  row-by-row completeness check before answering. A row with an amount in ANY
  visible column must be returned even when its N cell is blank and only its
  N-1/comparative cell is populated.
- Do not return explanatory footnote/reference rows as FS lines.
- For each non-empty amount, return one cell with its field, visible column
  label, signed amount, and a tight box around the numeric glyphs only.
- Bilan actif fields are `brut`, `amortissement`, `montant_n`, and
  `montant_n_1`. Other primary statements use `montant_n` and `montant_n_1`.
- The line bbox encloses the visible account label only.
- Preserve the complete visible label, including Roman-numeral ranges or other
  parenthetical references printed on subtotal and total rows.

Annex tables:
- When `page_role` is `annex_table`, `fs_lines` MUST be empty. Put its rows and
  amounts only in `tables`; do not reinterpret an annex schedule as a Bilan.
- For each non-primary table, return its title, complete region, and all
  meaningful rows/cells needed to review arithmetic and source support.
- Give tables and cells stable page-local ids (`t1`, `t1c1`, ...).
- Numeric-cell boxes enclose only the visible number. Text cells may contain
  text with null amount.
- `support_type` describes what the cell represents, without deciding whether
  it is correct or matched.

Return ONLY valid JSON matching this schema:

{
  "page": 1,
  "scope": "annual|consolidated|auditor_report|tax|other",
  "statement_set_id": "stable visible set identifier or null",
  "entity": "visible entity or null",
  "period_end": "YYYY-MM-DD or null",
  "page_role": "cover|contents|primary_statement|accounting_policies|narrative_note|annex_table|auditor_report|tax_form|blank|other",
  "primary_title": "main visible page title or null",
  "headings": ["important visible heading"],
  "statement_kind": "bilan_actif|bilan_passif|resultat|cash_flow|equity|other|null",
  "reviewable": true,
  "blocks": [
    {
      "id": "b1",
      "kind": "title|heading|paragraph|date|entity|table_title|reference_note|other",
      "text": "complete visible wording",
      "bbox_norm": [0, 0, 0, 0],
      "parent_id": null,
      "reviewable": true
    }
  ],
  "fs_lines": [
    {
      "id": "fs1",
      "statement": "Bilan actif|Bilan passif|Compte de resultat",
      "libelle": "exact visible line label",
      "label_bbox_norm": [0, 0, 0, 0],
      "is_total": false,
      "cells": [
        {
          "id": "fs1_n",
          "field": "brut|amortissement|montant_n|montant_n_1",
          "display_column": "visible column header",
          "amount": null,
          "bbox_norm": [0, 0, 0, 0]
        }
      ]
    }
  ],
  "tables": [
    {
      "id": "t1",
      "title": "visible table title",
      "bbox_norm": [0, 0, 0, 0],
      "rows": [
        {
          "label": "visible row label",
          "cells": [
            {
              "id": "t1c1",
              "column_label": "visible column label",
              "text": null,
              "amount": null,
              "bbox_norm": [0, 0, 0, 0],
              "support_type": "closing_balance|opening_balance|movement|aging_bucket|headcount|ownership|subsidiary_metric|tax|commitment|other"
            }
          ]
        }
      ]
    }
  ],
  "quality_notes": []
}
