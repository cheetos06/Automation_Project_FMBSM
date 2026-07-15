You are extracting prior-year French financial statements from rendered PDF page images.

Rules:
- Use only evidence visible in the attached images.
- Do not use PDF text, hidden text layers, OCR libraries, or external files.
- Extract only the Bilan actif, Bilan passif, and Compte de resultat / CPC pages.
- Ignore cover pages, reports, annexes, notes, legal text, narrative pages, and empty pages.
- The output is used to justify the N-1 comparison column of the current-year FS.
- IMPORTANT: this PDF is the RCA / financial statements of the prior year.
- For each visible line, extract the CURRENT PERIOD column of this PDF into montant_n.
- Example: if the PDF is for 31/12/2024 and also shows a comparative 31/12/2023 column, extract the 31/12/2024 column and ignore 31/12/2023.
- Do not extract the rightmost comparative column unless it is the only current-period column visible.
- Empty cells must be null, not 0.
- Preserve signs exactly.
- Page numbers must be the original physical PDF page numbers shown by the batch/image context, not printed footer page numbers.
- Classify every line as "annual" or "consolidated" and keep independent
  statement sets separate with a stable statement_set_id.

Return ONLY valid JSON with this schema:

{
  "document_type": "prior_financial_statements",
  "period_end": "YYYY-MM-DD or null",
  "currency": "EUR or null",
  "unit": "units|thousands|unknown",
  "lines": [
    {
      "statement": "Bilan actif|Bilan passif|Compte de resultat",
      "scope": "annual|consolidated",
      "statement_set_id": "stable visible set identifier",
      "entity": "visible entity or group name",
      "page": 0,
      "libelle": "exact visible FS line label",
      "montant_n": null,
      "is_total": false,
      "notes": "short visual evidence note"
    }
  ],
  "quality_notes": []
}
