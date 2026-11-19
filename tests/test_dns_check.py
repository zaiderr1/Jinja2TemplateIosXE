"""Tests for dns_check.py and the DNS exclusions. nslookup is replaced by fake
outputs (Windows English, Windows French, Linux); all data is fictional."""

import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path

import dns_check as chk
import dns_names as dns

SERVER = "Server:  dns1.example.intra\nAddress:  192.0.2.53\n\n"

WIN_PTR = SERVER + "1.113.0.203.in-addr.arpa\tname = CE01_Lo0.example.intra\n"
WIN_PTR_OTHER = SERVER + "1.113.0.203.in-addr.arpa\tname = old-router.example.intra\n"
WIN_PTR_TWO = WIN_PTR + "1.113.0.203.in-addr.arpa\tname = alias.example.intra\n"
WIN_NX = SERVER + "*** dns1.example.intra can't find {q}: Non-existent domain\n"
WIN_A = SERVER + "Name:    CE01_Lo0.example.intra\nAddress:  203.0.113.1\n\n"
WIN_A_OTHER = SERVER + "Name:    CE01_Lo0.example.intra\nAddress:  203.0.113.99\n\n"
WIN_TIMEOUT = "DNS request timed out.\n    timeout was 2 seconds.\nServer:  UnKnown\n"
FR_NX = ("Serveur :   dns1.example.intra\nAddress:  192.0.2.53\n\n"
         "*** dns1.example.intra ne parvient pas à trouver {q} : Non-existent domain\n")
FR_A = ("Serveur :   dns1.example.intra\nAddress:  192.0.2.53\n\n"
        "Réponse ne faisant pas autorité :\nNom :    CE01_Lo0.example.intra\n"
        "Address:  203.0.113.1\n")
FR_TIMEOUT = "La requête DNS a expiré.\n    le délai d'attente était de 2 secondes.\n"
LINUX_PTR = ("Server:\t\t192.0.2.53\nAddress:\t192.0.2.53#53\n\n"
             "1.113.0.203.in-addr.arpa\tname = CE01_Lo0.example.intra.\n")
LINUX_NX = ("Server:\t\t192.0.2.53\nAddress:\t192.0.2.53#53\n\n"
            "** server can't find {q}: NXDOMAIN\n")


def runner(ptr, a):
    """Fake nslookup: returns `ptr` for PTR queries and `a` for A queries."""
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        out = ptr if args[0] == "-type=PTR" else a
        if isinstance(out, Exception):
            raise out
        return out.format(q=args[1])
    run.calls = calls
    return run


def record(fqdn="CE01_Lo0.example.intra", ip="203.0.113.1"):
    return dns.DnsRecord(fqdn, ip, "CE01", "Loopback0")


class Parsing(unittest.TestCase):
    def test_ptr_outputs(self):
        self.assertEqual(chk.parse_ptr(WIN_PTR).values, ["CE01_Lo0.example.intra"])
        self.assertEqual(chk.parse_ptr(LINUX_PTR).values, ["CE01_Lo0.example.intra"])
        self.assertEqual(chk.parse_ptr(WIN_PTR_TWO).status, chk.FOUND)
        self.assertEqual(len(chk.parse_ptr(WIN_PTR_TWO).values), 2)
        for raw in (WIN_NX, FR_NX, LINUX_NX):
            self.assertEqual(chk.parse_ptr(raw.format(q="x.in-addr.arpa")).status, chk.NOT_FOUND)
        for raw in (WIN_TIMEOUT, FR_TIMEOUT):
            self.assertEqual(chk.parse_ptr(raw).status, chk.NO_ANSWER)
        self.assertEqual(chk.parse_ptr("something strange").status, chk.UNKNOWN)

    def test_a_outputs_ignore_the_dns_server_address(self):
        fqdn = "CE01_Lo0.example.intra"
        self.assertEqual(chk.parse_a(WIN_A, fqdn).values, ["203.0.113.1"])
        self.assertEqual(chk.parse_a(FR_A, fqdn).values, ["203.0.113.1"])
        nx = chk.parse_a(WIN_NX.format(q=fqdn + "."), fqdn)
        self.assertEqual((nx.status, nx.values), (chk.NOT_FOUND, []))   # 192.0.2.53 ignored
        self.assertEqual(chk.parse_a(FR_NX.format(q=fqdn + "."), fqdn).status, chk.NOT_FOUND)


