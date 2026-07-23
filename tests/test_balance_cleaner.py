from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "automation-mail-worker"
FS_REVIEW = SERVICE / "fs_review"
for candidate in (str(SERVICE), str(FS_REVIEW)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from balance_cleaner.render import _font, _text_width, _wrap, capture_workbook  # noqa: E402
from balance_cleaner.run_pipeline import (  # noqa: E402
    _batch_prompt,
    _extract_records,
    _write_workbook,
    chunk_screenshots,
)
from copilot_runtime.runtime import (  # noqa: E402
    _parse_first_json_value,
    _select_response_text,
)
from fmbsm_email_bot.worker import _job_kind  # noqa: E402


class BalanceRendererTests(unittest.TestCase):
    def test_wrapping_uses_pixel_width_without_dropping_visible_text(self) -> None:
        font = _font(False)
        value = "CRED IMPOST ART.1, COMMI DA 1051 A 1063, L. 178/20"
        wrapped = _wrap(value, 398, font)

        self.assertEqual("".join(wrapped.split()), "".join(value.split()))
        self.assertTrue(all(_text_width(font, line) <= 388 for line in wrapped.splitlines()))
        self.assertIn("L.", wrapped)

    def test_renderer_freezes_nonempty_rows_before_chunking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workbook_path = root / "input.xlsx"
            workbook = Workbook()
            cover = workbook.active
            cover.title = "Cover"
            cover["A1"] = "Title"
            balance = workbook.create_sheet("Any Balance")
            balance.append(["Compte", "Libellé", "Débit", "Crédit"])
            for row in (2, 99, 100, 101, 199, 200, 201, 205):
                balance.cell(row, 1, f"{row:06d}")
                balance.cell(row, 2, f"Account {row}")
                balance.cell(row, 3, row * 10)
                balance.cell(row, 4, 0)
            # Formatting alone must not inflate the detected last row.
            balance.cell(500, 12).fill = PatternFill("solid", fgColor="FFFF00")
            workbook.save(workbook_path)

            capture = capture_workbook(workbook_path, root / "rendered")

            self.assertEqual(capture.sheet_name, "Any Balance")
            self.assertEqual(capture.last_populated_row, 205)
            self.assertEqual(
                [(item.row_start, item.row_end) for item in capture.screenshots],
                [(1, 205)],
            )
            self.assertEqual(
                capture.screenshots[0].populated_rows,
                (1, 2, 99, 100, 101, 199, 200, 201, 205),
            )
            self.assertEqual(capture.first_populated_column, 1)
            self.assertEqual(capture.last_populated_column, 4)
            self.assertIsNotNone(capture.header_context)
            self.assertEqual(capture.header_context.populated_rows, (1,))
            with Image.open(capture.screenshots[0].path) as image:
                self.assertGreater(image.width, 300)
                self.assertGreater(image.height, 100)

            limited = capture_workbook(
                workbook_path,
                root / "limited",
                rows_per_image=30,
                last_row_limit=120,
            )
            self.assertEqual(limited.last_populated_row, 101)
            self.assertEqual(
                [(item.row_start, item.row_end) for item in limited.screenshots],
                [(1, 101)],
            )

    def test_renderer_freezes_one_compact_column_mask_for_every_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workbook_path = root / "spacers.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet["A1"] = "Title only"
            sheet["A2"] = "Second title"
            sheet["C5"] = "Company"
            sheet["D5"] = "Account"
            sheet["E5"] = "One-off spacer heading"
            sheet["F5"] = "Description"
            sheet["Q5"] = "Final balance"
            for row in range(7, 107):
                sheet.cell(row, 3, "BA02")
                sheet.cell(row, 4, f"{row:010d}")
                sheet.cell(row, 6, f"Account {row}")
                sheet.cell(row, 17, float(row))
            sheet.cell(6, 6, "Styled section label").font = Font(bold=True)
            for column in (3, 4, 6, 17):
                sheet.cell(37, column).font = Font(bold=True)
            workbook.save(workbook_path)

            capture = capture_workbook(
                workbook_path,
                root / "rendered",
                rows_per_image=30,
            )

            self.assertEqual(capture.populated_columns, (3, 4, 6, 17))
            self.assertIsNotNone(capture.header_context)
            self.assertEqual(capture.header_context.populated_rows, (5, 6))
            self.assertTrue(
                all(
                    screenshot.populated_columns == capture.populated_columns
                    for screenshot in capture.screenshots
                )
            )
            self.assertTrue(
                all(
                    len(screenshot.populated_rows) <= 30
                    for screenshot in capture.screenshots
                )
            )
            self.assertEqual(capture.screenshots[0].populated_rows[0], 5)
            captured_rows = {
                row
                for screenshot in capture.screenshots
                for row in screenshot.populated_rows
            }
            self.assertNotIn(6, captured_rows)
            self.assertNotIn(37, captured_rows)
            self.assertEqual(
                sum(item.estimated_records for item in capture.screenshots),
                99,
            )

    def test_copilot_batch_size_is_hard_capped_at_30(self) -> None:
        screenshots = tuple(SimpleNamespace(path=Path(f"{index}.png")) for index in range(65))
        batches = chunk_screenshots(screenshots, 999)  # type: ignore[arg-type]
        self.assertEqual([len(batch) for batch in batches], [29, 29, 7])
        self.assertLessEqual(1 + max(len(batch) for batch in batches), 30)

    def test_copilot_batches_are_limited_by_estimated_record_volume(self) -> None:
        screenshots = tuple(
            SimpleNamespace(
                path=Path(f"{index}.png"),
                estimated_records=60,
            )
            for index in range(5)
        )
        batches = chunk_screenshots(  # type: ignore[arg-type]
            screenshots,
            10,
            150,
        )
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_production_uses_ten_target_screenshots_per_request(self) -> None:
        settings = json.loads(
            (
                SERVICE
                / "balance_cleaner"
                / "config"
                / "settings.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(settings["batch_size"], 10)
        self.assertEqual(settings["rows_per_image"], 30)
        self.assertEqual(settings["max_estimated_records_per_request"], 120)

    def test_batch_prompt_retains_the_extraction_instructions(self) -> None:
        screenshot = SimpleNamespace(row_start=1, row_end=100)
        capture = SimpleNamespace(sheet_name="Balance")
        base = "EXTRACT THESE ACCOUNTS"
        batch_prompt = _batch_prompt(base, capture, (screenshot,), 1, 2)  # type: ignore[arg-type]
        self.assertTrue(batch_prompt.startswith(base))
        self.assertIn("Excel rows 1-100", batch_prompt)

        context_prompt = _batch_prompt(
            base,
            capture,
            (SimpleNamespace(row_start=101, row_end=200),),  # type: ignore[arg-type]
            2,
            2,
            (screenshot,),  # type: ignore[arg-type]
        )
        self.assertIn("HEADER/STRUCTURE CONTEXT ONLY", context_prompt)
        self.assertIn("Do not extract or repeat any account from those context images", context_prompt)
        self.assertIn("image 2=Excel rows 101-200", context_prompt)
        self.assertNotIn("independent verification read", context_prompt)


class BalanceResponseTests(unittest.TestCase):
    def test_runtime_accepts_json_arrays_and_prefers_latest_structured_answer(self) -> None:
        value = _parse_first_json_value(
            'Result follows: [{"account_number":"001","account_description":"Cash","saldo":1.5}] done'
        )
        self.assertIsInstance(value, list)
        selected = _select_response_text(
            ["Working...", "[{\"account_number\":\"001\",\"account_description\":\"Cash\",\"saldo\":1.5}]"]
        )
        self.assertTrue(selected.startswith("["))

    def test_records_are_validated_and_reconstructed_with_text_account_numbers(self) -> None:
        records = _extract_records(
            [
                {
                    "account_number": "000100",
                    "account_description": "Caisse",
                    "saldo": "1.234,50",
                },
                {
                    "account_number": "200",
                    "account_description": "Fournisseurs",
                    "saldo": -25,
                },
            ]
        )
        self.assertEqual(records[0]["saldo"], 1234.5)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cleaned.xlsx"
            _write_workbook(output, records)
            workbook = load_workbook(output, data_only=True)
            sheet = workbook["Cleaned balance"]
            self.assertEqual(
                tuple(sheet.cell(1, column).value for column in range(1, 4)),
                ("account_number", "account_description", "saldo"),
            )
            self.assertEqual(sheet["A2"].value, "000100")
            self.assertEqual(sheet["C2"].value, 1234.5)
            self.assertEqual(sheet.max_column, 3)
            workbook.close()

    def test_prompt_is_language_and_layout_agnostic(self) -> None:
        prompt = (
            SERVICE
            / "balance_cleaner"
            / "config"
            / "prompts"
            / "balance_extract_v1.md"
        ).read_text(encoding="utf-8")
        self.assertIn("any title, structure, accounting convention, or language", prompt)
        self.assertIn("Débit", prompt)
        self.assertIn("Solde", prompt)
        self.assertIn("dedicated account-number column", prompt)
        self.assertIn("distinguish visually similar characters such as S and 5", prompt)
        self.assertIn("Numeric zero is a real amount", prompt)
        self.assertIn("Treat the first visible row of every TARGET image", prompt)
        self.assertIn("silent completeness pass over every TARGET image", prompt)
        self.assertNotIn("Bilancio di verifica", prompt)


class BalanceDispatchTests(unittest.TestCase):
    def test_balance_subject_is_dispatched(self) -> None:
        settings = SimpleNamespace(
            subject_prefix="[optimda-extract-dates]",
            fs_subject_prefix="[fs-review]",
            effectif_subject_prefix="[optimda-effectif]",
            balance_subject_prefix="[balance-cleaner]",
        )
        self.assertEqual(
            _job_kind(settings, "[balance-cleaner] client trial balance"),
            "balance_cleaner",
        )


if __name__ == "__main__":
    unittest.main()
