# Generic Mapping Rules

## Objective

The mapper assigns each nonzero BG account to at most one detailed FS line.
Several BG accounts may compose the same FS line. The reverse reconciliation
then checks whether those accounts justify the FS amount.

Runtime decisions use only the BG, the FS extraction, and an optional reporting
year. Historical FAST mappings are not runtime inputs or rules.

## Processing Order

1. Validate that the BG and FS refer to a compatible entity and period.
2. Exclude empty and zero-value BG rows from material reconciliation.
3. Aggregate repeated rows by accounting-account identity before using sign.
4. Classify accounts by PCG family, label, sign, and presentation regime.
5. Match classifications to available detailed FS lines.
6. Reconcile all BG accounts composing each FS line, allowing minor rounding.
7. Leave ambiguous rows blank and report source or extraction differences.

## Core Accounting Logic

- Fixed assets use their economic account family. Related `28` amortization
  and `29` depreciation reduce the corresponding gross asset line.
- Stocks `30-37` and related `39` depreciation compose the same net stock
  family. `33` is goods work in progress and `34` is services work in progress.
- Debit customer accounts normally map to customer receivables. Credit customer
  balances map to liabilities unless an explicit advance account or label
  supports advances received.
- Credit supplier accounts normally map to supplier debt. Debit supplier
  balances map to receivables unless an explicit supplier-advance account or
  label supports advances paid.
- Third-party, current-account, VAT, and bank families use the aggregated
  closing sign. Sign is never inferred from one duplicate row in isolation.
- `512` represents bank balances and `519` bank financing/overdrafts, subject
  to the detected statement regime and available detailed lines.
- Equity, provisions, prepaid expenses, deferred income, and fixed-asset debt
  remain separated by their distinct PCG families.
- Purchases and revenue include their related rebate or contra-accounts when
  they belong to the same economic line.
- Operating, financial, exceptional, participation, and tax accounts are
  classified by economic nature, not merely by the first digit.

## Contextual Accounts

Some accounts cannot be mapped safely from their prefix alone:

- `648/649` personnel subdivisions require label meaning and exact residual
  reconciliation between salary and social-charge lines.
- `708` ancillary revenue may require the account label and the actual FS
  presentation.
- Unusual `65/67` subdivisions depend on economic nature and the applicable
  presentation regime.
- Custom subaccounts and entity-specific labels may require AI or reviewer
  reasoning.

An amount coincidence cannot override an incompatible accounting category.

## FS Safeguards

- Map to detailed leaf lines, not totals, subtotals, calculated results, or
  duplicate headings.
- Accept many BG accounts for one FS line only when their composition is
  accounting-compatible.
- Use a small rounding tolerance, but do not hide material differences.
- A certain account destination may remain mapped when the BG demonstrates
  that the published FS amount is wrong; the audit report labels this as a
  source difference.
- Separate debit and credit FS balances inferred from one net standardized BG
  balance are gross-up inferences, not direct justification.
- If totals, signs, or periods are inconsistent, inspect the rendered PDF
  pages visually and correct the extraction before assessing mapping quality.

## Reporting Regime

The mapper detects decisive modern or legacy line labels before using the
reporting year as a fallback. This matters because older templates and tax
forms may present the same PCG balance differently.

ANC regulation 2022-06 applies to exercises opened from 1 January 2025, with
possible early application. A year alone therefore cannot prove the layout.
When dates and presentation markers are both ambiguous, visual document review
is required.

Reference sources:

- French PCG published by the Autorite des normes comptables.
- ANC regulation 2022-06 and its commented account-to-statement passage tables.

## Reliability Boundary

Deterministic mapping is appropriate when account family, sign, label, FS line,
and amount composition agree. Use AI or human review for ambiguous custom
labels, incomplete FS extraction, gross/net information loss, regime ambiguity,
or material BG/FS contradictions.

## FS Review And Tickmarks

`FS_Mapped.xlsx` separates the evidence used for each visible amount:

- `BG`: a detailed N amount is directly justified by mapped BG accounts.
- `RCA N-1`: the comparative amount agrees with an independently extracted
  prior-year signed statement.
- `FS calculation`: a subtotal or result agrees with its visible components.
- `BG net + presentation inference`: the accounting destination may be
  explainable, but a standardized net collective account cannot reproduce the
  FS gross debit/credit presentation by itself. It remains an `Ecart a
  remonter` and receives the red cross.

The PDF tickmark script never discovers amount positions from hidden PDF text.
Coordinates must come from a visual extraction saved in
`fs_tick_layout.json`. The symbols distinguish direct N support, N-1 support,
calculations, review items, and reportable exceptions. The green coherence
tick is not used for Bilan/CPC amount mismatches. Comments are attached to the
PDF annotations and retained in `FS_Mapped.xlsx`.
