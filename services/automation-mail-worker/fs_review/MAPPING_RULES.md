# Generic Mapping Rules

## Objective

The mapper assigns each nonzero BG account to at most one detailed FS line.
Several BG accounts may compose the same FS line. The reverse reconciliation
then checks whether those accounts justify the FS amount.

Runtime decisions use only the BG, the FS extraction, and an optional reporting
year. Historical FAST mappings are not runtime inputs or rules.

## Processing Order

1. Apply direct exact accounting mappings.
2. Apply named, authorized accounting composites.
3. Prove derived result, account-coded, and repeated disclosure lines.
4. Apply exact single-row fallback mappings.
5. Search remaining lines for same-statement equal-sum combinations.
6. Search remaining lines for cross-statement equal-sum combinations.
7. Apply permissive semantic mappings to any remaining classified BG rows.

The first three stages are the accounting-backed layer. Later permissive
stages restore the historical coverage-first behavior and never conceal their
method, confidence, amount difference, participating accounts/categories, or
statement-family conflicts.

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
- Financial assets distinguish participations and related receivables, portfolio
  activity securities, other fixed securities, loans, and other financial
  assets according to the PCG account-to-statement passage.
- External charges retain useful PCG subfamilies (rentals, insurance, fees,
  travel, banking services, and others) for detailed presentations, then
  recombine on the standard `Autres achats et charges externes` line.
- Exceptional management items, asset disposals, dotations, and reversals stay
  distinct when the statement displays them separately.
- In the legacy PCG model, `675` and capital-nature `678` charges compose
  `Charges exceptionnelles sur operations en capital`; `775` and the
  capital-nature residual exceptional-income family compose the corresponding
  products line. This rule is limited to the legacy presentation and requires
  exact reconciliation. It is not carried into the modern 2025 presentation.

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

An amount coincidence cannot override an incompatible accounting category in
the accounting-backed stages. The explicit permissive stages may still emit
that candidate for review.

## FS Safeguards

- Prefer detailed leaf lines over totals, subtotals, calculated results, or
  duplicate headings. An exceptional or financial total may be used only when
  it is the sole available presentation line and the complete authorized
  category composition reconciles exactly.
- Prefer named presentation rules for multi-category lines. Remaining exact
  sums may be emitted by `exact_amount_combination`, `permissive_equal_sum`, or
  `cross_statement_equal_sum` after the accounting-backed stages are exhausted.
- Generic words such as `autres`, `dettes`, `créances`, `charges`, and
  `produits` cannot by themselves establish a mapping.
- Accounting-backed mappings do not cross asset, liability/equity, and result
  statements or result sections. The final permissive fallbacks may cross
  those boundaries, but the conflict is retained in the audit output.
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

- [French PCG 2024 published by the Autorite des normes comptables](https://www.anc.gouv.fr/files/anc/files/1_Normes_fran%C3%A7aises/Reglements/Recueils/PCG_Janvier2024/PCG--1er-janvier-2024.pdf).
- [DGFiP 2053 model and official notice](https://www.impots.gouv.fr/sites/default/files/formulaires/2032-not-sd/2025/2032-not-sd_5014.pdf).
- [DGFiP BOFiP treatment of legacy exceptional charges](https://bofip.impots.gouv.fr/bofip/1800-PGP.html/identifiant=BOI-BIC-CHG-60-10-20120912).
- ANC regulation 2022-06 and its commented account-to-statement passage tables.

## Reliability Boundary

Deterministic mapping is appropriate when account family, sign, label, FS line,
and amount composition agree. Treat `permissive_equal_sum`,
`cross_statement_equal_sum`, and `semantic_permissive` as review candidates,
not accounting-backed conclusions. Ambiguous custom labels, incomplete FS
extraction, gross/net information loss, regime ambiguity, and material BG/FS
contradictions require reviewer judgment.

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

It also appends mapping method, confidence, amount difference, conflicting
statement families, participating BG categories, and supporting BG accounts.
The same structured fields are present in `pipeline_report.json` under the
mapper reconciliation/audit records and in the Markdown mapper audit.

The PDF tickmark script never discovers amount positions from hidden PDF text.
Coordinates must come from a visual extraction saved in
`fs_tick_layout.json`. The symbols distinguish direct N support, N-1 support,
calculations, review items, and reportable exceptions. The green coherence
tick is not used for Bilan/CPC amount mismatches. Comments are attached to the
PDF annotations and retained in `FS_Mapped.xlsx`.
