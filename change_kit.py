#!/usr/bin/env python3
"""
change_kit.py - everything a change request needs, for each device generated
from the Excel workbook.

For every device selected in the Generate sheet:

    <device>.cfg            the configuration (same as generate.py)
    <device>_rollback.cfg   its rollback (rollback.py)
    <device>_checks.txt     the verification commands, with the real names
                            (templates/<os>/checks.j2), e.g. for a NetBrain runbook,
                            starting with the dependencies (dependencies.py)
and for all devices together:
    dns_records.csv         the DNS names of the new sub-interfaces (dns_names.py)
    dns_records.ps1         the same as Windows DNS commands, to review and run

    python change_kit.py netgen_input.xlsx [output_folder] [options]

    --running FOLDER   running-configs saved before the change (one text file
                       per device, matched by their "hostname" line). With it,
                       the rollback is exact: old values are put back, and a
                       VRF or neighbor that already existed is never removed,
                       and the dependencies of the config (route-maps, parent
                       interfaces...) missing from it are reported.
                       Without it, the rollback only removes what is clearly
                       specific to the change (sub-interfaces, BGP neighbors)
                       and suggests the rest as "! REVIEW" comments.
    --domain NAME      DNS domain (default: example.intra)
    --templates DIR    same as generate.py (checks.j2 is taken from the
                       standard templates when DIR has none)
    --force            replace existing files without asking

Standard library plus openpyxl and jinja2, like generate.py. Nothing is written
except the files above, in the folder you choose.
"""
import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

import dependencies
import dns_names
import generate
import rollback as rollback_engine

HERE = Path(__file__).resolve().parent
HOSTNAME = re.compile(r"^hostname\s+(\S+)\s*$", re.M | re.I)


@dataclass
class KitEntry:
    """The change kit of one device."""
    host: str
    output: str                       # file name without extension
    os: str
    config: str
    rollback: Optional[rollback_engine.RollbackResult] = None
    rollback_error: str = ""
    checks: str = ""
    checks_error: str = ""
    running_source: str = ""          # running-config used for the rollback ("" = none)
    missing: List[dict] = field(default_factory=list)   # dependencies not in it
    dns: List[dns_names.DnsRecord] = field(default_factory=list)

    @property
    def rollback_text(self):
        if self.rollback is not None:
            return self.rollback.text
        return "! Rollback could not be built: " + self.rollback_error + "\n"

    @property
    def files(self):
        """{file name: content} for this device."""
        return {f"{self.output}.cfg": self.config,
                f"{self.output}_rollback.cfg": self.rollback_text,
                f"{self.output}_checks.txt": self.checks or
                ("! Checks could not be built: " + self.checks_error + "\n")}


# =============================================================================
# Running-configs taken before the change
# =============================================================================

def load_running_configs(folder) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """Read every text file of `folder` and index them by hostname.

    Returns ({hostname (case-insensitive): (file name, text)}, problems).
    A file without a hostname line is indexed by its file name without
    extension. Two files for the same device are reported and both ignored.
    """
    found, problems, dup = {}, [], set()
    for path in dns_names.find_text_files(folder):
        try:
            text = dns_names.read_text_file(path)
        except OSError as e:
            problems.append(f"{path.name}: cannot read ({e})")
            continue
        m = HOSTNAME.search(text)
        name = m.group(1) if m else path.stem
        key = name.casefold()
        if key in found:
            dup.add(key)
            problems.append(f"{path.name}: another file is also for {name}; "
                            "both are ignored")
        found[key] = (path.name, text)
    for key in dup:
        found.pop(key, None)
    return found, problems


# =============================================================================
# Building the kit
# =============================================================================

def checks_env(os_name, templates=None):
    """Template environment for checks.j2: the custom folder if it has one,
    else the standard templates/<os>/."""
    folders = []
    if templates:
        folders.append(str(Path(templates).resolve()))
    folders.append(str(HERE / "templates" / os_name))
    return Environment(loader=FileSystemLoader(folders), trim_blocks=True,
                       keep_trailing_newline=True, undefined=StrictUndefined)


def render_checks(config, templates=None):
    env = checks_env(config["os"], templates)
    return env.get_template("checks.j2").render(
        host=config["host"], blocks=config["blocks"],
        dependencies=config.get("dependencies", []), **config["data"])


def dns_records_for(config, settings):
    """DNS records of the routed interfaces in this device's new configuration.
    Uses dns_names.py, so names follow exactly the same rules as the DNS tab."""
    text = f"hostname {config['host']}\n" + config["text"]
    parsed = dns_names.parse_config(text, config["output"] + ".cfg")
    return dns_names.build_records([parsed], settings).records


