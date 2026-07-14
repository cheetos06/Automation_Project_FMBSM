from __future__ import annotations

import itertools
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import openpyxl


@dataclass(frozen=True)
class BGRow:
    account: str
    label: str
    amount: float


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _account(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\D", "", str(value or ""))


def _amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def load_bg(path: Path) -> list[BGRow]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    values = list(sheet.iter_rows(values_only=True))
    workbook.close()
    if not values:
        return []
    headers = [_fold(value) for value in values[0]]

    def column(names: tuple[str, ...], fallback: int) -> int:
        for index, header in enumerate(headers):
            if any(name in header for name in names):
                return index
        return fallback

    account_col = column(("compte", "account"), 0)
    label_col = column(("libelle", "label"), 1)
    amount_col = column(("solde", "montant", "amount"), 2)
    result = []
    for raw in values[1:]:
        account = _account(raw[account_col] if account_col < len(raw) else None)
        amount = _amount(raw[amount_col] if amount_col < len(raw) else None)
        if not account or amount is None or abs(amount) < 0.005:
            continue
        result.append(
            BGRow(
                account=account,
                label=str(raw[label_col] or "") if label_col < len(raw) else "",
                amount=amount,
            )
        )
    return result


def _prefix_score(account: str, item_label: str, table_title: str) -> float:
    label = _fold(item_label)
    table = _fold(table_title)
    context = f"{table} {label}"
    rules: list[tuple[tuple[str, ...], tuple[str, ...], float]] = [
        (("fournisseur", "facture non parvenue"), ("408",), 1.0),
        (("personnel", "conge", "salaire"), ("428",), 0.9),
        (("social", "securite sociale"), ("438",), 0.9),
        (("fiscal", "impot", "taxe"), ("448",), 0.9),
        (("associe", "compte courant", "autres dettes"), ("455",), 0.8),
        (("charge constatee d avance",), ("486",), 1.0),
        (("produit constate d avance",), ("487",), 1.0),
        (("client", "creance"), ("41",), 0.65),
        (("capital",), ("101",), 0.8),
        (("immobilisation",), ("2",), 0.45),
        (("amortissement", "depreciation"), ("28", "29"), 0.75),
    ]
    score = 0.0
    for keywords, prefixes, value in rules:
        if any(keyword in context for keyword in keywords) and any(
            account.startswith(prefix) for prefix in prefixes
        ):
            score = max(score, value)
    if "charge a payer" in table and account.startswith(("408", "428", "438", "448", "455")):
        score = max(score, 0.55)
    return score


def _row_score(row: BGRow, item_label: str, table_title: str) -> float:
    label_score = SequenceMatcher(None, _fold(row.label), _fold(item_label)).ratio()
    return max(label_score, _prefix_score(row.account, item_label, table_title))


def _allowed_prefixes(item_label: str, column_label: str, table_title: str) -> tuple[str, ...] | None:
    item = _fold(item_label)
    column = _fold(column_label)
    table = _fold(table_title)
    if "depreciation" in table or "amortissement" in table:
        if any(word in column for word in ("dotation", "augmentation")):
            return ("68",)
        if any(word in column for word in ("reprise", "diminution")):
            return ("78",)
        if any(word in column for word in ("cloture", "ouverture", "solde")):
            return ("28", "29")
    if "charge a payer" in table:
        return ("408", "428", "438", "448", "455")
    if "produit constate d avance" in table:
        return ("487",)
    if "charge constatee d avance" in table:
        return ("486",)
    if "etat des creances" in table and "montant brut" in column:
        return ("4",)
    if "etat des dettes" in table and any(
        word in column for word in ("montant brut", "montant", "total")
    ):
        return ("4",)
    if "capital" in table and "capital" in item:
        return ("101",)
    if "variation des capitaux propres" in table:
        if "resultat" in item:
            return ("12",)
        if any(word in item for word in ("capitaux propres", "ouverture", "cloture")):
            return ("10", "11", "12", "13", "14")
    return None


def _match_item(
    item: dict[str, Any],
    table_title: str,
    rows: list[BGRow],
    used: set[int],
) -> tuple[list[int], float] | None:
    amount = _amount(item.get("amount"))
    if amount is None:
        return None
    label = str(item.get("row_label") or "")
    column_label = str(item.get("column_label") or "")
    allowed_prefixes = _allowed_prefixes(label, column_label, table_title)
    allow_used = _fold(label).startswith("total")
    ranked_indexes = sorted(
        (
            (
                max(
                    _row_score(row, label, table_title),
                    0.55 if allowed_prefixes is not None else 0.0,
                ),
                index,
            )
            for index, row in enumerate(rows)
            if (allow_used or index not in used)
            and (
                allowed_prefixes is None
                or row.account.startswith(allowed_prefixes)
            )
        ),
        reverse=True,
    )
    relevant = [index for score, index in ranked_indexes if score >= 0.38][:20]
    if not relevant and allowed_prefixes is None:
        relevant = [index for _, index in ranked_indexes[:20]]
    tolerance = max(1.0, abs(amount) * 0.0005)
    candidates: list[tuple[float, list[int], float]] = []
    for size in range(1, min(3, len(relevant)) + 1):
        for combo in itertools.combinations(relevant, size):
            total = sum(rows[index].amount for index in combo)
            difference = abs(abs(total) - abs(amount))
            if difference > tolerance:
                continue
            score = sum(_row_score(rows[index], label, table_title) for index in combo)
            score /= size
            score += 0.15 / size
            candidates.append((score, list(combo), total))
    if not candidates:
        return None
    _, indexes, total = max(candidates, key=lambda value: value[0])
    return indexes, total


def match_requirement_to_bg(
    requirement: dict[str, Any], bg_rows: list[BGRow]
) -> tuple[list[dict[str, Any]], bool]:
    items = [item for item in requirement.get("items", []) if isinstance(item, dict)]
    numeric_items = [item for item in items if _amount(item.get("amount")) is not None]
    if not numeric_items:
        return [], False
    used: set[int] = set()
    matches: list[dict[str, Any]] = []
    table_title = str(requirement.get("table_title") or "")
    for item in numeric_items:
        match = _match_item(item, table_title, bg_rows, used)
        if match is None:
            continue
        indexes, total = match
        if not _fold(item.get("row_label")).startswith("total"):
            used.update(indexes)
        matches.append(
            {
                "item": item,
                "accounts": [bg_rows[index].account for index in indexes],
                "labels": [bg_rows[index].label for index in indexes],
                "supporting_amount": abs(total),
            }
        )
    return matches, len(matches) == len(numeric_items)


def bg_supports_schedule_absence(label: Any, bg_rows: list[BGRow]) -> bool:
    """Return true when a prior-only schedule is immaterial because its PCG balances are zero."""

    folded = _fold(label)
    if "produit" in folded and "recevoir" in folded:
        prefixes = ("418", "4287", "4387", "4487")
    else:
        return False
    return not any(
        row.account.startswith(prefixes) and abs(row.amount) >= 0.005
        for row in bg_rows
    )
