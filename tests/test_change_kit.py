"""Tests for change_kit.py. All data is fictional (RFC 5737, private AS)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

import change_kit
import dns_names
import generate

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data" / "change_kit"
MIXED = ROOT / "examples" / "mixed" / "netgen_input_xe_xr_grt.xlsx"


def generated(path=MIXED, edit=None):
    wb = load_workbook(str(path), data_only=True)
    if edit:
        edit(wb)
    res = generate.generate_configs(wb)
    assert not res.errors, res.errors
    return res


def active(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("!")]


class WithoutRunningConfigs(unittest.TestCase):
    def setUp(self):
        self.entries = change_kit.build_kit(generated())
        self.by_host = {e.host: e for e in self.entries}

    def test_three_files_per_device_and_dns_files(self):
        names = set(change_kit.kit_files(self.entries, "example.intra"))
        self.assertEqual(names, {"CE01.cfg", "CE01_rollback.cfg", "CE01_checks.txt",
                                 "PE01.cfg", "PE01_rollback.cfg", "PE01_checks.txt",
                                 "dns_records.csv", "dns_records.ps1"})

    def test_config_is_exactly_the_generated_one(self):
        for e in self.entries:
            ref = ROOT / "examples" / "mixed" / "output" / f"{e.output}.cfg"
            self.assertEqual(e.config, ref.read_text(encoding="utf-8"))

    def test_rollback_only_removes_change_specific_items(self):
        for e in self.entries:
            for line in active(e.rollback_text):
                self.assertFalse(line.startswith(("no vrf", "no router", "no address-family")),
                                 f"{e.host}: {line}")
            self.assertIn("no interface", e.rollback_text)
            self.assertIn("no neighbor", e.rollback_text)
            self.assertEqual(e.running_source, "")

    def test_checks_use_the_real_names(self):
        ce = self.by_host["CE01"].checks
        self.assertIn("show interfaces TwentyFiveGigE1/0/1.100 | include", ce)
        self.assertIn("ping vrf underlay 192.0.2.1 repeat 5", ce)
        self.assertIn("show bgp vpnv4 unicast vrf underlay neighbors 192.0.2.1 advertised-routes", ce)
        self.assertIn("show bgp ipv4 unicast neighbors 198.51.100.1 routes", ce)   # GRT link
        pe = self.by_host["PE01"].checks
        self.assertIn("show ipv4 interface TenGigE0/0/0/1.100 brief", pe)
        self.assertIn("ping vrf underlay 192.0.2.0 count 5", pe)
        self.assertIn("show bgp vrf underlay neighbor 192.0.2.0 advertised-routes", pe)

    def test_dns_records_follow_the_dns_tool_rules(self):
        names = {r.fqdn for e in self.entries for r in e.dns if r.is_record}
        self.assertEqual(names, {"CE01_Twe1-0-1_100.example.intra", "CE01_Twe1-0-2_300.example.intra",
                                 "PE01_Te0-0-0-1_100.example.intra", "PE01_Te0-0-0-2_300.example.intra"})
        ps1 = change_kit.kit_files(self.entries, "example.intra")["dns_records.ps1"]
        self.assertIn('-Name "PE01_Te0-0-0-1_100" -IPv4Address "192.0.2.1" -CreatePtr', ps1)

    def test_dns_domain_and_abbreviations_come_from_settings(self):
        settings = dns_names.DnsSettings(domain="lab.example", lowercase=True)
        entries = change_kit.build_kit(generated(), dns_settings=settings)
        self.assertIn("ce01_twe1-0-1_100.lab.example",
                      {r.fqdn for e in entries for r in e.dns})


class WithRunningConfigs(unittest.TestCase):
    def test_exact_rollback_from_the_running_config(self):
        running, problems = change_kit.load_running_configs(DATA / "running")
        self.assertEqual(problems, [])
        entries = change_kit.build_kit(generated(), running)
        ce = next(e for e in entries if e.host == "CE01")
        self.assertEqual(ce.running_source, "ce01-show-run.txt")
        self.assertEqual(ce.rollback_text,
                         (DATA / "CE01_expected_rollback.cfg").read_text(encoding="utf-8"))
        pe = next(e for e in entries if e.host == "PE01")
        self.assertEqual(pe.running_source, "")      # no file for PE01: fallback mode

    def test_dependencies_missing_from_the_running_config_are_reported(self):
        running, _ = change_kit.load_running_configs(DATA / "running")
        entries = change_kit.build_kit(generated(), running)
        ce = next(e for e in entries if e.host == "CE01")
        # The fixture has the underlay route-maps, not the GRT ones.
        self.assertEqual([m["name"] for m in ce.missing],
                         ["RP-BGP65001OutBgp65100-GRT", "RP-BGP65100InBgp65001-GRT"])
        self.assertEqual(len(change_kit.missing_lines(entries)), 2)
        self.assertIn("show route-map RP-BGP65001OutBgp65100-GRT", ce.checks)

    def test_two_files_for_the_same_device_are_both_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            for n in ("a.txt", "b.txt"):
                (Path(folder) / n).write_text("hostname CE01\n")
            (Path(folder) / "PE01.txt").write_text("interface Loopback0\n")   # no hostname
            running, problems = change_kit.load_running_configs(folder)
        self.assertNotIn("ce01", running)
        self.assertIn("pe01", running)                    # indexed by file name
        self.assertTrue(any("CE01" in p for p in problems))


class GuiBackend(unittest.TestCase):
    """netgen_core.generate_kit_from_path: what the Config generator tab calls."""

    def test_kit_with_running_folder(self):
        import netgen_core
        res, kit, problems = netgen_core.generate_kit_from_path(
            MIXED, running_folder=str(DATA / "running"))
        self.assertFalse(res.errors)
        self.assertEqual(sorted(kit), ["CE01", "PE01"])
        self.assertEqual(kit["CE01"].running_source, "ce01-show-run.txt")
        # Only the missing GRT route-maps are reported (shown in the GUI notes).
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("is not in ce01-show-run.txt" in p for p in problems))

    def test_missing_running_folder_is_a_problem_not_a_crash(self):
        import netgen_core
        res, kit, problems = netgen_core.generate_kit_from_path(
            MIXED, running_folder=str(DATA / "nope"))
        self.assertTrue(problems)
        self.assertEqual(kit["CE01"].running_source, "")   # kit built without it


class Blocks(unittest.TestCase):
    def test_interfaces_off_means_no_dns_and_no_interface_checks(self):
        def no_interfaces(wb):
            wb["Generate"]["E2"] = "no"                   # CE01: Interfaces = no
        entries = change_kit.build_kit(generated(edit=no_interfaces))
        ce = next(e for e in entries if e.host == "CE01")
        self.assertEqual(ce.dns, [])
        self.assertNotIn("show interfaces", ce.checks)
        self.assertIn("show bgp", ce.checks)


class Writing(unittest.TestCase):
    def test_existing_files_are_kept_unless_overwrite(self):
        entries = change_kit.build_kit(generated())
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "CE01.cfg").write_text("mine")
            _, existing = change_kit.plan(folder, entries, "example.intra")
            self.assertEqual([p.name for p in existing], ["CE01.cfg"])
            change_kit.write_kit(folder, entries, "example.intra")
            self.assertEqual((Path(folder) / "CE01.cfg").read_text(), "mine")
            change_kit.write_kit(folder, entries, "example.intra", overwrite=True)
            self.assertNotEqual((Path(folder) / "CE01.cfg").read_text(), "mine")
            self.assertTrue((Path(folder) / "dns_records.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_cli_refuses_to_replace_without_force(self):
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(change_kit.main([str(MIXED), folder]), 0)
                self.assertEqual(change_kit.main([str(MIXED), folder]), 1)
                self.assertEqual(change_kit.main([str(MIXED), folder, "--force"]), 0)
            self.assertIn("already exist", out.getvalue())


if __name__ == "__main__":
    unittest.main()
