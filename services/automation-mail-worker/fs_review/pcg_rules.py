"""Explicit French-PCG semantic rules for BG account classification.

The rules in this module are based on accounting meaning and official PCG
passage logic. They do not contain ticket-specific mappings or FAST codes.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


ZERO_TOLERANCE = 0.01


@dataclass(frozen=True)
class AccountObservation:
    row_id: int
    account: Any
    label: str
    amount: float


@dataclass(frozen=True)
class RuleDecision:
    category: str | None
    rationale: str
    requires_context: bool = False


CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_capital_uncalled": ("capital souscrit non appele",),
    "asset_intangible_establishment": ("frais d etablissement",),
    "asset_intangible_research": (
        "frais de recherche et developpement",
        "frais d etudes de recherche et de developpement",
    ),
    "asset_intangible_concessions": (
        "concessions brevets licences marques procedes logiciels droits similaires",
        "concessions brevets et droits similaires",
    ),
    "asset_intangible_goodwill": ("fonds commercial",),
    "asset_intangible_other": ("autres immobilisations incorporelles",),
    "asset_intangible_in_progress": (
        "immobilisations incorporelles en cours avances et acomptes",
    ),
    "asset_land": ("terrains",),
    "asset_construction": ("constructions",),
    "asset_technical_equipment": (
        "installations techniques materiel et outillage industriels",
    ),
    "asset_other_corporeal": ("autres immobilisations corporelles",),
    "asset_corporeal_in_progress": (
        "immobilisations corporelles en cours avances et acomptes",
    ),
    "asset_participations": ("participations",),
    "asset_related_participation_receivables": (
        "creances rattachees a des participations",
    ),
    "asset_other_financial": (
        "autres immobilisations financieres",
        "prets et autres immobilisations financieres",
    ),
    "asset_redemption_premium": ("primes de remboursement des emprunts",),
    "asset_charges_to_spread": ("charges a repartir sur plusieurs exercices",),
    "asset_conversion_difference": (
        "ecarts de conversion actif",
        "differences de conversion actif",
    ),
    "stock_raw_materials": (
        "matieres premieres et autres approvisionnements",
        "matieres premieres approvisionnements",
    ),
    "stock_goods_wip": ("en cours de production de biens", "en cours de production"),
    "stock_service_wip": ("en cours de production de services",),
    "stock_products": ("produits intermediaires et finis", "stocks de produits"),
    "stock_merchandise": ("marchandises", "stocks de marchandises"),
    "supplier_advances": ("avances et acomptes verses sur commandes",),
    "clients": ("creances clients et comptes rattaches", "clients et comptes rattaches"),
    "supplier_debit_receivables": ("fournisseurs debiteurs", "autres creances"),
    "personnel_receivables": ("personnel", "autres creances"),
    "social_bodies_receivables": (
        "securite sociale et autres organismes sociaux",
        "organismes sociaux",
        "personnel",
        "autres creances",
    ),
    "income_tax_receivable": (
        "etat impots sur les benefices",
        "impots sur les benefices",
        "autres creances",
    ),
    "vat_receivable": (
        "etat taxes sur le chiffre d affaires",
        "taxe sur la valeur ajoutee",
        "autres creances",
    ),
    "other_tax_receivables": ("etat autres", "autres creances"),
    "other_receivables": ("autres creances",),
    "prepaid_expenses": ("charges constatees d avance",),
    "marketable_securities": ("valeurs mobilieres de placement",),
    "treasury_instruments": ("instruments de tresorerie actif",),
    "cash": ("disponibilites", "banques etablissements financiers et assimiles"),
    "equity_capital": ("capital social ou individuel", "capital social", "capital"),
    "equity_premiums": ("primes d emission fusion apport", "primes de fusion"),
    "equity_revaluation": ("ecarts de reevaluation",),
    "equity_legal_reserve": ("reserve legale",),
    "equity_other_reserves": ("autres reserves", "reserves reglementees"),
    "equity_retained_earnings": ("report a nouveau",),
    "equity_result": ("resultat de l exercice benefice ou perte",),
    "equity_investment_grants": ("subventions d investissement",),
    "equity_regulated_provisions": ("provisions reglementees",),
    "association_funds": (
        "fonds associatifs",
        "fonds propres complementaires",
        "fonds associatifs sans droit de reprise",
        "fonds associatifs avec droit de reprise",
    ),
    "provisions_risks": ("provisions pour risques",),
    "provisions_charges": ("provisions pour charges",),
    "bank_debt": ("emprunts et dettes aupres des etablissements de credit",),
    "bank_loans": (
        "emprunts et dettes aupres des etablissements de credit emprunts",
    ),
    "bank_overdrafts": (
        "decouverts et concours bancaires",
        "concours bancaires courants",
    ),
    "other_financial_debt": ("emprunts et dettes financieres divers",),
    "associate_financial_debt": (
        "emprunts et dettes financieres diverses associes",
        "comptes courants associes",
        "emprunts et dettes financieres divers",
    ),
    "customer_advances": ("avances et acomptes recus sur commandes en cours",),
    "supplier_debt": ("dettes fournisseurs et comptes rattaches",),
    "personnel_debt": ("personnel", "dettes fiscales et sociales"),
    "social_bodies_debt": (
        "securite sociale et autres organismes sociaux",
        "organismes sociaux",
        "dettes fiscales et sociales",
    ),
    "income_tax_debt": (
        "etat impots sur les benefices",
        "impots sur les benefices",
        "dettes fiscales et sociales",
    ),
    "vat_debt": (
        "etat taxes sur le chiffre d affaires",
        "taxe sur la valeur ajoutee",
        "dettes fiscales et sociales",
    ),
    "other_tax_debt": ("etat autres", "dettes fiscales et sociales"),
    "tax_social_debt": ("dettes fiscales et sociales",),
    "fixed_asset_debt": ("dettes sur immobilisations et comptes rattaches",),
    "other_debts": ("autres dettes",),
    "deferred_income": ("produits constates d avance",),
    "liability_conversion_difference": (
        "ecarts de conversion passif",
        "differences de conversion passif",
    ),
    "sales_merchandise": ("ventes de marchandises",),
    "production_goods": ("production vendue biens", "production vendue"),
    "production_services": (
        "production vendue services",
        "production vendue de services",
        "ventes de prestations de service",
        "production vendue",
    ),
    "production_stocked": ("production stockee",),
    "production_capitalized": ("production immobilisee",),
    "operating_subsidies": ("subventions d exploitation", "subventions"),
    "other_operating_income": ("autres produits",),
    "reversals_operating": (
        "reprises sur amortissements depreciations et provisions",
        "reprises sur amortissements et provisions",
    ),
    "legacy_charge_transfers": (
        "transferts de charges",
        "reprises sur amortissements depreciations provisions et transferts de charges",
        "autres produits",
    ),
    "purchases_merchandise": ("achats de marchandises",),
    "stock_variation_merchandise": ("variation de stock marchandises",),
    "purchases_raw_materials": (
        "achats de matieres premieres et autres approvisionnements",
    ),
    "stock_variation_raw_materials": (
        "variation de stock matieres premieres et approvisionnements",
    ),
    "external_charges": ("autres achats et charges externes",),
    "taxes": ("impots taxes et versements assimiles",),
    "salaries": ("salaires", "salaires et traitements"),
    "social_charges": ("cotisations sociales", "charges sociales"),
    "other_operating_charges": ("autres charges",),
    "amortization_operating": (
        "dotations aux amortissements",
        "dotations aux amortissements sur immobilisations",
    ),
    "depreciation_operating": (
        "sur actif circulant dotations et depreciations",
        "dotations aux depreciations",
    ),
    "provisions_operating": ("dotations aux provisions",),
    "financial_participation_income": (
        "produits financiers de participations",
        "produits de participations",
        "de participation",
    ),
    "financial_other_fixed_income": (
        "d autres valeurs mobilieres et creances de l actif immobilise",
        "produits des autres immobilisations financieres",
        "revenus des autres immobilisations financieres",
    ),
    "financial_other_interest_income": ("autres interets et produits assimiles",),
    "financial_positive_exchange": ("differences positives de change",),
    "financial_vmp_disposal_income": (
        "produits nets sur cessions de valeurs mobilieres de placement",
    ),
    "financial_income": ("produits financiers",),
    "financial_charges": (
        "interets et charges assimiles",
        "interets et charges assimilees",
        "charges financieres",
    ),
    "financial_reversals": ("reprises sur depreciations et provisions",),
    "financial_dotations": (
        "dotations aux amortissements aux depreciations et aux provisions",
    ),
    "exceptional_income": ("produits exceptionnels",),
    "exceptional_charges": ("charges exceptionnelles",),
    "employee_participation": ("participation des salaries",),
    "income_tax": ("impots sur les benefices", "impot sur les benefices"),
    "asset_disposal_income": ("produits de cession d immobilisations",),
    "asset_disposal_cost": (
        "valeurs comptables des immobilisations incorporelles et corporelles cedees",
        "autres charges",
    ),
    "contextual_ancillary_revenue": ("autres produits", "production vendue"),
    "contextual_purchase_adjustment": (
        "achats de marchandises",
        "achats de matieres premieres",
        "autres achats et charges externes",
    ),
    "contextual_personnel": ("salaires", "cotisations sociales", "charges sociales"),
    "contextual_financial_offset": (
        "interets et charges assimiles",
        "autres interets et produits assimiles",
    ),
    "contextual_fixed_asset": (
        "autres immobilisations incorporelles",
        "autres immobilisations corporelles",
    ),
    "contextual_suspense": ("autres creances", "autres dettes"),
    "contextual_revenue": ("production vendue", "autres produits"),
    "contextual_special_account": ("autres produits", "autres charges"),
}


def normalize_text(value: Any) -> str:
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or ""))
        if not unicodedata.combining(char)
    ).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def account_digits(value: Any) -> str:
    return "".join(char for char in str(value or "").split(".")[0] if char.isdigit())


def account_identity(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper().split(".")[0])


def balance_sign(amount: float) -> int:
    if amount > ZERO_TOLERANCE:
        return 1
    if amount < -ZERO_TOLERANCE:
        return -1
    return 0


def _decision(
    category: str | None, rationale: str, requires_context: bool = False
) -> RuleDecision:
    return RuleDecision(category, rationale, requires_context)


def classify_account(
    account: Any,
    label: str,
    balance: float,
    regime: str = "modern",
) -> RuleDecision:
    """Classify one aggregated account balance into a semantic PCG category."""

    code = account_digits(account)
    text = normalize_text(label)
    sign = balance_sign(balance)
    if not code or sign == 0:
        return _decision(None, "empty account or zero closing balance")

    # Net fixed assets: gross, amortization, and depreciation share one FS line.
    fixed_asset_prefixes = (
        ("203", "asset_intangible_research"),
        ("205", "asset_intangible_concessions"),
        ("207", "asset_intangible_goodwill"),
        ("208", "asset_intangible_other"),
        ("206", "asset_intangible_other"),
        ("232", "asset_intangible_in_progress"),
        ("237", "asset_intangible_in_progress"),
        ("211", "asset_land"),
        ("212", "asset_land"),
        ("213", "asset_construction"),
        ("214", "asset_construction"),
        ("215", "asset_technical_equipment"),
        ("218", "asset_other_corporeal"),
        ("231", "asset_corporeal_in_progress"),
        ("238", "asset_corporeal_in_progress"),
        ("230", "asset_corporeal_in_progress"),
        ("261", "asset_participations"),
        ("266", "asset_participations"),
        ("267", "asset_related_participation_receivables"),
        ("268", "asset_related_participation_receivables"),
        ("271", "asset_other_financial"),
        ("272", "asset_other_financial"),
        ("273", "asset_other_financial"),
        ("274", "asset_other_financial"),
        ("275", "asset_other_financial"),
        ("276", "asset_other_financial"),
        ("277", "asset_other_financial"),
    )
    fixed_contra_prefixes = (
        ("2803", "asset_intangible_research"),
        ("2903", "asset_intangible_research"),
        ("2805", "asset_intangible_concessions"),
        ("2905", "asset_intangible_concessions"),
        ("2807", "asset_intangible_goodwill"),
        ("2907", "asset_intangible_goodwill"),
        ("2808", "asset_intangible_other"),
        ("2908", "asset_intangible_other"),
        ("2811", "asset_land"),
        ("2911", "asset_land"),
        ("2812", "asset_land"),
        ("2912", "asset_land"),
        ("2813", "asset_construction"),
        ("2913", "asset_construction"),
        ("2814", "asset_construction"),
        ("2914", "asset_construction"),
        ("2815", "asset_technical_equipment"),
        ("2915", "asset_technical_equipment"),
        ("2818", "asset_other_corporeal"),
        ("2918", "asset_other_corporeal"),
        ("2961", "asset_participations"),
        ("2966", "asset_participations"),
        ("2967", "asset_related_participation_receivables"),
        ("297", "asset_other_financial"),
    )
    for prefix, category in fixed_contra_prefixes + fixed_asset_prefixes:
        if code.startswith(prefix):
            return _decision(category, f"PCG fixed-asset family {prefix}")
    if code.startswith("293"):
        category = (
            "asset_intangible_in_progress"
            if "incorp" in text
            else "asset_corporeal_in_progress"
        )
        return _decision(category, "depreciation of asset in progress")
    if code.startswith(("225", "282")):
        if "incorp" in text:
            return _decision("asset_intangible_other", "custom intangible fixed asset")
        if any(word in text for word in ("corporel", "vehicule", "materiel", "ppp")):
            return _decision("asset_other_corporeal", "custom corporeal fixed asset")
        return _decision(
            "contextual_fixed_asset",
            "custom fixed-asset family requires label/FS context",
            True,
        )
    if code.startswith("201"):
        return _decision("asset_intangible_establishment", "PCG account 201")
    if code.startswith(("2801", "2901")):
        return _decision("asset_intangible_establishment", "related 201 contra-account")
    if code.startswith(("28", "29")):
        if any(word in text for word in ("vehicule", "corporel", "materiel", "agencement", "amenagement", "installation")):
            return _decision("asset_other_corporeal", "labelled custom fixed-asset contra-account")
        if any(word in text for word in ("incorp", "logiciel", "droit", "brevet", "concession")):
            return _decision("asset_intangible_other", "labelled custom intangible contra-account")
        return _decision(
            "contextual_fixed_asset",
            "custom fixed-asset contra-account requires related gross asset",
            True,
        )
    if code.startswith("223"):
        return _decision("asset_construction", "construction on concession/third-party land")
    if code.startswith("278"):
        return _decision("asset_other_financial", "other financial asset")

    # Stocks and related depreciation.
    stock_prefixes = (
        ("391", "stock_raw_materials"),
        ("392", "stock_raw_materials"),
        ("393", "stock_goods_wip"),
        ("394", "stock_service_wip"),
        ("395", "stock_products"),
        ("397", "stock_merchandise"),
        ("381", "stock_raw_materials"),
        ("382", "stock_raw_materials"),
        ("383", "stock_goods_wip"),
        ("384", "stock_service_wip"),
        ("385", "stock_products"),
        ("387", "stock_merchandise"),
        ("31", "stock_raw_materials"),
        ("32", "stock_raw_materials"),
        ("33", "stock_goods_wip"),
        ("34", "stock_service_wip"),
        ("35", "stock_products"),
        ("37", "stock_merchandise"),
    )
    for prefix, category in stock_prefixes:
        if code.startswith(prefix):
            return _decision(category, f"PCG stock family {prefix}")

    # Balance-sheet special accounts and third parties.
    if code.startswith("109"):
        return _decision("asset_capital_uncalled", "uncalled subscribed capital")
    if code.startswith("169"):
        return _decision("asset_redemption_premium", "redemption premium asset")
    if code.startswith("481"):
        return _decision("asset_charges_to_spread", "charges spread over periods")
    if code.startswith(("269", "279", "404", "405", "4084")):
        return _decision("fixed_asset_debt", "fixed-asset supplier/payment balance")
    if code.startswith("4091"):
        return _decision("supplier_advances", "explicit supplier advance account")
    if code.startswith(("4097", "4098")):
        return _decision(
            "supplier_debit_receivables", "supplier credit/rebate receivable"
        )
    if code.startswith("409"):
        if sign < 0:
            return _decision("supplier_debt", "abnormal credit supplier-debtor account")
        return _decision(
            "supplier_advances"
            if any(word in text for word in ("avance", "acompte", "acpt"))
            else "supplier_debit_receivables",
            "generic supplier-debtor account classified from label",
            not any(word in text for word in ("avance", "acompte", "acpt")),
        )
    if code.startswith(("401", "402", "403", "408")):
        return _decision(
            "supplier_debit_receivables" if sign > 0 else "supplier_debt",
            "supplier balance classified from aggregated closing sign",
        )
    if code.startswith("4191"):
        return _decision("customer_advances", "explicit customer advance account")
    if code.startswith(("4197", "4198")):
        return _decision("other_debts", "customer credit/rebate payable")
    if code.startswith("419"):
        if sign > 0:
            return _decision("other_receivables", "abnormal debit customer-credit account")
        return _decision(
            "customer_advances" if any(word in text for word in ("avance", "acompte", "acpt")) else "other_debts",
            "generic customer-credit account classified from label",
            not any(word in text for word in ("avance", "acompte", "acpt")),
        )
    if code.startswith(("410", "411", "412", "413", "416", "418")):
        return _decision(
            "clients" if sign > 0 else "other_debts",
            "customer balance classified from aggregated closing sign",
        )
    if code.startswith("414"):
        return _decision(
            "clients" if sign > 0 else "other_debts",
            "customer balance classified from aggregated closing sign",
        )
    if code == "40":
        return _decision(
            "supplier_debit_receivables" if sign > 0 else "supplier_debt",
            "collective supplier account classified from closing sign",
        )
    if code == "41":
        return _decision(
            "clients" if sign > 0 else "other_debts",
            "collective customer account classified from closing sign",
        )
    if code.startswith("491"):
        return _decision("clients", "client receivable depreciation")
    if code.startswith(("495", "496")):
        return _decision("other_receivables", "other-receivable depreciation")
    if code.startswith("42"):
        return _decision(
            "personnel_receivables" if sign > 0 else "personnel_debt",
            "personnel balance classified from closing sign",
        )
    if code.startswith("43"):
        return _decision(
            "social_bodies_receivables" if sign > 0 else "social_bodies_debt",
            "social-body balance classified from closing sign",
        )
    if code.startswith("444"):
        return _decision(
            "income_tax_receivable" if sign > 0 else "income_tax_debt",
            "income-tax balance classified from closing sign",
        )
    if code.startswith("445"):
        return _decision(
            "vat_receivable" if sign > 0 else "vat_debt",
            "VAT balance classified from closing sign",
        )
    if code.startswith("44"):
        return _decision(
            "other_tax_receivables" if sign > 0 else "other_tax_debt",
            "other State/tax balance classified from closing sign",
        )
    if code.startswith(("45", "46")):
        if code.startswith("4562"):
            return _decision("asset_capital_uncalled", "called capital not yet paid")
        legacy_financial_current_account = (
            regime == "legacy"
            and sign < 0
            and (
                code.startswith("455")
                or (
                    code.startswith("451")
                    and any(
                        word in text
                        for word in ("associe", "groupe", "courant", "liee", "liees")
                    )
                )
            )
        )
        if legacy_financial_current_account:
            return _decision(
                "associate_financial_debt",
                "legacy presentation includes associate/group current accounts "
                "in other financial debt",
            )
        return _decision(
            "other_receivables" if sign > 0 else "other_debts",
            "current/other third-party balance classified from closing sign",
        )
    if code.startswith(("471", "472")):
        return _decision(
            "contextual_suspense",
            "suspense account should be cleared or justified before mapping",
            True,
        )
    if code.startswith("476"):
        return _decision("asset_conversion_difference", "conversion difference asset")
    if code.startswith("477"):
        return _decision(
            "liability_conversion_difference", "conversion difference liability"
        )
    if code.startswith("478"):
        return _decision(
            "asset_conversion_difference" if sign > 0 else "liability_conversion_difference",
            "conversion/evaluation difference classified from closing sign",
        )
    if code.startswith("486"):
        return _decision("prepaid_expenses", "prepaid expenses")
    if code.startswith("487"):
        return _decision("deferred_income", "deferred income")
    if code.startswith("50"):
        return _decision(
            "marketable_securities" if sign > 0 else "other_debts",
            "marketable-security balance classified from closing sign",
        )
    if code.startswith("52"):
        return _decision(
            "treasury_instruments" if sign > 0 else "other_debts",
            "treasury instrument classified from closing sign",
        )
    if code.startswith("59"):
        return _decision("marketable_securities", "marketable-security depreciation")
    if code.startswith("519"):
        return _decision(
            "bank_overdrafts" if regime == "legacy" else "bank_debt",
            "current bank financing",
        )
    if code.startswith("5181") and sign < 0:
        return _decision(
            "bank_loans" if regime == "legacy" else "bank_debt",
            "accrued interest payable to a credit institution",
        )
    if code.startswith(("51", "53")):
        return _decision(
            "cash"
            if sign > 0
            else ("bank_overdrafts" if regime == "legacy" else "bank_debt"),
            "cash/bank balance classified after account-level aggregation",
        )
    if code.startswith("58"):
        return _decision(
            "non_fs_internal_transfer",
            "internal transfer account should net to zero and is not an FS line",
        )

    # Equity, provisions, and financing.
    equity_prefixes = (
        ("101", "equity_capital"),
        ("108", "equity_capital"),
        ("104", "equity_premiums"),
        ("105", "equity_revaluation"),
        ("1061", "equity_legal_reserve"),
        ("106", "equity_other_reserves"),
        ("107", "equity_revaluation"),
        ("110", "equity_retained_earnings"),
        ("119", "equity_retained_earnings"),
        ("120", "equity_result"),
        ("129", "equity_result"),
        ("13", "equity_investment_grants"),
        ("14", "equity_regulated_provisions"),
        ("11", "equity_retained_earnings"),
    )
    for prefix, category in equity_prefixes:
        if code.startswith(prefix):
            return _decision(category, f"PCG equity family {prefix}")
    if code.startswith("102"):
        return _decision("association_funds", "association own funds")
    if code.startswith("151"):
        return _decision("provisions_risks", "provision for risks")
    if code.startswith("15"):
        return _decision("provisions_charges", "provision for charges")
    if code.startswith(("16", "17")):
        return _decision(
            (
                "bank_loans"
                if regime == "legacy"
                else "bank_debt"
            )
            if code.startswith(("164", "16884"))
            else "other_financial_debt",
            "financial debt family",
        )

    # Revenue and contra-revenue.
    if code.startswith("7097"):
        return _decision("sales_merchandise", "rebate offsets merchandise sales")
    if code.startswith(("7091", "7092", "7093")):
        return _decision("production_goods", "rebate offsets sold goods production")
    if code.startswith(("7094", "7095", "7096")):
        return _decision("production_services", "rebate offsets sold services")
    if code.startswith(("7098", "708")):
        return _decision(
            "contextual_ancillary_revenue",
            "ancillary revenue must follow its economic nature",
            True,
        )
    if code.startswith("709"):
        return _decision(
            "contextual_revenue",
            "generic sales rebate follows related revenue line",
            True,
        )
    if code.startswith(("701", "702", "703")):
        return _decision("production_goods", "sold goods production")
    if code.startswith(("704", "705", "706")):
        return _decision("production_services", "works/studies/services production")
    if code.startswith("707"):
        return _decision("sales_merchandise", "merchandise sales")
    if code.startswith("700"):
        return _decision(
            "contextual_revenue",
            "custom revenue account requires economic-label context",
            True,
        )
    if code.startswith("71"):
        return _decision("production_stocked", "stocked production")
    if code.startswith("72"):
        return _decision("production_capitalized", "capitalized production")
    if code.startswith("74"):
        return _decision("operating_subsidies", "operating subsidies")
    if code.startswith("757"):
        return _decision("asset_disposal_income", "asset disposal proceeds")
    if code.startswith("75"):
        return _decision("other_operating_income", "other operating income")
    if code.startswith("76"):
        if "dividende" in text or code.startswith("761"):
            return _decision(
                "financial_participation_income",
                "participation income by PCG family or dividend economic nature",
            )
        if code.startswith(("762", "763")):
            return _decision(
                "financial_other_fixed_income",
                "income from other financial fixed assets/receivables",
            )
        if code.startswith("766"):
            return _decision("financial_positive_exchange", "positive exchange difference")
        if code.startswith("767"):
            return _decision(
                "financial_vmp_disposal_income",
                "net income on disposal of marketable securities",
            )
        if code.startswith("768") and "credit agios" in text:
            return _decision(
                "contextual_financial_offset",
                "credit agios may offset interest charges in the source presentation",
                True,
            )
        if code.startswith(("764", "765", "768")):
            return _decision(
                "financial_other_interest_income",
                "other interest and similar financial income",
            )
        return _decision("financial_income", "custom financial income")
    if code.startswith("77"):
        return _decision("exceptional_income", "exceptional income")
    if code.startswith("78"):
        if code.startswith("786"):
            return _decision("financial_reversals", "financial reversals")
        if code.startswith("787"):
            return _decision("exceptional_income", "exceptional reversals")
        return _decision("reversals_operating", "operating reversals")
    if code.startswith("791"):
        if any(word in text for word in ("personnel", "salaire", "social")):
            return _decision(
                "contextual_personnel",
                "legacy personnel charge transfer follows related personnel line",
                True,
            )
        return _decision(
            "legacy_charge_transfers",
            "legacy charge transfer requires target-statement context",
            True,
        )

    # Charges and contra-charges.
    if code.startswith("6097"):
        return _decision("purchases_merchandise", "rebate offsets merchandise purchases")
    if code.startswith(("6091", "6092", "608", "609")):
        return _decision(
            "contextual_purchase_adjustment",
            "purchase accessory/rebate follows related purchase nature",
            True,
        )
    if code.startswith(("601", "602")):
        return _decision("purchases_raw_materials", "raw materials/supplies purchases")
    if code.startswith(("6031", "6032")):
        return _decision("stock_variation_raw_materials", "raw-material stock variation")
    if code.startswith("6037"):
        return _decision("stock_variation_merchandise", "merchandise stock variation")
    if code.startswith("607"):
        return _decision("purchases_merchandise", "merchandise purchases")
    if code.startswith(("604", "605", "606", "61", "62")):
        return _decision("external_charges", "other purchases/external charges")
    if code.startswith("63"):
        return _decision("taxes", "taxes and similar payments")
    if code.startswith(("641", "642", "643", "644")):
        return _decision("salaries", "salaries and wages")
    if code.startswith(("645", "647")):
        return _decision("social_charges", "social/personnel-related charges")
    if code.startswith(("648", "649")):
        return _decision(
            "contextual_personnel",
            "personnel subdivision must follow economic nature",
            True,
        )
    if code.startswith("657"):
        return _decision("asset_disposal_cost", "asset disposal carrying value")
    if code.startswith("65"):
        return _decision("other_operating_charges", "other operating charges")
    if code.startswith("66"):
        return _decision("financial_charges", "financial charges")
    if code.startswith("67"):
        return _decision("exceptional_charges", "exceptional charges")
    if code.startswith(("6811", "6812")):
        return _decision("amortization_operating", "operating amortization")
    if code.startswith("680"):
        return _decision("amortization_operating", "generic amortization dotation")
    if code.startswith("6815"):
        return _decision("provisions_operating", "operating risk/charge provision")
    if code.startswith(("6816", "6817")):
        return _decision("depreciation_operating", "operating depreciation")
    if code.startswith("686"):
        return _decision("financial_dotations", "financial dotations")
    if code.startswith("687"):
        return _decision("exceptional_charges", "exceptional dotations")
    if code.startswith("691"):
        return _decision("employee_participation", "employee participation")
    if code.startswith(("695", "698")):
        return _decision("income_tax", "income tax")
    if code.startswith("699"):
        return _decision("income_tax", "income-tax credit/reduction")
    if code.startswith("9"):
        return _decision(
            "non_fs_analytic",
            "class-9 analytic/internal account is outside statutory FS mapping",
        )
    if code.startswith("8"):
        return _decision(
            "contextual_special_account",
            "class-8 special account requires entity/statement context",
            True,
        )

    return _decision(None, f"no explicit PCG rule for {code}")


def classify_observations(
    observations: Iterable[AccountObservation],
    regime: str = "modern",
) -> dict[int, RuleDecision]:
    """Classify rows after aggregating repeated rows by account identifier."""

    grouped: dict[str, list[AccountObservation]] = defaultdict(list)
    for observation in observations:
        grouped[account_identity(observation.account)].append(observation)

    result: dict[int, RuleDecision] = {}
    for group in grouped.values():
        balance = sum(row.amount for row in group)
        label = " ".join(dict.fromkeys(row.label for row in group if row.label))
        decision = classify_account(group[0].account, label, balance, regime)
        for row in group:
            result[row.row_id] = decision
    return result
