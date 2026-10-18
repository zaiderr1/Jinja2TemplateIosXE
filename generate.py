#!/usr/bin/env python3
"""Generate device configs from netgen_input.xlsx with the templates in templates/<os>/.

Usage:  python generate.py [netgen_input.xlsx] [output_dir] [--templates FOLDER]
        --templates FOLDER: use main.j2 from FOLDER for every device, instead of
        templates/<os>/ chosen from the OS column of the Generate sheet.
        With --templates (e.g. drafts made by conf2jinja.py), only what the Excel
        data provides is generated: loops over lists the Excel doesn't fill
        produce nothing, and lines using a value the Excel doesn't have are left
        out. Everything left out is listed. Add --strict to stop instead.
Needs:  pip install openpyxl jinja2

Excel sheets (sheet names and headers: any case; columns: any order; extra
columns are ignored; the header row can be anywhere in the first 10 rows):
  Links    VRF | Subnet | DOT1Q | Vlan | DEVICE A | Port | IP | Device B | Port | IP
           one row per link. Both ends get a config: DEVICE A (Port A, IP A, neighbor
           IP B) and Device B (Port B, IP B, neighbor IP A). Port B can stay empty
           for a Device B you don't generate.
           A plain "Port" / "IP" column belongs to the nearest DEVICE A / Device B
           column on its left. "Port A", "IP B"... are also accepted, anywhere.
  VRFs     VRF | VPN ID                   -> rd <local AS>:<VPN ID>
  Devices  Device | BGP AS [| Router ID]  -> local AS of DEVICE A, remote-as of Device B
                                             (Router ID is optional)
  Generate Device | Generate | OS | VRF | Interfaces | BGP | Output name   (optional sheet)
           which devices to generate, with which OS templates and which blocks.
           Generate / VRF / Interfaces / BGP: yes or no (empty = yes).
           OS: ios-xe (templates/ios-xe/). Empty = ios-xe.
           Output name: file name without .cfg (empty = hostname).
           Only devices listed with Generate = yes are produced.
           Without this sheet (or if it is empty): every device (A and B), ios-xe,
           all blocks.

For each device (A or B side) the rows are turned into this structure, then main.j2 is rendered:
  local_as, router_id (optional)
  vrfs:          [{name, vpn_id}]
  subinterfaces: [{name, dot1q, vrf, ip, mask}]
  bgp_vrfs:      [{vrf, vpn_id, neighbors: [{ip, remote_as, device_b, description}]}]
                 (device_b = the device at the other end of the link)
"""
import argparse
import re
import sys
import ipaddress
from pathlib import Path

from openpyxl import load_workbook
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, StrictUndefined
from jinja2.exceptions import TemplateError, UndefinedError

HERE = Path(__file__).resolve().parent
SKIP = "\x00SKIP\x00"      # marks a line that used a value the Excel doesn't provide


class SkipUndefined(ChainableUndefined):
    """Missing value: loops over it produce nothing, a line printing it is dropped.
    Every missing name is recorded in SkipUndefined.seen for the report."""
    seen = set()

    def _note(self):
        SkipUndefined.seen.add(self._undefined_name or "?")

    def __str__(self):
        self._note()
        return SKIP

    def __iter__(self):
        self._note()
        return iter(())


class BlockLoader(FileSystemLoader):
    """Loads templates from a folder; block files switched off in the Generate
    sheet (vrf.j2, interfaces.j2, bgp.j2) come back empty."""
    def __init__(self, folder, disabled):
        super().__init__(str(folder))
        self.disabled = {f"{b}.j2" for b in disabled}

    def get_source(self, environment, template):
        if template in self.disabled:
            return "", None, lambda: True
        return super().get_source(environment, template)


class ExcelError(Exception):
    pass


def norm(text):
    """'DEVICE A', 'Device_A', ' device-a ' -> 'device a'"""
    return re.sub(r"[\s_\-]+", " ", str(text or "").strip().lower())


