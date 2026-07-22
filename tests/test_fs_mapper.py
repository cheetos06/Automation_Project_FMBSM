import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
FS_REVIEW = SERVICE / "fs_review"
sys.path.insert(0, str(SERVICE))
sys.path.insert(0, str(FS_REVIEW))

from map_bg_to_fs import BGRow, FSLine, load_fs
from fs_mapping import build_fs_mapping_rows
from pcg_rules import classify_account
from rule_based_mapper import PCGMapper


def fs_line(label, amount, family):
    return FSLine(
        label=label,
        amount=amount,
        amount_n_1=None,
        gross=None,
        depreciation=None,
        normalized=label.lower(),
        summary=False,
        statement_family=family,
    )


def test_category_difference_ignores_assigned_lines_without_amounts():
    row = BGRow(account="512000", label="Banque", amount=100.0)
    line = FSLine(
        label="Banques",
        amount=None,
        amount_n_1=None,
        gross=None,
        depreciation=None,
        normalized="banques",
        summary=False,
    )
    mapper = PCGMapper([row], [line])
    category = mapper.assigned_categories[0]
    assert category is not None
    mapper.row_line_indexes[id(row)] = 0

    assert mapper.build_category_differences({category: [row]}) == []


def test_flat_fs_extract_infers_statutory_statement_sections(tmp_path):
    extract = [
        {"libelle": "Disponibilites", "montant_n": 10},
        {"libelle": "TOTAL GENERAL ACTIF", "montant_n": 10},
        {"libelle": "Capital social", "montant_n": 10},
        {"libelle": "TOTAL GENERAL PASSIF", "montant_n": 10},
        {"libelle": "Production vendue", "montant_n": 20},
        {"libelle": "Autres charges", "montant_n": 10},
    ]
    path = tmp_path / "fs.json"
    path.write_text(json.dumps(extract), encoding="utf-8")

    lines = load_fs(path)

    assert [line.statement_family for line in lines] == [
        "asset",
        "asset",
        "liability",
        "liability",
        "result",
        "result",
    ]


def test_plain_capital_heading_starts_liability_section_in_flat_extract(tmp_path):
    extract = [
        {"libelle": "Disponibilites", "montant_n": 10014},
        {"libelle": "TOTAL GENERAL ACTIF", "montant_n": 10014},
        {"libelle": "Capital", "montant_n": 10014},
        {"libelle": "TOTAL GENERAL PASSIF", "montant_n": 10014},
        {"libelle": "Chiffre d'affaires net", "montant_n": 20000},
        {"libelle": "Autres achats et charges externes", "montant_n": 9986},
    ]
    path = tmp_path / "fs.json"
    path.write_text(json.dumps(extract), encoding="utf-8")

    lines = load_fs(path)

    assert [line.statement_family for line in lines] == [
        "asset",
        "asset",
        "liability",
        "liability",
        "result",
        "result",
    ]


def test_cross_statement_equal_sum_is_emitted_and_audited():
    rows = [
        BGRow("421000", "Personnel - remunerations dues", -40),
        BGRow("445710", "TVA collectee", -60),
        BGRow("512000", "Banque", 100),
    ]
    lines = [fs_line("Disponibilites", 200, "asset")]

    mapper = PCGMapper(rows, lines)
    stats = mapper.map()

    assert all(row.mapping == lines[0].label for row in rows)
    assert stats["method_counts"] == {"cross_statement_equal_sum": 3}
    audit = stats["reconciliation"][0]
    assert audit["status"] == "exact"
    assert audit["conflicting_statement_families"] == ["liability->asset"]
    assert audit["participating_accounts"] == ["512000", "421000", "445710"]
    assert stats["source_alignment"]["status"] == "unproven"

    fs_rows = build_fs_mapping_rows(rows, lines, mapper, stats)
    assert fs_rows[0]["mapping method"] == "cross_statement_equal_sum"
    assert fs_rows[0]["mapping confidence"] == 0.6
    assert fs_rows[0]["mapping amount difference"] == 0.0
    assert fs_rows[0]["conflicting statement families"] == "liability->asset"


def test_cross_statement_stock_combination_is_restored_as_fallback():
    rows = [
        BGRow("370000", "Stock de marchandises", 100),
        BGRow("603710", "Variation de stock de marchandises", 50),
    ]
    lines = [fs_line("Marchandises", 150, "asset")]

    stats = PCGMapper(rows, lines).map()

    assert all(row.mapping == lines[0].label for row in rows)
    assert stats["method_counts"] == {"cross_statement_equal_sum": 2}
    assert stats["reconciliation"][0]["conflicting_statement_families"] == [
        "result->asset"
    ]
    assert stats["source_alignment"]["status"] == "unproven"


