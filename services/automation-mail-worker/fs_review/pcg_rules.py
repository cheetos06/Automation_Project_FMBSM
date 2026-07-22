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
        "immobilisations incorporelles en cours",
    ),
    "asset_land": ("terrains",),
    "asset_construction": ("constructions",),
    "asset_technical_equipment": (
        "installations techniques materiel et outillage industriels",
        "installations techniques materiel et outillages industriels",
        "instal techniques materiel et outillages indus",
    ),
    "asset_other_corporeal": ("autres immobilisations corporelles",),
    "asset_affected_fixed_assets": (
        "immobilisations affectees ou mises a disposition",
        "immobilisations recues en affectation",
    ),
    "asset_corporeal_in_progress": (
        "immobilisations corporelles en cours avances et acomptes",
        "immobilisations corporelles en cours",
        "immobilisations corp en cours av et acptes",
    ),
    "asset_participations": ("participations",),
    "asset_related_participation_receivables": (
        "creances rattachees a des participations",
    ),
    "asset_portfolio_activity": (
        "titres immobilises de l activite en portefeuille",
        "titres de portefeuille",
    ),
    "asset_other_fixed_securities": (
        "autres titres immobilises",
        "autres valeurs mobilieres immobilisees",
    ),
    "asset_loans": ("prets", "prets et creances immobilisees"),
    "asset_other_financial": (
        "autres immobilisations financieres",
        "prets et autres immobilisations financieres",
    ),
    "asset_redemption_premium": ("primes de remboursement des emprunts",),
    "asset_charges_to_spread": ("charges a repartir sur plusieurs exercices",),
    "asset_conversion_difference": (
        "ecarts de conversion actif",
        "differences de conversion actif",
        "ecarts de conversion et differences d evaluation actif",
        "ecarts de conversion et diff d evaluation actif",
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
    "marketable_securities": (
        "valeurs mobilieres de placement",
        # Formal detail caption beneath the PCG VMP heading (Art. 821-1).
        "autres titres",
    ),
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
    "hospital_activity_revenue": (
        "produits de l activite hospitaliere",
        "produits activite hospitaliere",
    ),
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
    "stock_variation_merchandise": (
        "variation de stock marchandises",
        "variation de stocks",
        "variations de stocks",
    ),
    "other_own_funds": (
        "autres fonds propres",
        "avances conditionnees",
        "fonds non remboursables",
    ),
    "concession_grantor_rights": (
        "droits du concedant",
        "droits des locataires attributaires",
    ),
    "purchases_raw_materials": (
        "achats de matieres premieres et autres approvisionnements",
    ),
    "stock_variation_raw_materials": (
        "variation de stock matieres premieres et approvisionnements",
        "variation de stocks",
        "variations de stocks",
    ),
    "external_charges": ("autres achats et charges externes",),
    "external_purchases_services": (
        "achats d etudes et de prestations de services travaux et honoraires",
        "consommations de l exercice en provenance des tiers",
        "autres achats et charges externes",
    ),
    "external_subcontracting": (
        "sous traitance",
        "autres achats et charges externes",
    ),
    "external_rentals": ("locations", "autres achats et charges externes"),
    "external_maintenance": (
        "entretien et reparations",
        "autres achats et charges externes",
    ),
    "external_insurance": (
        "primes d assurances",
        "autres achats et charges externes",
    ),
    "external_studies": ("etudes et recherches", "autres achats et charges externes"),
    "external_personnel": (
        "personnel exterieur",
        "autres achats et charges externes",
    ),
    "external_fees": (
        "remunerations intermediaires et honoraires",
        "honoraires",
        "autres achats et charges externes",
    ),
    "external_advertising": (
        "publicite publications relations publiques",
        "autres achats et charges externes",
    ),
    "external_transport": ("transports", "autres achats et charges externes"),
    "external_travel": (
        "deplacements missions et receptions",
        "autres achats et charges externes",
    ),
    "external_telecommunications": (
        "frais postaux et telecommunications",
        "autres achats et charges externes",
    ),
    "external_banking": (
        "services bancaires",
        "autres achats et charges externes",
    ),
    "external_royalties": ("redevances", "autres achats et charges externes"),
    "external_other": (
        "autres services exterieurs",
        "autres achats et charges externes",
    ),
    "taxes": ("impots taxes et versements assimiles",),
    "salaries": ("salaires", "salaires et traitements"),
    "social_charges": ("cotisations sociales", "charges sociales"),
    "other_operating_charges": ("autres charges",),
    "amortization_operating": (
        "dotations aux amortissements",
        "dotations aux amortissements sur immobilisations",
        "dotations d exploitation sur immobilisations",
        "dotations d exploitation",
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
        "revenus des avances prets participatifs et autres",
    ),
    "financial_other_interest_income": ("autres interets et produits assimiles",),
    "financial_positive_exchange": ("differences positives de change",),
    "financial_negative_exchange": ("differences negatives de change",),
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
    "exceptional_management_income": (
        "produits exceptionnels sur operations de gestion",
        "produits exceptionnels",
    ),
    "exceptional_management_charges": (
        "charges exceptionnelles sur operations de gestion",
        "charges exceptionnelles",
    ),
    "exceptional_reversals": (
        "reprises exceptionnelles sur depreciations et provisions",
        "reprises sur provisions et depreciations et transferts",
        "produits exceptionnels",
    ),
    "exceptional_dotations": (
        "dotations exceptionnelles aux amortissements et provisions",
        "charges exceptionnelles",
    ),
    "employee_participation": ("participation des salaries",),
    "income_tax": ("impots sur les benefices", "impot sur les benefices"),
    "asset_disposal_income": (
        "produits de cession d immobilisations",
        "produits des cessions d elements d actif",
    ),
    "asset_disposal_cost": (
        "valeurs comptables des immobilisations incorporelles et corporelles cedees",
        "valeurs comptables des elements d actifs cedes demolis mis au rebut",
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

# English statutory captions occur in translated French-company accounts. The
# account classification remains PCG-based; these are presentation aliases,
# not a second chart of accounts.
ENGLISH_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_intangible_goodwill": ("goodwill",),
    "asset_construction": ("buildings",),
    "asset_other_corporeal": ("other tangible fixed assets",),
    "asset_corporeal_in_progress": ("tangible fixed assets in progress",),
    "asset_other_financial": ("other financial assets",),
    "stock_merchandise": ("merchandise", "inventories of merchandise"),
    "clients": ("trade receivable accounts", "trade receivables"),
    "other_receivables": ("sundry debtors", "other receivables"),
    "prepaid_expenses": ("prepaid expenses",),
    "equity_capital": ("share capital",),
    "equity_legal_reserve": ("legal reserve",),
    "equity_retained_earnings": (
        "profit and loss account brought forward",
        "retained earnings",
    ),
    "equity_result": (
        "result for the financial year profit or loss",
        "profit or loss for the financial year",
    ),
    "provisions_risks": ("provisions for risks",),
    "provisions_charges": ("provisions for charges",),
    "bank_debt": (
        "borrowing from credit institutions",
        "borrowings from credit institutions",
    ),
    "customer_advances": ("customers advance", "customer advances"),
    "supplier_debt": (
        "trade accounts payable and related liabilities",
        "trade payables",
    ),
    "tax_social_debt": ("taxes and social security debts",),
    "fixed_asset_debt": ("liabilities related to fixed assets",),
    "other_debts": ("other creditors", "other liabilities"),
    "sales_merchandise": ("sales of purchased goods merchandise",),
    "production_services": ("services", "sales of services"),
    "reversals_operating": (
        "write back of depreciation provisions and transferred charges",
        "write back of depreciation and provisions",
    ),
    "legacy_charge_transfers": (
        "write back of depreciation provisions and transferred charges",
    ),
    "other_operating_income": ("other income", "other operating income"),
    "purchases_merchandise": (
        "purchases of goods including customs duties",
        "purchases of goods",
    ),
    "stock_variation_merchandise": (
        "change in inventory of purchased goods",
        "change in inventories of merchandise",
    ),
    "purchases_raw_materials": ("purchases of raw materials and supplies",),
    "external_charges": ("other purchases and expenses", "external charges"),
    "taxes": ("indirect taxes", "taxes and similar payments"),
    "salaries": ("wages and salaries",),
    "social_charges": ("social security charges",),
    "amortization_operating": ("depreciation", "depreciation expense"),
    "other_operating_charges": ("other expenses", "other operating expenses"),
    "financial_charges": ("interest payable and similar charges",),
}

for _category, _aliases in ENGLISH_CATEGORY_ALIASES.items():
    CATEGORY_ALIASES[_category] = CATEGORY_ALIASES.get(_category, ()) + _aliases


# Statutory captions specific to public hospitals using instruction M21.
# These categories are activated only after M21 account/label signatures have
# identified the chart profile.
M21_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "m21_apports": ("apports",),
    "m21_investment_surplus": ("excedents affectes a l investissement",),
    "m21_compensation_reserve": ("reserves de compensation",),
    "m21_retained_surplus": ("report a nouveau excedentaire",),
    "m21_retained_deficit": ("report a nouveau deficitaire",),
    "m21_receivables_hospitalized": ("hospitalises et consultants",),
    "m21_receivables_pivot": ("caisse pivot",),
    "m21_receivables_third_party": ("autres tiers payants",),
    "m21_receivables_other": ("autres redevables", "autres"),
    "m21_receipts_to_regularize": (
        "recettes a classer et a regulariser credit",
        "recettes a classer ou a regulariser",
    ),
    "m21_expenses_to_regularize": (
        "depenses a classer ou regulariser",
        "depenses a classer ou a regulariser",
    ),
    "m21_stocked_purchases": (
        "achats stockes de matieres premieres ou fournitures",
        "achats stockes de matieres premieres ou",
    ),
    "m21_other_supply_variation": (
        "variation stocks des autres approvisionnements",
        "variation stocks des autres approvis",
    ),
    "m21_nonstocked_purchases": (
        "achats non stockes matieres et fournitures",
        "achats non stockes mat et fournitures",
    ),
    "m21_payroll_taxes": ("impots et taxes sur remunerations",),
    "m21_other_taxes": (
        "impots taxes et versements assimiles autres",
        "impots et taxes autres",
    ),
    "m21_personnel_remuneration": (
        "remunerations et autres charges de personnel",
        "remun et autres charges de personnel",
    ),
    "m21_fixed_asset_depreciation": (
        "dotations aux amortissements et depreciations sur immobilisations",
        "dot aux amort et deprec sur immo",
    ),
    "m21_current_asset_depreciation": (
        "dotations aux depreciations sur actif circulant",
        "dot aux deprec sur actif circulant",
    ),
    "m21_operating_provisions": (
        "dotations amortissements provisions depreciations risques et charges",
        "dot amort prov deprec risques et charges",
    ),
    "m21_vmp_income": ("revenus des valeurs mobilieres de placement",),
    "m21_exceptional_income_current": (
        "produits exceptionnels operations de gestion exercice courant",
        "prod except op gestion exercice courant",
    ),
    "m21_exceptional_income_prior": (
        "produits exceptionnels operations de gestion exercices anterieurs",
        "prod except op gestion exer anter",
    ),
    "m21_exceptional_income_capital": (
        "produits exceptionnels operations en capital",
        "prod excep operations en capital",
    ),
    "m21_exceptional_charge_current": (
        "charges exceptionnelles exercice courant",
        "charges except exercice courant",
    ),
    "m21_exceptional_charge_prior": (
        "charges exceptionnelles exercices anterieurs",
        "charges except exercices anterieurs",
    ),
    "m21_exceptional_charge_capital": (
        "charges exceptionnelles sur operations en capital",
        "charg except sur operations en capital",
    ),
    "m21_regulated_provision_expense": (
        "dotations aux provisions reglementees",
    ),
    "operating_subsidies": ("subv d exploitation et participations",),
    "other_operating_income": ("autres produits de gestion courante",),
    "other_operating_charges": ("autres charges de gestion courante",),
    "financial_reversals": ("reprise sur provisions",),
    "financial_dotations": ("dotations aux amort deprec et provis",),
}

