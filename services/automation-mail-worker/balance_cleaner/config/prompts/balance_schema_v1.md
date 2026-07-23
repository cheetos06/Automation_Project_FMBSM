This is the FIRST screenshot of a spreadsheet balance. Your analysis will be captured as JSON and injected into later extraction requests for consecutive screenshots whose column positions never change. Those later requests will not receive this full screenshot, so completeness and precision here are critical.

Analyze the complete visible table structure. Do NOT return a list of all accounts. Instead, identify every visible column/header and prove the interpretation using representative rows that are actually visible in this screenshot.

Return only one JSON object with this exact top-level structure:
{
  "analysis_purpose": "one sentence",
  "language_and_document_context": {
    "language": "visible language or unknown",
    "document_type_or_title": "exact visible value or null",
    "header_row_description": "where and how the headers appear"
  },
  "columns": [
    {
      "position": 1,
      "visible_header": "exact complete visible header, or null if blank",
      "role": "account_number | account_description | current_or_closing_balance | alternate_current_or_closing_balance | debit | credit | comparison_or_prior_period | variance | hierarchy_or_category | other | unknown",
      "interpretation": "what values in this column mean",
      "visible_examples": ["up to 3 exact values from this column"],
      "justification": "specific visible evidence for this role"
    }
  ],
  "account_fields": {
    "account_number_column_position": 1,
    "account_number_header": "exact header",
    "account_description_column_position": 2,
    "account_description_header": "exact header",
    "justification": "why these are the dedicated account fields"
  },
  "saldo": {
    "source_column_positions": [3],
    "source_headers": ["exact header"],
    "rule": "precise calculation or signed-column copy rule",
    "sign_convention": "precise sign handling",
    "valid_account_balance_patterns": [
      {
        "condition": "visible condition deciding which source column(s) apply",
        "source_column_positions": [3],
        "source_headers": ["exact header"],
        "rule": "calculation or signed copy rule for this pattern",
        "visible_account_examples": ["at least one exact account number"]
      }
    ],
    "columns_explicitly_not_used": [
      {
        "position": 4,
        "header": "exact header",
        "reason": "why it is comparison, movement, variance, etc."
      }
    ],
    "worked_visible_examples": [
      {
        "account_number": "exact visible account",
        "visible_source_values": ["exact visible values used"],
        "resulting_saldo": 0.0,
        "justification": "short calculation or selection explanation"
      }
    ],
    "justification": "why this is the current/closing/final balance"
  },
  "row_structure": {
    "actual_account_examples": [
      {
        "account_number": "exact visible account",
        "why_actual": "visible evidence"
      }
    ],
    "structural_or_summary_examples": [
      {
        "visible_label": "exact visible label",
        "why_not_account": "visible evidence"
      }
    ],
    "rules_for_later_screenshots": [
      "concrete rule based only on this visible layout"
    ]
  },
  "confidence_and_uncertainties": {
    "overall_confidence": "high | medium | low",
    "uncertainties": ["specific uncertainty, or an empty array"]
  }
}

Requirements:
- Enumerate ALL visible columns from left to right using 1-based positions, including blank-headed or auxiliary columns. Never silently omit a column.
- Preserve every visible header exactly, including punctuation and line breaks represented as spaces.
- Distinguish the current/reporting/closing/final balance from prior/comparison, opening, movement, and variance columns. Do not choose by proximity alone.
- Examine EVERY visible row whose dedicated account-number cell is populated. Determine every distinct numeric-column placement used for a valid account balance. A hierarchical report may place detail-account balances in one column and parent/summary-account balances in another column even though both rows have their own valid account numbers. Capture each placement as a separate valid_account_balance_pattern and include every applicable source position in source_column_positions.
- A column is not irrelevant merely because blank-account subtotal rows also use it. If any row with its own populated account number uses that column for its current balance, preserve it as a valid conditional balance source and explain the condition.
- Explain the selection using both header semantics and representative visible values/rows.
- Include at least two worked visible saldo examples when two actual account rows are readable. The first readable row with a populated dedicated account number MUST be one worked example. Include at least one worked example for every distinct valid_account_balance_pattern, and include a visible zero example if available.
- Explain how an actual account row differs from a parent, heading, subtotal, or recap row in this exact layout, with visible examples.
- If evidence is insufficient, use null and list the uncertainty instead of guessing.
- Return JSON only. No markdown and no text outside the JSON object.
