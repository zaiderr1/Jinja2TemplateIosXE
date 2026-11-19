"""Acceptance tests for offline Cisco running-config parsing and DNS names."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import dns_names as dns


XE_EXAMPLE = """hostname CE01
!
interface Loopback0
 ip address 203.0.113.1 255.255.255.255
!
interface TwentyFiveGigE1/0/1
 no ip address
!
interface TwentyFiveGigE1/0/1.100
 description # PE01 - TenGigE0/0/0/1.100 - ( underlay ) #
 encapsulation dot1Q 100
 vrf forwarding underlay
 ip address 192.0.2.0 255.255.255.254
!
interface TwentyFiveGigE1/0/2.300
 encapsulation dot1Q 300
 ip address 198.51.100.0 255.255.255.254
 shutdown
!
interface Vlan10
 ip address 198.51.100.65 255.255.255.192
 ip address 198.51.100.126 255.255.255.192 secondary
!
end
"""

XR_EXAMPLE = """hostname PE01
interface TenGigE0/0/0/1.100
 description # CE01 - TwentyFiveGigE1/0/1.100 - ( underlay ) #
 vrf underlay
 ipv4 address 192.0.2.1 255.255.255.254
 encapsulation dot1q 100
!
interface Bundle-Ether10.200
 ipv4 address 198.51.100.129/31
 encapsulation dot1q 201
