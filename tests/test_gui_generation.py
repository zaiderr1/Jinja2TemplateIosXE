"""Tests for the config generator path used by the GUI, without Tk windows."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from openpyxl import load_workbook

import netgen_gui
import netgen_core


ROOT = Path(__file__).resolve().parents[1]


class GuiGenerationTests(unittest.TestCase):
    def test_standard_workbook_matches_reference_bytes(self):
        result = netgen_gui.run_generation(str(ROOT / "netgen_input.xlsx"))
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.ok)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.skipped, [])
        self.assertEqual(set(c["output"] for c in result.configs), {"CE01", "PE01"})
        for config in result.configs:
            reference = ROOT / "examples" / "output" / (config["output"] + ".cfg")
            self.assertEqual(config["text"].encode("utf-8"), reference.read_bytes())

    def test_mixed_xe_xr_grt_workbook_matches_every_reference(self):
        workbook = ROOT / "examples" / "mixed" / "netgen_input_xe_xr_grt.xlsx"
        result = netgen_gui.run_generation(str(workbook))
        self.assertFalse(result.errors, result.errors)
        self.assertTrue(result.ok)
        self.assertEqual(result.failed, [])
        self.assertEqual(result.skipped, [])
        actual_names = set(c["output"] for c in result.configs)
        expected_names = set(p.stem for p in (ROOT / "examples" / "mixed" / "output").glob("*.cfg"))
        self.assertEqual(actual_names, expected_names)
        for config in result.configs:
            reference = ROOT / "examples" / "mixed" / "output" / (config["output"] + ".cfg")
            self.assertEqual(config["text"].encode("utf-8"), reference.read_bytes())

    def test_out_of_subnet_workbook_has_errors_and_no_configs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.xlsx"
            workbook = load_workbook(str(ROOT / "netgen_input.xlsx"))
            workbook["Links"]["J2"] = "192.0.2.9"
            workbook.save(str(path))
            workbook.close()
            result = netgen_gui.run_generation(str(path))
        self.assertTrue(result.errors)
        self.assertEqual(result.configs, [])

    def test_each_call_reloads_workbook_from_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "editable.xlsx"
            workbook = load_workbook(str(ROOT / "netgen_input.xlsx"))
            workbook.save(str(path))
            workbook.close()
            first = netgen_gui.run_generation(str(path))
            workbook = load_workbook(str(path))
            workbook["Devices"]["B2"] = 65011
            workbook.save(str(path))
            workbook.close()
            second = netgen_gui.run_generation(str(path))
        self.assertFalse(first.errors, first.errors)
        self.assertFalse(second.errors, second.errors)
        self.assertNotEqual(
            next(c["text"] for c in first.configs if c["host"] == "CE01"),
            next(c["text"] for c in second.configs if c["host"] == "CE01"))

    def test_gui_passes_custom_templates_and_strict_to_engine(self):
        sentinel = object()
        with mock.patch.object(netgen_core, "generate_from_path", return_value=sentinel) as call:
            got = netgen_gui.run_generation("book.xlsx", "draft-templates", True)
        self.assertIs(got, sentinel)
        call.assert_called_once_with("book.xlsx", templates="draft-templates", strict=True)

    def test_gui_disables_strict_without_template_folder(self):
        sentinel = object()
        with mock.patch.object(netgen_core, "generate_from_path", return_value=sentinel) as call:
            got = netgen_gui.run_generation("book.xlsx", "", True)
        self.assertIs(got, sentinel)
        call.assert_called_once_with("book.xlsx", templates=None, strict=False)

    def test_generate_cli_still_writes_the_standard_outputs(self):
        import generate
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "cli-output"
            args = ["generate.py", str(ROOT / "netgen_input.xlsx"), str(out)]
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
                try:
                    generate.main()
                except SystemExit as error:
                    self.assertEqual(error.code, 0)
            for name in ("CE01", "PE01"):
                text = (out / (name + ".cfg")).read_text(encoding="utf-8")
                reference = (ROOT / "examples" / "output" / (name + ".cfg")).read_text(encoding="utf-8")
                self.assertEqual(text.replace("\r\n", "\n"), reference)
                self.assertTrue(text.endswith("\n"))

    def test_conf2jinja_cli_still_accepts_a_running_config(self):
        import conf2jinja
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "drafts"
            args = ["conf2jinja.py", str(ROOT / "examples" / "running-config-CE01.txt"),
                    "-o", str(out)]
            with mock.patch("sys.argv", args), contextlib.redirect_stdout(io.StringIO()):
                try:
                    conf2jinja.main()
                except SystemExit as error:
                    self.assertEqual(error.code, 0)
            self.assertTrue(any(out.rglob("*.j2")))


if __name__ == "__main__":
    unittest.main()
