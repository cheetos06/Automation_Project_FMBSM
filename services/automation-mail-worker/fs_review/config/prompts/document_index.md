You are building a visual index of a French financial-report PDF.

Use only the rendered page images attached to this request. Do not use PDF
text, hidden text layers, OCR libraries, filenames, or external files.

For every physical PDF page, identify its document scope and main content.
This index will later be used to compare the current report with the prior-year
report, so preserve the physical page number supplied with each image.

Scope rules:
- "annual" means company/individual/social accounts (comptes annuels,
  comptes sociaux, comptes individuels).
- "consolidated" means group/consolidated accounts (comptes consolides).
- "auditor_report" means a statutory-auditor or accountant report, opinion,
  attestation, responsibilities, signatures, or report cover.
- "tax" means liasse fiscale/CERFA/tax forms.
- "other" means content outside those scopes.
- A cover, contents page, primary statement, note, or annex inherits annual or
  consolidated scope when the visible document section makes that clear.
- Never merge annual and consolidated sections. A PDF may contain both.

Page-role rules:
- cover
- contents
- primary_statement (Bilan, Compte de resultat/CPC, cash-flow statement,
  statement of changes in equity, or equivalent primary statement)
- accounting_policies
- narrative_note
- annex_table
- auditor_report
- tax_form
- blank
- other

Return one compact descriptor per physical page. Headings should contain the
few visible titles needed to align this page with a prior-year page. Do not
transcribe full paragraphs or table contents at this stage.

Return ONLY valid JSON:

{
  "document_type": "financial_report_index",
  "entity": "visible entity name or null",
  "period_end": "YYYY-MM-DD or null",
  "pages": [
    {
      "page": 1,
      "scope": "annual|consolidated|auditor_report|tax|other",
      "page_role": "cover|contents|primary_statement|accounting_policies|narrative_note|annex_table|auditor_report|tax_form|blank|other",
      "primary_title": "main visible page title or null",
      "headings": ["important visible heading"],
      "entity": "visible entity name or null",
      "period_end": "YYYY-MM-DD or null",
      "statement_kind": "bilan_actif|bilan_passif|resultat|cash_flow|equity|other|null",
      "reviewable": true,
      "notes": "short visual classification evidence"
    }
  ],
  "quality_notes": []
}
