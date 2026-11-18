"""Offline Cisco running-config to DNS records; no GUI and no implicit writes.

The public parsing/building/export functions keep only the allowlisted network
fields. Export functions return data; only the CLI writes explicit destinations.
"""

import argparse
import csv
import io
import ipaddress
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


DEFAULT_ABBREVIATIONS = (
    ("TenGigabitEthernet", "Te"), ("TenGigE", "Te"),
    ("TwentyFiveGigabitEthernet", "Twe"), ("TwentyFiveGigE", "Twe"),
    ("FortyGigabitEthernet", "Fo"), ("FortyGigE", "Fo"),
    ("HundredGigabitEthernet", "Hu"), ("HundredGigE", "Hu"),
    ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"),
    ("Ethernet", "Et"), ("Loopback", "Lo"), ("Vlan", "Vl"),
    ("Tunnel", "Tu"), ("Port-channel", "Po"), ("Bundle-Ether", "BE"),
    ("MgmtEth", "Mg"),
)
STATUS_OK = "OK"
STATUS_SHUTDOWN = "shutdown"
STATUS_SECONDARY = "secondary"
STATUS_DUP_NAME = "duplicate name"
STATUS_DUP_IP = "duplicate IP"
STATUS_TOO_LONG = "name too long"
STATUS_DOT1Q = "dot1q \u2260 sub-interface number"
STATUS_UNKNOWN_TYPE = "unknown interface type"
NOT_A_RECORD = "(not a record)"
TABLE_COLUMNS = ("FQDN", "IPv4 address", "Device", "Interface", "dot1q", "VRF", "Status")


@dataclass
class Address:
    ip: str
    prefix_len: int
    secondary: bool = False


@dataclass
class ParsedInterface:
    name: str
    addresses: List[Address] = field(default_factory=list)
    shutdown: bool = False
    vrf: str = ""
    dot1q: Optional[str] = None
    line_no: int = 0


@dataclass
class LoadedFile:
    source: str = ""
    hostname: Optional[str] = None
    platform: str = "unknown"
    interfaces: List[ParsedInterface] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


@dataclass
class DnsSettings:
    domain: str = "example.intra"
    include_secondary: bool = False
    exclude_shutdown: bool = False
    lowercase: bool = False
    abbreviations: List[Tuple[str, str]] = field(
        default_factory=lambda: list(DEFAULT_ABBREVIATIONS))


@dataclass
class DnsRecord:
    fqdn: str
    ip: str
    device: str
    interface: str
    dot1q: str = ""
    vrf: str = ""
    warnings: List[str] = field(default_factory=list)
    is_record: bool = True
    source: str = ""

    @property
    def status(self):
        return "; ".join(self.warnings) if self.warnings else STATUS_OK


