"""
dns_check.py - does a DNS record already exist? (read-only, with nslookup)

For each record of dns_names.py it runs the nslookup command of the computer:

    nslookup -type=PTR <ip> [server]      is the IP already registered, and as what?
    nslookup -type=A <fqdn>. [server]     does the name already exist, and where?

and sets on the record:

    existing_kind   existing (shown in the "Existing DNS" column)
    none            ""                          nothing in DNS: the record can be created
    same            "same"                      already correct, nothing to do
    conflict        "PTR: <name>"               the IP already has another name
                    "multiple PTR: a, b"        the IP has several names
                    "name -> <ip>"              the name already points elsewhere
                    "A only (no PTR)"           the name exists without its PTR
                    "PTR only (no A)"           the PTR exists without the name
    no answer       "no answer"                 timeout, unreachable server, no nslookup
    unknown         "unknown"                   output not understood: double-click the row

It only *asks* DNS: nothing is created, changed or deleted, and nothing is
written to disk. Only validated values (an IPv4 address, a host name, the DNS
server) are ever passed to nslookup, as an argument list (never a shell).
Standard library only.
"""
import ipaddress
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List

TIMEOUT = 5          # seconds per nslookup
MAX_PARALLEL = 8     # nslookup processes at the same time

# Words of the nslookup output, in English and French Windows (extend if needed).
NOT_FOUND_WORDS = ("non-existent domain", "nxdomain", "can't find", "can\u2019t find",
                   "ne parvient pas à trouver", "ne parvient pas a trouver",
                   "no answer")
TIMEOUT_WORDS = ("timed out", "a expiré", "a expire", "no response from server",
                 "connection refused", "unreachable", "network is unreachable")

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
NAME = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9_.-]{0,252})$")

FOUND, NOT_FOUND, NO_ANSWER, UNKNOWN = "found", "not found", "no answer", "unknown"


@dataclass
class Lookup:
    """Result of one nslookup."""
    status: str            # found / not found / no answer / unknown
    values: List[str]      # PTR names or A addresses
    raw: str               # nslookup output (kept in memory only)


# ---------------------------------------------------------------------------
# Validation: nothing unchecked ever reaches the command line
# ---------------------------------------------------------------------------
def valid_ip(value):
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except (ValueError, AttributeError):
        raise ValueError("not an IPv4 address: {!r}".format(value))


def valid_name(value):
    value = (value or "").strip().rstrip(".")
    if not NAME.match(value) or ".." in value:
        raise ValueError("not a valid DNS name: {!r}".format(value))
    return value


def valid_server(value):
    """'' (the computer's DNS server), an IPv4 address or a host name."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return valid_ip(value)
    except ValueError:
        return valid_name(value)


# ---------------------------------------------------------------------------
# Running nslookup
# ---------------------------------------------------------------------------
def run_nslookup(args, timeout=TIMEOUT):
    """Run nslookup with an argument list; returns its output text.
    Raises TimeoutError, or FileNotFoundError when nslookup isn't installed."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # no console flash on Windows
    try:
        done = subprocess.run(["nslookup"] + list(args), capture_output=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=flags)
    except subprocess.TimeoutExpired:
        raise TimeoutError("nslookup took more than {} s".format(timeout))
    return _decode(done.stdout) + "\n" + _decode(done.stderr)