def test_exact_row_fallback_keeps_cross_statement_match_and_conflict():
    row = BGRow("762500", "Revenus des prets", -100)
    lines = [fs_line("Autres immobilisations financieres", 100, "asset")]

    stats = PCGMapper([row], lines).map()

    assert row.mapping == lines[0].label
    assert stats["method_counts"] == {"row_exact": 1}
    assert stats["reconciliation"][0]["conflicting_statement_families"] == [
        "result->asset"
    ]


def test_personnel_debt_cannot_fill_income_statement_revenue():
    row = BGRow("421000", "Personnel - remunerations dues", -100)
    lines = [fs_line("Production vendue de services", 100, "result")]

    PCGMapper([row], lines).map()

    assert row.mapping == ""


def test_generic_other_products_does_not_capture_fixed_asset_interest_income():
    row = BGRow("762600", "Revenus des prets", -100)
    lines = [fs_line("Autres produits", 100, "result")]
    lines[0].result_section = "operating"

    PCGMapper([row], lines).map()

    assert row.mapping == ""


def test_ifrs_consolidated_source_difference_is_not_semantically_mapped():
    row = BGRow("512000", "Banque", 100)
    lines = [
        fs_line("Tresorerie et equivalents de tresorerie", 120, "asset"),
        fs_line("Ecarts d'acquisition", 500, "asset"),
        fs_line("Droits d'utilisation des biens pris en location", 200, "asset"),
        fs_line("Interets minoritaires", 100, "liability"),
    ]
    for line in lines:
        line.scope = "consolidated"

    stats = PCGMapper([row], lines).map()

    assert stats["fs_framework"] == "ifrs_consolidated"
    assert stats["semantic_matches"] == 0
    assert row.mapping == ""


def test_generic_other_label_does_not_capture_external_charges():
    row = BGRow("625100", "Voyages et deplacements", 100)
    lines = [fs_line("Autres", 100, "result")]
    lines[0].result_section = "operating"

    PCGMapper([row], lines).map()

    assert row.mapping == ""


def test_generic_other_charges_is_not_external_charge_composite_evidence():
    rows = [
        BGRow("604000", "Prestations de services", 10),
        BGRow("616000", "Assurances", 20),
        BGRow("625000", "Deplacements", 30),
    ]
    lines = [
        fs_line("Autres achats et charges externes", 60, "result"),
        fs_line("Autres charges", 60, "result"),
    ]
    for line in lines:
        line.result_section = "operating"

    mapper = PCGMapper(rows, lines)
    stats = mapper.map()

    assert {row.mapping for row in rows} == {lines[0].label}
    assert mapper.justified_lines == {0}
    assert stats["repeated_exact_matches"] == 0


def test_negative_exchange_difference_maps_to_its_specific_financial_line():
    row = BGRow("666000", "Pertes de change", 100)
    lines = [fs_line("Differences negatives de change", 100, "result")]
    lines[0].result_section = "financial"

    PCGMapper([row], lines).map()

    assert row.mapping == "Differences negatives de change"


def test_exact_row_fallback_records_cross_section_conflict():
    row = BGRow("786600", "Reprise depreciation comptes courants", -100)
    lines = [
        fs_line(
            "Reprises sur amortissements, depreciations et provisions",
            100,
            "result",
        )
    ]
    lines[0].result_section = "operating"

    stats = PCGMapper([row], lines).map()

    assert row.mapping == lines[0].label
    assert stats["method_counts"] == {"row_exact": 1}
    assert stats["reconciliation"][0]["conflicting_statement_families"] == [
        "result:financial->result:operating"
    ]


def test_tax_and_social_debt_is_an_authorized_exact_composite():
    rows = [
        BGRow("421000", "Personnel - remunerations dues", -40),
        BGRow("431000", "Securite sociale", -30),
        BGRow("445710", "TVA collectee", -30),
    ]
    lines = [fs_line("Dettes fiscales et sociales", 100, "liability")]

    stats = PCGMapper(rows, lines).map()

    assert {row.mapping for row in rows} == {"Dettes fiscales et sociales"}
    assert stats["combined_match_details"][0]["rule"] == "tax_and_social_debt"


