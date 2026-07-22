"""Reconcile explicit PCG semantic categories to extracted FS lines."""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

from pcg_rules import (
    AccountObservation,
    CATEGORY_ALIASES,
    RuleDecision,
    account_identity,
    classify_observations,
    detect_chart_profile,
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
    "other_own_funds",
    "concession_grantor_rights",
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
    "m21_apports",
    "m21_investment_surplus",
    "m21_compensation_reserve",
    "m21_retained_surplus",
    "m21_retained_deficit",
    "m21_receipts_to_regularize",
    "scpi_financial_debt",
    "scpi_associate_debt",
    "scpi_operating_debt",
    "scpi_misc_debt",
}

ASSET_CATEGORIES = {
    "asset_capital_uncalled",
    "asset_intangible_establishment",
    "asset_intangible_research",
    "asset_intangible_concessions",
    "asset_intangible_goodwill",
    "asset_intangible_other",
    "asset_intangible_in_progress",
    "asset_land",
    "asset_construction",
    "asset_technical_equipment",
    "asset_other_corporeal",
    "asset_affected_fixed_assets",
    "asset_corporeal_in_progress",
    "asset_participations",
    "asset_related_participation_receivables",
    "asset_portfolio_activity",
    "asset_other_fixed_securities",
    "asset_loans",
    "asset_other_financial",
    "asset_redemption_premium",
    "asset_charges_to_spread",
    "asset_conversion_difference",
    "stock_raw_materials",
    "stock_goods_wip",
    "stock_service_wip",
    "stock_products",
    "stock_merchandise",
    "supplier_advances",
    "clients",
    "supplier_debit_receivables",
    "personnel_receivables",
    "social_bodies_receivables",
    "income_tax_receivable",
    "vat_receivable",
    "other_tax_receivables",
    "other_receivables",
    "prepaid_expenses",
    "marketable_securities",
    "treasury_instruments",
    "cash",
    "contextual_fixed_asset",
    "m21_receivables_hospitalized",
    "m21_receivables_pivot",
    "m21_receivables_third_party",
    "m21_receivables_other",
    "m21_expenses_to_regularize",
    "scpi_rental_property",
    "scpi_property_in_progress",
    "scpi_major_maintenance_provision",
    "scpi_tenant_receivables",
    "scpi_other_receivables",
    "scpi_cash",
    "scpi_net_regularization",
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
    "hospital_activity_revenue",
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
    "exceptional_management_income",
    "exceptional_reversals",
    "asset_disposal_income",
    "m21_vmp_income",
    "m21_exceptional_income_current",
    "m21_exceptional_income_prior",
    "m21_exceptional_income_capital",
    "scpi_rent_income",
    "scpi_recharged_income",
    "scpi_financial_income",
}

EXTERNAL_CHARGE_CATEGORIES = {
    "external_charges",
    "external_purchases_services",
    "external_subcontracting",
    "external_rentals",
    "external_maintenance",
    "external_insurance",
    "external_studies",
    "external_personnel",
    "external_fees",
    "external_advertising",
    "external_transport",
    "external_travel",
    "external_telecommunications",
    "external_banking",
    "external_royalties",
    "external_other",
}

EXPENSE_CATEGORIES = {
    "purchases_merchandise",
    "stock_variation_merchandise",
    "purchases_raw_materials",
    "stock_variation_raw_materials",
    "external_charges",
    "taxes",
    "salaries",
    "social_charges",
    "other_operating_charges",
    "amortization_operating",
    "depreciation_operating",
    "provisions_operating",
    "financial_charges",
    "financial_negative_exchange",
    "financial_dotations",
    "exceptional_charges",
    "exceptional_management_charges",
    "exceptional_dotations",
    "employee_participation",
    "income_tax",
    "asset_disposal_cost",
    "contextual_purchase_adjustment",
    "contextual_personnel",
    "m21_stocked_purchases",
    "m21_other_supply_variation",
    "m21_nonstocked_purchases",
    "m21_payroll_taxes",
    "m21_other_taxes",
    "m21_personnel_remuneration",
    "m21_fixed_asset_depreciation",
    "m21_current_asset_depreciation",
    "m21_operating_provisions",
    "m21_exceptional_charge_current",
    "m21_exceptional_charge_prior",
    "m21_exceptional_charge_capital",
    "m21_regulated_provision_expense",
    "scpi_recharged_property_expense",
    "scpi_other_property_expense",
    "scpi_management_commission",
    "scpi_company_operating_expense",
    "scpi_operating_amortization",
    "scpi_other_operating_expense",
    "scpi_financial_expense",
} | EXTERNAL_CHARGE_CATEGORIES

RESULT_CATEGORIES = INCOME_CATEGORIES | EXPENSE_CATEGORIES | {
    "contextual_financial_offset",
    "contextual_special_account",
}

FINANCIAL_RESULT_CATEGORIES = {
    "financial_participation_income",
    "financial_other_fixed_income",
    "financial_other_interest_income",
    "financial_positive_exchange",
    "financial_negative_exchange",
    "financial_vmp_disposal_income",
    "financial_income",
    "financial_charges",
    "financial_reversals",
    "financial_dotations",
    "contextual_financial_offset",
    "m21_vmp_income",
    "scpi_financial_income",
    "scpi_financial_expense",
}

EXCEPTIONAL_RESULT_CATEGORIES = {
    "exceptional_income",
    "exceptional_charges",
    "exceptional_management_income",
    "exceptional_management_charges",
    "exceptional_reversals",
    "exceptional_dotations",
    "m21_exceptional_income_current",
    "m21_exceptional_income_prior",
    "m21_exceptional_income_capital",
    "m21_exceptional_charge_current",
    "m21_exceptional_charge_prior",
    "m21_exceptional_charge_capital",
    "m21_regulated_provision_expense",
}

TAX_RESULT_CATEGORIES = {"employee_participation", "income_tax"}

NON_FS_CATEGORIES = {
    "non_fs_internal_transfer",
    "non_fs_net_zero_account",
    "non_fs_analytic",
}

NEGATIVE_SCPI_LIABILITY_CATEGORIES = {
    "scpi_financial_debt",
    "scpi_associate_debt",
    "scpi_operating_debt",
    "scpi_misc_debt",
}

EXCEPTIONAL_CATEGORIES = EXCEPTIONAL_RESULT_CATEGORIES

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

GENERIC_LABEL_WORDS = {
    "autres",
    "charge",
    "charges",
    "creance",
    "creances",
    "dette",
    "dettes",
    "immobilisation",
    "immobilisations",
    "montant",
    "net",
    "produit",
    "produits",
    "total",
}

