"""Tests for rollback.py. All configurations are fictional (RFC 5737, private AS)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import rollback

DATA = Path(__file__).resolve().parent / "data" / "rollback"


def rb(before, change):
    return rollback.build_rollback(before, change).text


def active_lines(text):
    """Commands that would be applied (comments removed)."""
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("!")]


class ReferenceScenarios(unittest.TestCase):
    """Reviewed scenarios: the rollback must stay exactly the same."""

    def check(self, prefix):
        res = rollback.build_rollback(
            (DATA / f"{prefix}_running_before.txt").read_text(encoding="utf-8"),
            (DATA / f"{prefix}_change.cfg").read_text(encoding="utf-8"),
            f"{prefix}_running_before.txt", f"{prefix}_change.cfg")
        expected = (DATA / f"{prefix}_expected_rollback.cfg").read_text(encoding="utf-8")
        self.assertEqual(res.text, expected)

    def test_ios_xe_change(self):
        self.check("xe")

    def test_ios_xr_change(self):
        self.check("xr")


class Rules(unittest.TestCase):
    BEFORE = """hostname CE01
vrf definition underlay
 rd 65001:100
 !
 address-family ipv4
 exit-address-family
!
interface GigabitEthernet0/0/1
 description OLD
 vrf forwarding underlay
 ip address 192.0.2.0 255.255.255.254
!
interface GigabitEthernet0/0/2
 no ip address
!
router bgp 65001
 address-family ipv4 vrf underlay
  neighbor 192.0.2.1 remote-as 65100
  neighbor 192.0.2.1 activate
 exit-address-family
