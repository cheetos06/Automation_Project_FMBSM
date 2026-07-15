You are visually reviewing one CURRENT-period page from a French financial
report. Use only the rendered image attached to this request. Do not use PDF
text, hidden text layers, OCR libraries, filenames, or external files.

The prior-year page-alignment stage found no sufficiently similar N-1 page.
Review what can still be concluded from the current page without pretending
that N-1 support exists.

Rules:
- Check visible entity names, reporting dates, period labels, page title, and
  internal wording coherence. A correct current-period date/year is
  "coherent_current".
- A meaningful current title/section absent from N-1 is "wording_new" when it
  is internally coherent. Use "difference" only for a visible contradiction,
  malformed date/entity, or suspicious omission indicated on this page.
- A reformulated title with the same semantic meaning as the surrounding
  section is not an error.
- Do not annotate individual Bilan actif, Bilan passif, or Compte de resultat
  account rows or amount cells. Those are reviewed by the BG mapper.
- You may review a primary-statement title and column dates once.
- For non-primary annex tables, identify visible arithmetic totals and the
  evidence needed for current-period data.
- A BG can often support closing account totals, but cannot by itself prove
  movement columns, maturity/aging buckets, headcount, commitments, ownership,
  subsidiary figures, tax calculations, or other dimensional breakdowns.
- Arithmetic coherence proves only the calculation, not the source inputs.
- Direct account-balance schedules can be fully supported by the BG when each
  displayed current amount corresponds to an identifiable account or account
  family. Use required_source "bg" and bg_support "full" for those schedules.
- Return an evidence requirement only when at least one non-empty current value
  needs support. Do not request evidence for an entirely blank table.
- Return one annotation per meaningful block, not one per wrapped line.

Coordinates:
- page is the physical current PDF page supplied in the request.
- bbox_norm is [x1,y1,x2,y2], normalized 0..1 on the current image.
- For calculation_match and calculation_difference, bbox_norm MUST tightly
  enclose the exact numeric total cell, never the full row or table.

Return ONLY valid JSON:

{
  "current_pages": [0],
  "prior_pages": [],
  "scope": "annual|consolidated|auditor_report|tax|other",
  "annotations": [
    {
      "kind": "wording_new|difference|calculation_match|calculation_difference|coherent_current|suspense",
      "page": 0,
      "label": "short visible label",
      "bbox_norm": [0,0,0,0],
      "anchor_bbox_norm": null,
      "comment": "concise French audit comment",
      "evidence": "visible basis for the decision"
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
  "removed_prior_blocks": [],
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
