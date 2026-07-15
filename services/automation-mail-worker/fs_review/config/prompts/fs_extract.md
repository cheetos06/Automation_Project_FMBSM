You are extracting French financial statements from rendered PDF page images.

Rules:
- Use only evidence visible in the attached images.
- Do not use PDF text, hidden text layers, OCR libraries, or external files.
- Extract only the Bilan actif, Bilan passif, and Compte de resultat / CPC pages.
- Ignore cover pages, reports, annexes, notes, legal text, narrative pages, and empty pages.
- Keep the FS labels as written, but normalize obvious accents if visible.
- Preserve signs: parentheses, minus signs, and loss lines must be negative.
- Empty cells must be null, not 0.
- Do not invent lines that are not visible.
- Include subtotal and total lines, because they are needed for later tickmarks.
- For Bilan actif, extract Brut N, Amortissements/depreciations N, Net N, and Net N-1 when visible.
- For Bilan passif and Compte de resultat, extract Total N and Total N-1 when visible.
- Page numbers must be the original physical PDF page numbers shown by the batch/image context, not printed footer page numbers.
- Classify every extracted line as "annual" for company/social/individual
  accounts or "consolidated" for group accounts. A PDF may contain both; never
  merge them. Use a stable statement_set_id for each independent statement set.

Return ONLY valid JSON with this schema:

{
  "document_type": "financial_statements",
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
      "brut": null,
      "amortissement": null,
      "montant_n": null,
      "montant_n_1": null,
      "is_total": false,
      "notes": "short visual evidence note"
    }
  ],
  "quality_notes": []
}
