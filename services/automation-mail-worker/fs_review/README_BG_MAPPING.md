# BG to FS Mapping

`map_bg_to_fs.py` maps `bg_standardized.xlsx` accounts to the detailed
financial-statement lines in `fs_extract_N.json`.

The mapper does not open `fast.xlsm` and does not contain ticket-specific
mappings. FAST can be used downstream for review, but it is not a prediction
input.

## Files

- `map_bg_to_fs.py`: command-line entry point, input/output handling, and
  reconciliation reporting.
- `fs_mapping.py`: builds the FS-centric reconciliation used for review and
  PDF tickmarks.
- `tick_fs_pdf.py`: places BG, RCA, calculation, coherence, and exception
  tickmarks on an unpointed FS PDF.
- `pcg_rules.py`: generic French PCG account classification rules.
- `rule_based_mapper.py`: matching and amount-reconciliation engine.
- `MAPPING_RULES.md`: accounting logic, safeguards, and unresolved cases.

## Requirements

- Python 3
- `openpyxl`
- `PyMuPDF`

```powershell
python -m pip install openpyxl pymupdf
```

## Single Ticket

Pass either the extracted ticket folder or its `Input` folder:

```powershell
python map_bg_to_fs.py --ticket "Raw files from service desk\<ticket> - Extracted"
```

The default output is `<ticket>\Output\BG_Mapped.xlsx`. When known, supply the
reporting year:

```powershell
python map_bg_to_fs.py `
  --ticket "Raw files from service desk\<ticket> - Extracted" `
  --year 2025
```

Optional paths:

```powershell
python map_bg_to_fs.py `
  --ticket "Raw files from service desk\<ticket> - Extracted" `
  --fs-json "<path>\fs_extract_N.json" `
  --output "<path>\BG_Mapped.xlsx" `
  --audit-report "<path>\reconciliation.md"
```

## Batch Processing

```powershell
python map_bg_to_fs.py --batch "Raw files from service desk"
```

Folders without both required inputs are skipped. For a mixed-year batch,
process ambiguous legacy dossiers individually with their own `--year`.

## Required Inputs

The ticket `Input` folder must contain:

- `bg_standardized.xlsx`
- `fs_extract_N.json`

The FS extraction must contain visible detailed statement lines and amounts.
Summary-only, incomplete, or incorrect extraction should be reviewed from
rendered PDF page images before relying on the result.

## Output

`BG_Mapped.xlsx` contains one sheet and only:

1. `bg account`
2. `libelle`
3. `montant`
4. `mapping FS`

Blank mappings mean that the available accounting and reconciliation evidence
did not justify a destination. The script deliberately does not force them.

The mapper also creates `FS_Mapped.xlsx`. This separate workbook contains each
visible FS amount, its BG or RCA support, difference, review status, tickmark
type, comment, and optional PDF coordinates. `BG_Mapped.xlsx` remains unchanged
and keeps its original four columns.

## PDF Tickmarks

PDF coordinates are visual extraction data, not mapper rules. Store them in
the ticket as `fs_tick_layout.json`. If an independently reviewed prior-year
statement is available, store its extracted amounts as `fs_extract_N_1.json`.
The mapper picks both files up automatically.

Then point the unannotated PDF:

```powershell
python tick_fs_pdf.py `
  --pdf "<ticket>\financial_statements_N.pdf" `
  --fs-mapped "<ticket>\FS_Mapped.xlsx" `
  --output "<ticket>\reviewed_fs_N.pdf"
```

The generated legend distinguishes direct BG matches, RCA N-1 matches,
recalculated totals, review items, and exceptions. A BG/FS amount mismatch
receives the red `Ecart a remonter` cross even when the workbook contains a
probable presentation explanation.

## Audit Report

`--audit-report` creates a separate Markdown reconciliation report. It can
identify exact compositions, inferred gross-ups, probable source differences,
and unjustified FS lines. It is optional and never changes the four-column
Excel output.

## Verification

```powershell
python -m py_compile map_bg_to_fs.py fs_mapping.py tick_fs_pdf.py pcg_rules.py rule_based_mapper.py
python map_bg_to_fs.py --help
```