for _category, _aliases in M21_CATEGORY_ALIASES.items():
    CATEGORY_ALIASES[_category] = CATEGORY_ALIASES.get(_category, ()) + _aliases


# ANC 2016-03 / 2026 asset-management compendium presentation for SCPI.
SCPI_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "scpi_rental_property": (
        "immobilisations locatives",
        "terrains et constructions locatives",
    ),
    "scpi_property_in_progress": ("immobilisations en cours",),
    "scpi_major_maintenance_provision": (
        "provisions liees aux placements immobiliers",
        "gros entretiens",
    ),
    "scpi_tenant_receivables": ("locataires et comptes rattaches",),
    "scpi_other_receivables": ("autres creances",),
    "scpi_cash": (
        "valeurs de placement et disponibilites",
        "autres disponibilites",
    ),
    "scpi_financial_debt": ("dettes financieres",),
    "scpi_associate_debt": ("dette associee",),
    "scpi_operating_debt": ("dettes d exploitation",),
    "scpi_misc_debt": ("dettes diverses",),
    "scpi_net_regularization": ("comptes de regularisation actif et passif",),
    "scpi_rent_income": ("loyers",),
    "scpi_recharged_income": ("charges facturees",),
    "scpi_recharged_property_expense": (
        "charges ayant leur contrepartie en produits",
    ),
    "scpi_other_property_expense": ("autres charges immobilieres",),
    "scpi_management_commission": ("commissions de la societe de gestion",),
    "scpi_company_operating_expense": (
        "charges d exploitation de la societe",
    ),
    "scpi_operating_amortization": (
        "dotations aux amortissements d exploitation",
    ),
    "scpi_other_operating_expense": ("autres charges",),
    "scpi_financial_income": ("autres produits financiers",),
    "scpi_financial_expense": ("charges d interets des emprunts",),
}