def test_english_tax_and_social_debt_is_the_same_authorized_composite():
    rows = [
        BGRow("421000", "Personnel - remuneration due", -40),
        BGRow("431000", "Social security", -30),
        BGRow("445710", "VAT payable", -30),
    ]
    lines = [fs_line("Taxes and social security debts", 100, "liability")]

    stats = PCGMapper(rows, lines).map()

    assert {row.mapping for row in rows} == {lines[0].label}
    assert stats["combined_match_details"][0]["rule"] == "tax_and_social_debt"


def test_same_statement_arbitrary_equal_sum_is_emitted_for_review():
    rows = [
        BGRow("101000", "Capital", -40),
        BGRow("164000", "Emprunt", -60),
    ]
    lines = [fs_line("Report a nouveau", 100, "liability")]

    stats = PCGMapper(rows, lines).map()

    assert all(row.mapping == lines[0].label for row in rows)
    assert stats["method_counts"] == {"permissive_equal_sum": 2}
    assert stats["reconciliation"][0]["confidence"] < 0.70


def test_same_statement_specific_equal_sum_keeps_exact_combination_method():
    rows = [
        BGRow("512000", "Banque", 40),
        BGRow("411000", "Clients", 60),
    ]
    line = fs_line("Disponibilites et creances clients", 100, "asset")

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["method_counts"] == {"exact_amount_combination": 2}
    audit = stats["mapping_audit_records"][0]
    assert audit["amount_difference"] == 0.0
    assert audit["participating_categories"] == ["cash", "clients"]


def test_generic_stock_variation_lines_are_disambiguated_by_exact_amount():
    rows = [
        BGRow("603100", "Variation matieres premieres", 100),
        BGRow("603700", "Variation marchandises", -200),
    ]
    lines = [
        fs_line("Variations de stocks", 100, "result"),
        fs_line("Variations de stocks", -200, "result"),
    ]
    for line in lines:
        line.result_section = "operating"
    mapper = PCGMapper(rows, lines)

    mapper.map()

    assert mapper.row_line_indexes[id(rows[0])] == 0
    assert mapper.row_line_indexes[id(rows[1])] == 1
    assert mapper.justified_lines == {0, 1}


def test_class_27_financial_assets_follow_official_pcg_subfamilies():
    assert classify_account("273000", "Titres de portefeuille", 1).category == (
        "asset_portfolio_activity"
    )
    assert classify_account("271000", "Titres immobilises", 1).category == (
        "asset_other_fixed_securities"
    )
    assert classify_account("274000", "Prets", 1).category == "asset_loans"
    assert classify_account("275000", "Depots et cautionnements", 1).category == (
        "asset_other_financial"
    )


def test_fixed_asset_loan_income_can_use_an_exact_aggregated_interest_line():
    row = BGRow("762600", "Revenus des prets", -100)
    lines = [fs_line("Autres interets et produits assimiles", 100, "result")]
    lines[0].result_section = "financial"

    stats = PCGMapper([row], lines).map()

    assert row.mapping == "Autres interets et produits assimiles"
    assert stats["combined_match_details"][0]["rule"] == (
        "financial_fixed_income_in_other_interest"
    )


def test_aggregated_financial_debt_accepts_only_financial_debt_categories():
    rows = [
        BGRow("164000", "Emprunts bancaires", -60),
        BGRow("168800", "Autres dettes financieres", -40),
    ]
    lines = [fs_line("Emprunts et dettes financieres divers", 100, "liability")]

    stats = PCGMapper(rows, lines).map()

    assert all(row.mapping for row in rows)
    assert stats["combined_match_details"][0]["rule"] == (
        "aggregated_financial_debt"
    )


def test_legacy_exceptional_capital_lines_use_only_capital_account_families():
    rows = [
        BGRow("775200", "Produits de cession d'immobilisations", -30),
        BGRow("778800", "Autres produits exceptionnels", -20),
        BGRow("675200", "Valeur comptable des immobilisations cedees", 40),
        BGRow("678800", "Autres charges exceptionnelles", 10),
    ]
    lines = [
        fs_line("Produits exceptionnels sur operations en capital", 50, "result"),
        fs_line("Charges exceptionnelles sur operations en capital", 50, "result"),
    ]
    for line in lines:
        line.result_section = "exceptional"

    stats = PCGMapper(rows, lines, regime="legacy").map()

    assert {row.mapping for row in rows[:2]} == {lines[0].label}
    assert {row.mapping for row in rows[2:]} == {lines[1].label}
    assert {item["rule"] for item in stats["combined_match_details"]} == {
        "legacy_capital_exceptional_income",
        "legacy_capital_exceptional_charges",
    }


