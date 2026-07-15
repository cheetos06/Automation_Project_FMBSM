"""Build an FS-centric reconciliation workbook from BG mapping results.

The workbook is deliberately separate from BG_Mapped.xlsx. It records one row
per visible FS amount/column so it can drive PDF tickmarks without changing the
clean four-column BG output.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import openpyxl

from pcg_rules import account_digits


FS_OUTPUT_HEADERS = [
    "page",
    "statement",
    "fs line",
    "display column",
    "fs amount",
    "support source",
    "supporting amount",
    "difference",
    "status",
    "tickmark",
    "comment",
    "supporting BG accounts",
    "supporting BG labels",
    "tick x",
    "tick y",
]

CONTRA_ASSET_PREFIXES = ("28", "29", "39", "49", "59")


def _repair_mojibake(value: Any) -> str:
    text = str(value or "")
    if "Ã" not in text and "Â" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def normalize_key(value: Any) -> str:
    text = _repair_mojibake(value).lower()
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _numeric(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_lines(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("lines", "lignes", "data", "items", "fs_lines"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []


def load_prior_amounts(path: Path | None) -> dict[str, float]:
    if path is None or not path.exists():
        return {}
    result: dict[str, float] = {}
    for item in _json_lines(path):
        label = item.get("libelle") or item.get("label") or item.get("name")
        amount = _numeric(item.get("montant_n", item.get("amount_n")))
        if label and amount is not None:
            result[normalize_key(label)] = amount
    return result


def load_layout(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    entries = data.get("entries", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"Invalid FS layout in {path}")
    return [item for item in entries if isinstance(item, dict)]


def _line_field_amount(line: Any, field: str) -> float | None:
    if field == "montant_n":
        return line.amount
    if field == "montant_n_1":
        return line.amount_n_1
    if field == "brut":
        return getattr(line, "gross", None)
    if field == "amortissement":
        return getattr(line, "depreciation", None)
    return None


def _display_column(field: str) -> str:
    return {
        "montant_n": "N",
        "montant_n_1": "N-1",
        "brut": "Brut N",
        "amortissement": "Amort./Dep. N",
    }.get(field, field)


def _infer_statement(index: int, fs_lines: list[Any]) -> str:
    label = normalize_key(fs_lines[index].label)
    passif_end = next(
        (
            i
            for i, line in enumerate(fs_lines)
            if "total general du passif" in normalize_key(line.label)
        ),
        len(fs_lines),
    )
    actif_end = next(
        (
            i
            for i, line in enumerate(fs_lines)
            if "total general de l actif" in normalize_key(line.label)
        ),
        -1,
    )
    if index <= actif_end:
        return "Bilan actif"
    if index <= passif_end:
        return "Bilan passif"
    if "resultat" in label or index > passif_end:
        return "Compte de resultat"
    return ""


def _default_layout(fs_lines: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, line in enumerate(fs_lines):
        for field in ("brut", "amortissement", "montant_n", "montant_n_1"):
            if _line_field_amount(line, field) is None:
                continue
            result.append(
                {
                    "libelle": line.label,
                    "field": field,
                    "display_column": _display_column(field),
                    "statement": _infer_statement(index, fs_lines),
                }
            )
    return result


def _merge_layout(
    fs_lines: list[Any], layout_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not layout_entries:
        return _default_layout(fs_lines)

    positioned = {
        (normalize_key(item.get("libelle")), str(item.get("field", "montant_n")))
        for item in layout_entries
    }
    merged = list(layout_entries)
    for item in _default_layout(fs_lines):
        key = (normalize_key(item["libelle"]), item["field"])
        if key not in positioned:
            merged.append(item)
    return merged


def _sum_leaf_range(
    fs_lines: list[Any], start: int, end: int, field: str
) -> float:
    total = 0.0
    for line in fs_lines[start:end]:
        if line.summary:
            continue
        if field == "brut":
            amount = getattr(line, "gross", None)
            if amount is None:
                amount = line.amount
        elif field == "amortissement":
            amount = getattr(line, "depreciation", None)
        else:
            amount = line.amount
        total += amount or 0.0
    return total


def _summary_formulas(fs_lines: list[Any]) -> dict[tuple[int, str], float]:
    """Calculate standard French balance-sheet and P&L totals from FS lines."""

    normalized = [normalize_key(line.label) for line in fs_lines]

    def find(fragment: str) -> int | None:
        return next(
            (i for i, label in enumerate(normalized) if fragment in label),
            None,
        )

    def amount(fragment: str) -> float:
        index = find(fragment)
        if index is None:
            return 0.0
        return fs_lines[index].amount or 0.0

    formulas: dict[tuple[int, str], float] = {}
    fixed_total = find("total de l actif immobilise")
    current_total = find("total de l actif circulant")
    asset_total = find("total general de l actif")
    equity_total = find("total des capitaux propres")
    other_equity_total = find("total des autres fonds propres")
    provisions_total = find("total des provisions")
    debt_total = find("total des emprunts et dettes")
    passif_total = find("total general du passif")
    net_revenue = find("montant net du chiffres d affaires")
    operating_products = find("total des produits d exploitation")
    operating_charges = find("total des charges d exploitation")
    operating_result = find("resultat d exploitation")
    financial_products = find("total des produits financiers")
    financial_charges = find("total des charges financieres")
    financial_result = find("resultat financier")
    current_result = find("resultat courant avant impots")
    total_products = find("total des produits i iii v vii")
    total_charges = find("total des charges ii iv vi viii ix x")
    profit_loss = find("benefice ou perte total des produits total des charges")
    balance_result = find("resultat de l exercice benefice ou perte")

    if fixed_total is not None:
        for field in ("montant_n", "brut", "amortissement"):
            formulas[(fixed_total, field)] = _sum_leaf_range(
                fs_lines, 0, fixed_total, field
            )
    if fixed_total is not None and current_total is not None:
        for field in ("montant_n", "brut", "amortissement"):
            formulas[(current_total, field)] = _sum_leaf_range(
                fs_lines, fixed_total + 1, current_total, field
            )
    if current_total is not None and asset_total is not None:
        for field in ("montant_n", "brut", "amortissement"):
            formulas[(asset_total, field)] = (
                formulas.get((fixed_total, field), 0.0)
                + formulas.get((current_total, field), 0.0)
                + _sum_leaf_range(
                    fs_lines, current_total + 1, asset_total, field
                )
            )

    section_totals = [
        (asset_total, equity_total),
        (equity_total, other_equity_total),
        (other_equity_total, provisions_total),
        (provisions_total, debt_total),
    ]
    for previous, target in section_totals:
        if previous is not None and target is not None:
            formulas[(target, "montant_n")] = _sum_leaf_range(
                fs_lines, previous + 1, target, "montant_n"
            )
    if debt_total is not None and (debt_total, "montant_n") not in formulas:
        previous_candidates = [
            index
            for index in (
                equity_total,
                other_equity_total,
                provisions_total,
                asset_total,
            )
            if index is not None and index < debt_total
        ]
        previous = max(previous_candidates) if previous_candidates else -1
        formulas[(debt_total, "montant_n")] = _sum_leaf_range(
            fs_lines, previous + 1, debt_total, "montant_n"
        )
    if asset_total is not None and equity_total is not None:
        # The balance-sheet result is calculated, but it remains an equity
        # component and must be included in the equity subtotal.
        formulas[(equity_total, "montant_n")] = sum(
            line.amount or 0.0
            for line in fs_lines[asset_total + 1 : equity_total]
        )
    if passif_total is not None:
        formulas[(passif_total, "montant_n")] = sum(
            fs_lines[index].amount or 0.0
            for index in (
                equity_total,
                other_equity_total,
                provisions_total,
                debt_total,
            )
            if index is not None
        )

    pnl_start = (passif_total + 1) if passif_total is not None else 0
    if net_revenue is not None:
        formulas[(net_revenue, "montant_n")] = _sum_leaf_range(
            fs_lines, pnl_start, net_revenue, "montant_n"
        )
    if net_revenue is not None and operating_products is not None:
        formulas[(operating_products, "montant_n")] = (
            formulas.get((net_revenue, "montant_n"), 0.0)
            + _sum_leaf_range(
                fs_lines, net_revenue + 1, operating_products, "montant_n"
            )
        )
    if operating_products is not None and operating_charges is not None:
        formulas[(operating_charges, "montant_n")] = _sum_leaf_range(
            fs_lines,
            operating_products + 1,
            operating_charges,
            "montant_n",
        )
    if operating_result is not None:
        formulas[(operating_result, "montant_n")] = (
            amount("total des produits d exploitation")
            - amount("total des charges d exploitation")
        )
    if operating_result is not None and financial_products is not None:
        formulas[(financial_products, "montant_n")] = _sum_leaf_range(
            fs_lines,
            operating_result + 1,
            financial_products,
            "montant_n",
        )
    if financial_products is not None and financial_charges is not None:
        formulas[(financial_charges, "montant_n")] = _sum_leaf_range(
            fs_lines,
            financial_products + 1,
            financial_charges,
            "montant_n",
        )
    if financial_result is not None:
        formulas[(financial_result, "montant_n")] = (
            amount("total des produits financiers")
            - amount("total des charges financieres")
        )
    if current_result is not None:
        formulas[(current_result, "montant_n")] = (
            amount("resultat d exploitation") + amount("resultat financier")
        )
    if total_products is not None:
        formulas[(total_products, "montant_n")] = (
            amount("total des produits d exploitation")
            + amount("total des produits financiers")
            + amount("produits exceptionnels")
        )
    if total_charges is not None:
        formulas[(total_charges, "montant_n")] = (
            amount("total des charges d exploitation")
            + amount("total des charges financieres")
            + amount("charges exceptionnelles")
            + amount("participation des salaries aux resultats")
            + amount("impots sur les benefices")
        )
    final_result = (
        amount("total des produits i iii v vii")
        - amount("total des charges ii iv vi viii ix x")
    )
    if profit_loss is not None:
        formulas[(profit_loss, "montant_n")] = final_result
    if balance_result is not None:
        formulas[(balance_result, "montant_n")] = final_result
    return formulas


def _rounding_match(left: float, right: float) -> bool:
    return abs(left - right) <= 1.0


def _gross_up_lines(stats: dict[str, Any]) -> dict[str, str]:
    """Recognize the common supplier debit/credit gross presentation pair."""

    review = stats.get("category_differences", [])
    supplier = next(
        (
            item
            for item in review
            if "fournisseur" in normalize_key(item.get("fs_line"))
        ),
        None,
    )
    receivable = next(
        (
            item
            for item in review
            if "creance" in normalize_key(item.get("fs_line"))
        ),
        None,
    )
    if supplier is None or receivable is None:
        return {}
    supplier_diff = float(supplier.get("difference", 0.0))
    receivable_diff = float(receivable.get("difference", 0.0))
    tolerance = max(2.0, 0.01 * max(abs(supplier_diff), abs(receivable_diff), 1.0))
    if (
        supplier_diff <= 0
        or receivable_diff <= 0
        or abs(supplier_diff - receivable_diff) > tolerance
    ):
        return {}
    inferred = (supplier_diff + receivable_diff) / 2
    comment = (
        "Presentation brute probable: la BG standardisee conserve un solde "
        f"net, tandis que le FS ventile environ {inferred:.0f} EUR entre "
        "creances et dettes fournisseurs."
    )
    return {
        normalize_key(supplier["fs_line"]): comment,
        normalize_key(receivable["fs_line"]): comment,
    }


def build_fs_mapping_rows(
    rows: list[Any],
    fs_lines: list[Any],
    mapper: Any,
    stats: dict[str, Any],
    layout_path: Path | None = None,
    prior_fs_path: Path | None = None,
) -> list[dict[str, Any]]:
    layout = _merge_layout(fs_lines, load_layout(layout_path))
    prior = load_prior_amounts(prior_fs_path)
    formulas = _summary_formulas(fs_lines)
    gross_up_comments = _gross_up_lines(stats)
    line_by_key = {
        normalize_key(line.label): (index, line)
        for index, line in enumerate(fs_lines)
    }
    rows_by_line: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for row_index, row in enumerate(rows):
        line_index = mapper.row_line_indexes.get(id(row))
        if line_index is not None:
            rows_by_line[line_index].append((row_index, row))

    output: list[dict[str, Any]] = []
    for entry in layout:
        label_key = normalize_key(entry.get("libelle"))
        field = str(entry.get("field", "montant_n"))
        match = line_by_key.get(label_key)
        if match is None:
            continue
        line_index, line = match
        fs_amount = _line_field_amount(line, field)
        if fs_amount is None:
            continue

        mapped = rows_by_line.get(line_index, [])
        supporting_rows = [row for _, row in mapped]
        support_source = ""
        supporting_amount: float | None = None
        status = "review"
        tickmark = "suspense"
        comment = ""

        if field == "montant_n_1":
            supporting_amount = prior.get(label_key)
            support_source = "RCA N-1" if supporting_amount is not None else ""
            if supporting_amount is not None and _rounding_match(
                fs_amount, supporting_amount
            ):
                status = "matched"
                tickmark = "n1_match"
            elif supporting_amount is not None:
                status = "difference"
                tickmark = "difference"
                comment = "Le comparatif N-1 du FS differe du RCA N-1."
            else:
                comment = "Montant N-1 non retrouve dans le RCA extrait."
        elif (line_index, field) in formulas:
            supporting_amount = formulas[(line_index, field)]
            support_source = "FS calculation"
            if _rounding_match(fs_amount, supporting_amount):
                status = "calculated"
                tickmark = "calculation"
            else:
                status = "difference"
                tickmark = "difference"
                comment = "Le total affiche ne correspond pas au calcul des lignes."
        elif field == "brut":
            gross_rows = [
                row
                for row in supporting_rows
                if not account_digits(row.account).startswith(CONTRA_ASSET_PREFIXES)
            ]
            supporting_amount = sum(row.amount for row in gross_rows)
            support_source = "BG"
            supporting_rows = gross_rows
        elif field == "amortissement":
            contra_rows = [
                row
                for row in supporting_rows
                if account_digits(row.account).startswith(CONTRA_ASSET_PREFIXES)
            ]
            supporting_amount = abs(sum(row.amount for row in contra_rows))
            support_source = "BG"
            supporting_rows = contra_rows
        else:
            supporting_amount = sum(
                mapper.category_value(
                    mapper.assigned_categories[row_index] or "", [row]
                )
                for row_index, row in mapped
            )
            support_source = "BG"

        if (
            field != "montant_n_1"
            and (line_index, field) not in formulas
            and supporting_amount is not None
        ):
            if _rounding_match(fs_amount, supporting_amount):
                status = "matched"
                tickmark = "n_match"
            elif label_key in gross_up_comments:
                status = "difference"
                tickmark = "difference"
                support_source = "BG net + presentation inference"
                comment = gross_up_comments[label_key]
            elif supporting_rows:
                status = "difference"
                tickmark = "difference"
                comment = "Le montant FS differe du montant justifie par la BG."
            else:
                status = "review"
                tickmark = "suspense"
                comment = "Aucun compte BG directement affecte a cette ligne."

        difference = (
            fs_amount - supporting_amount
            if supporting_amount is not None
            else None
        )
        accounts = ", ".join(str(row.account) for row in supporting_rows)
        labels = " | ".join(row.label for row in supporting_rows)
        output.append(
            {
                "page": entry.get("page"),
                "statement": entry.get(
                    "statement", _infer_statement(line_index, fs_lines)
                ),
                "fs line": _repair_mojibake(line.label),
                "display column": entry.get(
                    "display_column", _display_column(field)
                ),
                "fs amount": fs_amount,
                "support source": support_source,
                "supporting amount": supporting_amount,
                "difference": difference,
                "status": status,
                "tickmark": tickmark,
                "comment": entry.get("comment") or comment,
                "supporting BG accounts": accounts,
                "supporting BG labels": labels,
                "tick x": entry.get("x"),
                "tick y": entry.get("y"),
            }
        )
    return output


def write_fs_mapping(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "FS_Mapped"
    sheet.append(FS_OUTPUT_HEADERS)
    for item in rows:
        sheet.append([item.get(header) for header in FS_OUTPUT_HEADERS])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = {
        "A": 8,
        "B": 22,
        "C": 55,
        "D": 18,
        "E": 16,
        "F": 28,
        "G": 18,
        "H": 14,
        "I": 14,
        "J": 16,
        "K": 70,
        "L": 45,
        "M": 70,
        "N": 10,
        "O": 10,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for cell in sheet[1]:
        cell.font = openpyxl.styles.Font(bold=True)
    for row in sheet.iter_rows(min_row=2):
        for column in (5, 7, 8):
            row[column - 1].number_format = '#,##0.00;[Red]-#,##0.00'
    workbook.save(path)
