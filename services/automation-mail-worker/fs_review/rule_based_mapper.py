"""Reconcile explicit PCG semantic categories to extracted FS lines."""

from __future__ import annotations

import itertools
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from pcg_rules import (
    AccountObservation,
    CATEGORY_ALIASES,
    RuleDecision,
    account_identity,
    classify_observations,
    normalize_text,
)


LIABILITY_CATEGORIES = {
    "equity_capital",
    "equity_premiums",
    "equity_revaluation",
    "equity_legal_reserve",
    "equity_other_reserves",
    "equity_retained_earnings",
    "equity_result",
    "equity_investment_grants",
    "equity_regulated_provisions",
    "association_funds",
    "provisions_risks",
    "provisions_charges",
    "bank_debt",
    "bank_loans",
    "bank_overdrafts",
    "other_financial_debt",
    "associate_financial_debt",
    "customer_advances",
    "supplier_debt",
    "personnel_debt",
    "social_bodies_debt",
    "income_tax_debt",
    "vat_debt",
    "other_tax_debt",
    "tax_social_debt",
    "fixed_asset_debt",
    "other_debts",
    "deferred_income",
    "liability_conversion_difference",
}

INCOME_CATEGORIES = {
    "sales_merchandise",
    "production_goods",
    "production_services",
    "contextual_ancillary_revenue",
    "contextual_revenue",
    "production_stocked",
    "production_capitalized",
    "operating_subsidies",
    "other_operating_income",
    "reversals_operating",
    "legacy_charge_transfers",
    "financial_participation_income",
    "financial_other_fixed_income",
    "financial_other_interest_income",
    "financial_positive_exchange",
    "financial_vmp_disposal_income",
    "financial_income",
    "financial_reversals",
    "exceptional_income",
    "asset_disposal_income",
}

NON_FS_CATEGORIES = {
    "non_fs_internal_transfer",
    "non_fs_analytic",
}

EXCEPTIONAL_CATEGORIES = {
    "exceptional_charges",
    "exceptional_income",
}

SUMMARY_MAPPABLE_CATEGORIES = EXCEPTIONAL_CATEGORIES | {
    "financial_charges",
    "financial_income",
}

PRIMARY_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "bank_loans": ("etablissements de credit emprunts",),
    "bank_overdrafts": ("decouverts et concours bancaires",),
    "associate_financial_debt": ("financieres diverses associes",),
    "personnel_debt": ("dettes fiscales et sociales personnel",),
    "social_bodies_debt": ("organismes sociaux",),
    "income_tax_debt": ("etat impots sur les benefices",),
    "vat_debt": ("etat taxes sur le chiffre d affaires",),
    "other_tax_debt": ("dettes fiscales et sociales autres dettes",),
    "personnel_receivables": ("personnel",),
    "social_bodies_receivables": ("organismes sociaux", "personnel"),
    "income_tax_receivable": ("etat impots sur les benefices",),
    "vat_receivable": ("etat taxes sur le chiffre d affaires",),
}


def words(value: Any) -> list[str]:
    return normalize_text(value).split()


def text_similarity(left: Any, right: Any) -> float:
    left_words = words(left)
    right_words = words(right)
    if not left_words or not right_words:
        return 0.0
    left_set = set(left_words)
    right_set = set(right_words)
    overlap = len(left_set & right_set)
    precision = overlap / len(left_set)
    recall = overlap / len(right_set)
    token_f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    sequence = SequenceMatcher(None, " ".join(left_words), " ".join(right_words)).ratio()
    containment = 1.0 if left_set <= right_set or right_set <= left_set else 0.0
    return max(token_f1, 0.75 * token_f1 + 0.25 * sequence, 0.82 * containment)


def best_alias_similarity(aliases: Iterable[str], label: str) -> float:
    return max((text_similarity(alias, label) for alias in aliases), default=0.0)


def amount_similarity(expected: float | None, actual: float | None) -> float:
    if expected is None or actual is None:
        return 0.0
    difference = abs(expected - actual)
    scale = max(abs(expected), abs(actual), 1.0)
    if difference <= max(2.0, 0.00005 * scale):
        return 1.0
    relative = difference / scale
    if relative <= 0.001:
        return 0.82
    if relative <= 0.01:
        return 0.55
    if relative <= 0.05:
        return 0.25
    return 0.0