@dataclass
class DnsResult:
    records: List[DnsRecord] = field(default_factory=list)
    files: List[LoadedFile] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def read_text_file(path):
    """Read UTF-8 (including BOM), falling back to Windows cp1252."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def parse_config(text, source=""):
    """Parse only top-level identity and indented interface fields.

    Banner bodies are opaque, even when they contain apparent config commands.
    Invalid address statements are ignored rather than exposing their contents.
    """
    result = LoadedFile(source=source)
    current = None
    banner_end = None
    xr = False
    for number, line in enumerate(text.splitlines(), 1):
        if banner_end is not None:
            if banner_end in line:
                banner_end = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("!"):
            continue
        banner = re.match(r"banner\s+\S+\s+(.+)", stripped, re.I)
        if banner:
            payload = banner.group(1)
            delimiter = "^C" if payload.startswith("^C") else payload[0]
            if delimiter not in payload[len(delimiter):]:
                banner_end = delimiter
            current = None
            continue
        if not line[0].isspace():
            current = None
            host = re.fullmatch(r"hostname\s+(\S+)", stripped, re.I)
            if host and result.hostname is None:
                result.hostname = host.group(1)
            interface = re.fullmatch(r"interface\s+(\S+)", stripped, re.I)
            if interface:
                current = ParsedInterface(interface.group(1), line_no=number)
                result.interfaces.append(current)
                xr = xr or bool(re.search(r"\d+/\d+/\d+/\d+", current.name))
            continue
        if current is None:
            continue
        if re.match(r"ipv4\s+address\b", stripped, re.I):
            xr = True
        if stripped.lower() in ("shutdown", "no shutdown"):
            current.shutdown = stripped.lower() == "shutdown"
        vrf = re.fullmatch(r"vrf\s+(?:forwarding\s+)?(\S+)", stripped, re.I)
        if vrf:
            current.vrf = vrf.group(1)
        encap = re.match(r"encapsulation\s+dot1q\s+(\d+)\b", stripped, re.I)
        if encap:
            current.dot1q = encap.group(1)
        address = re.fullmatch(
            r"(ip|ipv4)\s+address\s+(\S+)(?:\s+(\S+))?(?:\s+(secondary))?",
            stripped, re.I)
        if not address:
            continue
        family, value, mask, secondary = address.groups()
        try:
            if "/" in value and family.lower() == "ipv4":
                if mask and mask.lower() != "secondary":
                    continue
                parsed = ipaddress.IPv4Interface(value)
                is_secondary = bool(secondary or mask)
            else:
                if not mask:
                    continue
                parsed = ipaddress.IPv4Interface(value + "/" + mask)
                is_secondary = bool(secondary)
            current.addresses.append(Address(str(parsed.ip), parsed.network.prefixlen,
                                             is_secondary))
        except ValueError:
            continue
    result.platform = "IOS-XR" if xr else ("IOS-XE" if result.interfaces else "unknown")
    if result.hostname is None:
        result.problems.append("no hostname line")
    return result


def load_file(path):
    """Load a source without retaining its raw text or raising read errors."""
    try:
        return parse_config(read_text_file(path), str(path))
    except OSError as error:
        return LoadedFile(source=str(path), problems=["cannot read file: " + str(error)])


def find_text_files(folder, recursive=False):
    """Return sorted regular files, excluding NUL-containing files and >5 MiB."""
    base = Path(folder)
    found = []
    for path in (base.rglob("*") if recursive else base.iterdir()):
        try:
            if path.is_file() and path.stat().st_size <= 5 * 1024 * 1024:
                if b"\0" not in path.read_bytes():
                    found.append(path)
        except OSError:
            continue
    return sorted(found, key=lambda p: str(p).casefold())


def short_interface_name(name, abbreviations):
    """Return (sanitized short name, sub-interface number, known type)."""
    sub = re.search(r"\.(\d+)$", name)
    base = name[:sub.start()] if sub else name
    token = re.match(r"[A-Za-z]+(?:-[A-Za-z]+)*", base)
    kind = token.group(0) if token else ""
    known = False
    for long_name, short_name in sorted(abbreviations, key=lambda pair: -len(pair[0])):
        if kind.casefold() == long_name.casefold():
            base = short_name + base[len(kind):]
            known = True
            break
    if not known:
        for long_name, short_name in sorted(abbreviations, key=lambda pair: -len(pair[1])):
            if kind.casefold() == short_name.casefold():
                base = short_name + base[len(kind):]
                known = True
                break
    return re.sub(r"[^A-Za-z0-9-]", "-", base), sub.group(1) if sub else "", known


def mark_duplicates(records):
    """Mark every member of each duplicate group, across all sources."""
    names, ips = {}, {}
    for record in records:
        if record.is_record and record.fqdn:
            names.setdefault(record.fqdn.casefold(), []).append(record)
        if record.ip:
            ips.setdefault(record.ip, []).append(record)
    for groups, warning in ((names, STATUS_DUP_NAME), (ips, STATUS_DUP_IP)):
        for group in groups.values():
            if len(group) > 1:
                for record in group:
                    if warning not in record.warnings:
                        record.warnings.append(warning)


def build_records(files, settings=None):
    """Apply naming options then detect duplicates globally; inputs are unchanged."""
    settings = settings if settings is not None else DnsSettings()
    result = DnsResult(files=list(files))
    for loaded in result.files:
        result.problems.extend((loaded.source + ": " if loaded.source else "") + p
                               for p in loaded.problems)
        if not loaded.hostname:
            if not loaded.problems:
                result.problems.append("no hostname line")
            continue
        for interface in loaded.interfaces:
            if interface.shutdown and settings.exclude_shutdown:
                continue
            short, sub, known = short_interface_name(interface.name, settings.abbreviations)
            dot1q = (interface.dot1q or sub) if sub else ""
            label = loaded.hostname + "_" + short + ("_" + dot1q if dot1q else "")
            secondary_number = 0
            for address in interface.addresses:
                warnings = []
                if interface.shutdown:
                    warnings.append(STATUS_SHUTDOWN)
                if address.secondary:
                    secondary_number += 1
                    warnings.append(STATUS_SECONDARY)
                if not known:
                    warnings.append(STATUS_UNKNOWN_TYPE)
                if sub and interface.dot1q and int(sub) != int(interface.dot1q):
                    warnings.append(STATUS_DOT1Q)
                is_record = not address.secondary or settings.include_secondary
                suffix = "-sec" + str(secondary_number) if address.secondary else ""
                fqdn = label + suffix + "." + settings.domain if is_record else ""
                if settings.lowercase:
                    fqdn = fqdn.lower()
                if any(len(part) > 63 for part in fqdn.split(".")):
                    warnings.append(STATUS_TOO_LONG)
                result.records.append(DnsRecord(
                    fqdn, address.ip, loaded.hostname, interface.name, dot1q,
                    interface.vrf, warnings, is_record, loaded.source))
    mark_duplicates(result.records)
    return result


def record_row(record):
    return (record.fqdn or NOT_A_RECORD, record.ip, record.device,
            record.interface, record.dot1q, record.vrf, record.status)


def summary_text(result):
    devices = {item.hostname for item in result.files if item.hostname}
    return "{} records from {} devices, {} warnings".format(
        sum(r.is_record for r in result.records), len(devices),
        sum(bool(r.warnings) for r in result.records))


def table_sort_key(column_index, value):
    if column_index in (1, 4):
        try:
            number = int(ipaddress.IPv4Address(value)) if column_index == 1 else int(value)
            return (0, number)
        except ValueError:
            return (1, 0)
    return value.casefold()


def to_csv_bytes(records, delimiter=","):
    """Return Excel-friendly UTF-8 BOM CSV with all informational rows."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow(TABLE_COLUMNS)
    writer.writerows(record_row(r) for r in records)
    return output.getvalue().encode("utf-8-sig")


