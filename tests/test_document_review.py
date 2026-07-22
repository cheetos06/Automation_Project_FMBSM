import fitz

from canonical_extraction import (
    _reclassify_secondary_statement_pages,
    _reclassify_suspicious_primary_pages,
)
from canonical_review import resolve_comparison_targets
from document_review import build_review_rows


def test_annex_only_sparse_schedule_is_not_mapped_as_primary_statement():
    canonical = {
        "pages": [
            {
                "page": 1,
                "page_role": "cover",
                "primary_title": "ANNEXE AUX COMPTES ANNUELS",
                "headings": [],
                "blocks": [],
                "fs_lines": [],
            },
            {
                "page": 2,
                "period_role": "N",
                "page_role": "primary_statement",
                "primary_title": None,
                "headings": ["31/12/2025"],
                "statement_kind": "bilan_actif",
                "blocks": [],
                "tables": [],
                "quality_notes": [],
                "fs_lines": [
                    {
                        "id": f"N:p2:fs{index}",
                        "libelle": f"Ligne {index}",
                        "label_bbox_norm": [0.1, index / 10, 0.3, index / 10 + 0.02],
                        "cells": (
                            [
                                {
                                    "id": "N:p2:value",
                                    "field": "montant_n",
                                    "display_column": "Montant",
                                    "amount": 100.0,
                                    "bbox_norm": [0.7, 0.1, 0.8, 0.12],
                                }
                            ]
                            if index == 1
                            else []
                        ),
                    }
                    for index in range(1, 6)
                ],
            },
        ]
    }

    corrected = _reclassify_suspicious_primary_pages(canonical)
    page = corrected["pages"][1]

    assert page["page_role"] == "annex_table"
    assert page["statement_kind"] is None
    assert page["fs_lines"] == []
    assert len(page["tables"]) == 1
    assert len(page["tables"][0]["rows"]) == 5


def test_detail_and_tax_replica_statements_do_not_duplicate_primary_mapping():
    canonical = {
        "pages": [
            {
                "page": 1,
                "page_role": "primary_statement",
                "scope": "annual",
                "primary_title": "Bilan Actif dÃ©taillÃ©",
                "headings": [],
                "blocks": [],
                "fs_lines": [
                    {
                        "id": "detail",
                        "libelle": "411000 Client",
                        "cells": [],
                    }
                ],
                "tables": [],
            },
            {
                "page": 2,
                "page_role": "cover",
                "scope": "tax",
                "primary_title": "ETATS FISCAUX",
                "headings": [],
                "blocks": [],
                "fs_lines": [],
            },
            {
                "page": 3,
                "page_role": "primary_statement",
                "scope": "annual",
                "primary_title": "BILAN - ACTIF",
                "headings": [],
                "blocks": [],
                "fs_lines": [
                    {"id": "tax", "libelle": "TOTAL", "cells": []}
                ],
                "tables": [],
            },
        ]
    }

    corrected = _reclassify_secondary_statement_pages(canonical)

    assert corrected["pages"][0]["page_role"] == "annex_table"
    assert corrected["pages"][0]["fs_lines"] == []
    assert corrected["pages"][2]["page_role"] == "tax_form"
    assert corrected["pages"][2]["scope"] == "tax"
    assert corrected["pages"][2]["fs_lines"] == []


def test_evidence_region_is_limited_to_affected_cells():
    current = {
        "pages": [
            {
                "page": 1,
                "blocks": [],
                "fs_lines": [],
                "tables": [
                    {
                        "id": "N:p1:t1",
                        "bbox_norm": [0.1, 0.1, 0.9, 0.9],
                        "rows": [
                            {
                                "label": "A",
                                "cells": [
                                    {
                                        "id": "N:p1:c1",
                                        "amount": 10,
                                        "bbox_norm": [0.6, 0.2, 0.7, 0.25],
                                    },
                                    {
                                        "id": "N:p1:c2",
                                        "amount": 20,
                                        "bbox_norm": [0.7, 0.4, 0.8, 0.45],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    comparison = {
        "job_id": "review_001",
        "annotations": [],
        "removed_prior_blocks": [],
        "evidence_requirements": [
            {
                "table_id": "N:p1:t1",
                "items": [
                    {"target_id": "N:p1:c1"},
                    {"target_id": "N:p1:c2"},
                ],
            }
        ],
    }

    resolved = resolve_comparison_targets(
        comparison,
        job={
            "job_id": "review_001",
            "current_pages": [1],
            "prior_pages": [],
            "scope": "annual",
        },
        current=current,
    )

    assert resolved["evidence_requirements"][0]["bbox_norm"] == [0.6, 0.2, 0.8, 0.45]


def test_annex_totals_are_recomputed_deterministically(tmp_path):
    pdf = tmp_path / "one-page.pdf"
    document = fitz.open()
    document.new_page(width=600, height=800)
    document.save(pdf)
    document.close()
    canonical = {
        "pages": [
            {
                "page": 1,
                "scope": "annual",
                "page_role": "annex_table",
                "tables": [
                    {
                        "id": "N:p1:t1",
                        "title": "Etat de test",
                        "rows": [
                            {
                                "label": "A",
                                "cells": [
                                    {
                                        "id": "a",
                                        "column_label": "Montant",
                                        "amount": 100.0,
                                        "bbox_norm": [0.6, 0.2, 0.7, 0.22],
                                    }
                                ],
                            },
                            {
                                "label": "B",
                                "cells": [
                                    {
                                        "id": "b",
                                        "column_label": "Montant",
                                        "amount": 50.0,
                                        "bbox_norm": [0.6, 0.3, 0.7, 0.32],
                                    }
                                ],
                            },
                            {
                                "label": "TOTAL",
                                "cells": [
                                    {
                                        "id": "total",
                                        "column_label": "Montant",
                                        "amount": 150.0,
                                        "bbox_norm": [0.6, 0.4, 0.7, 0.42],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ]
    }
    rows = build_review_rows(
        [],
        current_pdf=pdf,
        current_index={"pages": [{"page": 1, "page_role": "annex_table"}]},
        current_canonical=canonical,
    )

    assert len(rows) == 1
    assert rows[0]["review kind"] == "calculation_match"
    assert rows[0]["tickmark"] == "calculation"