class Judging(unittest.TestCase):
    CASES = [
        # (ptr output, a output, expected kind, expected text)
        (WIN_NX, WIN_NX, "none", ""),
        (FR_NX, FR_NX, "none", ""),
        (WIN_PTR, WIN_A, "same", "same"),
        (WIN_PTR, FR_A, "same", "same"),
        (WIN_PTR_OTHER, WIN_NX, "conflict", "PTR: old-router.example.intra"),
        (WIN_PTR_TWO, WIN_A, "conflict",
         "multiple PTR: CE01_Lo0.example.intra, alias.example.intra"),
        (WIN_NX, WIN_A_OTHER, "conflict", "name -> 203.0.113.99"),
        (WIN_NX, WIN_A, "conflict", "A only (no PTR)"),
        (WIN_PTR, WIN_NX, "conflict", "PTR only (no A)"),
        (WIN_TIMEOUT, WIN_NX, "no answer", "no answer"),
        (WIN_PTR_OTHER, WIN_TIMEOUT, "conflict", "PTR: old-router.example.intra"),
        (TimeoutError("5 s"), WIN_NX, "no answer", "no answer"),
        (FileNotFoundError("nslookup"), FileNotFoundError("nslookup"), "no answer", "no answer"),
        (WIN_NX, "??", "unknown", "unknown"),
    ]

    def test_every_case(self):
        for ptr, a, kind, text in self.CASES:
            with self.subTest(kind=kind, text=text):
                r = chk.check_record(record(), runner=runner(ptr, a))
                self.assertEqual((r.existing_kind, r.existing), (kind, text))
                self.assertIn("nslookup -type=PTR", r.lookup_raw)

    def test_reverse_only(self):
        r = chk.check_record(record(), forward=False, runner=runner(WIN_PTR, WIN_A_OTHER))
        self.assertEqual(r.existing_kind, "same")
        r = chk.check_record(record(), forward=False, runner=runner(WIN_NX, WIN_A))
        self.assertEqual(r.existing_kind, "none")

    def test_case_and_trailing_dot_do_not_matter(self):
        r = chk.check_record(record("ce01_lo0.EXAMPLE.intra"), runner=runner(LINUX_PTR, WIN_A))
        self.assertEqual(r.existing_kind, "same")


class SafeCommandLine(unittest.TestCase):
    def test_arguments_passed_to_nslookup(self):
        fake = runner(WIN_NX, WIN_NX)
        chk.check_record(record(), server="192.0.2.53", runner=fake)
        self.assertEqual(fake.calls, [["-type=PTR", "203.0.113.1", "192.0.2.53"],
                                      ["-type=A", "CE01_Lo0.example.intra.", "192.0.2.53"]])

    def test_options_and_odd_values_never_reach_nslookup(self):
        for bad in ("-debug", "a b", "x;calc", "x&y", "", ".."):
            with self.subTest(server=bad):
                if bad:
                    with self.assertRaises(ValueError):
                        chk.valid_server(bad)
        with self.assertRaises(ValueError):
            chk.valid_ip("203.0.113.1 -debug")
        with self.assertRaises(ValueError):
            chk.valid_name("-type=ANY")

    def test_bad_name_becomes_unknown_without_running_anything(self):
        fake = runner(WIN_NX, WIN_NX)
        r = record(fqdn="bad name.example.intra")
        chk.check_records([r], runner=fake)
        self.assertEqual(r.existing_kind, "unknown")
        self.assertEqual([c for c in fake.calls if c[0] == "-type=A"], [])

    def test_bad_server_stops_before_any_lookup(self):
        fake = runner(WIN_NX, WIN_NX)
        with self.assertRaises(ValueError):
            chk.check_records([record()], server="-x", runner=fake)
        self.assertEqual(fake.calls, [])

    def test_missing_nslookup_is_no_answer(self):
        def missing(args, timeout):
            raise FileNotFoundError("nslookup")
        self.assertEqual(chk.check_record(record(), runner=missing).existing_kind, "no answer")