# Accepted header spellings (after norm) -> internal name. Case, spaces, "_" and
# "-" don't matter. Column order doesn't matter either.
LINKS_COLS = {
    "VRF": {"vrf"},
    "Subnet": {"subnet"},
    "DOT1Q": {"dot1q"},
    "Vlan": {"vlan"},
    "DEVICE A": {"device a", "devicea"},
    "Device B": {"device b", "deviceb"},
    "Port_A": {"port a", "porta", "a port", "interface a"},
    "Port_B": {"port b", "portb", "b port", "interface b"},
    "IP_A": {"ip a", "ipa", "a ip"},
    "IP_B": {"ip b", "ipb", "b ip"},
}
LINKS_REQUIRED = ["VRF", "Subnet", "DOT1Q", "DEVICE A", "Device B", "Port_A", "IP_A", "IP_B"]
# Plain "Port" / "IP" columns belong to the nearest DEVICE A / Device B column on their left
PLAIN = {"port": "Port", "interface": "Port", "ip": "IP"}

VRFS_COLS = {"VRF": {"vrf"}, "VPN ID": {"vpn id", "vpnid"}}
GEN_COLS = {"Device": {"device", "hostname"}, "Generate": {"generate"},
            "OS": {"os", "os version", "platform"}, "VRF": {"vrf"},
            "Interfaces": {"interfaces", "interface"}, "BGP": {"bgp"},
            "Output name": {"output name", "output", "file name", "filename"}}
BLOCK_COLS = {"vrf": "VRF", "interfaces": "Interfaces", "bgp": "BGP"}
YES = {"", "yes", "y", "x", "oui", "o", "1", "true"}
NO = {"no", "n", "non", "0", "false"}
OS_NAMES = {"ios xe": "ios-xe", "iosxe": "ios-xe", "xe": "ios-xe",
            "ios xr": "ios-xr", "iosxr": "ios-xr", "xr": "ios-xr"}

DEVICES_COLS = {"Device": {"device", "hostname"}, "BGP AS": {"bgp as", "as", "asn", "bgp asn"},
                "Router ID": {"router id", "routerid"}}


def map_headers(raw, spec):
    """Header cells -> {column index: internal name}. Raises ExcelError on ambiguity."""
    mapping, side = {}, None
    lookup = {alias: key for key, aliases in spec.items() for alias in aliases}
    for col, cell in enumerate(raw):
        h = norm(cell)
        if not h:
            continue
        key = lookup.get(h)
        if key in ("DEVICE A", "Device B"):
            side = "A" if key == "DEVICE A" else "B"
        if key is None and h in PLAIN and "DEVICE A" in spec:
            if side is None:
                raise ExcelError(f"column '{cell}' comes before any DEVICE A / Device B "
                                 f"column, so it's unclear which side it belongs to. "
                                 f"Rename it '{PLAIN[h]} A' or '{PLAIN[h]} B'.")
            key = f"{PLAIN[h]}_{side}"
        if key is None:
            continue                      # extra columns are ignored
        if key in mapping.values():
            raise ExcelError(f"two columns mean '{key.replace('_', ' ')}' "
                             f"(second one: '{cell}'). Rename one of them.")
        mapping[col] = key
    return mapping


def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)                        # 2025.0 -> 2025
    return str(v).strip()


