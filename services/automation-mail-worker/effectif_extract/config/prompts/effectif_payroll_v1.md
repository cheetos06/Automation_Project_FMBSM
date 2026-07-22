You are extracting ONLY employee headcount and payroll-cost evidence from a complete French audit/financial PDF rendered as page images.

Analyze ALL attached page images. For a batch of a larger PDF, extract only evidence visible in that batch; the system will consolidate later.

Return ONLY valid JSON matching this schema. Use null when absent, unreadable, or unsupported. Never invent values. Use exercise N, not N-1.

{
  "entity_name": null,
  "period_end_date": null,
  "effectif": null,
  "charges_personnel": null,
  "salaires_traitements": null,
  "charges_sociales": null,
  "autres_charges_salariales": null,
  "effectif_is_really_zero": null,
  "effectif_zero_assessment": null,
  "effectif_justification": null,
  "raw_values": {
    "effectif": {"raw_text": null, "evidence_pages": []},
    "charges_personnel": {"raw_text": null, "unit": null, "currency": null, "evidence_pages": []},
    "salaires_traitements": {"raw_text": null, "unit": null, "currency": null, "evidence_pages": []},
    "charges_sociales": {"raw_text": null, "unit": null, "currency": null, "evidence_pages": []},
    "autres_charges_salariales": {"raw_text": null, "unit": null, "currency": null, "evidence_pages": []}
  },
  "confidence": null,
  "evidence_pages": [],
  "notes": []
}

effectif_zero_assessment must be "confirmed_zero", "likely_not_zero", "reported_nonzero", "inconclusive", or null.
effectif_is_really_zero is true only for explicit zero/no staff without contradictory non-zero payroll evidence; false for reported headcount above zero or visibly non-zero current-period payroll/salary charges; otherwise null.

Normalize monetary outputs to K EUR: EUR divided by 1000, K EUR unchanged, M EUR multiplied by 1000. Preserve exact line/value, unit, currency, and page in raw_values. A visible dash/zero is 0; absence is null.

Do not extract signatures, opinions, revenue, balance-sheet totals, or unrelated financial fields.

{{SECTOR_RULES}}
