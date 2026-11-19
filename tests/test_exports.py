"""Tests for CSV, PowerShell and tab-separated DNS exports."""

import csv
import io
import unittest

import dns_names as dns


class ExportTests(unittest.TestCase):
    def setUp(self):
        text = """hostname CE01
interface Loopback0
 ip address 203.0.113.1 255.255.255.255
!
interface GigabitEthernet0/0.100
 encapsulation dot1q 100
 ip address 192.0.2.1 255.255.255.254
 shutdown
!
interface Vlan10
 ip address 198.51.100.65 255.255.255.192 secondary
"""
        self.result = dns.build_records([dns.parse_config(text, "fake.cfg")])

    def test_powershell_active_and_commented_lines(self):
        output = dns.to_powershell(self.result.records, "example.intra")
        self.assertEqual(output.splitlines(), [
            'Add-DnsServerResourceRecordA -ZoneName "example.intra" -Name "CE01_Lo0" -IPv4Address "203.0.113.1" -CreatePtr',
            '# Add-DnsServerResourceRecordA -ZoneName "example.intra" -Name "CE01_Gi0-0_100" -IPv4Address "192.0.2.1" -CreatePtr  # shutdown',
        ])
        self.assertNotIn("198.51.100.65", output)  # secondary is informational by default

    def test_powershell_escapes_domain_and_name_arguments(self):
        record = dns.DnsRecord(
            'C`$"E_Lo0.zone', "203.0.113.3", 'C`$"E', "Loopback0")
        line = dns.to_powershell([record], 'z`$"one')
        expected_domain = '"' + "z" + "``" + "`$" + '`"' + "one" + '"'
        expected_name = '"' + "C" + "``" + "`$" + '`"' + "E_Lo0.zone" + '"'
        expected = ('Add-DnsServerResourceRecordA -ZoneName ' + expected_domain +
                    ' -Name ' + expected_name +
                    ' -IPv4Address "203.0.113.3" -CreatePtr\r\n')
        self.assertEqual(line, expected)

    def test_csv_is_bom_utf8_with_headers_and_all_rows(self):
        payload = dns.to_csv_bytes(self.result.records)
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        decoded = payload.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(decoded, newline="")))
        self.assertEqual(tuple(rows[0]), dns.TABLE_COLUMNS)
        self.assertEqual(len(rows[0]), 8)
        self.assertEqual(len(rows), len(self.result.records) + 1)
        self.assertEqual(rows[-1][0], dns.NOT_A_RECORD)
        self.assertIn("secondary", rows[-1][-2])

    def test_tsv_has_header_and_sanitizes_tabs_and_newlines(self):
        record = dns.DnsRecord("CE01_Lo0.example.intra", "203.0.113.4", "CE01\tPE01",
                               "Loopback0\nInjected", warnings=[])
        rows = dns.to_tsv([record]).splitlines()
        self.assertEqual(rows[0].split("\t"), list(dns.TABLE_COLUMNS))
        self.assertEqual(rows[1].split("\t")[2:4], ["CE01 PE01", "Loopback0 Injected"])
        self.assertEqual(len(rows[1].split("\t")), 8)


if __name__ == "__main__":
    unittest.main()