!
"""


class DnsNameTests(unittest.TestCase):
    def test_exact_xe_example_including_secondary_informational_row(self):
        result = dns.build_records([dns.parse_config(XE_EXAMPLE, "xe.txt")])
        rows = [dns.record_row(r) for r in result.records]
        self.assertEqual(rows, [
            ("CE01_Lo0.example.intra", "203.0.113.1", "CE01", "Loopback0", "", "", "OK", ""),
            ("CE01_Twe1-0-1_100.example.intra", "192.0.2.0", "CE01",
             "TwentyFiveGigE1/0/1.100", "100", "underlay", "OK", ""),
            ("CE01_Twe1-0-2_300.example.intra", "198.51.100.0", "CE01",
             "TwentyFiveGigE1/0/2.300", "300", "", "shutdown", ""),
            ("CE01_Vl10.example.intra", "198.51.100.65", "CE01", "Vlan10", "", "", "OK", ""),
            (dns.NOT_A_RECORD, "198.51.100.126", "CE01", "Vlan10", "", "", "secondary", ""),
        ])

    def test_exact_xr_example_and_mismatched_dot1q(self):
        result = dns.build_records([dns.parse_config(XR_EXAMPLE, "xr.txt")])
        self.assertEqual([dns.record_row(r) for r in result.records], [
            ("PE01_Te0-0-0-1_100.example.intra", "192.0.2.1", "PE01",
             "TenGigE0/0/0/1.100", "100", "underlay", "OK", ""),
            ("PE01_BE10_201.example.intra", "198.51.100.129", "PE01",
             "Bundle-Ether10.200", "201", "", dns.STATUS_DOT1Q, ""),
        ])
        self.assertEqual(result.files[0].platform, "IOS-XR")

    def test_long_and_short_spellings_for_every_abbreviation_type(self):
        aliases = (
            ("TenGigabitEthernet", "TenGigE", "Te"),
            ("TwentyFiveGigabitEthernet", "TwentyFiveGigE", "Twe"),
            ("FortyGigabitEthernet", "FortyGigE", "Fo"),
            ("HundredGigabitEthernet", "HundredGigE", "Hu"),
            ("GigabitEthernet", "Gi", "Gi"),
            ("FastEthernet", "Fa", "Fa"),
            ("Ethernet", "Et", "Et"),
            ("Loopback", "Lo", "Lo"),
            ("Vlan", "Vl", "Vl"),
            ("Tunnel", "Tu", "Tu"),
            ("Port-channel", "Po", "Po"),
            ("Bundle-Ether", "BE", "BE"),
            ("MgmtEth", "Mg", "Mg"),
        )
        for long_name, short_name, abbreviation in aliases:
            for spelling in (long_name, short_name, long_name.lower(), short_name.lower()):
                with self.subTest(interface_type=spelling):
                    short, sub, known = dns.short_interface_name(spelling + "0/1", dns.DEFAULT_ABBREVIATIONS)
                    self.assertEqual(short, abbreviation + "0-1")
                    self.assertEqual(sub, "")
                    self.assertTrue(known)
        for long_name, short_name, abbreviation in (
                ("TenGigabitEthernet", "TenGigE", "Te"),):
            self.assertEqual(dns.short_interface_name(long_name + "0/1", dns.DEFAULT_ABBREVIATIONS)[0], "Te0-1")
            self.assertEqual(dns.short_interface_name(short_name + "0/1", dns.DEFAULT_ABBREVIATIONS)[0], "Te0-1")

    def test_custom_abbreviation_and_unknown_type(self):
        text = "hostname CE01\ninterface StrangeThing0/1\n ip address 203.0.113.2 255.255.255.0\n"
        loaded = dns.parse_config(text)
        default = dns.build_records([loaded]).records[0]
        self.assertEqual(default.fqdn, "CE01_StrangeThing0-1.example.intra")
        self.assertIn(dns.STATUS_UNKNOWN_TYPE, default.warnings)
        settings = dns.DnsSettings(abbreviations=list(dns.DEFAULT_ABBREVIATIONS) + [("StrangeThing", "St")])
        changed = dns.build_records([loaded], settings).records[0]
        self.assertEqual(changed.fqdn, "CE01_St0-1.example.intra")
        self.assertNotIn(dns.STATUS_UNKNOWN_TYPE, changed.warnings)

    def test_secondary_shutdown_lowercase_options(self):
        loaded = dns.parse_config(XE_EXAMPLE)
        settings = dns.DnsSettings(include_secondary=True, lowercase=True)
        rows = dns.build_records([loaded], settings).records
        secondary = next(r for r in rows if r.ip == "198.51.100.126")
        self.assertTrue(secondary.is_record)
        self.assertEqual(secondary.fqdn, "ce01_vl10-sec1.example.intra")
        self.assertIn(dns.STATUS_SECONDARY, secondary.warnings)
        kept = [r for r in dns.build_records([loaded], dns.DnsSettings(exclude_shutdown=True)).records
                if not r.excluded]
        self.assertEqual(len(kept), 4)

    def test_duplicate_names_and_ips_are_marked_across_files(self):
        first = dns.parse_config("hostname CE01\ninterface Loopback0\n ip address 203.0.113.7 255.255.255.255\n", "one")
        same_name = dns.parse_config("hostname CE01\ninterface Loopback0\n ip address 203.0.113.8 255.255.255.255\n", "two")
        same_ip = dns.parse_config("hostname PE01\ninterface Loopback1\n ip address 203.0.113.7 255.255.255.255\n", "three")
        records = dns.build_records([first, same_name, same_ip]).records
        by_ip = {r.ip: r for r in records}
        self.assertIn(dns.STATUS_DUP_NAME, records[0].warnings)
        self.assertIn(dns.STATUS_DUP_NAME, records[1].warnings)
        self.assertIn(dns.STATUS_DUP_IP, by_ip["203.0.113.7"].warnings)
        self.assertNotIn(dns.STATUS_DUP_IP, by_ip["203.0.113.8"].warnings)

    def test_64_character_label_is_warned_and_63_is_allowed(self):
        def result_for(host):
            return dns.build_records([dns.parse_config(
                "hostname {}\ninterface Loopback0\n ip address 203.0.113.9 255.255.255.255\n".format(host))]).records[0]
        record_63 = result_for("H" * 59)  # hostname + underscore + Lo0
        record_64 = result_for("H" * 60)
        self.assertNotIn(dns.STATUS_TOO_LONG, record_63.warnings)
        self.assertIn(dns.STATUS_TOO_LONG, record_64.warnings)

    def test_hostname_absent_is_reported_and_skipped(self):
        loaded = dns.parse_config("interface Loopback0\n ip address 203.0.113.10 255.255.255.255\n", "missing.txt")
        result = dns.build_records([loaded])
        self.assertIsNone(loaded.hostname)
        self.assertEqual(loaded.problems, ["no hostname line"])
        self.assertEqual(result.records, [])
        self.assertTrue(any("no hostname line" in p for p in result.problems))

    def test_banner_headers_and_excluded_address_forms_are_ignored(self):
        text = """Building configuration...\nCurrent configuration : 99 bytes\nhostname CE01
banner motd ^C
interface Fake0
 ip address 203.0.113.90 255.255.255.0
^C
interface GigabitEthernet0/0
 no ip address
 ip address dhcp
 ip unnumbered Loopback0
 ipv4 unnumbered Loopback0
 ip address not-an-ip 255.255.255.0
 ipv4 address 203.0.113.91/24
!
end
"""
        loaded = dns.parse_config(text)
        self.assertEqual(len(loaded.interfaces), 1)
        self.assertEqual(len(loaded.interfaces[0].addresses), 1)
        self.assertEqual(loaded.interfaces[0].addresses[0].ip, "203.0.113.91")
        self.assertEqual(loaded.platform, "IOS-XR")
        self.assertEqual(len(dns.build_records([loaded]).records), 1)

    def test_cli_dns_with_fictional_temporary_input_and_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "router.cfg"
            target = Path(folder) / "result.csv"
            source.write_text("hostname CE01\ninterface Loopback0\n ip address 203.0.113.11 255.255.255.255\n", encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = dns.main([str(source), "--domain", "example.intra", "--csv", str(target)])
            self.assertEqual(code, 0)
            self.assertEqual(stdout.getvalue().strip(), "1 records from 1 devices, 0 warnings")
            self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