def _decode(data):
    """nslookup prints in the console code page (cp850 on French Windows)."""
    if not data:
        return ""
    for encoding in ("utf-8", "cp850", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _lookup(args, parse, runner, timeout):
    try:
        raw = runner(args, timeout)
    except (TimeoutError, OSError) as error:   # OSError: nslookup not available
        return Lookup(NO_ANSWER, [], "{}: {}".format(type(error).__name__, error))
    return parse(raw)


def lookup_ptr(ip, server="", runner=run_nslookup, timeout=TIMEOUT):
    args = ["-type=PTR", valid_ip(ip)] + ([valid_server(server)] if server else [])
    return _lookup(args, parse_ptr, runner, timeout)


def lookup_a(fqdn, server="", runner=run_nslookup, timeout=TIMEOUT):
    # Trailing dot: the computer's DNS suffixes are not appended to the name.
    name = valid_name(fqdn)
    args = ["-type=A", name + "."] + ([valid_server(server)] if server else [])
    return _lookup(args, lambda raw: parse_a(raw, name), runner, timeout)


# ---------------------------------------------------------------------------
# Reading the output (English or French Windows, Linux)
# ---------------------------------------------------------------------------
def _has(raw, words):
    low = raw.casefold()
    return any(w in low for w in words)


def parse_ptr(raw):
    """Every 'name =' line is a PTR answer (this part is never translated)."""
    names = []
    for line in raw.splitlines():
        if "name =" in line.lower():
            value = line.split("=", 1)[1].strip().rstrip(".")
            if value and value.casefold() not in (n.casefold() for n in names):
                names.append(value)
    if names:
        return Lookup(FOUND, names, raw)
    if _has(raw, TIMEOUT_WORDS):
        return Lookup(NO_ANSWER, [], raw)
    if _has(raw, NOT_FOUND_WORDS):
        return Lookup(NOT_FOUND, [], raw)
    return Lookup(UNKNOWN, [], raw)


def parse_a(raw, fqdn):
    """Addresses after the line naming the queried FQDN are the answers; the
    lines before it describe the DNS server itself."""
    lines = raw.splitlines()
    start = next((i for i, l in enumerate(lines) if fqdn.casefold() in l.casefold()
                  and not _has(l, NOT_FOUND_WORDS)), None)
    ips = []
    if start is not None:
        for line in lines[start:]:
            for ip in IPV4.findall(line):
                if ip not in ips:
                    ips.append(ip)
    if ips:
        return Lookup(FOUND, ips, raw)
    if _has(raw, TIMEOUT_WORDS):
        return Lookup(NO_ANSWER, [], raw)
    if _has(raw, NOT_FOUND_WORDS):
        return Lookup(NOT_FOUND, [], raw)
    return Lookup(UNKNOWN, [], raw)


# ---------------------------------------------------------------------------
# One record, many records
# ---------------------------------------------------------------------------
def judge(fqdn, ip, ptr, a=None):
    """(existing_kind, existing text) from the PTR lookup and the A lookup
    (a=None when the generated names are not checked)."""
    fqdn_cf = fqdn.casefold().rstrip(".")
    # A clear conflict wins over everything else.
    if ptr.status == FOUND:
        if len(ptr.values) > 1:
            return "conflict", "multiple PTR: " + ", ".join(ptr.values)
        if ptr.values[0].casefold() != fqdn_cf:
            return "conflict", "PTR: " + ptr.values[0]
    if a is not None and a.status == FOUND and set(a.values) != {ip}:
        return "conflict", "name -> " + ", ".join(a.values)
    lookups = [ptr] + ([a] if a is not None else [])
    if any(l.status == NO_ANSWER for l in lookups):
        return "no answer", "no answer"
    if any(l.status == UNKNOWN for l in lookups):
        return "unknown", "unknown"
    ptr_ok = ptr.status == FOUND
    if a is None:
        return ("same", "same") if ptr_ok else ("none", "")
    a_ok = a.status == FOUND
    if ptr_ok and a_ok:
        return "same", "same"
    if a_ok:
        return "conflict", "A only (no PTR)"
    if ptr_ok:
        return "conflict", "PTR only (no A)"
    return "none", ""


def check_record(record, server="", forward=True, runner=run_nslookup, timeout=TIMEOUT):
    """Check one dns_names.DnsRecord and store the result on it."""
    ptr = lookup_ptr(record.ip, server, runner, timeout)
    a = lookup_a(record.fqdn, server, runner, timeout) if forward else None
    record.existing_kind, record.existing = judge(record.fqdn, record.ip, ptr, a)
    record.lookup_raw = ("--- nslookup -type=PTR " + record.ip + "\n" + ptr.raw.strip() +
                         ("\n\n--- nslookup -type=A " + record.fqdn + ".\n" + a.raw.strip()
                          if a is not None else ""))
    return record


def checkable(records):
    """Records the DNS check looks at: real records, not excluded."""
    return [r for r in records if r.is_record and not r.excluded and r.fqdn]


def check_records(records, server="", forward=True, runner=run_nslookup,
                  timeout=TIMEOUT, cancel=None, progress=None):
    """Check every checkable record, MAX_PARALLEL at a time.

    cancel:   threading.Event; when set, no new lookup is started.
    progress: function(done, total), called from worker threads.
    Returns the number of records checked. Raises ValueError for a bad server
    (before anything runs)."""
    server = valid_server(server)
    todo = checkable(records)
    for r in records:                      # forget the results of a previous check
        r.existing = r.existing_kind = r.lookup_raw = ""
    cancel = cancel or threading.Event()
    lock, done = threading.Lock(), [0]

    def work(record):
        if cancel.is_set():
            return
        try:
            check_record(record, server, forward, runner, timeout)
        except ValueError as error:        # a name that can't be looked up
            record.existing_kind, record.existing = "unknown", "unknown"
            record.lookup_raw = str(error)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], len(todo))

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        list(pool.map(work, todo))
    return done[0]


def main(argv=None):
    """python dns_check.py <ip> [fqdn] [--server X]: try one lookup by hand."""
    import argparse
    ap = argparse.ArgumentParser(description="Read-only DNS check of one record")
    ap.add_argument("ip")
    ap.add_argument("fqdn", nargs="?")
    ap.add_argument("--server", default="")
    args = ap.parse_args(argv)
    try:
        ptr = lookup_ptr(args.ip, args.server)
        a = lookup_a(args.fqdn, args.server) if args.fqdn else None
    except ValueError as error:
        print(error)
        return 1
    print("PTR:", ptr.status, ", ".join(ptr.values))
    if a is not None:
        print("A:  ", a.status, ", ".join(a.values))
        print("=>", *judge(args.fqdn, args.ip, ptr, a))
    return 0


if __name__ == "__main__":
    sys.exit(main())