def build_kit(res, running=None, dns_settings=None, templates=None):
    """Change kit of every configuration of a generate.Result.

    running:   {hostname casefold: (file name, text)} from load_running_configs,
               or None when no running-configs were given.
    """
    settings = dns_settings or dns_names.DnsSettings()
    entries = []
    for c in res.configs:
        e = KitEntry(c["host"], c["output"], c["os"], c["text"])
        before = (running or {}).get(c["host"].casefold())
        if before:
            e.running_source = before[0]
            e.missing = dependencies.missing(c.get("dependencies", []), before[1])
        try:
            e.rollback = rollback_engine.build_rollback(
                before[1] if before else None, c["text"],
                before_name=before[0] if before else "running-config",
                change_name=c["output"] + ".cfg", platform=c["os"])
        except rollback_engine.FormatError as err:
            e.rollback_error = str(err)
        try:
            e.checks = render_checks(c, templates)
        except Exception as err:        # template error: shown, not fatal
            e.checks_error = f"{type(err).__name__}: {err}"
        e.dns = dns_records_for(c, settings) if c["blocks"].get("interfaces") else []
        entries.append(e)
    # Duplicates are judged across all devices of the kit.
    all_records = [r for e in entries for r in e.dns]
    for r in all_records:
        for w in (dns_names.STATUS_DUP_NAME, dns_names.STATUS_DUP_IP):
            if w in r.warnings:
                r.warnings.remove(w)
    dns_names.mark_duplicates(all_records)
    return entries


def kit_files(entries, domain):
    """{file name: content} for the whole kit (devices + DNS files)."""
    files = {}
    for e in entries:
        files.update(e.files)
    records = [r for e in entries for r in e.dns]
    if records:
        files["dns_records.csv"] = dns_names.to_csv_bytes(records)
        files["dns_records.ps1"] = dns_names.to_powershell(records, domain)
    return files


def plan(folder, entries, domain):
    """(all paths, paths that already exist) - to ask before replacing."""
    paths = [Path(folder) / name for name in kit_files(entries, domain)]
    return paths, [p for p in paths if p.exists()]


def write_kit(folder, entries, domain, overwrite=False):
    """Write the kit; returns the paths written. Existing files are kept unless
    overwrite=True."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in kit_files(entries, domain).items():
        path = folder / name
        if path.exists() and not overwrite:
            continue
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def summary(entries):
    """One line per device, for the command line and the GUI."""
    lines = []
    for e in entries:
        rb = ("rollback from " + e.running_source) if e.running_source \
            else "rollback WITHOUT running-config"
        if e.rollback is None:
            rb = "rollback FAILED: " + e.rollback_error
        elif e.rollback.reviews:
            rb += f", {len(e.rollback.reviews)} point(s) to review"
        dns = f"{sum(r.is_record for r in e.dns)} DNS record(s)"
        lines.append(f"{e.host}: {rb}; {dns}" + ("; checks FAILED" if e.checks_error else ""))
        lines += [f"  MISSING in {e.running_source}: {m['kind']} {m['name']}"
                  for m in e.missing]
    return lines


def missing_lines(entries):
    """One line per dependency the device's running-config doesn't have."""
    return [f"{e.host}: {m['kind']} {m['name']} is not in {e.running_source} "
            f"(used by {dependencies.users_text(m)})" for e in entries for m in e.missing]


# =============================================================================
# Command line
# =============================================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description="Excel -> configs, rollbacks, checks and "
                                             "DNS records for a change request")
    ap.add_argument("xlsx", nargs="?", default=str(HERE / "netgen_input.xlsx"))
    ap.add_argument("outdir", nargs="?", default=str(HERE / "output"))
    ap.add_argument("--running", help="folder of running-configs taken before the change")
    ap.add_argument("--domain", default="example.intra", help="DNS domain")
    ap.add_argument("--templates", help="template folder (as in generate.py)")
    ap.add_argument("--force", action="store_true", help="replace existing files")
    args = ap.parse_args(argv)

    from openpyxl import load_workbook
    if not Path(args.xlsx).exists():
        print(f"Excel file not found: {Path(args.xlsx).resolve()}")
        return 1
    res = generate.generate_configs(load_workbook(args.xlsx, data_only=True),
                                    templates=args.templates)
    for note in res.notes:
        print(f"note: {note}")
    if res.errors:
        print(res.error_title)
        print("\n".join(f"  - {e}" for e in res.errors))
        return 1

    running = None
    if args.running:
        running, problems = load_running_configs(args.running)
        for p in problems:
            print(f"note: {p}")
    entries = build_kit(res, running, dns_names.DnsSettings(domain=args.domain),
                        args.templates)
    _, existing = plan(args.outdir, entries, args.domain)
    if existing and not args.force:
        print("These files already exist (use --force to replace them):")
        print("\n".join(f"  - {p}" for p in existing))
        return 1
    written = write_kit(args.outdir, entries, args.domain, overwrite=True)
    print("\n".join(summary(entries)))
    print(f"{len(written)} file(s) written in {args.outdir}")
    for f in res.failed:
        print("\n".join(f["lines"]))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