def test_detailed_external_charge_maps_to_its_specific_presentation_line():
    row = BGRow("625100", "Voyages et deplacements", 137.22)
    lines = [fs_line("Deplacements, missions et receptions", 137.22, "result")]
    lines[0].result_section = "operating"

    PCGMapper([row], lines).map()

    assert row.mapping == "Deplacements, missions et receptions"


def test_external_charge_subfamilies_recombine_on_standard_pcg_line():
    rows = [
        BGRow("604000", "Prestations de services", 10),
        BGRow("616000", "Assurances", 20),
        BGRow("625000", "Deplacements", 30),
    ]
    lines = [fs_line("Autres achats et charges externes", 60, "result")]
    lines[0].result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert all(row.mapping for row in rows)
    assert stats["combined_match_details"][0]["rule"] == (
        "external_purchases_and_charges"
    )


def test_external_charge_subfamilies_recombine_on_translated_pcg_line():
    rows = [
        BGRow("604000", "Services", 10),
        BGRow("616000", "Insurance", 20),
        BGRow("625000", "Travel", 30),
    ]
    line = fs_line("Other purchases and expenses", 60, "result")
    line.result_section = "operating"

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["combined_match_details"][0]["rule"] == (
        "external_purchases_and_charges"
    )


def test_ancillary_revenue_uses_income_sign_when_residual_proves_services():
    rows = [
        BGRow("706000", "Services", -90),
        BGRow("708500", "Ancillary service revenue", -10),
    ]
    line = fs_line("Services", 100, "result")
    line.result_section = "operating"

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["contextual_resolved"] == 1
    assert stats["reconciliation"][0]["status"] == "exact"


def test_interest_charges_cannot_map_to_financial_dotations():
    row = BGRow("661500", "Interets comptes courants", 100)
    lines = [
        fs_line(
            "Dotations aux amortissements et provisions - charges financieres",
            100,
            "result",
        )
    ]
    lines[0].result_section = "financial"

    PCGMapper([row], lines).map()

    assert row.mapping == ""


def test_legacy_asset_disposal_accounts_use_specific_disposal_lines():
    rows = [
        BGRow("675600", "VNC immobilisation cedee", 100),
        BGRow("775600", "Produit cession immobilisation", -120),
    ]
    lines = [
        fs_line("Valeurs comptables des elements d actifs cedes", 100, "result"),
        fs_line("Produits des cessions d elements d actif", 120, "result"),
    ]
    for line in lines:
        line.result_section = "exceptional"

    PCGMapper(rows, lines, regime="legacy").map()

    assert rows[0].mapping == lines[0].label
    assert rows[1].mapping == lines[1].label


def test_tax_debt_does_not_fall_into_generic_other_debts():
    row = BGRow("447000", "Autres impots a payer", -100)
    lines = [fs_line("Autres dettes", 100, "liability")]

    PCGMapper([row], lines).map()

    assert row.mapping == ""


def test_plain_revenue_heading_starts_result_section_in_flat_extract(tmp_path):
    extract = [
        {"libelle": "Disponibilites", "montant_n": 10},
        {"libelle": "TOTAL ACTIF", "montant_n": 10},
        {"libelle": "Capital social", "montant_n": 10},
        {"libelle": "TOTAL PASSIF", "montant_n": 10},
        {"libelle": "Chiffre d'affaires", "montant_n": 20},
    ]
    path = tmp_path / "fs.json"
    path.write_text(json.dumps(extract), encoding="utf-8")

    assert load_fs(path)[-1].statement_family == "result"


def test_flat_extract_can_present_result_before_balance_sheet(tmp_path):
    extract = [
        {"libelle": "Production vendue", "montant_n": 20},
        {"libelle": "Autres charges", "montant_n": 10},
        {"libelle": "Total actif general", "montant_n": 10},
        {"libelle": "Total passif general", "montant_n": 10},
        {"libelle": "Capital social", "montant_n": 10},
        {"libelle": "Dettes fournisseurs", "montant_n": 5},
    ]
    path = tmp_path / "fs.json"
    path.write_text(json.dumps(extract), encoding="utf-8")

    lines = load_fs(path)

    assert [line.statement_family for line in lines] == [
        "result",
        "result",
        "asset",
        "liability",
        "liability",
        "liability",
    ]