!
"""

    def test_value_change_restores_old_value(self):
        out = rb(self.BEFORE, "interface GigabitEthernet0/0/1\n description NEW\n")
        self.assertIn("interface GigabitEthernet0/0/1\n description OLD\n", out)
        self.assertNotIn("no description", out)

    def test_new_line_is_removed(self):
        out = rb(self.BEFORE, "interface GigabitEthernet0/0/2\n shutdown\n")
        self.assertIn(" no shutdown", out)

    def test_new_subinterface_is_deleted(self):
        out = rb(self.BEFORE, "interface GigabitEthernet0/0/2.300\n"
                              " encapsulation dot1Q 300\n ip address 198.51.100.0 255.255.255.254\n")
        self.assertEqual(active_lines(out), ["no interface GigabitEthernet0/0/2.300", "end"])

    def test_vrf_change_restores_vrf_then_addresses(self):
        out = rb(self.BEFORE, "interface GigabitEthernet0/0/1\n vrf forwarding internet\n"
                              " ip address 198.51.100.4 255.255.255.254\n")
        lines = active_lines(out)
        i = lines.index("vrf forwarding underlay")
        self.assertEqual(lines[i + 1], "ip address 192.0.2.0 255.255.255.254")

    def test_vrf_added_removes_it_and_skips_addresses_cleared_with_it(self):
        out = rb(self.BEFORE, "interface GigabitEthernet0/0/2\n vrf forwarding underlay\n"
                              " ip address 198.51.100.4 255.255.255.254\n")
        self.assertIn(" no vrf forwarding underlay", out)
        self.assertNotIn("no ip address 198.51.100.4", out)

    def test_deleted_vrf_is_restored_with_its_interfaces(self):
        res = rollback.build_rollback(self.BEFORE, "no vrf definition underlay\n")
        self.assertIn("vrf definition underlay\n rd 65001:100\n address-family ipv4\n"
                      " exit-address-family\n", res.text)
        self.assertIn("interface GigabitEthernet0/0/1\n vrf forwarding underlay\n"
                      " ip address 192.0.2.0 255.255.255.254\n", res.text)
        self.assertTrue(res.reviews)

    def test_new_neighbor_removed_with_one_line(self):
        out = rb(self.BEFORE, "router bgp 65001\n address-family ipv4 vrf underlay\n"
                              "  neighbor 192.0.2.9 remote-as 65200\n  neighbor 192.0.2.9 activate\n"
                              "  neighbor 192.0.2.9 route-map RM-IN in\n exit-address-family\n")
        self.assertEqual([l for l in active_lines(out) if "192.0.2.9" in l],
                         ["no neighbor 192.0.2.9"])

    def test_identical_change_gives_nothing(self):
        res = rollback.build_rollback(self.BEFORE, "interface GigabitEthernet0/0/1\n description OLD\n")
        self.assertTrue(res.empty)
        self.assertEqual(active_lines(res.text), [])

    def test_router_missing_from_running_config_is_never_removed(self):
        res = rollback.build_rollback(self.BEFORE, "router ospf 1\n network 192.0.2.0 0.0.0.1 area 0\n")
        self.assertNotIn("no router ospf 1", active_lines(res.text))
        self.assertIn("!   no router ospf 1", res.text)
        self.assertTrue(res.reviews)

    def test_new_global_address_family_is_only_suggested(self):
        res = rollback.build_rollback(self.BEFORE, "router bgp 65001\n address-family ipv4\n"
                                                   "  neighbor 198.51.100.1 activate\n"
                                                   " exit-address-family\n")
        self.assertNotIn("no address-family ipv4", active_lines(res.text))
        self.assertIn("!   no address-family ipv4", res.text)

    def test_new_physical_interface_is_never_removed(self):
        res = rollback.build_rollback(self.BEFORE, "interface GigabitEthernet0/0/9\n description X\n")
        self.assertNotIn("no interface GigabitEthernet0/0/9", active_lines(res.text))
        self.assertTrue(res.reviews)

    def test_abbreviated_interface_name_asks_for_review(self):
        res = rollback.build_rollback(self.BEFORE, "interface Gi0/0/1\n description NEW\n")
        self.assertTrue(any("in full" in m for _, m in res.reviews))

    def test_flat_change_file_is_refused(self):
        with self.assertRaises(rollback.FormatError):
            rollback.build_rollback(self.BEFORE, "interface GigabitEthernet0/0/1\ndescription NEW\n")

    def test_banner_is_ignored(self):
        out = rb("banner motd ^C\ninterface Fake\n description trap\n^C\n" + self.BEFORE,
                 "interface GigabitEthernet0/0/1\n description NEW\n")
        self.assertNotIn("Fake", out)


class WithoutRunningConfig(unittest.TestCase):
    """Change kits: no running-config. Only change-specific items are removed."""

    def test_generated_configs_never_remove_vrf_router_or_address_family(self):
        root = Path(__file__).resolve().parents[1]
        for f in [root / "examples/output/CE01.cfg", root / "examples/output/PE01.cfg",
                  *sorted((root / "examples/mixed/output").glob("*.cfg"))]:
            res = rollback.build_rollback(None, f.read_text(encoding="utf-8"))
            for line in active_lines(res.text):
                self.assertFalse(line.startswith(("no vrf", "no router", "no address-family",
                                                  "no ip vrf", "no rd", "no redistribute")),
                                 f"{f.name}: {line}")
            self.assertTrue(any(l.startswith("no interface") for l in active_lines(res.text)))
            self.assertTrue(any(l.startswith("no neighbor") for l in active_lines(res.text)))

    def test_ios_xr_ends_without_commit(self):
        root = Path(__file__).resolve().parents[1]
        res = rollback.build_rollback(None, (root / "examples/mixed/output/PE01.cfg").read_text())
        self.assertEqual(res.platform, "ios-xr")
        self.assertNotIn("commit", active_lines(res.text))
        self.assertNotIn("end", active_lines(res.text))


class CommandLine(unittest.TestCase):
    def test_writes_the_rollback_file(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "rb.cfg"
            with contextlib.redirect_stdout(io.StringIO()):
                code = rollback.main([str(DATA / "xe_running_before.txt"),
                                      str(DATA / "xe_change.cfg"), "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("no interface TwentyFiveGigE1/0/1.200", out.read_text())

    def test_flat_file_returns_error_code(self):
        with tempfile.TemporaryDirectory() as folder:
            flat = Path(folder) / "flat.cfg"
            flat.write_text("interface GigabitEthernet1\ndescription X\n")
            with contextlib.redirect_stderr(io.StringIO()):
                code = rollback.main([str(DATA / "xe_running_before.txt"), str(flat)])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