CATEGORY_FORBIDDEN_LABEL_WORDS: dict[str, frozenset[str]] = {
    "personnel_debt": frozenset({"etat", "organismes", "taxes"}),
    "social_bodies_debt": frozenset({"etat", "personnel", "taxes"}),
    "income_tax_debt": frozenset({"organismes", "personnel"}),
    "vat_debt": frozenset({"organismes", "personnel"}),
    "other_tax_debt": frozenset({"organismes", "personnel"}),
    "personnel_receivables": frozenset({"etat", "fournisseurs", "organismes"}),
    "social_bodies_receivables": frozenset({"etat", "fournisseurs", "personnel"}),
    "income_tax_receivable": frozenset({"fournisseurs", "organismes", "personnel"}),
    "vat_receivable": frozenset({"fournisseurs", "organismes", "personnel"}),
    "other_tax_receivables": frozenset({"fournisseurs", "organismes", "personnel"}),
    "financial_charges": frozenset({"dotations", "reprises"}),
    "financial_dotations": frozenset({"reprises"}),
    "financial_reversals": frozenset({"dotations"}),
    "amortization_operating": frozenset({"reprises"}),
    "depreciation_operating": frozenset({"reprises"}),
    "provisions_operating": frozenset({"reprises"}),
    "reversals_operating": frozenset({"dotations"}),
    "exceptional_dotations": frozenset({"gestion"}),
    "exceptional_management_charges": frozenset({"dotations", "cedes", "cedees"}),
    "exceptional_reversals": frozenset({"gestion", "cession", "cessions"}),
    "exceptional_management_income": frozenset({"reprises", "cession", "cessions"}),
    "exceptional_income": frozenset({"capital", "gestion", "cession", "cessions"}),
    "exceptional_charges": frozenset({"capital", "gestion", "dotations"}),
}


@dataclass(frozen=True)
class CompositeRule:
    """An authorized many-PCG-categories-to-one-FS-line presentation rule."""

    name: str
    aliases: tuple[str, ...]
    categories: frozenset[str]
    regimes: frozenset[str] = frozenset({"legacy", "modern"})
    min_categories: int = 2
    allow_summary: bool = False
    chart_profiles: frozenset[str] = frozenset({"pcg", "m21"})


COMPOSITE_RULES = (
    CompositeRule(
        "other_receivables",
        ("autres creances",),
        frozenset(
            {
                "supplier_debit_receivables",
                "personnel_receivables",
                "social_bodies_receivables",
                "income_tax_receivable",
                "vat_receivable",
                "other_tax_receivables",
                "other_receivables",
            }
        ),
    ),
    CompositeRule(
        "tax_and_social_debt",
        ("dettes fiscales et sociales", "taxes and social security debts"),
        frozenset(
            {
                "personnel_debt",
                "social_bodies_debt",
                "income_tax_debt",
                "vat_debt",
                "other_tax_debt",
                "tax_social_debt",
            }
        ),
    ),
    CompositeRule(
        "production_sold_services",
        ("production vendue de services", "production vendue services"),
        frozenset(
            {
                "production_services",
                "contextual_ancillary_revenue",
                "contextual_revenue",
            }
        ),
    ),
    CompositeRule(
        "production_sold",
        ("production vendue", "production vendue biens et services"),
        frozenset(
            {
                "production_goods",
                "production_services",
                "contextual_ancillary_revenue",
                "contextual_revenue",
            }
        ),
    ),
    CompositeRule(
        "nonprofit_sales_goods",
        ("ventes de biens", "vente de biens"),
        frozenset({"sales_merchandise", "production_goods"}),
        min_categories=1,
    ),
    CompositeRule(
        "nonprofit_sales_goods_and_services",
        ("ventes de biens et services", "vente de biens et services"),
        frozenset(
            {
                "sales_merchandise",
                "production_goods",
                "production_services",
                "contextual_ancillary_revenue",
                "contextual_revenue",
            }
        ),
        min_categories=1,
    ),
    CompositeRule(
        "raw_material_purchases",
        ("achats de matieres premieres et autres approvisionnements",),
        frozenset({"purchases_raw_materials", "contextual_purchase_adjustment"}),
    ),
    CompositeRule(
        "external_purchases_and_charges",
        (
            "autres achats et charges externes",
            "other purchases and expenses",
            "services exterieurs et autres",
        ),
        frozenset(EXTERNAL_CHARGE_CATEGORIES | {"contextual_purchase_adjustment"}),
    ),
    CompositeRule(
        "operating_depreciation_and_provisions",
        (
            "amortissements et provisions",
            "dotations aux amortissements depreciations et provisions",
            "dotations d exploitation",
        ),
        frozenset(
            {
                "amortization_operating",
                "depreciation_operating",
                "provisions_operating",
            }
        ),
    ),
    CompositeRule(
        "legacy_other_operating_income",
        ("autres produits",),
        frozenset({"other_operating_income", "legacy_charge_transfers"}),
        frozenset({"legacy"}),
    ),
    CompositeRule(
        "legacy_reversals_and_transfers",
        (
            "reprises sur provisions et amortissements transferts de charges",
            "reprises sur amortissements depreciations provisions transferts de charges",
            "reprises sur depreciations et provisions transferts de charges",
        ),
        frozenset({"reversals_operating", "legacy_charge_transfers"}),
        frozenset({"legacy"}),
        min_categories=1,
    ),
    CompositeRule(
        "generic_stock_variation",
        ("variation de stocks", "variations de stocks"),
        frozenset(
            {"stock_variation_merchandise", "stock_variation_raw_materials"}
        ),
    ),
    CompositeRule(
        "aggregated_financial_debt",
        ("emprunts et dettes financieres divers",),
        frozenset(
            {
                "bank_loans",
                "bank_debt",
                "other_financial_debt",
                "associate_financial_debt",
            }
        ),
    ),
    CompositeRule(
        "cash_and_treasury_instruments",
        ("disponibilites",),
        frozenset({"cash", "treasury_instruments"}),
    ),
    CompositeRule(
        "financial_fixed_income_in_other_interest",
        ("autres interets et produits assimiles",),
        frozenset(
            {"financial_other_fixed_income", "financial_other_interest_income"}
        ),
        min_categories=1,
    ),
    CompositeRule(
        "in_progress_with_other_intangibles",
        ("autres immobilisations incorporelles",),
        frozenset({"asset_intangible_in_progress"}),
        min_categories=1,
    ),
    CompositeRule(
        "abbreviated_intangible_assets",
        ("immobilisations incorporelles",),
        frozenset(
            {
                "asset_intangible_establishment",
                "asset_intangible_research",
                "asset_intangible_concessions",
                "asset_intangible_goodwill",
                "asset_intangible_other",
                "asset_intangible_in_progress",
            }
        ),
    ),
    CompositeRule(
        "abbreviated_corporeal_assets",
        ("immobilisations corporelles",),
        frozenset(
            {
                "asset_land",
                "asset_construction",
                "asset_technical_equipment",
                "asset_other_corporeal",
                "asset_corporeal_in_progress",
            }
        ),
    ),
    CompositeRule(
        "abbreviated_financial_assets",
        ("immobilisations financieres",),
        frozenset(
            {
                "asset_participations",
                "asset_related_participation_receivables",
                "asset_portfolio_activity",
                "asset_other_fixed_securities",
                "asset_loans",
                "asset_other_financial",
            }
        ),
    ),
    CompositeRule(
        "abbreviated_stocks",
        ("stocks et en cours", "stocks en cours"),
        frozenset(
            {
                "stock_raw_materials",
                "stock_goods_wip",
                "stock_service_wip",
                "stock_products",
                "stock_merchandise",
            }
        ),
        min_categories=1,
    ),
    CompositeRule(
        "legacy_capital_exceptional_income",
        ("produits exceptionnels sur operations en capital",),
        frozenset({"asset_disposal_income", "exceptional_income"}),
        frozenset({"legacy"}),
        min_categories=1,
    ),
    CompositeRule(
        "legacy_capital_exceptional_charges",
        ("charges exceptionnelles sur operations en capital",),
        frozenset({"asset_disposal_cost", "exceptional_charges"}),
        frozenset({"legacy"}),
        min_categories=1,
    ),
    CompositeRule(
        "exceptional_income_aggregate",
        ("produits exceptionnels", "total produits exceptionnels"),
        frozenset(
            {
                "exceptional_income",
                "exceptional_management_income",
                "exceptional_reversals",
                "asset_disposal_income",
            }
        ),
        min_categories=1,
        allow_summary=True,
    ),
    CompositeRule(
        "exceptional_charge_aggregate",
        ("charges exceptionnelles", "total charges exceptionnelles"),
        frozenset(
            {
                "exceptional_charges",
                "exceptional_management_charges",
                "exceptional_dotations",
                "asset_disposal_cost",
            }
        ),
        min_categories=1,
        allow_summary=True,
    ),
    CompositeRule(
        "financial_income_aggregate",
        ("produits financiers", "total produits financiers"),
        frozenset(
            {
                "financial_participation_income",
                "financial_other_fixed_income",
                "financial_other_interest_income",
                "financial_positive_exchange",
                "financial_vmp_disposal_income",
                "financial_income",
                "financial_reversals",
            }
        ),
        min_categories=1,
        allow_summary=True,
    ),
    CompositeRule(
        "financial_charge_aggregate",
        ("charges financieres", "total charges financieres"),
        frozenset(
            {
                "financial_charges",
                "financial_negative_exchange",
                "financial_dotations",
            }
        ),
        min_categories=1,
        allow_summary=True,
    ),
    CompositeRule(
        "m21_other_financial_assets",
        ("autres",),
        frozenset({"asset_other_financial"}),
        min_categories=1,
        chart_profiles=frozenset({"m21"}),
    ),
    CompositeRule(
        "m21_other_redevables",
        ("autres",),
        frozenset(
            {
                "m21_receivables_other",
                "supplier_debit_receivables",
                "supplier_advances",
                "personnel_receivables",
                "vat_receivable",
            }
        ),
        chart_profiles=frozenset({"m21"}),
    ),
    CompositeRule(
        "m21_misc_receivables",
        ("creances diverses",),
        frozenset({"other_receivables", "other_tax_receivables"}),
        chart_profiles=frozenset({"m21"}),
    ),
    CompositeRule(
        "scpi_accounting_equity",
        ("capitaux propres comptables",),
        frozenset(
            {
                "equity_capital",
                "equity_premiums",
                "equity_retained_earnings",
                "equity_result",
            }
        ),
        min_categories=2,
        allow_summary=True,
        chart_profiles=frozenset({"scpi"}),
    ),
)


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