class PCGMapper:
    """Map BG rows to FS labels through official-PCG semantic categories."""

    def __init__(
        self, rows: list[Any], fs_lines: list[Any], regime: str = "modern"
    ) -> None:
        self.rows = rows
        self.fs_lines = fs_lines
        self.regime = regime
        observations = [
            AccountObservation(index, row.account, row.label, row.amount)
            for index, row in enumerate(rows)
        ]
        self.decisions: dict[int, RuleDecision] = classify_observations(
            observations, regime
        )
        self.assigned_categories: dict[int, str | None] = {
            index: decision.category for index, decision in self.decisions.items()
        }
        self.justified_lines: set[int] = set()
        self.gross_up_lines: set[int] = set()
        self.semantic_only_lines: set[int] = set()
        self.row_line_indexes: dict[int, int] = {}
        self.gross_up_adjustments: list[dict[str, Any]] = []
        self.contextual_resolved = 0

    def category_value(self, category: str, rows: list[Any]) -> float:
        value = sum(row.amount for row in rows)
        if category in LIABILITY_CATEGORIES or category in INCOME_CATEGORIES:
            return -value
        return value

    def category_aliases(self, category: str) -> tuple[str, ...]:
        return CATEGORY_ALIASES.get(category, ())

    def category_label_score(self, category: str, label: str) -> float:
        general = best_alias_similarity(self.category_aliases(category), label)
        primary = best_alias_similarity(
            PRIMARY_CATEGORY_ALIASES.get(category, ()), label
        )
        return max(general, min(1.0, primary + 0.20))

    def category_candidate(
        self, category: str, rows: list[Any]
    ) -> tuple[int | None, float, float, float]:
        aliases = self.category_aliases(category)
        expected = self.category_value(category, rows)
        best: tuple[int | None, float, float, float] = (None, 0.0, 0.0, 0.0)
        for index, line in enumerate(self.fs_lines):
            label_score = self.category_label_score(category, line.label)
            amount_score = amount_similarity(expected, line.amount)
            score = 0.60 * label_score + 0.40 * amount_score
            category_total = (
                category in SUMMARY_MAPPABLE_CATEGORIES
                and line.summary
                and label_score >= 0.50
                and amount_score >= 0.98
            )
            if line.summary and not category_total:
                score *= 0.25
            if score > best[1]:
                best = (index, score, label_score, amount_score)
        return best

    def best_leaf_line(self, category: str) -> tuple[int, Any] | None:
        """Find the detailed FS line that most clearly names a semantic category."""

        aliases = self.category_aliases(category)
        candidates = [
            (self.category_label_score(category, line.label), index, line)
            for index, line in enumerate(self.fs_lines)
            if not line.summary and line.amount is not None
        ]
        if not candidates:
            return None
        label_score, index, line = max(candidates, key=lambda item: item[0])
        return (index, line) if label_score >= 0.50 else None

    def resolve_contextual_by_residual(
        self, contextual_category: str, target_categories: tuple[str, ...]
    ) -> None:
        """Allocate ambiguous account groups only when FS residuals prove them."""

        contextual_groups: dict[str, list[int]] = defaultdict(list)
        for index, category in self.assigned_categories.items():
            if category == contextual_category:
                contextual_groups[account_identity(self.rows[index].account)].append(index)
        groups = list(contextual_groups.values())
        if not groups or len(groups) > 14:
            return

        targets: dict[str, tuple[float, int]] = {}
        for category in target_categories:
            candidate = self.best_leaf_line(category)
            if candidate is None:
                continue
            line_index, line = candidate
            base_rows = [
                self.rows[index]
                for index, assigned in self.assigned_categories.items()
                if assigned == category
            ]
            residual = line.amount - self.category_value(category, base_rows)
            targets[category] = (residual, line_index)
        if not targets:
            return

        group_values = [sum(self.rows[index].amount for index in group) for group in groups]
        subset_candidates: dict[str, list[tuple[set[int], float]]] = defaultdict(list)
        for category, (residual, _) in targets.items():
            if abs(residual) <= 0.01:
                continue
            aliases = self.category_aliases(category)
            for size in range(1, len(groups) + 1):
                for subset in itertools.combinations(range(len(groups)), size):
                    if amount_similarity(
                        sum(group_values[index] for index in subset), residual
                    ) < 0.98:
                        continue
                    label_score = sum(
                        self.category_label_score(
                            category,
                            " ".join(self.rows[row_index].label for row_index in groups[index]),
                        )
                        for index in subset
                    ) / len(subset)
                    subset_candidates[category].append((set(subset), label_score))

        choices: list[tuple[set[int], float, dict[str, set[int]]]] = [
            (set(), 0.0, {})
        ]
        for category in target_categories:
            expanded = list(choices)
            for used, score, assignments in choices:
                for subset, label_score in subset_candidates.get(category, []):
                    if used & subset:
                        continue
                    expanded.append(
                        (
                            used | subset,
                            score + label_score,
                            {**assignments, category: subset},
                        )
                    )
            choices = expanded

        choices = [choice for choice in choices if choice[0]]
        if not choices:
            return

        _, _, assignments = max(choices, key=lambda item: (len(item[0]), item[1]))
        for category, subset in assignments.items():
            for group_index in subset:
                for row_index in groups[group_index]:
                    self.assigned_categories[row_index] = category
                    self.contextual_resolved += 1

    def resolve_contextual_personnel(self) -> None:
        """Allocate 648/649 only when salary/social statement residuals prove it."""

        self.resolve_contextual_by_residual(
            "contextual_personnel", ("salaries", "social_charges")
        )

    def resolve_contextual_financial_offsets(self) -> None:
        """Allocate credit-agios products only when a detailed FS residual proves it."""

        self.resolve_contextual_by_residual(
            "contextual_financial_offset",
            ("financial_charges", "financial_other_interest_income"),
        )

    def direct_category_matches(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        matched = 0
        for category, rows in grouped_rows.items():
            index, score, label_score, amount_score = self.category_candidate(category, rows)
            if index is None:
                continue
            line = self.fs_lines[index]
            category_total = (
                category in SUMMARY_MAPPABLE_CATEGORIES
                and line.summary
                and label_score >= 0.50
                and amount_score >= 0.98
            )
            exact_composition = (
                (not line.summary or category_total)
                and label_score >= 0.65
                and amount_score >= 0.98
                and score >= 0.50
            )
            missing_amount_exact_label = (
                not line.summary and line.amount is None and label_score >= 0.82
            )
            if not (exact_composition or missing_amount_exact_label):
                continue
            for row in rows:
                row.mapping = line.label
                self.row_line_indexes[id(row)] = index
                matched += 1
            self.justified_lines.add(index)
        return matched

    def reconcile_combined_categories(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        matched = 0
        unresolved = {
            category: rows
            for category, rows in grouped_rows.items()
            if rows and all(not row.mapping for row in rows)
        }
        for line_index, line in enumerate(self.fs_lines):
            if line.summary or line.amount is None:
                continue
            candidates: list[tuple[float, str, list[Any], float]] = []
            for category, rows in unresolved.items():
                if any(row.mapping for row in rows):
                    continue
                label_score = self.category_label_score(category, line.label)
                if label_score >= 0.18:
                    candidates.append(
                        (label_score, category, rows, self.category_value(category, rows))
                    )
            candidates.sort(reverse=True, key=lambda item: item[0])
            candidates = candidates[:10]

            best_combo: tuple[float, tuple[Any, ...]] | None = None
            for size in range(2, min(5, len(candidates)) + 1):
                for combo in itertools.combinations(candidates, size):
                    amount_score = amount_similarity(
                        sum(item[3] for item in combo), line.amount
                    )
                    if amount_score < 0.98:
                        continue
                    average_label = sum(item[0] for item in combo) / len(combo)
                    score = 0.55 * average_label + 0.45 * amount_score
                    if average_label >= 0.18 and (
                        best_combo is None or score > best_combo[0]
                    ):
                        best_combo = (score, combo)
            if best_combo is None:
                continue
            for _, category, rows, _ in best_combo[1]:
                for row in rows:
                    row.mapping = line.label
                    self.row_line_indexes[id(row)] = line_index
                    matched += 1
                unresolved.pop(category, None)
            self.justified_lines.add(line_index)
        return matched

    def exact_row_fallback(self) -> int:
        matched = 0
        for row_index, row in enumerate(self.rows):
            if row.mapping:
                continue
            decision = self.decisions[row_index]
            category = self.assigned_categories[row_index]
            unresolved_context = decision.requires_context and category == decision.category
            if not category or category in NON_FS_CATEGORIES or unresolved_context:
                continue
            aliases = self.category_aliases(category)
            expected = self.category_value(category, [row])
            best: tuple[int | None, float] = (None, 0.0)
            for line_index, line in enumerate(self.fs_lines):
                if line.summary:
                    continue
                amount_score = amount_similarity(expected, line.amount)
                label_score = max(
                    text_similarity(row.label, line.label),
                    self.category_label_score(category, line.label),
                )
                score = 0.55 * label_score + 0.45 * amount_score
                if amount_score >= 0.98 and label_score >= 0.65 and score > best[1]:
                    best = (line_index, score)
            if best[0] is not None:
                line = self.fs_lines[best[0]]
                row.mapping = line.label
                self.row_line_indexes[id(row)] = best[0]
                self.justified_lines.add(best[0])
                matched += 1
        return matched

    def reconcile_gross_up_pairs(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        """Map net BG balances when two FS lines prove a gross presentation.

        Standardized trial balances sometimes retain only a net collective
        customer or supplier account. The FS then presents debit and credit
        balances separately. This method maps the observable net balance but
        records that the hidden counter-balance is inferred, not justified.
        """

        pairs = (
            ("clients", "other_debts"),
            ("supplier_debt", "supplier_debit_receivables"),
        )
        matched = 0
        for left_category, right_category in pairs:
            left_candidate = self.best_leaf_line(left_category)
            right_candidate = self.best_leaf_line(right_category)
            if left_candidate is None or right_candidate is None:
                continue
            left_index, left_line = left_candidate
            right_index, right_line = right_candidate
            left_rows = grouped_rows.get(left_category, [])
            right_rows = grouped_rows.get(right_category, [])
            left_bg = self.category_value(left_category, left_rows)
            right_bg = self.category_value(right_category, right_rows)
            left_residual = left_line.amount - left_bg
            right_residual = right_line.amount - right_bg
            scale = max(
                abs(left_line.amount),
                abs(right_line.amount),
                abs(left_residual),
                1.0,
            )
            tolerance = max(2.0, 0.001 * scale)
            if (
                left_residual <= tolerance
                or right_residual <= tolerance
                or abs(left_residual - right_residual) > tolerance
            ):
                continue

            for category, rows, line_index, line in (
                (left_category, left_rows, left_index, left_line),
                (right_category, right_rows, right_index, right_line),
            ):
                for row in rows:
                    if row.mapping:
                        continue
                    row.mapping = line.label
                    self.row_line_indexes[id(row)] = line_index
                    matched += 1
                self.gross_up_lines.add(line_index)

            self.gross_up_adjustments.append(
                {
                    "categories": [left_category, right_category],
                    "fs_lines": [left_line.label, right_line.label],
                    "inferred_hidden_balance": round(
                        (left_residual + right_residual) / 2, 2
                    ),
                    "explanation": (
                        "FS debit and credit lines exceed the standardized BG "
                        "net balances by the same amount"
                    ),
                }
            )
        return matched

    def semantic_group_fallback(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        """Map accounting-certain categories despite a source amount difference.

        These mappings are never counted as justified. They allow the output
        to say where an account belongs while the reconciliation report states
        what the FS amount should have been according to the BG.
        """

        matched = 0
        for category, rows in grouped_rows.items():
            unresolved = [row for row in rows if not row.mapping]
            if not unresolved:
                continue
            row_indexes = [
                index
                for index, assigned in self.assigned_categories.items()
                if assigned == category
            ]
            if any(
                self.decisions[index].requires_context
                and self.assigned_categories[index] == self.decisions[index].category
                for index in row_indexes
            ):
                continue

            aliases = self.category_aliases(category)
            expected = self.category_value(category, rows)
            candidates: list[tuple[float, float, int, Any]] = []
            for line_index, line in enumerate(self.fs_lines):
                if line.summary or line.amount is None:
                    continue
                label_score = self.category_label_score(category, line.label)
                if label_score < 0.82:
                    continue
                amount_distance = abs(line.amount - expected)
                candidates.append((label_score, -amount_distance, line_index, line))
            if not candidates:
                continue
            _, _, line_index, line = max(candidates)
            for row in unresolved:
                row.mapping = line.label
                self.row_line_indexes[id(row)] = line_index
                matched += 1
            self.semantic_only_lines.add(line_index)
        return matched

    def build_reconciliation(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        rows_by_line: dict[int, list[tuple[int, Any]]] = defaultdict(list)
        for row_index, row in enumerate(self.rows):
            line_index = self.row_line_indexes.get(id(row))
            if line_index is not None:
                rows_by_line[line_index].append((row_index, row))

        for line_index, line in enumerate(self.fs_lines):
            if line.summary or line.amount is None or abs(line.amount) <= 0.01:
                continue
            mapped_rows = rows_by_line.get(line_index, [])
            bg_amount = sum(
                self.category_value(
                    self.assigned_categories[row_index] or "", [row]
                )
                for row_index, row in mapped_rows
            )
            difference = line.amount - bg_amount
            if line_index in self.justified_lines and amount_similarity(
                bg_amount, line.amount
            ) >= 0.98:
                status = "exact"
            elif line_index in self.gross_up_lines:
                status = "gross-up inferred"
            elif line_index in self.semantic_only_lines and mapped_rows:
                status = "source difference"
            elif mapped_rows:
                status = "difference"
            else:
                status = "unjustified"
            result.append(
                {
                    "fs_line": line.label,
                    "fs_amount": round(line.amount, 2),
                    "bg_amount": round(bg_amount, 2),
                    "difference": round(difference, 2),
                    "status": status,
                    "bg_rows": len(mapped_rows),
                }
            )
        return result

    def build_category_differences(
        self, grouped_rows: dict[str, list[Any]]
    ) -> list[dict[str, Any]]:
        """Expose BG-to-FS differences even when strict mapping was withheld."""

        candidate_groups: dict[
            tuple[int, ...], list[tuple[str, float]]
        ] = defaultdict(list)
        for category, rows in grouped_rows.items():
            assigned_indexes = {
                self.row_line_indexes[id(row)]
                for row in rows
                if id(row) in self.row_line_indexes
            }
            if assigned_indexes:
                candidate_groups[tuple(sorted(assigned_indexes))].append(
                    (category, self.category_value(category, rows))
                )
                continue

            aliases = self.category_aliases(category)
            scored = [
                (
                    self.category_label_score(category, line.label),
                    line_index,
                    line,
                )
                for line_index, line in enumerate(self.fs_lines)
                if line.amount is not None
                and (
                    not line.summary
                    or category in SUMMARY_MAPPABLE_CATEGORIES
                )
            ]
            if not scored:
                continue
            max_score = max(item[0] for item in scored)
            if max_score < 0.50:
                continue
            expected = self.category_value(category, rows)
            line_indexes = tuple(
                sorted(
                    item[1]
                    for item in scored
                    if item[0] >= max_score - 0.02
                    and item[0] >= 0.50
                )
            )
            candidate_groups[line_indexes].append((category, expected))

        result: list[dict[str, Any]] = []
        for line_indexes, categories in candidate_groups.items():
            expected = sum(item[1] for item in categories)
            fs_amount = sum(self.fs_lines[index].amount for index in line_indexes)
            difference = fs_amount - expected
            exact = amount_similarity(expected, fs_amount) >= 0.98
            if exact:
                continue
            line_labels = " + ".join(
                self.fs_lines[index].label for index in line_indexes
            )
            result.append(
                {
                    "category": " + ".join(item[0] for item in categories),
                    "fs_line": line_labels,
                    "fs_amount": round(fs_amount, 2),
                    "bg_expected": round(expected, 2),
                    "difference": round(difference, 2),
                    "status": "review",
                    "fs_line_indexes": list(line_indexes),
                }
            )
        return result

    def map(self) -> dict[str, Any]:
        self.resolve_contextual_personnel()
        self.resolve_contextual_financial_offsets()
        grouped_rows: dict[str, list[Any]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            category = self.assigned_categories[index]
            if category and category not in NON_FS_CATEGORIES:
                grouped_rows[category].append(row)

        direct_matches = self.direct_category_matches(grouped_rows)
        combined_matches = self.reconcile_combined_categories(grouped_rows)
        gross_up_matches = self.reconcile_gross_up_pairs(grouped_rows)
        row_matches = self.exact_row_fallback()
        semantic_matches = self.semantic_group_fallback(grouped_rows)
        matched = sum(bool(row.mapping) for row in self.rows)
        non_fs = sum(
            self.decisions[index].category in NON_FS_CATEGORIES
            for index in range(len(self.rows))
        )
        contextual = sum(
            self.decisions[index].requires_context for index in range(len(self.rows))
        )
        classified = sum(
            self.decisions[index].category is not None for index in range(len(self.rows))
        )
        return {
            "variant": "official-pcg",
            "rows": len(self.rows),
            "matched": matched,
            "unmatched": len(self.rows) - matched,
            "classified": classified,
            "contextual": contextual,
            "contextual_resolved": self.contextual_resolved,
            "non_fs": non_fs,
            "group_matches": direct_matches,
            "combined_group_matches": combined_matches,
            "gross_up_matches": gross_up_matches,
            "semantic_matches": semantic_matches,
            "gross_up_adjustments": self.gross_up_adjustments,
            "row_matches": row_matches,
            "fs_lines": len(self.fs_lines),
            "fs_leaf_lines": sum(not line.summary for line in self.fs_lines),
            "justified_fs_lines": len(self.justified_lines),
            "reconciliation": self.build_reconciliation(),
            "category_differences": self.build_category_differences(grouped_rows),
            "regime": self.regime,
        }