for _category, _aliases in SCPI_CATEGORY_ALIASES.items():
    CATEGORY_ALIASES[_category] = CATEGORY_ALIASES.get(_category, ()) + _aliases


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
        ("262", "asset_participations"),
        ("266", "asset_participations"),
        ("267", "asset_related_participation_receivables"),
        ("268", "asset_related_participation_receivables"),
        ("271", "asset_other_fixed_securities"),
        ("272", "asset_other_fixed_securities"),
        ("273", "asset_portfolio_activity"),
        ("274", "asset_loans"),
        ("275", "asset_other_financial"),
        ("27682", "asset_other_fixed_securities"),
        ("27684", "asset_loans"),
        ("276", "asset_other_financial"),
        ("277", "asset_other_fixed_securities"),
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
        ("2971", "asset_other_fixed_securities"),
        ("2972", "asset_other_fixed_securities"),
        ("2973", "asset_portfolio_activity"),
        ("2974", "asset_loans"),
        ("2975", "asset_other_financial"),
        ("2976", "asset_other_financial"),
        ("2977", "asset_other_fixed_securities"),
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
    if code.startswith("47"):
        return _decision(
            "contextual_suspense",
            "other suspense/transitory account requires clearing or supporting detail",
            True,
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
    if code.startswith(("1671", "1673", "1674")):
        return _decision("other_own_funds", "PCG other-own-funds family")
    if code.startswith("151"):
        return _decision("provisions_risks", "provision for risks")
    if code.startswith("15"):
        return _decision("provisions_charges", "provision for charges")
    if code.startswith("229"):
        return _decision("concession_grantor_rights", "concession grantor rights")
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
        if code.startswith("761") and any(
            word in text for word in ("avance", "pret")
        ):
            return _decision(
                "financial_other_fixed_income",
                "custom 761 subdivision identified as loan/advance income by label",
            )
        if "dividende" in text or code.startswith("761"):
            return _decision(
                "financial_participation_income",
                "participation income by PCG family or dividend economic nature",
            )
        if code.startswith(("762", "764")):
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
        if code.startswith(("763", "765", "768")):
            return _decision(
                "financial_other_interest_income",
                "other interest and similar financial income",
            )
        return _decision("financial_income", "custom financial income")
    if code.startswith("775"):
        return _decision("asset_disposal_income", "legacy asset disposal proceeds")
    if code.startswith(("771", "772")):
        return _decision("exceptional_management_income", "legacy management exceptional income")
    if code.startswith("77"):
        return _decision("exceptional_income", "exceptional income")
    if code.startswith("78"):
        if code.startswith("786"):
            return _decision("financial_reversals", "financial reversals")
        if code.startswith("787"):
            return _decision("exceptional_reversals", "exceptional reversals")
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
    external_charge_prefixes = (
        (("604", "605", "606"), "external_purchases_services"),
        (("611",), "external_subcontracting"),
        (("613",), "external_rentals"),
        (("615",), "external_maintenance"),
        (("616",), "external_insurance"),
        (("617",), "external_studies"),
        (("621",), "external_personnel"),
        (("622",), "external_fees"),
        (("623",), "external_advertising"),
        (("624",), "external_transport"),
        (("625",), "external_travel"),
        (("626",), "external_telecommunications"),
        (("627",), "external_banking"),
        (("612", "614", "618", "628"), "external_other"),
    )
    if code.startswith("6285") and "redevance" in text:
        return _decision("external_royalties", "royalty identified by economic label")
    for prefixes, category in external_charge_prefixes:
        if code.startswith(prefixes):
            return _decision(category, f"PCG external-charge family {prefixes[0]}")
    if code.startswith(("61", "62")):
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
    if code.startswith("666"):
        return _decision("financial_negative_exchange", "negative exchange difference")
    if code.startswith("66"):
        return _decision("financial_charges", "financial charges")
    if code.startswith("675"):
        return _decision("asset_disposal_cost", "legacy asset disposal carrying value")
    if code.startswith(("671", "672")):
        return _decision("exceptional_management_charges", "legacy management exceptional charge")
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
        return _decision("exceptional_dotations", "exceptional dotations")
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
    chart_profile: str | None = None,
) -> dict[int, RuleDecision]:
    """Classify rows after aggregating repeated rows by account identifier."""

    observations = list(observations)
    if chart_profile is None:
        chart_profile = detect_chart_profile(observations)
    grouped: dict[str, list[AccountObservation]] = defaultdict(list)
    for observation in observations:
        grouped[account_identity(observation.account)].append(observation)

    liaison_rows = [
        observation
        for observation in observations
        if account_digits(observation.account).startswith("18")
    ]
    liaison_gross = sum(abs(row.amount) for row in liaison_rows)
    liaison_balance = sum(row.amount for row in liaison_rows)
    liaison_tolerance = max(2.0, liaison_gross * 1e-9)
    liaison_nets_to_zero = bool(liaison_rows) and abs(liaison_balance) <= liaison_tolerance

    result: dict[int, RuleDecision] = {}
    for group in grouped.values():
        balance = sum(row.amount for row in group)
        gross = sum(abs(row.amount) for row in group)
        label = " ".join(dict.fromkeys(row.label for row in group if row.label))
        code = account_digits(group[0].account)
        if code.startswith("18"):
            decision = (
                _decision(
                    "non_fs_internal_transfer",
                    "PCG class-18 establishment liaison accounts net to zero "
                    "for the reporting entity",
                )
                if liaison_nets_to_zero
                else _decision(
                    None,
                    "PCG class-18 liaison accounts do not net to zero; reporting "
                    "perimeter or counterparty detail is required",
                )
            )
        elif balance_sign(balance) == 0 and gross > ZERO_TOLERANCE:
            decision = _decision(
                "non_fs_net_zero_account",
                "repeated rows for the same account offset to a zero closing balance",
            )
        elif chart_profile == "m21" and abs(balance) > ZERO_TOLERANCE:
            if code.startswith("102"):
                decision = _decision("m21_apports", "M21 account 102 apports")
            elif code.startswith("10682"):
                decision = _decision(
                    "m21_investment_surplus",
                    "M21 surplus allocated to investment",
                )
            elif code.startswith(("10686", "10687")):
                decision = _decision(
                    "m21_compensation_reserve", "M21 compensation reserve"
                )
            elif code.startswith("110"):
                decision = _decision(
                    "m21_retained_surplus", "M21 surplus carried forward"
                )
            elif code.startswith("119"):
                decision = _decision(
                    "m21_retained_deficit", "M21 deficit carried forward"
                )
            elif code.startswith("151"):
                decision = _decision("provisions_risks", "M21 risk provision")
            elif code.startswith("152"):
                decision = _decision("provisions_risks", "M21 borrowing risk provision")
            elif code.startswith(("153", "157", "158")):
                decision = _decision("provisions_charges", "M21 charge provision")
            elif code.startswith(("4111", "4121", "4161", "4181", "491")):
                decision = _decision(
                    "m21_receivables_hospitalized",
                    "M21 hospitalized/consultant receivable family",
                )
            elif code.startswith(("4112", "4122", "4162", "4182")):
                decision = _decision(
                    "m21_receivables_pivot", "M21 caisse-pivot receivable family"
                )
            elif code.startswith(
                (
                    "4113", "4114", "4115", "4116", "4117",
                    "4123", "4124", "4125", "4126", "4127",
                    "4163", "4164", "4165", "4166", "4167",
                )
            ):
                decision = _decision(
                    "m21_receivables_third_party",
                    "M21 other third-party payer receivable family",
                )
            elif code.startswith(
                (
                    "4118", "4128", "414", "4168", "4184", "4188",
                )
            ):
                decision = _decision(
                    "m21_receivables_other", "M21 other redevable family"
                )
            elif code.startswith("471"):
                decision = _decision(
                    "m21_receipts_to_regularize",
                    "M21 receipts awaiting classification or regularization",
                )
            elif code.startswith(("472", "478")):
                decision = _decision(
                    "m21_expenses_to_regularize",
                    "M21 expenses/other transitory amounts awaiting regularization",
                )
            elif code.startswith("456"):
                decision = _decision(
                    "non_fs_internal_transfer",
                    "M21 liaison account between principal and annex activities",
                )
            elif code.startswith(("602", "6092")):
                decision = _decision(
                    "m21_stocked_purchases",
                    "M21 stocked supplies and related rebates",
                )
            elif code.startswith("6032"):
                decision = _decision(
                    "m21_other_supply_variation",
                    "M21 stock variation of other supplies",
                )
            elif code.startswith(("606", "6096")):
                decision = _decision(
                    "m21_nonstocked_purchases",
                    "M21 non-stocked materials/supplies and related rebates",
                )
            elif code.startswith(("631", "633")):
                decision = _decision(
                    "m21_payroll_taxes", "M21 taxes based on remuneration"
                )
            elif code.startswith(("635", "637")):
                decision = _decision(
                    "m21_other_taxes", "M21 taxes not based on remuneration"
                )
            elif code.startswith(("641", "642", "648", "649")):
                decision = _decision(
                    "m21_personnel_remuneration",
                    "M21 remuneration and other personnel charges",
                )
            elif code.startswith("657"):
                decision = _decision(
                    "other_operating_charges", "M21 subsidies and participations paid"
                )
            elif code.startswith(("6811", "6816")):
                decision = _decision(
                    "m21_fixed_asset_depreciation",
                    "M21 fixed-asset amortization and depreciation",
                )
            elif code.startswith("6817"):
                decision = _decision(
                    "m21_current_asset_depreciation",
                    "M21 current-asset depreciation",
                )
            elif code.startswith("6815"):
                decision = _decision(
                    "m21_operating_provisions",
                    "M21 operating provisions for risks and charges",
                )
            elif code.startswith("768"):
                decision = _decision(
                    "m21_vmp_income", "M21 income from marketable securities"
                )
            elif code.startswith(("771", "778")):
                decision = _decision(
                    "m21_exceptional_income_current",
                    "M21 current-year exceptional management income",
                )
            elif code.startswith(("772", "773")):
                decision = _decision(
                    "m21_exceptional_income_prior",
                    "M21 prior-year exceptional management income",
                )
            elif code.startswith(("775", "777")):
                decision = _decision(
                    "m21_exceptional_income_capital",
                    "M21 exceptional income on capital operations",
                )
            elif code.startswith(("671", "678")):
                decision = _decision(
                    "m21_exceptional_charge_current",
                    "M21 current-year exceptional charges",
                )
            elif code.startswith(("672", "673")):
                decision = _decision(
                    "m21_exceptional_charge_prior",
                    "M21 prior-year exceptional charges",
                )
            elif code.startswith("675"):
                decision = _decision(
                    "m21_exceptional_charge_capital",
                    "M21 exceptional charges on capital operations",
                )
            elif code.startswith("6874"):
                decision = _decision(
                    "m21_regulated_provision_expense",
                    "M21 regulated-provision dotation",
                )
            elif code.startswith(("241", "249")):
                decision = _decision(
                    "asset_affected_fixed_assets",
                    "M21 affected/made-available fixed asset and related contra-account",
                )
            elif code.startswith("541"):
                decision = _decision(
                    "cash", "M21 availability held by advance/receipt officers"
                )
            elif code.startswith("73"):
                decision = _decision(
                    "hospital_activity_revenue",
                    "M21 products of hospital activity",
                )
            else:
                decision = classify_account(group[0].account, label, balance, regime)
        elif chart_profile == "scpi" and abs(balance) > ZERO_TOLERANCE:
            if code.startswith("1572"):
                decision = _decision(
                    "scpi_major_maintenance_provision",
                    "SCPI provision for major maintenance",
                )
            elif code.startswith("213"):
                decision = _decision(
                    "scpi_rental_property", "SCPI rental land/buildings"
                )
            elif code.startswith("23"):
                decision = _decision(
                    "scpi_property_in_progress", "SCPI property in progress"
                )
            elif code.startswith(("411", "418", "491")):
                decision = _decision(
                    "scpi_tenant_receivables", "SCPI tenants and related accounts"
                )
            elif code.startswith("445"):
                decision = _decision(
                    "scpi_other_receivables" if balance > 0 else "scpi_operating_debt",
                    "SCPI tax receivable/debt classified from closing sign",
                )
            elif code.startswith("467"):
                decision = _decision(
                    "scpi_other_receivables" if balance > 0 else "scpi_misc_debt",
                    "SCPI miscellaneous debtor/creditor classified from closing sign",
                )
            elif code.startswith(("462", "409")):
                decision = _decision(
                    "scpi_other_receivables", "SCPI other operating receivable"
                )
            elif code.startswith(("481", "486", "487", "488")):
                decision = _decision(
                    "scpi_net_regularization",
                    "SCPI net active/passive regularization accounts",
                )
            elif code.startswith(("50", "51", "53")):
                decision = _decision(
                    "scpi_cash", "SCPI investments and cash equivalents"
                )
            elif code.startswith(("164", "168")):
                decision = _decision(
                    "scpi_financial_debt", "SCPI financial debt"
                )
            elif code.startswith("455"):
                decision = _decision("scpi_associate_debt", "SCPI associate debt")
            elif code.startswith(("401", "404", "408")):
                decision = _decision(
                    "scpi_operating_debt", "SCPI operating supplier debt"
                )
            elif code.startswith("419"):
                decision = _decision("scpi_misc_debt", "SCPI miscellaneous debt")
            elif code.startswith("601"):
                decision = _decision(
                    "scpi_recharged_property_expense",
                    "SCPI property costs having a corresponding recharge",
                )
            elif code.startswith(("604", "605", "606", "607")):
                decision = _decision(
                    "scpi_other_property_expense", "SCPI other property charges"
                )
            elif code.startswith("6221"):
                decision = _decision(
                    "scpi_management_commission", "SCPI management-company commission"
                )
            elif code.startswith("6812"):
                decision = _decision(
                    "scpi_operating_amortization",
                    "SCPI operating amortization",
                )
            elif code.startswith("658"):
                decision = _decision(
                    "scpi_other_operating_expense",
                    "SCPI miscellaneous operating charges",
                )
            elif code.startswith(("61", "62", "63", "65")):
                decision = _decision(
                    "scpi_company_operating_expense",
                    "SCPI company operating charges",
                )
            elif code.startswith("701"):
                decision = _decision("scpi_rent_income", "SCPI rental income")
            elif code.startswith("702"):
                decision = _decision(
                    "scpi_recharged_income", "SCPI property charges invoiced"
                )
            elif code.startswith("768"):
                decision = _decision(
                    "scpi_financial_income", "SCPI other financial income"
                )
            elif code.startswith("661"):
                decision = _decision(
                    "scpi_financial_expense", "SCPI borrowing interest"
                )
            else:
                decision = classify_account(group[0].account, label, balance, regime)
        else:
            decision = classify_account(group[0].account, label, balance, regime)
        for row in group:
            result[row.row_id] = decision
    return result


