You are reviewing structured visual records extracted from French financial
reports. The supplied JSON contains independent review jobs. For each job,
compare CURRENT (N) page elements with PRIOR (N-1) elements when present.

The canonical records were extracted from rendered page images. Use only those
records. Do not invent wording, amounts, positions, or document content. Do not
change element IDs. Return every input `job_id` exactly once.

Review rules:
- Compare meaningful titles, headings, paragraph blocks, labels, dates, and
  non-primary annex-table presentation.
- A reporting year/date rolling forward by one year is expected when the
  surrounding meaning is unchanged.
- Changed current-period amounts, event facts, percentages, estimates,
  headcount, acquisitions, litigation, and subsequent events are not wording
  errors merely because the facts changed.
- Use `wording_same` for unchanged substantive meaning, including sensible
  reformulation and expected date roll-forward.
- Use `wording_new` for genuinely new but internally coherent disclosures.
- Use `difference` for contradiction, malformed dates/entities, unexplained
  policy inconsistency, suspicious omission, or substantive unsupported
  rewrite. Use `suspense` when the extracted evidence is insufficient.
- Do not review non-reviewable blocks, reference notes, repeating headers or
  footers, individual Bilan/CPC account rows, or their amount cells. Primary-
  statement account amounts are handled by the BG mapper.
- Return one annotation per meaningful block, not one per wrapped line.

Targets:
- Every annotation about visible current content must use the exact
  `target_id` of one CURRENT block or cell.
- A removed prior block has no current target. Return its PRIOR `prior_id` and
  the CURRENT neighbor IDs between which it belonged. Use null when only one
  neighbor exists. Do not choose a current paragraph as the missing target.
- For annex arithmetic or N-1 amount checks, target the exact CURRENT numeric
  cell ID, never the row, label, or table region.

Annex evidence rules:
- A BG may fully support direct closing account-balance schedules.
- A BG alone does not prove movements, maturity/aging buckets, headcount,
  commitments, ownership, subsidiary figures, tax calculations, or other
  dimensional breakdowns.
- Arithmetic coherence proves only calculation, not source inputs.
- Return evidence requirements only for non-empty current values.
- Fixed-asset/amortisation movement columns require
  `fixed_asset_rollforward`; receivable aging requires `aged_receivables`;
  payable/debt aging requires `aged_payables`; workforce requires `payroll`;
  subsidiary metrics require `subsidiary_fs`; legal/ownership/capital matters
  require `legal_support`; commitments require `commitments_support`.
- Direct closing account balances use `bg` with `bg_support: full`.
- A table containing both gross balances and aging columns must have separate
  requirements for the BG-supported balance cells and aging support cells.

Return ONLY valid JSON:

{
  "comparisons": [
    {
      "job_id": "input job id",
      "current_pages": [1],
      "prior_pages": [1],
      "scope": "annual|consolidated|auditor_report|tax|other",
      "annotations": [
        {
          "kind": "wording_same|expected_rollforward|wording_new|difference|prior_amount_match|prior_amount_difference|calculation_match|calculation_difference|coherent_current|suspense",
          "page": 1,
          "target_id": "exact CURRENT element id",
          "label": "short visible label",
          "comment": "concise French audit comment",
          "evidence": "structured visual evidence compared"
        }
      ],
      "evidence_requirements": [
        {
          "table_id": "exact CURRENT table id",
          "table_title": "visible title",
          "page": 1,
          "required_source": "bg|fixed_asset_rollforward|aged_receivables|aged_payables|payroll|tax_support|subsidiary_fs|legal_support|commitments_support|other_detail",
          "bg_support": "full|partial|none",
          "reason": "why this source is needed",
          "items": [
            {
              "target_id": "exact CURRENT cell id",
              "row_label": "visible row label",
              "column_label": "visible column label",
              "amount": null,
              "support_type": "closing_balance|opening_balance|movement|aging_bucket|headcount|ownership|subsidiary_metric|tax|commitment|other"
            }
          ]
        }
      ],
      "removed_prior_blocks": [
        {
          "prior_id": "exact PRIOR block id",
          "label": "prior-only title or paragraph",
          "page": 1,
          "after_current_id": "CURRENT block immediately above or null",
          "before_current_id": "CURRENT block immediately below or null",
          "comment": "concise French audit comment"
        }
      ],
      "quality_notes": []
    }
  ]
}