def composite_label_score(rule: CompositeRule, label: str) -> float:
    """Require a specific multi-word line name before applying a composite."""

    label_words = set(words(label))
    best = 0.0
    for alias in rule.aliases:
        alias_words = set(words(alias))
        if alias_words == label_words and alias_words:
            return 1.0
        overlap = alias_words & label_words
        # A shortened caption made only of generic words is not accounting
        # evidence for a statutory composite. In particular, ``Autres
        # charges`` must never stand in for ``Autres achats et charges
        # externes`` merely because the amount happens to agree.
        distinctive_overlap = overlap - GENERIC_LABEL_WORDS
        if len(overlap) < 2 or not distinctive_overlap:
            continue
        best = max(best, text_similarity(alias, label))
    return best


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


def scaled_amount_similarity(
    expected: float | None,
    displayed: float | None,
    unit_scale: float,
) -> float:
    """Compare a BG amount with an FS amount displayed in euros, kEUR or MEUR."""

    if expected is None or displayed is None:
        return 0.0
    actual = displayed * unit_scale
    # Published statements rounded to the nearest displayed unit can differ by
    # half that unit from the detailed ledger without being a reconciliation
    # difference.
    if unit_scale > 1.0 and abs(expected - actual) <= 0.51 * unit_scale:
        return 1.0
    return amount_similarity(expected, actual)


def category_statement_family(category: str) -> str:
    if category in ASSET_CATEGORIES:
        return "asset"
    if category in LIABILITY_CATEGORIES:
        return "liability"
    if category in RESULT_CATEGORIES:
        return "result"
    return ""


def category_result_section(category: str, regime: str) -> str:
    if category in FINANCIAL_RESULT_CATEGORIES:
        return "financial"
    if category in EXCEPTIONAL_RESULT_CATEGORIES:
        return "exceptional"
    if category in TAX_RESULT_CATEGORIES:
        return "tax"
    if category in {"asset_disposal_income", "asset_disposal_cost"}:
        return "operating" if regime == "modern" else "exceptional"
    if category in RESULT_CATEGORIES:
        return "operating"
    return ""


def statement_compatible(category: str, line: Any, regime: str = "modern") -> bool:
    """Reject mappings across statements and result-statement sections."""

    category_family = category_statement_family(category)
    line_family = str(getattr(line, "statement_family", "") or "")
    if category_family and line_family and category_family != line_family:
        return False
    category_section = category_result_section(category, regime)
    line_section = str(getattr(line, "result_section", "") or "")
    return not category_section or not line_section or category_section == line_section


IFRS_CONSOLIDATED_SIGNATURES = (
    "ecarts d acquisition",
    "droits d utilisation",
    "interets minoritaires",
    "part du groupe",
    "actifs non courants",
    "passifs non courants",
    "dettes locatives",
)