def test_exceptional_dotation_cannot_map_to_management_charge_line():
    row = BGRow("687600", "Dotation exceptionnelle", 100)
    lines = [
        fs_line("Charges exceptionnelles sur operations de gestion", 100, "result")
    ]
    lines[0].result_section = "exceptional"

    PCGMapper([row], lines, regime="legacy").map()

    assert row.mapping == ""


def test_custom_761_loan_income_uses_economic_label_context():
    decision = classify_account(
        "761310", "Revenus des avances et prets participatifs", -100
    )

    assert decision.category == "financial_other_fixed_income"


def test_exceptional_categories_can_reconcile_to_only_available_total():
    rows = [
        BGRow("678800", "Autres charges exceptionnelles", 1),
        BGRow("687200", "Dotations exceptionnelles", 99),
    ]
    line = fs_line("Total Charges Exceptionnelles", 100, "result")
    line.result_section = "exceptional"
    line.summary = True

    stats = PCGMapper(rows, [line], regime="modern").map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["combined_match_details"][0]["rule"] == (
        "exceptional_charge_aggregate"
    )


def test_m21_chart_is_detected_and_maps_hospital_specific_accounts():
    rows = [
        BGRow("241800", "Autres immobilisations affectees", 100),
        BGRow("249800", "Droits du remettant", -40),
        BGRow("541100", "Disponibilites chez regisseurs d'avances", 10),
        BGRow("731111", "Groupes homogenes de sejour GHS", -100),
        BGRow("732100", "Hospitalises et consultants", -200),
        BGRow("733100", "Prestations de soins", -300),
    ]
    lines = [
        fs_line("Immobilisations affectees ou mises a disposition", 60, "asset"),
        fs_line("Disponibilites", 10, "asset"),
        fs_line("Produits de l'activite hospitaliere", 600, "result"),
    ]
    lines[-1].result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["chart_profile"] == "m21"
    assert all(row.mapping for row in rows)
    assert rows[0].mapping == lines[0].label
    assert rows[1].mapping == lines[0].label
    assert rows[2].mapping == lines[1].label
    assert {row.mapping for row in rows[3:]} == {lines[2].label}


def test_m21_official_subfamilies_reconcile_to_hospital_statement_lines():
    rows = [
        # M21 signature and hospital activity.
        BGRow("541100", "Disponibilites chez regisseur", 10),
        BGRow("731100", "Produits de l hospitalisation", -1),
        BGRow("732100", "Hospitalises et consultants", -2),
        BGRow("733100", "Prestations de soins", -3),
        # Receivables and equity subfamilies from the official nomenclature.
        BGRow("411100", "Hospitalises et consultants", 12),
        BGRow("416100", "Hospitalises contentieux", 3),
        BGRow("491000", "Depreciation comptes de redevables", -5),
        BGRow("411200", "Caisse pivot", 20),
        BGRow("418200", "Produits a recevoir caisse pivot", 2),
        BGRow("411300", "Caisses de securite sociale", 4),
        BGRow("411500", "Autres tiers payants", 6),
        BGRow("102100", "Dotation", -100),
        BGRow("119000", "Report a nouveau deficitaire", 4),
        # M21 operating and exceptional statement subfamilies.
        BGRow("602100", "Achats stockes", 30),
        BGRow("609200", "RRR achats stockes", -5),
        BGRow("631100", "Taxes sur remunerations", 7),
        BGRow("633100", "Taxes sur remunerations", 5),
        BGRow("771000", "Produit exceptionnel courant", -2),
        BGRow("778000", "Autre produit exceptionnel", -5),
    ]
    lines = [
        fs_line("Disponibilites", 10, "asset"),
        fs_line("Hospitalises et consultants", 10, "asset"),
        fs_line("Caisse pivot", 22, "asset"),
        fs_line("Autres tiers payants", 10, "asset"),
        fs_line("APPORTS", 100, "liability"),
        fs_line("Report a nouveau deficitaire", -4, "liability"),
        fs_line("Produits de l activite hospitaliere", 6, "result"),
        fs_line("Achats stockes de matieres premieres ou", 25, "result"),
        fs_line("Impots et taxes sur remunerations", 12, "result"),
        fs_line("Prod except op gestion exercice courant", 7, "result"),
    ]
    for line in lines[6:9]:
        line.result_section = "operating"
    lines[9].result_section = "exceptional"

    stats = PCGMapper(rows, lines).map()

    assert stats["chart_profile"] == "m21"
    assert stats["justified_fs_lines"] == len(lines)
    assert all(row.mapping for row in rows)