def detect_chart_profile(observations: Iterable[AccountObservation]) -> str:
    """Identify a specialized chart from accounting signatures, never ticket IDs."""

    observations = list(observations)
    class_73 = [
        row
        for row in observations
        if account_digits(row.account).startswith("73")
        and abs(row.amount) > ZERO_TOLERANCE
    ]
    labels = " ".join(normalize_text(row.label) for row in class_73)
    hospital_markers = (
        "hospital",
        "sejour",
        "soins",
        "consultant",
        "dialyse",
        "mco",
        "ghs",
    )
    has_regie = any(
        account_digits(row.account).startswith("541")
        and any(marker in normalize_text(row.label) for marker in ("regisseur", "gisseur"))
        for row in observations
    )
    marker_count = sum(marker in labels for marker in hospital_markers)
    if (
        len(class_73) >= 3
        and ("hospital" in labels or marker_count >= 2)
        and (has_regie or len(class_73) >= 10)
    ):
        return "m21"
    significant = [
        row for row in observations if abs(row.amount) > ZERO_TOLERANCE
    ]
    has_scpi_property = any(
        account_digits(row.account).startswith("213")
        and any(
            marker in normalize_text(row.label)
            for marker in ("ensemble immobilier", "immeuble locatif")
        )
        for row in significant
    )
    has_scpi_maintenance = any(
        account_digits(row.account).startswith("1572")
        and "gros entretien" in normalize_text(row.label)
        for row in significant
    )
    has_scpi_rents = any(
        account_digits(row.account).startswith("701")
        and "loyer" in normalize_text(row.label)
        for row in significant
    )
    if has_scpi_property and has_scpi_maintenance and has_scpi_rents:
        return "scpi"
    return "pcg"