def detect_fs_framework(lines: Iterable[Any]) -> tuple[str, list[str]]:
    """Identify statements whose presentation is not an annual PCG model.

    This is deliberately signature-based and conservative.  A single
    translated or customized caption cannot switch framework; at least three
    independent consolidated/IFRS presentation markers are required.
    """

    line_list = list(lines)
    labels = " | ".join(normalize_text(line.label) for line in line_list)
    signatures = [marker for marker in IFRS_CONSOLIDATED_SIGNATURES if marker in labels]
    scopes = {str(getattr(line, "scope", "") or "").lower() for line in line_list}
    if len(signatures) >= 3:
        return "ifrs_consolidated", signatures
    if "consolidated" in scopes:
        return "consolidated_other", signatures
    return "pcg_annual", signatures


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
        self.chart_profile = detect_chart_profile(observations)
        self.fs_framework, self.fs_framework_signatures = detect_fs_framework(fs_lines)
        self.decisions: dict[int, RuleDecision] = classify_observations(
            observations, regime, self.chart_profile
        )
        self.assigned_categories: dict[int, str | None] = {
            index: decision.category for index, decision in self.decisions.items()
        }
        self.fs_unit_scale = self.detect_fs_unit_scale()
        self.fs_expense_sign = self.detect_fs_expense_sign()
        self.fs_liability_sign = -1 if self.chart_profile == "scpi" else 1
        self.justified_lines: set[int] = set()
        self.gross_up_lines: set[int] = set()
        self.semantic_only_lines: set[int] = set()
        self.row_line_indexes: dict[int, int] = {}
        self.row_methods: dict[int, str] = {}
        self.row_mapping_details: dict[int, dict[str, Any]] = {}
        self.gross_up_adjustments: list[dict[str, Any]] = []
        self.combined_match_details: list[dict[str, Any]] = []
        self.derived_line_details: dict[int, dict[str, Any]] = {}
        self.supplemental_line_details: dict[int, dict[str, Any]] = {}
        self.contextual_resolved = 0
        self.source_alignment: dict[str, Any] = {
            "status": "not_assessed",
            "exact_primary_lines": 0,
            "statement_families": [],
            "semantic_mapping_allowed": False,
        }

    def detect_fs_unit_scale(self) -> float:
        """Infer EUR/kEUR/MEUR only from several independent exact matches.

        A scale is accepted only when at least three strongly named non-zero
        lines across two statement families reconcile after scaling and it
        clearly beats the unscaled and alternative-scale evidence. This keeps
        unit handling generic while failing closed on weak evidence.
        """

        grouped_rows: dict[str, list[Any]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            category = self.assigned_categories[index]
            if category and category not in NON_FS_CATEGORIES:
                grouped_rows[category].append(row)

        evidence: dict[float, tuple[int, int]] = {}
        for scale in (1.0, 1_000.0, 1_000_000.0):
            matched_categories: set[str] = set()
            families: set[str] = set()
            for category, rows in grouped_rows.items():
                expected = self.category_value(category, rows)
                if abs(expected) <= 0.01:
                    continue
                for line in self.fs_lines:
                    if (
                        line.summary
                        or line.amount is None
                        or abs(line.amount) <= 0.01
                        or not statement_compatible(category, line, self.regime)
                        or self.category_label_score(category, line.label) < 0.82
                    ):
                        continue
                    if scaled_amount_similarity(expected, line.amount, scale) >= 0.98:
                        matched_categories.add(category)
                        if line.statement_family:
                            families.add(line.statement_family)
                        break
            evidence[scale] = (len(matched_categories), len(families))

        best_scale = max(evidence, key=lambda scale: evidence[scale])
        best_matches, best_families = evidence[best_scale]
        runner_up = max(
            (matches for scale, (matches, _) in evidence.items() if scale != best_scale),
            default=0,
        )
        if (
            best_scale != 1.0
            and best_matches >= 3
            and best_families >= 2
            and best_matches >= runner_up + 2
        ):
            return best_scale
        return 1.0

    def detect_fs_expense_sign(self) -> int:
        """Infer whether result-statement expenses are displayed as negatives.

        PCG models normally show charges as positive amounts, but management or
        translated statements sometimes present them with a minus sign.  The
        alternate convention is accepted only from several independently
        named, exactly reconciling expense categories; a single unusual line
        or a naturally signed stock variation cannot switch the convention.
        """

        grouped_rows: dict[str, list[Any]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            category = self.assigned_categories[index]
            if category in EXPENSE_CATEGORIES:
                grouped_rows[category].append(row)

        evidence: dict[int, set[str]] = {1: set(), -1: set()}
        for category, rows in grouped_rows.items():
            expected = self.category_value(category, rows)
            if abs(expected) <= 0.01:
                continue
            for line in self.fs_lines:
                if (
                    line.summary
                    or line.amount is None
                    or abs(line.amount) <= 0.01
                    or not statement_compatible(category, line, self.regime)
                    or self.category_label_score(category, line.label) < 0.82
                ):
                    continue
                normal = scaled_amount_similarity(
                    expected, line.amount, self.fs_unit_scale
                )
                inverted = scaled_amount_similarity(
                    expected, -line.amount, self.fs_unit_scale
                )
                if normal >= 0.98 and inverted < 0.98:
                    evidence[1].add(category)
                elif inverted >= 0.98 and normal < 0.98:
                    evidence[-1].add(category)

        if len(evidence[-1]) >= 3 and len(evidence[-1]) >= len(evidence[1]) + 2:
            return -1
        return 1

    def presentation_sign_for(
        self, categories: str | Iterable[str] | None
    ) -> int:
        if categories is None:
            return 1
        values = {categories} if isinstance(categories, str) else set(categories)
        if values and values <= EXPENSE_CATEGORIES:
            return self.fs_expense_sign
        if values and values <= NEGATIVE_SCPI_LIABILITY_CATEGORIES:
            return self.fs_liability_sign
        return 1

    def line_amount_similarity(
        self,
        expected: float | None,
        displayed: float | None,
        categories: str | Iterable[str] | None = None,
    ) -> float:
        adjusted = (
            displayed * self.presentation_sign_for(categories)
            if displayed is not None
            else None
        )
        return scaled_amount_similarity(expected, adjusted, self.fs_unit_scale)

    def line_amount_in_bg_units(
        self, line: Any, categories: str | Iterable[str] | None = None
    ) -> float | None:
        if line.amount is None:
            return None
        return (
            line.amount
            * self.fs_unit_scale
            * self.presentation_sign_for(categories)
        )

    def amount_in_fs_units(
        self, amount: float, categories: str | Iterable[str] | None = None
    ) -> float:
        return (
            amount
            / self.fs_unit_scale
            * self.presentation_sign_for(categories)
        )

    def mapping_conflicts(self, categories: Iterable[str], line: Any) -> list[str]:
        """Describe statement/section conflicts without blocking a fallback."""

        conflicts: set[str] = set()
        line_family = str(getattr(line, "statement_family", "") or "")
        line_section = str(getattr(line, "result_section", "") or "")
        for category in categories:
            family = category_statement_family(category)
            section = category_result_section(category, self.regime)
            if family and line_family and family != line_family:
                conflicts.add(f"{family}->{line_family}")
            elif (
                family == "result"
                and line_family == "result"
                and section
                and line_section
                and section != line_section
            ):
                conflicts.add(f"result:{section}->result:{line_section}")
        return sorted(conflicts)

    def record_row_mapping(
        self,
        row: Any,
        line_index: int,
        method: str,
        confidence: float,
        categories: Iterable[str],
        participating_accounts: Iterable[Any],
        expected: float | None,
    ) -> None:
        """Assign one BG row and retain machine-readable mapping evidence."""

        line = self.fs_lines[line_index]
        category_list = sorted({str(value) for value in categories if value})
        account_list = list(dict.fromkeys(str(value) for value in participating_accounts))
        amount_difference = None
        if line.amount is not None and expected is not None:
            amount_difference = round(
                line.amount - self.amount_in_fs_units(expected, category_list), 2
            )
        row.mapping = line.label
        self.row_line_indexes[id(row)] = line_index
        self.row_methods[id(row)] = method
        self.row_mapping_details[id(row)] = {
            "method": method,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "amount_difference": amount_difference,
            "conflicting_statement_families": self.mapping_conflicts(
                category_list, line
            ),
            "participating_accounts": account_list,
            "participating_categories": category_list,
        }

    def category_value(self, category: str, rows: list[Any]) -> float:
        value = sum(row.amount for row in rows)
        if category in LIABILITY_CATEGORIES or category in INCOME_CATEGORIES:
            return -value
        return value

    def category_aliases(self, category: str) -> tuple[str, ...]:
        return CATEGORY_ALIASES.get(category, ())

    def category_label_is_specific(self, category: str, label: str) -> bool:
        label_words = set(words(label))
        aliases = self.category_aliases(category) + PRIMARY_CATEGORY_ALIASES.get(
            category, ()
        )
        for alias in aliases:
            alias_words = set(words(alias))
            if alias_words == label_words and alias_words:
                return True
            overlap = alias_words & label_words
            if any(word not in GENERIC_LABEL_WORDS for word in overlap):
                return True
        return False

    def category_label_score(self, category: str, label: str) -> float:
        if set(words(label)) & CATEGORY_FORBIDDEN_LABEL_WORDS.get(
            category, frozenset()
        ):
            return 0.0
        general = best_alias_similarity(self.category_aliases(category), label)
        primary = best_alias_similarity(
            PRIMARY_CATEGORY_ALIASES.get(category, ()), label
        )
        score = max(general, min(1.0, primary + 0.20))
        return score if self.category_label_is_specific(category, label) else min(score, 0.49)

    def category_candidate(
        self, category: str, rows: list[Any]
    ) -> tuple[int | None, float, float, float]:
        aliases = self.category_aliases(category)
        expected = self.category_value(category, rows)
        best: tuple[int | None, float, float, float] = (None, 0.0, 0.0, 0.0)
        for index, line in enumerate(self.fs_lines):
            if not statement_compatible(category, line, self.regime):
                continue
            label_score = self.category_label_score(category, line.label)
            amount_score = self.line_amount_similarity(expected, line.amount, category)
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
            if not line.summary
            and line.amount is not None
            and statement_compatible(category, line, self.regime)
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
            residual = (
                self.line_amount_in_bg_units(line, category)
                - self.category_value(category, base_rows)
            )
            targets[category] = (residual, line_index)
        if not targets:
            return

        subset_candidates: dict[str, list[tuple[set[int], float]]] = defaultdict(list)
        for category, (residual, _) in targets.items():
            if abs(residual) <= 0.01:
                continue
            aliases = self.category_aliases(category)
            for size in range(1, len(groups) + 1):
                for subset in itertools.combinations(range(len(groups)), size):
                    subset_value = sum(
                        self.category_value(
                            category,
                            [self.rows[row_index] for row_index in groups[index]],
                        )
                        for index in subset
                    )
                    if amount_similarity(
                        subset_value, residual
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

    def resolve_contextual_revenue(self) -> None:
        """Allocate ancillary/generic revenue only when an FS residual proves it."""

        targets = (
            "production_goods",
            "production_services",
            "other_operating_income",
        )
        self.resolve_contextual_by_residual("contextual_ancillary_revenue", targets)
        self.resolve_contextual_by_residual("contextual_revenue", targets)

    def direct_category_matches(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        matched = 0
        for category, rows in grouped_rows.items():
            index, score, label_score, amount_score = self.category_candidate(category, rows)
            if index is None:
                continue
            line = self.fs_lines[index]
            if line.summary:
                continue
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
            expected = self.category_value(category, rows)
            method = "direct_exact" if line.amount is not None else "direct_label_only"
            for row in rows:
                self.record_row_mapping(
                    row,
                    index,
                    method,
                    min(label_score, amount_score) if line.amount is not None else label_score,
                    [category],
                    [item.account for item in rows],
                    expected,
                )
                matched += 1
            self.justified_lines.add(index)
        return matched

    def reconcile_combined_categories(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        """Apply only authorized statutory presentation combinations.

        Exact amount equality is necessary but not sufficient: categories may
        only be combined when an explicit PCG presentation rule names both the
        target FS line and every participating accounting category.
        """

        matched = 0
        unresolved = {
            category: rows
            for category, rows in grouped_rows.items()
            if rows and all(not row.mapping for row in rows)
        }
        for line_index, line in enumerate(self.fs_lines):
            if line.amount is None:
                continue
            best_combo: tuple[tuple[float, int, int], CompositeRule, tuple[Any, ...]] | None = None
            for rule in COMPOSITE_RULES:
                if line.summary and not rule.allow_summary:
                    continue
                if self.regime not in rule.regimes:
                    continue
                if self.chart_profile not in rule.chart_profiles:
                    continue
                label_score = composite_label_score(rule, line.label)
                if label_score < 0.74:
                    continue
                exact_rule_caption = normalize_text(line.label) in {
                    normalize_text(alias) for alias in rule.aliases
                }
                candidates = [
                    (
                        category,
                        rows,
                        self.category_value(category, rows),
                    )
                    for category, rows in unresolved.items()
                    if category in rule.categories
                    and not any(row.mapping for row in rows)
                    and statement_compatible(category, line, self.regime)
                    and (
                        exact_rule_caption
                        or not (
                            set(words(line.label))
                            & CATEGORY_FORBIDDEN_LABEL_WORDS.get(
                                category, frozenset()
                            )
                        )
                    )
                    and (
                        not line.summary
                        or not any(
                            not detailed.summary
                            and statement_compatible(category, detailed, self.regime)
                            and self.category_label_score(category, detailed.label) >= 0.50
                            for detailed in self.fs_lines
                        )
                    )
                ]
                for size in range(rule.min_categories, len(candidates) + 1):
                    for combo in itertools.combinations(candidates, size):
                        if self.line_amount_similarity(
                            sum(item[2] for item in combo),
                            line.amount,
                            (item[0] for item in combo),
                        ) < 0.98:
                            continue
                        row_count = sum(len(item[1]) for item in combo)
                        score = (label_score, size, row_count)
                        if best_combo is None or score > best_combo[0]:
                            best_combo = (score, rule, combo)
            if best_combo is None:
                continue
            _, rule, combo = best_combo
            categories = [item[0] for item in combo]
            expected = sum(item[2] for item in combo)
            accounts = [row.account for _, rows, _ in combo for row in rows]
            self.combined_match_details.append(
                {
                    "rule": rule.name,
                    "method": f"composite_exact:{rule.name}",
                    "fs_line": line.label,
                    "fs_amount": line.amount,
                    "confidence": round(best_combo[0][0], 4),
                    "amount_difference": round(
                        line.amount
                        - self.amount_in_fs_units(expected, categories),
                        2,
                    ),
                    "conflicting_statement_families": self.mapping_conflicts(
                        categories, line
                    ),
                    "participating_accounts": [str(account) for account in accounts],
                    "categories": categories,
                    "category_values": {
                        item[0]: round(
                            self.amount_in_fs_units(item[2], item[0]), 2
                        )
                        for item in combo
                    },
                }
            )
            for category, rows, _ in combo:
                for row in rows:
                    self.record_row_mapping(
                        row,
                        line_index,
                        f"composite_exact:{rule.name}",
                        best_combo[0][0],
                        categories,
                        accounts,
                        expected,
                    )
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
            best: tuple[int | None, tuple[int, float], float, float] = (
                None,
                (-1, 0.0),
                0.0,
                0.0,
            )
            for line_index, line in enumerate(self.fs_lines):
                if line.summary:
                    continue
                amount_score = self.line_amount_similarity(
                    expected, line.amount, category
                )
                label_score = max(
                    text_similarity(row.label, line.label),
                    self.category_label_score(category, line.label),
                )
                score = 0.55 * label_score + 0.45 * amount_score
                rank = (
                    int(statement_compatible(category, line, self.regime)),
                    score,
                )
                if amount_score >= 0.98 and label_score >= 0.65 and rank > best[1]:
                    best = (line_index, rank, label_score, amount_score)
            if best[0] is not None:
                line = self.fs_lines[best[0]]
                self.record_row_mapping(
                    row,
                    best[0],
                    "row_exact",
                    min(best[2], best[3]),
                    [category],
                    [row.account],
                    expected,
                )
                self.justified_lines.add(best[0])
                matched += 1
        return matched

    def reconcile_permissive_equal_sum(
        self,
        grouped_rows: dict[str, list[Any]],
        *,
        cross_statement: bool,
    ) -> int:
        """Restore the legacy exact-sum search as an explicit final fallback.

        The historical mapper considered up to five unresolved category groups
        whose weak caption similarity averaged at least 0.18.  The first pass
        retains combinations compatible with the target statement; the second
        deliberately permits cross-statement/section combinations.  Both are
        emitted as mappings and fully labelled for downstream review.
        """

        matched = 0
        unresolved = {
            category: rows
            for category, rows in grouped_rows.items()
            if rows and all(not row.mapping for row in rows)
        }
        for line_index, line in enumerate(self.fs_lines):
            if (
                line.summary
                or line.amount is None
                or line_index in self.justified_lines
            ):
                continue
            candidates: list[tuple[float, str, list[Any], float]] = []
            for category, rows in unresolved.items():
                if any(row.mapping for row in rows):
                    continue
                label_score = self.category_label_score(category, line.label)
                if label_score >= 0.18:
                    candidates.append(
                        (
                            label_score,
                            category,
                            rows,
                            self.category_value(category, rows),
                        )
                    )
            candidates.sort(reverse=True, key=lambda item: item[0])
            candidates = candidates[:10]

            best_combo: tuple[float, float, tuple[Any, ...]] | None = None
            for size in range(2, min(5, len(candidates)) + 1):
                for combo in itertools.combinations(candidates, size):
                    compatibility = [
                        statement_compatible(item[1], line, self.regime)
                        for item in combo
                    ]
                    if cross_statement == all(compatibility):
                        continue
                    categories = [item[1] for item in combo]
                    amount_score = self.line_amount_similarity(
                        sum(item[3] for item in combo),
                        line.amount,
                        categories,
                    )
                    if amount_score < 0.98:
                        continue
                    average_label = sum(item[0] for item in combo) / len(combo)
                    score = 0.55 * average_label + 0.45 * amount_score
                    if average_label >= 0.18 and (
                        best_combo is None or score > best_combo[0]
                    ):
                        best_combo = (score, average_label, combo)
            if best_combo is None:
                continue

            score, average_label, combo = best_combo
            categories = [item[1] for item in combo]
            expected = sum(item[3] for item in combo)
            accounts = [row.account for _, _, rows, _ in combo for row in rows]
            if cross_statement:
                method = "cross_statement_equal_sum"
                confidence = min(0.60, score)
            elif average_label >= 0.50 and all(
                self.category_label_is_specific(item[1], line.label)
                for item in combo
            ):
                method = "exact_amount_combination"
                confidence = min(0.90, score)
            else:
                method = "permissive_equal_sum"
                confidence = min(0.70, score)

            detail = {
                "rule": "legacy_equal_sum",
                "method": method,
                "fs_line": line.label,
                "fs_amount": line.amount,
                "confidence": round(confidence, 4),
                "amount_difference": round(
                    line.amount - self.amount_in_fs_units(expected, categories), 2
                ),
                "conflicting_statement_families": self.mapping_conflicts(
                    categories, line
                ),
                "participating_accounts": [str(account) for account in accounts],
                "categories": categories,
                "category_values": {
                    item[1]: round(
                        self.amount_in_fs_units(item[3], item[1]), 2
                    )
                    for item in combo
                },
            }
            self.combined_match_details.append(detail)
            for _, category, rows, _ in combo:
                for row in rows:
                    self.record_row_mapping(
                        row,
                        line_index,
                        method,
                        confidence,
                        categories,
                        accounts,
                        expected,
                    )
                    matched += 1
                unresolved.pop(category, None)
            self.justified_lines.add(line_index)
        return matched

    def reconcile_derived_equity_result(self) -> int:
        """Cross-foot the balance-sheet result to classes 6 and 7.

        A trial balance delivered before closing entries may contain no class
        12 account.  In that case the result shown in equity is still exactly
        auditable as revenues less expenses.  The class-6/7 rows retain their
        income-statement mappings; this records a derived proof instead of
        assigning the same row twice.
        """

        if any(
            category == "equity_result" and abs(self.rows[index].amount) > 0.01
            for index, category in self.assigned_categories.items()
        ):
            return 0
        result_rows = [
            row
            for row in self.rows
            if account_identity(row.account).startswith(("6", "7"))
            and abs(row.amount) > 0.01
        ]
        has_expenses = any(
            account_identity(row.account).startswith("6") for row in result_rows
        )
        has_income = any(
            account_identity(row.account).startswith("7") for row in result_rows
        )
        if len(result_rows) < 5 or not (has_expenses and has_income):
            return 0

        expected = -sum(row.amount for row in result_rows)
        candidates = [
            (index, line)
            for index, line in enumerate(self.fs_lines)
            if not line.summary
            and line.amount is not None
            and line.statement_family == "liability"
            and self.category_label_score("equity_result", line.label) >= 0.65
            and self.line_amount_similarity(expected, line.amount) >= 0.98
        ]
        if len(candidates) != 1:
            return 0
        line_index, line = candidates[0]
        self.justified_lines.add(line_index)
        self.derived_line_details[line_index] = {
            "method": "derived_result_classes_6_7",
            "fs_line": line.label,
            "expected": expected,
            "source_account_count": len(result_rows),
            "bg_accounts": [str(row.account) for row in result_rows],
        }
        return 1

    def reconcile_account_coded_fs_lines(self) -> int:
        """Prove account-coded disclosure lines without remapping BG rows.

        A statutory statement or disclosure can repeat a primary amount with
        an explicit account-family prefix (for example M21 line ``7312 ...``).
        The BG rows keep their primary-statement mapping, while the repeated
        FS line receives independent exact evidence from every non-zero BG
        account sharing that prefix.  No fuzzy label or subset search is used.
        """

        matched = 0
        for line_index, line in enumerate(self.fs_lines):
            if (
                line_index in self.justified_lines
                or line.summary
                or line.amount is None
                or abs(line.amount) <= 0.01
            ):
                continue
            prefix_match = re.match(r"^(\d{3,8})(?:\D|$)", normalize_text(line.label))
            if prefix_match is None:
                continue
            prefix = prefix_match.group(1)
            candidates: list[tuple[int, Any, str]] = []
            for row_index, row in enumerate(self.rows):
                category = self.assigned_categories[row_index]
                if (
                    abs(row.amount) <= 0.01
                    or not category
                    or category in NON_FS_CATEGORIES
                    or not account_identity(row.account).startswith(prefix)
                    or not statement_compatible(category, line, self.regime)
                ):
                    continue
                candidates.append((row_index, row, category))
            if not candidates:
                continue
            categories = {category for _, _, category in candidates}
            expected = sum(
                self.category_value(category, [row])
                for _, row, category in candidates
            )
            if self.line_amount_similarity(expected, line.amount, categories) < 0.98:
                continue
            self.justified_lines.add(line_index)
            self.supplemental_line_details[line_index] = {
                "method": "account_prefix_exact",
                "fs_line": line.label,
                "fs_amount": line.amount,
                "fs_page": line.page,
                "fs_statement": line.statement,
                "fs_scope": line.scope,
                "account_prefix": prefix,
                "expected": expected,
                "source_account_count": len(candidates),
                "categories": sorted(categories),
                "bg_accounts": [str(row.account) for _, row, _ in candidates],
            }
            matched += 1
        return matched

    def reconcile_repeated_exact_fs_lines(
        self, grouped_rows: dict[str, list[Any]]
    ) -> int:
        """Prove repeated primary/detail lines from already mapped BG groups.

        Annual reports often repeat the same statutory amount in a summary
        statement and a detailed schedule.  A BG row still receives exactly
        one primary mapping, but every repeated FS line can be independently
        evidenced when the complete, already-mapped category group (or an
        authorized statutory composite) reconciles exactly.
        """

        matched = 0
        already_mapped = {
            category: rows
            for category, rows in grouped_rows.items()
            if rows and all(row.mapping for row in rows)
        }
        for line_index, line in enumerate(self.fs_lines):
            if (
                line_index in self.justified_lines
                or line.summary
                or line.amount is None
                or abs(line.amount) <= 0.01
            ):
                continue

            direct_candidates: list[tuple[float, str, float, list[Any]]] = []
            for category, rows in already_mapped.items():
                if not statement_compatible(category, line, self.regime):
                    continue
                label_score = self.category_label_score(category, line.label)
                if (
                    label_score < 0.65
                    or not self.category_label_is_specific(category, line.label)
                ):
                    continue
                expected = self.category_value(category, rows)
                if self.line_amount_similarity(expected, line.amount, category) >= 0.98:
                    direct_candidates.append((label_score, category, expected, rows))

            selected: dict[str, Any] | None = None
            if direct_candidates:
                direct_candidates.sort(key=lambda item: item[0], reverse=True)
                best = direct_candidates[0]
                # Equal-strength competing meanings are not auditable.
                if len(direct_candidates) == 1 or best[0] > direct_candidates[1][0] + 0.02:
                    selected = {
                        "method": "repeated_category_exact",
                        "fs_line": line.label,
                        "fs_amount": line.amount,
                        "fs_page": line.page,
                        "fs_statement": line.statement,
                        "fs_scope": line.scope,
                        "expected": best[2],
                        "source_account_count": len(best[3]),
                        "categories": [best[1]],
                        "bg_accounts": [str(row.account) for row in best[3]],
                    }

            if selected is None:
                best_composite: tuple[
                    tuple[float, int, int], CompositeRule, tuple[Any, ...]
                ] | None = None
                for rule in COMPOSITE_RULES:
                    if (
                        self.regime not in rule.regimes
                        or self.chart_profile not in rule.chart_profiles
                        or composite_label_score(rule, line.label) < 0.74
                    ):
                        continue
                    candidates = [
                        (
                            category,
                            rows,
                            self.category_value(category, rows),
                        )
                        for category, rows in already_mapped.items()
                        if category in rule.categories
                        and statement_compatible(category, line, self.regime)
                    ]
                    for size in range(rule.min_categories, len(candidates) + 1):
                        for combo in itertools.combinations(candidates, size):
                            categories = [item[0] for item in combo]
                            expected = sum(item[2] for item in combo)
                            if self.line_amount_similarity(
                                expected, line.amount, categories
                            ) < 0.98:
                                continue
                            score = (
                                composite_label_score(rule, line.label),
                                size,
                                sum(len(item[1]) for item in combo),
                            )
                            if best_composite is None or score > best_composite[0]:
                                best_composite = (score, rule, combo)
                if best_composite is not None:
                    _, rule, combo = best_composite
                    selected = {
                        "method": f"repeated_composite_exact:{rule.name}",
                        "fs_line": line.label,
                        "fs_amount": line.amount,
                        "fs_page": line.page,
                        "fs_statement": line.statement,
                        "fs_scope": line.scope,
                        "expected": sum(item[2] for item in combo),
                        "source_account_count": sum(len(item[1]) for item in combo),
                        "categories": [item[0] for item in combo],
                        "bg_accounts": [
                            str(row.account)
                            for _, rows, _ in combo
                            for row in rows
                        ],
                    }

            if selected is None:
                continue
            self.justified_lines.add(line_index)
            self.supplemental_line_details[line_index] = selected
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
            left_residual = self.line_amount_in_bg_units(left_line) - left_bg
            right_residual = self.line_amount_in_bg_units(right_line) - right_bg
            scale = max(
                abs(self.line_amount_in_bg_units(left_line)),
                abs(self.line_amount_in_bg_units(right_line)),
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
                expected = self.category_value(category, rows)
                for row in rows:
                    if row.mapping:
                        continue
                    self.record_row_mapping(
                        row,
                        line_index,
                        "gross_up_inferred",
                        0.50,
                        [category],
                        [item.account for item in rows],
                        expected,
                    )
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
        """Apply the legacy semantic mapper after all exact-sum searches.

        These mappings are never counted as justified. They allow the output
        to say where an account belongs while the reconciliation report states
        what the FS amount should have been according to the BG.  Framework,
        source-alignment, statement-family and result-section conflicts are
        recorded in the audit metadata rather than used as abstention gates.
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
                amount_distance = abs(
                    self.line_amount_in_bg_units(line, category) - expected
                )
                candidates.append((label_score, -amount_distance, line_index, line))
            if not candidates:
                continue
            label_score, _, line_index, line = max(candidates)
            participating_accounts = [row.account for row in unresolved]
            unresolved_expected = self.category_value(category, unresolved)
            for row in unresolved:
                self.record_row_mapping(
                    row,
                    line_index,
                    "semantic_permissive",
                    min(0.80, 0.70 * label_score),
                    [category],
                    participating_accounts,
                    unresolved_expected,
                )
                matched += 1
            self.semantic_only_lines.add(line_index)
        return matched

    def assess_source_alignment(self) -> dict[str, Any]:
        """Require independent exact anchors before mapping amount differences.

        Semantic account-family assignment is useful only after the BG and FS
        have been shown to share a reporting source. Three exact detailed
        lines spanning two statements are strong evidence; five exact lines
        within a single-statement extract are accepted for intentionally
        limited reports. Repeated display lines are assessed later and cannot
        manufacture this evidence.
        """

        nonzero_leaf_indexes = {
            index
            for index, line in enumerate(self.fs_lines)
            if not line.summary and line.amount is not None and abs(line.amount) > 0.01
        }
        exact_indexes = self.justified_lines & nonzero_leaf_indexes
        families = sorted(
            {
                self.fs_lines[index].statement_family
                for index in exact_indexes
                if self.fs_lines[index].statement_family
            }
        )
        exact_count = len(exact_indexes)
        if self.fs_framework == "ifrs_consolidated":
            status = "framework_restricted"
            allowed = False
        elif exact_count >= 3 and len(families) >= 2:
            status = "aligned"
            allowed = True
        elif exact_count >= 5:
            status = "aligned_single_statement"
            allowed = True
        elif exact_count:
            status = "weak"
            allowed = False
        else:
            status = "unproven"
            allowed = False
        return {
            "status": status,
            "exact_primary_lines": exact_count,
            "statement_families": families,
            "semantic_mapping_allowed": allowed,
        }

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
            derived = self.derived_line_details.get(line_index)
            supplemental = self.supplemental_line_details.get(line_index)
            bg_amount = sum(
                self.category_value(
                    self.assigned_categories[row_index] or "", [row]
                )
                for row_index, row in mapped_rows
            )
            if derived is not None and not mapped_rows:
                bg_amount = float(derived["expected"])
            elif supplemental is not None and not mapped_rows:
                bg_amount = float(supplemental["expected"])
            mapped_categories = {
                self.assigned_categories[row_index] or ""
                for row_index, _ in mapped_rows
            } - {""}
            if supplemental is not None and not mapped_categories:
                mapped_categories = set(supplemental.get("categories", []))
            bg_amount_display = self.amount_in_fs_units(
                bg_amount, mapped_categories
            )
            difference = line.amount - bg_amount_display
            row_details = [
                self.row_mapping_details[id(row)]
                for _, row in mapped_rows
                if id(row) in self.row_mapping_details
            ]
            if derived is not None and self.line_amount_similarity(
                bg_amount, line.amount
            ) >= 0.98:
                status = "derived exact"
            elif supplemental is not None and self.line_amount_similarity(
                bg_amount, line.amount, mapped_categories
            ) >= 0.98:
                status = "supplemental exact"
            elif line_index in self.justified_lines and self.line_amount_similarity(
                bg_amount, line.amount, mapped_categories
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
            mapping_methods = sorted(
                {
                    self.row_methods.get(id(row), "unknown")
                    for _, row in mapped_rows
                }
            )
            if not mapping_methods and derived is not None:
                mapping_methods = [str(derived["method"])]
            elif not mapping_methods and supplemental is not None:
                mapping_methods = [str(supplemental["method"])]
            confidence = (
                min(float(detail.get("confidence", 0.0)) for detail in row_details)
                if row_details
                else 1.0
                if derived is not None or supplemental is not None
                else 0.0
            )
            conflicts = sorted(
                {
                    conflict
                    for detail in row_details
                    for conflict in detail.get(
                        "conflicting_statement_families", []
                    )
                }
            )
            participating_accounts = list(
                dict.fromkeys(
                    account
                    for detail in row_details
                    for account in detail.get("participating_accounts", [])
                )
            )
            if not participating_accounts:
                participating_accounts = (
                    [str(row.account) for _, row in mapped_rows]
                    if mapped_rows
                    else list(derived.get("bg_accounts", []))
                    if derived
                    else list(supplemental.get("bg_accounts", []))
                    if supplemental
                    else []
                )
            result.append(
                {
                    "fs_line_index": line_index,
                    "fs_line": line.label,
                    "fs_amount": round(line.amount, 2),
                    "bg_amount": round(bg_amount_display, 2),
                    "difference": round(difference, 2),
                    "status": status,
                    "bg_rows": len(mapped_rows),
                    "categories": sorted(
                        {
                            self.assigned_categories[row_index] or ""
                            for row_index, _ in mapped_rows
                        }
                        - {""}
                    )
                    or (["equity_result_derived"] if derived is not None else [])
                    or (list(supplemental.get("categories", [])) if supplemental else []),
                    "mapping_methods": mapping_methods,
                    "confidence": round(confidence, 4),
                    "amount_difference": round(difference, 2),
                    "conflicting_statement_families": conflicts,
                    "participating_accounts": participating_accounts,
                    "participating_categories": sorted(mapped_categories),
                    "bg_accounts": participating_accounts,
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
                and statement_compatible(category, line, self.regime)
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
            numeric_line_indexes = tuple(
                index
                for index in line_indexes
                if self.fs_lines[index].amount is not None
            )
            if not numeric_line_indexes:
                continue
            expected = sum(item[1] for item in categories)
            fs_amount = sum(
                self.fs_lines[index].amount for index in numeric_line_indexes
            )
            category_names = [item[0] for item in categories]
            expected_display = self.amount_in_fs_units(expected, category_names)
            difference = fs_amount - expected_display
            exact = self.line_amount_similarity(
                expected, fs_amount, category_names
            ) >= 0.98
            if exact:
                continue
            line_labels = " + ".join(
                self.fs_lines[index].label for index in numeric_line_indexes
            )
            result.append(
                {
                    "category": " + ".join(item[0] for item in categories),
                    "fs_line": line_labels,
                    "fs_amount": round(fs_amount, 2),
                    "bg_expected": round(expected_display, 2),
                    "difference": round(difference, 2),
                    "status": "review",
                    "fs_line_indexes": list(numeric_line_indexes),
                }
            )
        return result

    def map(self) -> dict[str, Any]:
        self.resolve_contextual_personnel()
        self.resolve_contextual_financial_offsets()
        self.resolve_contextual_revenue()
        grouped_rows: dict[str, list[Any]] = defaultdict(list)
        for index, row in enumerate(self.rows):
            category = self.assigned_categories[index]
            if category and category not in NON_FS_CATEGORIES:
                grouped_rows[category].append(row)

        direct_matches = self.direct_category_matches(grouped_rows)
        combined_matches = self.reconcile_combined_categories(grouped_rows)
        derived_matches = self.reconcile_derived_equity_result()
        account_coded_matches = self.reconcile_account_coded_fs_lines()
        repeated_exact_matches = self.reconcile_repeated_exact_fs_lines(grouped_rows)
        supplemental_matches = account_coded_matches + repeated_exact_matches
        row_matches = self.exact_row_fallback()
        gross_up_matches = self.reconcile_gross_up_pairs(grouped_rows)
        self.source_alignment = self.assess_source_alignment()
        same_statement_permissive_matches = self.reconcile_permissive_equal_sum(
            grouped_rows, cross_statement=False
        )
        cross_statement_permissive_matches = self.reconcile_permissive_equal_sum(
            grouped_rows, cross_statement=True
        )
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
        method_counts: dict[str, int] = defaultdict(int)
        for row in self.rows:
            if id(row) in self.row_methods:
                method_counts[self.row_methods[id(row)]] += 1
        reconciliation = self.build_reconciliation()
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
            "combined_match_details": self.combined_match_details,
            "same_statement_permissive_matches": (
                same_statement_permissive_matches
            ),
            "cross_statement_permissive_matches": (
                cross_statement_permissive_matches
            ),
            "gross_up_matches": gross_up_matches,
            "semantic_matches": semantic_matches,
            "gross_up_adjustments": self.gross_up_adjustments,
            "row_matches": row_matches,
            "derived_matches": derived_matches,
            "derived_line_details": list(self.derived_line_details.values()),
            "supplemental_matches": supplemental_matches,
            "account_coded_matches": account_coded_matches,
            "repeated_exact_matches": repeated_exact_matches,
            "supplemental_line_details": list(
                self.supplemental_line_details.values()
            ),
            "method_counts": dict(sorted(method_counts.items())),
            "mapping_audit_records": reconciliation,
            "fs_lines": len(self.fs_lines),
            "fs_leaf_lines": sum(not line.summary for line in self.fs_lines),
            "justified_fs_lines": len(self.justified_lines),
            "reconciliation": reconciliation,
            "category_differences": self.build_category_differences(grouped_rows),
            "regime": self.regime,
            "chart_profile": self.chart_profile,
            "fs_framework": self.fs_framework,
            "fs_framework_signatures": self.fs_framework_signatures,
            "source_alignment": self.source_alignment,
            "fs_scopes": sorted(
                {
                    str(line.scope).lower()
                    for line in self.fs_lines
                    if str(line.scope or "").strip()
                }
            ),
            "fs_unit_scale": self.fs_unit_scale,
            "fs_expense_sign": self.fs_expense_sign,
            "fs_liability_sign": self.fs_liability_sign,
        }