def test_scpi_statutory_hierarchy_keeps_parents_out_of_leaf_coverage(tmp_path):
    extract = {
        "lines": [
            {
                "libelle": "Immobilisations locatives",
                "montant_n": 120,
                "statement": "Etat du patrimoine",
            },
            {
                "libelle": "Terrains et constructions locatives",
                "montant_n": 120,
                "statement": "Etat du patrimoine",
            },
            {
                "libelle": "Dettes",
                "montant_n": -40,
                "statement": "Etat du patrimoine",
            },
            {
                "libelle": "Dettes financières",
                "montant_n": -40,
                "statement": "Etat du patrimoine",
            },
            {
                "libelle": "CAPITAUX PROPRES COMPTABLES (I+II+III+IV+V)",
                "montant_n": 80,
                "statement": "Etat du patrimoine",
                "is_total": True,
            },
            {
                "libelle": "Charges d'exploitation (II)",
                "montant_n": 12,
                "statement": "Compte de resultat",
            },
            {
                "libelle": "Charges d'exploitation de la société",
                "montant_n": 12,
                "statement": "Compte de resultat",
                # Canonical extraction may identify this line as a total from
                # typography; the ANC SCPI model makes it a detail line.
                "is_total": True,
            },
        ]
    }
    path = tmp_path / "fs.json"
    path.write_text(json.dumps(extract), encoding="utf-8")

    by_label = {line.label: line for line in load_fs(path)}

    assert by_label["Immobilisations locatives"].summary
    assert not by_label["Terrains et constructions locatives"].summary
    assert by_label["Dettes"].summary
    assert by_label["Dettes financières"].statement_family == "liability"
    assert by_label["CAPITAUX PROPRES COMPTABLES (I+II+III+IV+V)"].statement_family == (
        "liability"
    )
    assert by_label["Charges d'exploitation (II)"].summary
    assert not by_label["Charges d'exploitation de la société"].summary
    assert by_label["Charges d'exploitation de la société"].statement_family == (
        "result"
    )


def test_scpi_profile_maps_official_families_and_negative_debt_presentation():
    rows = [
        # Three independent signatures are required to activate the SCPI chart.
        BGRow("213100", "Ensemble immobilier locatif", 120),
        BGRow("157200", "Provision pour gros entretien", -10),
        BGRow("701000", "Loyers", -25),
        BGRow("702000", "Charges locatives facturees", -5),
        BGRow("164000", "Emprunt bancaire", -40),
        BGRow("601000", "Charges ayant contrepartie en produits", 5),
        BGRow("622100", "Commission societe de gestion", 3),
        BGRow("681200", "Dotation amortissement exploitation", 2),
    ]
    lines = [
        fs_line("Terrains et constructions locatives", 120, "asset"),
        fs_line("Gros entretiens", -10, "asset"),
        fs_line("Dettes financieres", -40, "liability"),
        fs_line("Loyers", 25, "result"),
        fs_line("Charges facturees", 5, "result"),
        fs_line("Charges ayant leur contrepartie en produits", 5, "result"),
        fs_line("Commissions de la societe de gestion", 3, "result"),
        fs_line("Dotations aux amortissements d exploitation", 2, "result"),
    ]
    for line in lines[3:]:
        line.result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["chart_profile"] == "scpi"
    assert stats["fs_liability_sign"] == -1
    assert stats["justified_fs_lines"] == len(lines)
    assert all(row.mapping for row in rows)
    assert all(item["status"] == "exact" for item in stats["reconciliation"])


def test_pcg_property_company_is_not_scpi_without_all_three_signatures():
    rows = [
        BGRow("213100", "Immeuble locatif", 120),
        BGRow("157200", "Provision pour gros entretien", -10),
        # Ordinary PCG rental revenue is normally account 706, not SCPI 701.
        BGRow("706000", "Loyers", -25),
    ]

    stats = PCGMapper(rows, []).map()

    assert stats["chart_profile"] == "pcg"


def test_formal_other_titles_caption_maps_current_asset_vmp_accounts():
    rows = [
        BGRow("503000", "VMP actions", 440_056.20),
        BGRow("506000", "Obligations", 21_000.00),
        BGRow("508400", "Compte a terme", 60_000.00),
    ]
    line = fs_line("Autres titres", 521_056.20, "asset")

    stats = PCGMapper(rows, [line]).map()

    assert stats["justified_fs_lines"] == 1
    assert all(row.mapping == line.label for row in rows)


