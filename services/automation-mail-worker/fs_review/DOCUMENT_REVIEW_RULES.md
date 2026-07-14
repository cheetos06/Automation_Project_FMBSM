# Document Review and Tick Rules

This note describes the generic rules used by the Copilot-based document
review. Historical reviewed PDFs were used as examples, not as truth.

## Evidence meanings

The generated PDF keeps evidence types separate:

- `OK BG N`: the current amount is reconciled to one or more BG accounts.
- `OK RCA N-1`: the comparative amount agrees with the prior-year report.
- `OK calcul`: the displayed total recalculates from visible components.
- `OK coherent N`: current entity/date/period wording is internally coherent.
- `Wording idem N-1`: substantive wording is unchanged; normal one-year date
  roll-forward and updated current-period facts are allowed.
- `Wording nouveau N`: new or materially updated current-period disclosure is
  internally coherent but was not present in N-1.
- `Piece manquante`: the visible table requires evidence not present in the
  dossier. The affected table region is boxed and the required source is named.
- `Ecart a remonter`: a visible contradiction, unexplained omission, or failed
  reconciliation remains.

Arithmetic agreement is never treated as proof of the underlying source.

`Wording idem N-1`, `Wording nouveau N`, and other coherent current wording
all use the same plain green checkmark. They are not boxed. The workbook keeps
their semantic categories separate for review and filtering. A genuine removed
or contradictory block uses the red boxed cross.

## Visual placement

Review decisions and coordinates are deliberately separated. After Copilot
decides which titles, paragraphs, cells, and evidence regions need review, a
second Copilot vision pass reopens each current page with a pale normalized
coordinate grid and refines only those positions.

- A title tick sits immediately to the right of the actual title.
- A paragraph tick sits immediately to the right of the complete paragraph and
  at its vertical midpoint, not beside the first line.
- A numeric tick uses a tight amount-cell box.
- A prior-only block uses open whitespace at the logical insertion level and
  may not overlap current text.
- A missing-evidence region keeps its box, while its marker uses the refined
  right-middle anchor.
- Deterministic code validates coordinates and never shifts a correct anchor
  merely because another annotation is nearby.

The refinement input is generated from the current comparison result. It does
not contain report-specific keywords, labels, or fixed page coordinates.

## Wording comparison

Copilot indexes every rendered page before comparing content. Matching is
restricted to the same statement scope and uses page role, statement kind,
titles, headings, and document order.

- Annual/company accounts and consolidated accounts are independent scopes.
- Multi-page sections are compared as groups. Page breaks are not treated as
  additions or deletions.
- A reporting year/date moving forward by one year is expected.
- Period-specific facts may legitimately change: acquisitions, disposals,
  litigation, subsequent events, estimates, headcount, and similar disclosures.
- A shortened or reformulated title with the same meaning is not an error.
- Prior-only content is checked against the whole current index before it is
  treated as removed.
- Blank footnote references and explanatory note rows are not financial lines.

## Annex evidence

The BG can justify direct closing account balances when account type, label,
sign, and amount reconcile. The annex matcher constrains candidates by French
PCG account families; amount coincidence alone is rejected.

Examples that can often be supported from the BG:

- accrued expenses by direct account (`408`, `428`, `438`, `448`, `455`);
- prepaid/deferred balances (`486`, `487`);
- closing depreciation/impairment balances (`28`, `29`);
- related current-period dotation/reversal accounts when the table and BG make
  the movement explicit (`68`, `78`);
- gross receivable/payable totals and some equity closing balances.

Evidence normally required beyond the BG:

- fixed-asset movement schedules: fixed-asset roll-forward;
- receivable/payable maturity buckets: aged ledger or detailed schedule;
- headcount: payroll/HR support;
- ownership, capital movements, and legal characteristics: legal support;
- subsidiary metrics: subsidiary financial statements and ownership support;
- commitments: commitment support;
- tax calculations: tax support.

An entirely blank table does not trigger a missing-evidence box.

## 2025 presentation

For annual accounts governed by the French PCG, ANC regulation 2022-06 is
mandatory for exercises opened from 1 January 2025. The modernized models and
removal of the developed-format model mean that some 2024 lines or sublines may
legitimately disappear or be regrouped in 2025. These presentation changes are
not treated as account errors by the wording pass.

References:

- [ANC regulations page](https://www.anc.gouv.fr/normes-comptables-francaises/reglements-de-lanc)
- [CNCC-CNOEC presentation FAQ](https://doc.cncc.fr/docs/questionsreponses-relatives-a-la)

## Scope safety

Copilot labels every FS line and page as `annual` or `consolidated`. If both are
present, the pipeline never applies one generic BG to both. Provide separate
files with scope-specific names, for example:

```text
bg_standardized_annual.xlsx
bg_standardized_consolidated.xlsx
```

The same can be configured through `scope_bg_files` in `settings.json`.
Outputs are then written under `Output/annual/` and `Output/consolidated/` and
combined only at PDF annotation time.

## Validation evidence

- Ticket 17779: 19-page annual report, prior report, BG mapping, wording review,
  annex evidence review, and visual PDF placement.
- Ticket 16533: autonomous scope indexing identified consolidated pages 1-30,
  annual/company pages 31-42, and out-of-scope pages 43-45.
- Additional human-reviewed dossiers inspected for rule behavior included
  16500, 16501, 16515, 16551, 16874, 16877, 7239, 7407, 7569, 7796, 8853,
  8860, 9112, 9172, and 9986.

Copilot is used for visual extraction and semantic comparison. Deterministic
code handles page grouping, scope isolation, BG reconciliation, arithmetic
status, coordinate conversion, and annotation rendering. Unsupported facts are
flagged rather than invented.