def _single_line(value):
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def to_tsv(records, header=True):
    rows = [TABLE_COLUMNS] if header else []
    rows.extend(record_row(r) for r in records)
    return "".join("\t".join(_single_line(v) for v in row) + "\n" for row in rows)


def _powershell_quote(value):
    # Newlines are escaped too: arbitrary caller values cannot inject commands.
    value = value.replace("`", "``").replace("$", "`$").replace('"', '`"')
    return '"' + value.replace("\r", "`r").replace("\n", "`n") + '"'


def to_powershell(records, domain):
    """Return CRLF commands; warning rows remain reviewable comments."""
    lines = []
    for record in records:
        if not record.is_record:
            continue
        suffix = "." + domain
        name = record.fqdn[:-len(suffix)] if record.fqdn.lower().endswith(suffix.lower()) else record.fqdn
        line = ("Add-DnsServerResourceRecordA -ZoneName {} -Name {} "
                "-IPv4Address {} -CreatePtr").format(
                    _powershell_quote(domain), _powershell_quote(name),
                    _powershell_quote(record.ip))
        if record.warnings:
            line = "# " + line + "  # " + _single_line(record.status)
        lines.append(line)
    return "".join(line + "\r\n" for line in lines)


def main(argv=None):
    """CLI: no output files unless --csv or --powershell is supplied."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+")
    parser.add_argument("--domain", default="example.intra")
    parser.add_argument("--csv", metavar="PATH")
    parser.add_argument("--powershell", metavar="PATH")
    parser.add_argument("--lowercase", action="store_true")
    parser.add_argument("--include-secondary", action="store_true")
    parser.add_argument("--exclude-shutdown", action="store_true")
    args = parser.parse_args(argv)
    settings = DnsSettings(args.domain, args.include_secondary,
                           args.exclude_shutdown, args.lowercase)
    result = build_records([load_file(path) for path in args.files], settings)
    for problem in result.problems:
        print(problem, file=sys.stderr)
    try:
        if args.csv:
            Path(args.csv).write_bytes(to_csv_bytes(result.records))
        if args.powershell:
            Path(args.powershell).write_bytes(
                to_powershell(result.records, settings.domain).encode("utf-8-sig"))
    except OSError as error:
        print("Cannot export: " + str(error), file=sys.stderr)
        return 1
    print(summary_text(result))
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(main())