def test_abbreviated_conversion_difference_caption_is_still_specific():
    row = BGRow("476000", "Ecart de conversion actif", 156_816)
    line = fs_line("Ecarts de conversion et diff. d'evaluation (VII)", 156_816, "asset")

    stats = PCGMapper([row], [line]).map()

    assert row.mapping == line.label
    assert stats["justified_fs_lines"] == 1


def test_abbreviated_technical_equipment_caption_maps_215_and_2815_net():
    rows = [
        BGRow("215400", "Materiel industriel", 110_000.40),
        BGRow("281540", "Amortissement materiel industriel", -15_177.80),
    ]
    line = fs_line("Instal. techniques, materiel et outillages indus.", 94_822.60, "asset")

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["justified_fs_lines"] == 1


def test_single_stock_family_can_fill_formal_stock_aggregate():
    rows = [
        BGRow("355100", "Stock de produits finis", 8_358),
        BGRow("355200", "Autres produits finis", 4_352),
    ]
    line = fs_line("Stocks et en-cours", 12_710, "asset")

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["combined_match_details"][0]["rule"] == "abbreviated_stocks"


def test_nonprofit_sales_of_goods_accepts_merchandise_accounts():
    rows = [
        BGRow("707100", "Vente de livres", -48_000),
        BGRow("701200", "Vente de produits finis", -45_733),
    ]
    line = fs_line("Ventes de biens", 93_733, "result")
    line.result_section = "operating"

    stats = PCGMapper(rows, [line]).map()

    assert all(row.mapping == line.label for row in rows)
    assert stats["combined_match_details"][0]["rule"] == "nonprofit_sales_goods"


def test_account_coded_disclosure_gets_exact_support_without_duplicate_bg_mapping():
    rows = [
        BGRow("706100", "Prestations A", -60),
        BGRow("706200", "Prestations B", -40),
    ]
    primary = fs_line("Production vendue de services", 100, "result")
    disclosure = fs_line("706 Prestations de services", 100, "result")
    primary.result_section = "operating"
    disclosure.result_section = "operating"

    stats = PCGMapper(rows, [primary, disclosure]).map()

    assert len({row.mapping for row in rows}) == 1
    assert stats["justified_fs_lines"] == 2
    assert stats["supplemental_matches"] == 1
    assert sorted(item["status"] for item in stats["reconciliation"]) == [
        "exact",
        "supplemental exact",
    ]
    supplemental = next(
        item
        for item in stats["reconciliation"]
        if item["status"] == "supplemental exact"
    )
    assert supplemental["bg_accounts"] == ["706100", "706200"]


def test_repeated_detail_caption_gets_support_without_duplicate_bg_mapping():
    rows = [
        BGRow("512100", "Banque A", 60),
        BGRow("512200", "Banque B", 40),
    ]
    primary = fs_line("Disponibilites", 100, "asset")
    repeated = fs_line("Banques, etablissements financiers et assimiles", 100, "asset")

    stats = PCGMapper(rows, [primary, repeated]).map()

    assert len({row.mapping for row in rows}) == 1
    assert stats["justified_fs_lines"] == 2
    assert stats["repeated_exact_matches"] == 1
    assert {item["status"] for item in stats["reconciliation"]} == {
        "exact",
        "supplemental exact",
    }


def test_balance_sheet_result_cross_foots_when_class_12_is_not_closed():
    rows = [
        BGRow("601000", "Purchases", 40),
        BGRow("641000", "Salaries", 30),
        BGRow("661000", "Interest", 10),
        BGRow("706000", "Services", -100),
        BGRow("758000", "Other income", -5),
    ]
    line = fs_line("Resultat de l'exercice", 25, "liability")

    stats = PCGMapper(rows, [line]).map()

    assert stats["derived_matches"] == 1
    assert stats["justified_fs_lines"] == 1
    assert stats["reconciliation"][0]["status"] == "derived exact"
    assert stats["reconciliation"][0]["bg_amount"] == 25
    # The result is a cross-foot; rows are not duplicated onto the equity line.
    assert all(row.mapping == "" for row in rows)


def test_million_euro_display_scale_requires_and_uses_cross_statement_evidence():
    rows = [
        BGRow("512000", "Banque", 250_000_000),
        BGRow("101000", "Capital social", -60_005_320),
        BGRow("706000", "Prestations de services", -36_250_000),
    ]
    lines = [
        fs_line("Disponibilites", 250, "asset"),
        fs_line("Capital social", 60, "liability"),
        fs_line("Production vendue de services", 36, "result"),
    ]
    lines[-1].result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["fs_unit_scale"] == 1_000_000
    assert stats["justified_fs_lines"] == 3
    assert all(row.mapping for row in rows)
    assert all(item["status"] == "exact" for item in stats["reconciliation"])


