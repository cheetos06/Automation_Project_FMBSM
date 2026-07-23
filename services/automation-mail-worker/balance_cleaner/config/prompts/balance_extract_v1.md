You are extracting an account balance from a series of screenshots of the same Excel sheet.

The workbook can use any title, structure, accounting convention, or language. Identify fields from the visible headers and table context, not from one expected name.

Your task is to extract ONLY the actual account lines.

Ignore:

Titles
Headers
Footers
Totals and subtotals
Empty rows
Page breaks
Column headers
Any formatting elements

For each account, extract exactly:

account_number
account_description
saldo

where saldo is the signed closing/final balance normalized as follows:

- When separate debit and credit columns are present, saldo = debit - credit.
- Debit equivalents include, without limitation: Dare, Debit, Débit, Soll, Debe, and localized equivalents.
- Credit equivalents include, without limitation: Avere, Credit, Crédit, Haben, Haber, and localized equivalents.
- If separate debit-balance and credit-balance columns are present, saldo = debit balance - credit balance.
- If one signed net-balance column is present (for example Saldo, Solde, Balance, Net balance, or a localized equivalent), preserve that value and sign exactly.
- If one amount column is paired with a debit/credit side indicator, return debit amounts as positive and credit amounts as negative.
- If the table contains movement/turnover columns as well as closing/final/ending balance columns, use the closing/final/ending balance. Do not calculate saldo from movements when a closing balance is available.
- If the relevant columns or sign convention cannot be determined reliably from the visible table, omit the row rather than guessing.

Examples:

If Debit/Dare = 1250.50 and Credit/Avere = 0 → saldo = 1250.50

If Debit/Dare = 0 and Credit/Avere = 1250.50 → saldo = -1250.50

If both debit and credit are present → saldo = debit - credit

If a signed Solde/Saldo/Balance is -8421.15 → saldo = -8421.15

Keep the sign exactly as calculated.

Return ONLY a JSON array (not wrapped in markdown, not inside a code block).

Output format:

[
  {
    "account_number": "100000",
    "account_description": "Cash",
    "saldo": 1250.50
  },
  {
    "account_number": "200100",
    "account_description": "Suppliers",
    "saldo": -8421.15
  }
]

Rules:

Preserve the account number exactly as written.
First identify the dedicated account-number column from the visible header and table structure (for example Account, Account number, Kontonummer, Sachkonto, Compte, or a localized equivalent). Copy account_number only from that column. If the description begins with a similar, longer, shorter, corrected, or zero-padded number, do not use that embedded number to replace or alter the value in the dedicated account-number column.
Account numbers may mix letters, digits, spaces, punctuation, and leading zeroes. Read every character from the dedicated account-number cell itself and preserve it literally. In particular, distinguish visually similar characters such as S and 5, O and 0, I/l and 1, B and 8, and Z and 2. Do not normalize an alphanumeric account into a numeric-looking code and do not guess from the description.
Preserve the account description exactly as written.
Copy every visible word and punctuation mark in the account description. Never shorten, abbreviate, normalize, correct, or paraphrase it.
Use a numeric value for saldo (no currency symbols, no thousands separators).
Convert decimal commas to decimal points.
Numeric zero is a real amount. A relevant balance cell showing 0, 0.00, 0,00, or an equivalent formatted zero is not blank and must not cause an otherwise valid account row to be omitted. A genuinely empty balance cell is different from a displayed zero.
Do not invent, merge, split, or infer accounts.
Only associate an amount with an account when the account number, account description, and that amount are visibly on the same horizontal row. Never move an amount from a neighboring detail, parent, category, total, or subtotal row.
An account number can appear again in an explanatory, "thereof"/"davon", note, disclosure, or recap row. A repeated occurrence is a valid account line only when that occurrence's own row visibly contains its own relevant current/closing balance. If its relevant amount cells are blank, ignore that occurrence. Never copy, carry forward, or reuse an amount from an earlier or later occurrence of the account.
If context images or repeated report presentation cause the exact same account number, account description, and saldo to be seen more than once without distinct amount-bearing source rows, return that record only once. However, if separate physical rows each visibly contain their own amounts, preserve both rows even when their account numbers match.
Scan the entire image from its first visible row through its last visible row. A screenshot can begin or end in the middle of an account section; do not skip valid lines near either edge.
Treat the first visible row of every TARGET image as a normal source row. If it visibly has its own account number and relevant numeric balance, include it before interpreting the rows below it. A later blank, explanatory, or repeated occurrence of that account does not cancel the earlier amount-bearing row.
Do not omit an indented detail/leaf account merely because a parent, category, total, or subtotal is visible nearby.
Before returning the JSON, perform a silent completeness pass over every TARGET image, one image at a time and one row at a time from top to bottom. For every row whose dedicated account-number cell is non-empty and whose relevant current/closing/final balance cell visibly contains a number (including zero), verify that exactly one corresponding JSON object is present, unless that row is explicitly a title, header, footer, total, or subtotal. Recheck the first and last visible rows of every TARGET image. Do not describe this check in the response.
If a row is unreadable, omit it rather than guessing.
The screenshots are consecutive parts of the same worksheet. Treat them as one continuous table and avoid duplicates between screenshots.
Return nothing except the JSON array.