def read_sheet(wb, name, spec, required):
    """Find the sheet (any case), find its header row (in the first 10 rows),
    map the columns by name and return the rows as dicts."""
    ws = next((wb[s] for s in wb.sheetnames if norm(s) == norm(name)), None)
    if ws is None:
        raise ExcelError(f"sheet '{name}' not found (sheets: {', '.join(wb.sheetnames)})")
    rows = [list(r) for r in ws.iter_rows(min_row=1, min_col=1, values_only=True)]
    # Merged cells: Excel keeps the value only in the top-left cell of the block.
    # Copy it to every cell of the block (e.g. a Port merged over several links).
    for rng in ws.merged_cells.ranges:
        value = ws.cell(rng.min_row, rng.min_col).value
        for r in range(rng.min_row, min(rng.max_row, len(rows)) + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if c <= len(rows[r - 1]):
                    rows[r - 1][c - 1] = value
    best, best_found, header_error = None, -1, None
    for i, raw in enumerate(rows[:10]):
        try:
            mapping = map_headers(raw, spec)
        except ExcelError as e:
            header_error = header_error or e
            continue
        found = sum(1 for k in required if k in mapping.values())
        if found > best_found:
            best, best_found = (i, mapping), found
        if found == len(required):
            break
    if best is None or (best_found < len(required) and header_error):
        raise ExcelError(f"sheet '{ws.title}': {header_error or 'empty'}")
    header_row, mapping = best
    missing = [k.replace("_", " ") for k in required if k not in mapping.values()]
    if missing:
        raise ExcelError(f"sheet '{ws.title}': column(s) not found: {', '.join(missing)}. "
                         f"Headers seen: {[c for c in rows[header_row] if c]}")
    out = []
    for excel_row, raw in enumerate(rows[header_row + 1:], start=header_row + 2):
        if raw is None or all(v in (None, "") for v in raw):
            continue
        row = {key: "" for key in spec}
        row["_row"] = excel_row               # real Excel row number, for messages
        row.update({key: cell_text(raw[col]) for col, key in mapping.items() if col < len(raw)})
        out.append(row)
    return out


def yes_no(value, where, column):
    v = norm(value)
    if v in YES:
        return True
    if v in NO:
        return False
    raise ExcelError(f"{where}: {column} must be yes or no, not '{value}'")


def read_generate(wb, link_hosts):
    """Generate sheet -> [{host, os, blocks, output}]. Without the sheet (or with
    no rows): every DEVICE A, ios-xe, all blocks."""
    everything = {b: True for b in BLOCK_COLS}
    if not any(norm(s) == "generate" for s in wb.sheetnames):
        return [{"host": h, "os": "ios-xe", "blocks": everything, "output": h}
                for h in link_hosts], None
    rows = read_sheet(wb, "Generate", GEN_COLS, ["Device"])
    if not rows:
        return [{"host": h, "os": "ios-xe", "blocks": everything, "output": h}
                for h in link_hosts], "Generate sheet is empty: generating every device."
    jobs, seen = [], set()
    for r in rows:
        where = f"Generate row {r['_row']} ({r['Device']})"
        if not r["Device"]:
            continue
        if r["Device"] in seen:
            raise ExcelError(f"{where}: device listed twice")
        seen.add(r["Device"])
        if not yes_no(r["Generate"], where, "Generate"):
            continue
        os_name = OS_NAMES.get(norm(r["OS"]) or "ios xe")
        if os_name is None:
            raise ExcelError(f"{where}: OS must be ios-xe or ios-xr, not '{r['OS']}'")
        blocks = {b: yes_no(r[col], where, col) for b, col in BLOCK_COLS.items()}
        if not any(blocks.values()):
            raise ExcelError(f"{where}: VRF, Interfaces and BGP are all 'no'")
        jobs.append({"host": r["Device"], "os": os_name, "blocks": blocks,
                     "output": r["Output name"] or r["Device"]})
    return jobs, None


def build_devices(links, vpn_ids, devices):
    """Excel rows -> one data structure per device, for BOTH ends of each link.
    Returns (data, errors, incomplete). incomplete: devices that are Device B on a
    row with an empty Port B (their config can't be built) -> {device: [rows]}."""
    errors, data, incomplete = [], {}, {}
    used_subifs = {}

    def add_side(host, port, ip_local, ip_peer, peer, vrf, dot1q, net, where):
        key = (host, port, dot1q)
        if key in used_subifs:
            errors.append(f"{where}: {port}.{dot1q} already used on {host} "
                          f"({used_subifs[key]})")
            return
        used_subifs[key] = where
        d = data.setdefault(host, {
            "local_as": devices[host]["as"],
            "vrfs": [], "subinterfaces": [], "bgp_vrfs": [],
        })
        if devices[host]["router_id"]:
            d["router_id"] = devices[host]["router_id"]
        if not any(v["name"] == vrf for v in d["vrfs"]):
            d["vrfs"].append({"name": vrf, "vpn_id": vpn_ids[vrf]})
        d["subinterfaces"].append({
            "name": port,
            "dot1q": dot1q,
            "vrf": vrf,
            "ip": str(ip_local),
            "mask": str(net.netmask),
        })
        af = next((a for a in d["bgp_vrfs"] if a["vrf"] == vrf), None)
        if af is None:
            af = {"vrf": vrf, "vpn_id": vpn_ids[vrf], "neighbors": []}
            d["bgp_vrfs"].append(af)
        af["neighbors"].append({
            "ip": str(ip_peer),
            "remote_as": devices[peer]["as"],
            "device_b": peer,                       # = the device at the other end
            "peer": peer,
            "description": f"{peer}-(vrf {vrf})",   # your naming standard
        })

    for r in links:
        where = f"Links row {r['_row']}"
        before = len(errors)
        host_a, host_b, vrf, dot1q = r["DEVICE A"], r["Device B"], r["VRF"], r["DOT1Q"]
        empty = [k.replace("_", " ") for k in LINKS_REQUIRED if not r[k]]
        if empty:
            errors.append(f"{where}: empty cell(s): {', '.join(empty)}")
            continue
        try:
            net = ipaddress.ip_network(r["Subnet"], strict=True)
            ip_a = ipaddress.ip_address(r["IP_A"])
            ip_b = ipaddress.ip_address(r["IP_B"])
        except ValueError as e:
            errors.append(f"{where}: {e}")
            continue
        for label, ip in (("IP A", ip_a), ("IP B", ip_b)):
            if ip not in net:
                errors.append(f"{where}: {label} {ip} is not in {net}")
        if ip_a == ip_b:
            errors.append(f"{where}: IP A and IP B are both {ip_a}")
        if host_a == host_b:
            errors.append(f"{where}: DEVICE A and Device B are both {host_a}")
        for dev in (host_a, host_b):
            if dev not in devices:
                errors.append(f"{where}: device '{dev}' missing in Devices sheet")
        if vrf not in vpn_ids:
            errors.append(f"{where}: VRF '{vrf}' missing in VRFs sheet")
        if not dot1q.isdigit() or not 1 <= int(dot1q) <= 4094:
            errors.append(f"{where}: DOT1Q '{dot1q}' must be 1-4094")
        if len(errors) > before:
            continue

        add_side(host_a, r["Port_A"], ip_a, ip_b, host_b, vrf, dot1q, net, where)
        if r["Port_B"]:
            add_side(host_b, r["Port_B"], ip_b, ip_a, host_a, vrf, dot1q, net, where)
        else:
            incomplete.setdefault(host_b, []).append(r["_row"])

    # a device missing a Port B on any of its links gets no config at all
    # (a config with only some of its links would be misleading)
    for host in incomplete:
        data.pop(host, None)
    return data, errors, incomplete


def main():
    ap = argparse.ArgumentParser(description="Excel -> device configs")
    ap.add_argument("xlsx", nargs="?", default=str(HERE / "netgen_input.xlsx"))
    ap.add_argument("outdir", nargs="?", default=str(HERE / "output"))
    ap.add_argument("--templates", help="template folder containing main.j2 "
                    "(default: templates/<os>/ next to this script)")
    ap.add_argument("--strict", action="store_true",
                    help="with --templates: stop on a value the Excel doesn't provide "
                         "instead of leaving it out")
    args = ap.parse_args()
    xlsx, outdir = Path(args.xlsx), Path(args.outdir)
    if not xlsx.exists():
        print(f"Excel file not found: {xlsx.resolve()}")
        sys.exit(1)

    def template_dir(os_name):
        return Path(args.templates).resolve() if args.templates else HERE / "templates" / os_name

    wb = load_workbook(xlsx, data_only=True)
    try:
        links = read_sheet(wb, "Links", LINKS_COLS, LINKS_REQUIRED)
        vpn_ids = {r["VRF"]: r["VPN ID"]
                   for r in read_sheet(wb, "VRFs", VRFS_COLS, ["VRF", "VPN ID"])}
        devices = {r["Device"]: {"as": r["BGP AS"], "router_id": r["Router ID"]}
                   for r in read_sheet(wb, "Devices", DEVICES_COLS, ["Device", "BGP AS"])}
    except ExcelError as e:
        print(f"Excel problem: {e}")
        sys.exit(1)

    data, errors, incomplete = build_devices(links, vpn_ids, devices)
    if errors:
        print("Fix these errors in the Excel file first:")
        print("\n".join(f"  - {e}" for e in errors))
        sys.exit(1)
    if not data and not incomplete:
        print("No links found in the Links sheet.")
        sys.exit(1)

    try:
        jobs, note = read_generate(wb, list(data))
    except ExcelError as e:
        print(f"Excel problem: {e}")
        sys.exit(1)
    if note:
        print(note)

    problems, outputs = [], {}
    for job in jobs:
        tpl_dir = template_dir(job["os"])
        if job["host"] in incomplete:
            rows = ", ".join(str(n) for n in incomplete[job["host"]])
            problems.append(f"{job['host']}: is Device B with an empty Port B "
                            f"(Links row(s) {rows})")
        elif job["host"] not in data:
            problems.append(f"{job['host']}: not found as DEVICE A or Device B in the Links sheet")
        elif not (tpl_dir / "main.j2").exists():
            what = f"in {tpl_dir}" if args.templates else f"for {job['os']}"
            problems.append(f"{job['host']}: no main.j2 {what} "
                            f"(expected {tpl_dir / 'main.j2'})")
        if job["output"] in outputs:
            problems.append(f"{job['host']}: output name '{job['output']}' already used "
                            f"by {outputs[job['output']]}")
        outputs[job["output"]] = job["host"]
    if problems:
        print("Nothing generated. Fix these first:")
        print("\n".join(f"  - {p}" for p in problems))
        sys.exit(1)
    if not jobs:
        print("No device has Generate = yes in the Generate sheet.")
        sys.exit(1)

    lenient = bool(args.templates) and not args.strict
    if lenient:
        # lists split by structure in a draft (bgp_vrfs_2, subinterfaces_2...) can't
        # be filled from Excel: all rows go to the first list, with its structure
        tpl_dir = template_dir("")
        variants = sorted({m for f in tpl_dir.glob("*.j2")
                           for m in re.findall(r"{%-?\s*for\s+\w+\s+in\s+(?:\w+\.)?(\w+_\d+)\b",
                                               f.read_text(errors="replace"))})
        if variants:
            print(f"warning: these loops are variants and stay empty: {', '.join(variants)}")
            print("  Excel rows are rendered with the structure of the first loop "
                  "(e.g. bgp_vrfs, not bgp_vrfs_2).")
            print("  Check the result, or merge the variants in the draft.")

    failed = False
    outdir.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        tpl_dir = template_dir(job["os"])
        disabled = [b for b, on in job["blocks"].items() if not on]
        env = Environment(loader=BlockLoader(tpl_dir, disabled), trim_blocks=True,
                          keep_trailing_newline=True,
                          undefined=SkipUndefined if lenient else StrictUndefined)
        d = data[job["host"]]
        SkipUndefined.seen = set()
        try:
            text = env.get_template("main.j2").render(blocks=job["blocks"], **d)
            if lenient:
                text = "\n".join(l for l in text.split("\n") if SKIP not in l)
        except UndefinedError as e:
            print(f"{job['host']}: the templates in {tpl_dir} use a variable the Excel "
                  f"data doesn't provide: {e}")
            print("  Available: local_as, router_id (if filled), blocks.vrf/interfaces/bgp,")
            print("    vrfs[].name/vpn_id, subinterfaces[].name/dot1q/vrf/ip/mask,")
            print("    bgp_vrfs[].vrf/vpn_id, bgp_vrfs[].neighbors[].ip/remote_as/device_b/description")
            print("  Run without --strict to leave out what the Excel doesn't provide.")
            failed = True
            continue
        except TemplateError as e:
            print(f"{job['host']}: template error in {tpl_dir}: {e}")
            failed = True
            continue
        path = outdir / f"{job['output']}.cfg"
        path.write_text(text)
        parts = [b for b, on in job["blocks"].items() if on]
        src = tpl_dir if args.templates else job["os"]
        print(f"written {path}  [{src}: {', '.join(parts)}]")
        if lenient and SkipUndefined.seen:
            print(f"  left out (not in Excel): {', '.join(sorted(SkipUndefined.seen))}")

    selected = {j["host"] for j in jobs}
    skipped = [h for h in data if h not in selected]
    if skipped:
        print(f"not generated (not selected in Generate sheet): {', '.join(skipped)}")
    no_port = [h for h in incomplete if h not in selected]
    if no_port:
        print("not generated (Port B empty, so the Device B side can't be built): " +
              ", ".join(f"{h} (row {', '.join(map(str, incomplete[h]))})" for h in no_port))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
