"""Tests for dependencies.py. All data is fictional (RFC 5737, private AS)."""

import unittest
from pathlib import Path

import dependencies

ROOT = Path(__file__).resolve().parents[1]


def names(text):
    return [(d["kind"], d["name"]) for d in dependencies.find(text)]


class Find(unittest.TestCase):
    def test_generated_ios_xe_config(self):
        deps = dependencies.find((ROOT / "examples/mixed/output/CE01.cfg").read_text())
        self.assertEqual([(d["kind"], d["name"], d["used_by"]) for d in deps], [
            ("interface", "TwentyFiveGigE1/0/1", ["sub-interface .100"]),
            ("interface", "TwentyFiveGigE1/0/2", ["sub-interface .300"]),
            ("route-map", "RP-BGP65001OutBgp65100-GRT", ["neighbor 198.51.100.1 out"]),
            ("route-map", "RP-BGP65100InBgp65001-GRT", ["neighbor 198.51.100.1 in"]),
            ("route-map", "RP-BGP65001OutBgp65100-underlay",
             ["neighbor 192.0.2.1 out (vrf underlay)"]),
            ("route-map", "RP-BGP65100InBgp65001-underlay",
             ["neighbor 192.0.2.1 in (vrf underlay)"]),
        ])

    def test_generated_ios_xr_config(self):
        deps = dependencies.find((ROOT / "examples/mixed/output/PE01.cfg").read_text())
        self.assertEqual([d["kind"] for d in deps], ["interface"] * 2 + ["route-policy"] * 4)
        self.assertIn("neighbor 192.0.2.0 in (vrf underlay)",
                      [u for d in deps for u in d["used_by"]])

    def test_objects_defined_in_the_config_are_not_dependencies(self):
        text = ("route-map RM-IN permit 10\n match ip address prefix-list PL-IN\n!\n"
                "router bgp 65001\n address-family ipv4 vrf A\n"
                "  neighbor 192.0.2.1 route-map RM-IN in\n")
        self.assertEqual(names(text), [("vrf", "A"), ("prefix-list", "PL-IN")])
        self.assertEqual(dependencies.find(text)[1]["used_by"], ["route-map RM-IN permit 10"])

    def test_vrf_needed_when_the_vrf_block_is_not_generated(self):
        text = ("interface GigabitEthernet0/0/1.100\n encapsulation dot1Q 100\n"
                " vrf forwarding A\n ip address 192.0.2.0 255.255.255.254\n")
        self.assertIn(("vrf", "A"), names(text))
        self.assertNotIn(("vrf", "A"), names("vrf definition A\n rd 65001:1\n!\n" + text))

    def test_other_references(self):
        text = ("interface GigabitEthernet0/0/1.100\n ip access-group ACL-IN in\n"
                " service-policy output PM-OUT\n"
                "router bgp 65001\n neighbor PG peer-group\n neighbor 192.0.2.9 peer-group PG\n"
                " neighbor 192.0.2.10 peer-group PG2\n neighbor 192.0.2.10 update-source Loopback0\n"
                "router bgp 65100\n vrf A\n  neighbor 192.0.2.0\n   use neighbor-group NG\n"
                "   address-family ipv4 unicast\n    route-policy PASS in\n")
        self.assertEqual(names(text), [
            ("interface", "GigabitEthernet0/0/1"), ("interface", "Loopback0"), ("vrf", "A"),
            ("peer-group", "PG2"), ("neighbor-group", "NG"), ("route-policy", "PASS"),
            ("access-list", "ACL-IN"), ("policy-map", "PM-OUT")])

    def test_descriptions_and_comments_are_ignored(self):
        self.assertEqual(names("interface Loopback1\n description route-map X in\n"
                               "! neighbor 192.0.2.1 route-map Y in\n"), [])


class Slot(unittest.TestCase):
    def test_slot_is_at_the_top_of_generated_configs(self):
        for f in (ROOT / "examples").rglob("*.cfg"):
            if "drafts" in f.parts:
                continue
            text = f.read_text()
            self.assertTrue(text.startswith("! ----"), f)
            self.assertIn("DEPENDENCIES of", text.splitlines()[1])

    def test_slot_lines_are_comments_only(self):
        deps = dependencies.find((ROOT / "examples/mixed/output/CE01.cfg").read_text())
        for line in dependencies.slot(deps, "CE01").splitlines():
            self.assertTrue(line.startswith("!"), line)

    def test_no_dependency(self):
        self.assertIn("none", dependencies.slot([], "CE01"))


class Missing(unittest.TestCase):
    def test_compared_with_a_running_config(self):
        deps = dependencies.find("interface GigabitEthernet0/0/1.100\n vrf forwarding A\n"
                                 "router bgp 65001\n address-family ipv4 vrf A\n"
                                 "  neighbor 192.0.2.1 route-map RM-IN in\n"
                                 "  neighbor 192.0.2.1 route-map RM-OUT out\n")
        running = ("vrf definition A\n rd 65001:1\n!\ninterface GigabitEthernet0/0/1\n"
                   " no ip address\n!\nroute-map RM-IN permit 10\n")
        self.assertEqual([(d["kind"], d["name"]) for d in dependencies.missing(deps, running)],
                         [("route-map", "RM-OUT")])

    def test_names_are_matched_exactly(self):
        deps = [{"kind": "route-map", "name": "RM-IN", "used_by": ["x"]}]
        self.assertTrue(dependencies.missing(deps, "route-map RM-IN-OLD permit 10\n"))
        self.assertTrue(dependencies.missing(deps, " route-map RM-IN permit 10\n"))  # not top level


if __name__ == "__main__":
    unittest.main()