def test_translated_english_statutory_captions_use_pcg_categories():
    rows = [
        BGRow("207000", "Fonds commercial", 100),
        BGRow("101000", "Capital social", -200),
        BGRow("607000", "Achats de marchandises", 300),
        BGRow("401000", "Fournisseurs", -400),
    ]
    lines = [
        fs_line("Goodwill", 100, "asset"),
        fs_line("Share capital", 200, "liability"),
        fs_line("Purchases of goods (including customs duties)", 300, "result"),
        fs_line("Trade accounts payable and related liabilities", 400, "liability"),
    ]
    lines[2].result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["justified_fs_lines"] == 4
    assert [row.mapping for row in rows] == [line.label for line in lines]


def test_single_line_does_not_guess_a_display_unit_scale():
    row = BGRow("512000", "Banque", 250_000_000)
    line = fs_line("Disponibilites", 250, "asset")

    stats = PCGMapper([row], [line]).map()

    assert stats["fs_unit_scale"] == 1
    assert stats["justified_fs_lines"] == 0


def test_three_exact_expenses_prove_negative_expense_presentation():
    rows = [
        BGRow("607000", "Achats de marchandises", 30),
        BGRow("631000", "Impots et taxes", 20),
        BGRow("641000", "Salaires", 50),
    ]
    lines = [
        fs_line("Achats de marchandises", -30, "result"),
        fs_line("Impots, taxes et versements assimiles", -20, "result"),
        fs_line("Salaires", -50, "result"),
    ]
    for line in lines:
        line.result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["fs_expense_sign"] == -1
    assert stats["justified_fs_lines"] == 3
    assert all(row.mapping for row in rows)
    assert all(item["status"] == "exact" for item in stats["reconciliation"])
    assert [item["difference"] for item in stats["reconciliation"]] == [0, 0, 0]


def test_one_negative_expense_does_not_switch_the_statement_convention():
    row = BGRow("607000", "Achats de marchandises", 30)
    line = fs_line("Achats de marchandises", -30, "result")
    line.result_section = "operating"

    stats = PCGMapper([row], [line]).map()

    assert stats["fs_expense_sign"] == 1
    assert stats["justified_fs_lines"] == 0
    assert stats["source_alignment"]["status"] == "unproven"
    assert row.mapping == line.label
    assert stats["method_counts"] == {"semantic_permissive": 1}
    assert stats["reconciliation"][0]["status"] == "source difference"


def test_semantic_permissive_is_identified_after_exact_anchors():
    rows = [
        BGRow("512000", "Banque", 100),
        BGRow("486000", "Charges constatees d'avance", 10),
        BGRow("101300", "Capital appele verse", -50),
        BGRow("706000", "Prestations de services", -90),
    ]
    lines = [
        fs_line("Disponibilites", 100, "asset"),
        fs_line("Charges constatees d'avance", 10, "asset"),
        fs_line("Capital social", 50, "liability"),
        fs_line("Production vendue de services", 100, "result"),
    ]
    lines[-1].result_section = "operating"

    stats = PCGMapper(rows, lines).map()

    assert stats["source_alignment"]["status"] == "aligned"
    assert stats["source_alignment"]["exact_primary_lines"] == 3
    assert rows[-1].mapping == lines[-1].label
    assert stats["semantic_matches"] == 1


def test_balanced_class_18_liaison_accounts_are_non_fs_internal_transfers():
    rows = [
        BGRow("180001", "Compte de liaison etablissement A", 1_436_478_048.73),
        BGRow("180002", "Compte de liaison etablissement B", -1_436_478_049.53),
    ]

    stats = PCGMapper(rows, []).map()

    assert stats["non_fs"] == 2
    assert stats["classified"] == 2
    assert all(not row.mapping for row in rows)


def test_unbalanced_class_18_liaison_accounts_require_perimeter_review():
    row = BGRow("180001", "Compte de liaison etablissement", 100)

    stats = PCGMapper([row], []).map()

    assert stats["non_fs"] == 0
    assert stats["classified"] == 0
    assert row.mapping == ""


def test_repeated_account_rows_with_zero_closing_balance_are_non_fs():
    rows = [
        BGRow("411200", "Client", 100),
        BGRow("411200", "Client", -100),
    ]

    stats = PCGMapper(rows, []).map()

    assert stats["non_fs"] == 2
    assert stats["classified"] == 2
    assert all(not row.mapping for row in rows)
