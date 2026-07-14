You are visually reviewing two pages from French financial reports.

One or more CURRENT report (N) page images are attached first, followed by one
or more PRIOR-YEAR report (N-1) page images. The request tells you which image
and physical page belongs to each period. Compare the complete supplied
section, not same-position pages in isolation. Use only what is visibly
rendered in those images. Do not use PDF text, hidden text layers, OCR
libraries, filenames, or external files.

Review objective:
- Compare meaningful titles, headings, paragraph blocks, labels, and table
  presentation in N against N-1.
- Treat a reporting year/date rolling forward by one year as expected when the
  surrounding meaning is unchanged.
- Do not treat current-period monetary amounts, event facts, percentages, or
  estimates as wording errors merely because they changed; identify them as
  current-period facts requiring their own evidence where relevant.
- A paragraph is "wording_same" when its substantive accounting meaning is the
  same, allowing expected year/date roll-forward and updated current facts.
- A reformulated or shortened title with the same clear semantic meaning is
  "wording_same", not "difference".
- A genuinely new but internally coherent disclosure is "wording_new".
- Sections whose purpose is to describe period-specific facts naturally change:
  faits caracteristiques, current activity, acquisitions/disposals, litigation,
  subsequent events, estimates, headcount, and other current-year disclosures.
  When their heading/purpose remains valid and the new content is internally
  coherent for N, classify the changed factual content as "wording_new" or
  "expected_rollforward", not "difference" merely because the facts differ.
- A disclosed accounting-policy change may be legitimate. Mark it as
  "wording_new" when clearly presented and internally coherent; use
  "difference" only for contradiction, malformed dates/entities, an
  unexplained policy inconsistency, or a suspicious omission.
- A removed prior heading/paragraph, an unexplained substantive rewrite, a
  contradictory date/entity, or a malformed disclosure is "difference".
- Be conservative: if visual evidence is insufficient, use "suspense" rather
  than claiming a match.

Avoid duplicate work:
- Do not annotate individual Bilan actif, Bilan passif, or Compte de resultat
  account rows or amount cells. Those are reviewed by the BG mapper.
- You may review the primary-statement page title and column dates once.
- Do not treat footnote references such as "(1)", "(2)", or "(3)" as separate
  accounts or separate paragraphs.
- Return one annotation for each meaningful block, not one per wrapped text
  line and not one for decorative text.

Non-primary annex tables:
- Compare N-1 comparative cells to the corresponding current-period values on
  the prior page when visibly comparable.
- Identify arithmetic totals that can be recomputed from visible rows.
- Classify what evidence is needed for current-period table details. A BG can
  often support closing account totals, but it cannot by itself prove movement
  columns, maturity/aging buckets, headcount, commitments, ownership data,
  subsidiary figures, tax calculations, or other dimensional breakdowns.
- Do not mark a table as missing evidence merely because the BG is not attached
  to this comparison call. Return it in evidence_requirements instead.
- Direct account-balance schedules can be fully supported by the BG when each
  displayed current amount corresponds to an identifiable account or account
  family. This commonly includes detailed accrued-expense/account-balance
  schedules. In that case use required_source "bg" and bg_support "full".
- Return an evidence requirement only when at least one non-empty current value
  needs that evidence. Do not request evidence for an entirely blank table.
- Do not report generic old layout labels such as "Cadre A" or "Cadre B" as
  removed disclosures when the underlying table has simply been reformatted
  and its substantive information remains present.
- Arithmetic coherence proves only the calculation. It does not prove the
  source of movement, aging, ownership, headcount, or other detailed inputs.

Coordinates:
- Every annotation must identify its physical CURRENT page and use a bbox on
  that CURRENT image.
- For prior_amount_match, prior_amount_difference, calculation_match, and
  calculation_difference, bbox_norm MUST tightly enclose the exact numeric
  amount cell on the CURRENT page that receives the tick. Never use the full
  row, row label, or whole table. A comparative N-1 check must point to the
  comparative amount cell in the current report, not the N amount cell.
- For wording annotations, bbox_norm encloses the reviewed text block.
- bbox_norm is [x1,y1,x2,y2], normalized from 0 to 1, tightly enclosing the
  current title, paragraph, cell, or region being reviewed.
- For content present only in N-1 and missing in N, anchor_bbox_norm must mark
  a small area of clear whitespace on N where the omitted block belongs,
  normally between the surrounding matched blocks. It must not overlap or
  enclose any current title, paragraph, table, number, header, or footer.

Return ONLY valid JSON:

{
  "current_pages": [0],
  "prior_pages": [0],
  "scope": "annual|consolidated|auditor_report|tax|other",
  "annotations": [
    {
      "kind": "wording_same|expected_rollforward|wording_new|difference|prior_amount_match|prior_amount_difference|calculation_match|calculation_difference|coherent_current|suspense",
      "page": 0,
      "label": "short visible label",
      "bbox_norm": [0,0,0,0],
      "anchor_bbox_norm": null,
      "comment": "concise French audit comment",
      "evidence": "what was visibly compared"
    }
  ],
  "evidence_requirements": [
    {
      "table_title": "visible title",
      "page": 0,
      "bbox_norm": [0,0,0,0],
      "required_source": "bg|fixed_asset_rollforward|aged_receivables|aged_payables|payroll|tax_support|subsidiary_fs|legal_support|commitments_support|other_detail",
      "bg_support": "full|partial|none",
      "reason": "why this source is needed",
      "items": [
        {
          "row_label": "visible row label",
          "column_label": "visible column label",
          "amount": null,
          "bbox_norm": [0,0,0,0],
          "support_type": "closing_balance|opening_balance|movement|aging_bucket|headcount|ownership|subsidiary_metric|tax|commitment|other"
        }
      ]
    }
  ],
  "removed_prior_blocks": [
    {
      "label": "prior-only title or paragraph",
      "page": 0,
      "anchor_bbox_norm": [0,0,0,0],
      "comment": "concise French audit comment"
    }
  ],
  "quality_notes": []
}

For every evidence item, amount MUST be the visible signed numeric value when a
value is present. Use null only when the cell is visibly empty. Do not leave a
clearly readable amount null.
Each evidence-item bbox_norm MUST tightly enclose that item's numeric amount
cell, not its row label or the full row.

Evidence-source classification must use these general rules:
- fixed-asset, amortisation, depreciation, or impairment movement columns:
  fixed_asset_rollforward;
- receivable maturity/aging buckets: aged_receivables;
- payable/debt maturity/aging buckets: aged_payables;
- workforce/headcount: payroll;
- subsidiary financial metrics: subsidiary_fs;
- ownership, capital movements, commitments, or legal characteristics:
  legal_support or commitments_support as applicable;
- direct closing account-balance schedules: bg.
- When a maturity table contains both a gross/total balance column and aging
  columns, return two separate evidence requirements: bg/full for the gross
  balance cells, and aged_receivables or aged_payables for the aging cells.