class ManyRecords(unittest.TestCase):
    def setUp(self):
        text = ("hostname CE01\ninterface Loopback0\n ip address 203.0.113.1 255.255.255.255\n"
                "interface Loopback1\n ip address 203.0.113.2 255.255.255.255\n"
                "interface MgmtEth0/RP0/CPU0/0\n vrf Mgmt-intf\n"
                " ipv4 address 198.51.100.10 255.255.255.0\n"
                "interface Vlan10\n ip address 198.51.100.65 255.255.255.192\n"
                " ip address 198.51.100.126 255.255.255.192 secondary\n")
        settings = dns.DnsSettings(exclude_vrfs=["Mgmt-intf"])
        self.records = dns.build_records([dns.parse_config(text)], settings).records

    def test_only_real_records_are_checked_and_progress_is_reported(self):
        seen = []
        n = chk.check_records(self.records, runner=runner(WIN_NX, WIN_NX),
                              progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(n, 3)               # Lo0, Lo1, Vl10 (not mgmt, not secondary)
        self.assertEqual(sorted(seen)[-1], (3, 3))
        mgmt = next(r for r in self.records if r.excluded)
        self.assertEqual(mgmt.existing, "")

    def test_cancel_starts_no_new_lookup(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(chk.check_records(self.records, runner=runner(WIN_NX, WIN_NX),
                                           cancel=stop), 0)

    def test_powershell_after_check(self):
        def by_ip(args, timeout):
            if args[0] == "-type=PTR" and args[1] == "203.0.113.1":
                return WIN_PTR
            if args[0] == "-type=A" and args[1].startswith("CE01_Lo0"):
                return WIN_A
            if args[1] == "203.0.113.2":
                return WIN_PTR_OTHER
            return WIN_NX.format(q=args[1])
        chk.check_records(self.records, runner=by_ip)
        ps = dns.to_powershell(self.records, "example.intra").splitlines()
        self.assertTrue(ps[0].startswith("# Add-") and ps[0].endswith(
            "# already in DNS: same"))
        self.assertTrue(ps[1].startswith("# Add-") and "conflict: PTR: old-router" in ps[1])
        self.assertTrue(ps[2].startswith("Add-DnsServerResourceRecordA") and "Vl10" in ps[2])
        self.assertEqual(len(ps), 3)         # excluded mgmt and secondary: no line
        summary = dns.summary_text(dns.DnsResult(self.records, [], []))
        self.assertIn("1 to create, 1 already correct, 1 conflicts, 0 no answer", summary)


class Exclusions(unittest.TestCase):
    TEXT = ("hostname PE01\ninterface Loopback0\n ipv4 address 203.0.113.1 255.255.255.255\n"
            "interface MgmtEth0/RP0/CPU0/0\n vrf Mgmt-intf\n ipv4 address 198.51.100.10/24\n"
            "interface TenGigE0/0/0/1.4094\n vrf underlay\n ipv4 address 192.0.2.1 255.255.255.254\n"
            " encapsulation dot1q 4094\n"
            "interface TenGigE0/0/0/1.100\n vrf underlay\n ipv4 address 192.0.2.3 255.255.255.254\n"
            " encapsulation dot1q 100\n"
            "interface Strange0\n ipv4 address 203.0.113.5 255.255.255.255\n")

    def build(self, **options):
        return {r.interface: r for r in dns.build_records(
            [dns.parse_config(self.TEXT)], dns.DnsSettings(**options)).records}

    def test_nothing_excluded_by_default(self):
        self.assertFalse(any(r.excluded for r in self.build().values()))

    def test_by_type_long_or_short_spelling(self):
        r = self.build(excluded_types=["Lo"])
        self.assertEqual(r["Loopback0"].excluded, "type Lo")
        self.assertEqual(r["Loopback0"].status, "excluded (type Lo)")
        self.assertFalse(r["Loopback0"].is_record)
        self.assertEqual(r["TenGigE0/0/0/1.100"].excluded, "")
        r = self.build(excluded_types=["te"])
        self.assertEqual(r["TenGigE0/0/0/1.100"].excluded, "type Te")

    def test_unknown_types(self):
        self.assertEqual(self.build(exclude_unknown_types=True)["Strange0"].excluded,
                         "unknown type")

    def test_by_name_pattern_and_vrf(self):
        r = self.build(exclude_patterns=["mgmteth*", "*.4094"])
        self.assertEqual(r["MgmtEth0/RP0/CPU0/0"].excluded, "pattern mgmteth*")
        self.assertEqual(r["TenGigE0/0/0/1.4094"].excluded, "pattern *.4094")
        self.assertEqual(r["TenGigE0/0/0/1.100"].excluded, "")
        r = self.build(exclude_vrfs=["mgmt-intf"])
        self.assertEqual(r["MgmtEth0/RP0/CPU0/0"].excluded, "VRF Mgmt-intf")

    def test_excluded_are_never_exported_nor_duplicates(self):
        text = self.TEXT + "interface Loopback9\n ipv4 address 203.0.113.1 255.255.255.255\n"
        records = dns.build_records([dns.parse_config(text)],
                                    dns.DnsSettings(exclude_patterns=["Loopback9"])).records
        self.assertNotIn(dns.STATUS_DUP_IP, records[0].warnings)
        self.assertNotIn("Loopback9", dns.to_powershell(records, "example.intra"))
        self.assertNotIn(b"Loopback9", dns.to_csv_bytes(records))
        self.assertNotIn("Loopback9", dns.to_tsv(records))
        self.assertIn("1 excluded", dns.summary_text(dns.DnsResult(records, [], [])))

    def test_cli_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / "pe01.cfg"
            src.write_text(self.TEXT, encoding="utf-8")
            out = Path(folder) / "out.csv"
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                dns.main([str(src), "--csv", str(out), "--exclude-type", "Loopback",
                          "--exclude-vrf", "Mgmt-intf", "--exclude-interface", "*.4094",
                          "--exclude-unknown-types"])
            csv_text = out.read_bytes().decode("utf-8-sig")
        self.assertIn("1 records from 1 devices, 0 warnings, 4 excluded", stdout.getvalue())
        self.assertIn("TenGigE0/0/0/1.100", csv_text)
        self.assertNotIn("Loopback0", csv_text)


class Preferences(unittest.TestCase):
    def test_exclusions_are_saved_but_not_the_dns_server(self):
        import netgen_core
        clean = netgen_core._filter_settings({"dns": {
            "excluded_types": ["Lo", 5, "", "Lo"], "exclude_patterns": ["MgmtEth*", "a\nb"],
            "exclude_vrfs": "Mgmt-intf", "exclude_unknown_types": 1, "check_forward": False,
            "dns_server": "192.0.2.53"}})["dns"]
        self.assertEqual(clean["excluded_types"], ["Lo"])
        self.assertEqual(clean["exclude_patterns"], ["MgmtEth*"])
        self.assertEqual(clean["exclude_vrfs"], [])          # not a list: default kept
        self.assertTrue(clean["exclude_unknown_types"])
        self.assertFalse(clean["check_forward"])
        self.assertNotIn("dns_server", clean)


if __name__ == "__main__":
    unittest.main()
