#!/usr/bin/env python3
"""Generate device configs from netgen_input.xlsx with the templates in templates/<os>/.

Usage:  python generate.py [netgen_input.xlsx] [output_dir]
Needs:  pip install openpyxl jinja2

Excel sheets (sheet names and headers: any case; columns: any order; extra
columns are ignored; the header row can be anywhere in the first 10 rows):
  Links    VRF | Subnet | DOT1Q | Vlan | DEVICE A | Port | IP | Device B | Port | IP
           one row per link. DEVICE A is the device configured, Device B its BGP peer.
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
           Without this sheet (or if it is empty): every DEVICE A, ios-xe, all blocks.

For each DEVICE A the rows are turned into this structure, then main.j2 is rendered:
  local_as, router_id (optional)
  vrfs:          [{name, vpn_id}]
  subinterfaces: [{name, dot1q, vrf, ip, mask}]
  bgp_vrfs:      [{vrf, neighbors: [{ip, remote_as, device_b}]}]
"""
import re
import sys
import ipaddress
from pathlib import Path

from openpyxl import load_workbook
from jinja2 import Environment, FileSystemLoader, StrictUndefined

HERE = Path(__file__).resolve().parent


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
    """Excel rows -> one data structure per DEVICE A. Returns (data, errors)."""
    errors, data = [], {}
    used_subifs = set()
    for r in links:
        where = f"Links row {r['_row']}"
        before = len(errors)
        host, peer, vrf = r["DEVICE A"], r["Device B"], r["VRF"]
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
        for dev in (host, peer):
            if dev not in devices:
                errors.append(f"{where}: device '{dev}' missing in Devices sheet")
        if vrf not in vpn_ids:
            errors.append(f"{where}: VRF '{vrf}' missing in VRFs sheet")
        if not r["DOT1Q"].isdigit() or not 1 <= int(r["DOT1Q"]) <= 4094:
            errors.append(f"{where}: DOT1Q '{r['DOT1Q']}' must be 1-4094")
        key = (host, r["Port_A"], r["DOT1Q"])
        if key in used_subifs:
            errors.append(f"{where}: {r['Port_A']}.{r['DOT1Q']} already used on {host}")
        used_subifs.add(key)
        if len(errors) > before:
            continue

        d = data.setdefault(host, {
            "local_as": devices[host]["as"],
            "vrfs": [], "subinterfaces": [], "bgp_vrfs": [],
        })
        if devices[host]["router_id"]:
            d["router_id"] = devices[host]["router_id"]

        if not any(v["name"] == vrf for v in d["vrfs"]):
            d["vrfs"].append({"name": vrf, "vpn_id": vpn_ids[vrf]})

        d["subinterfaces"].append({
            "name": r["Port_A"],
            "dot1q": r["DOT1Q"],
            "vrf": vrf,
            "ip": str(ip_a),
            "mask": str(net.netmask),
        })

        af = next((a for a in d["bgp_vrfs"] if a["vrf"] == vrf), None)
        if af is None:
            af = {"vrf": vrf, "neighbors": []}
            d["bgp_vrfs"].append(af)
        af["neighbors"].append({
            "ip": str(ip_b),
            "remote_as": devices[peer]["as"],
            "device_b": peer,
        })
    return data, errors


def main():
    xlsx = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "netgen_input.xlsx"
    outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "output"

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

    data, errors = build_devices(links, vpn_ids, devices)
    if errors:
        print("Fix these errors in the Excel file first:")
        print("\n".join(f"  - {e}" for e in errors))
        sys.exit(1)
    if not data:
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
        tpl_dir = HERE / "templates" / job["os"]
        if job["host"] not in data:
            problems.append(f"{job['host']}: no rows with this DEVICE A in the Links sheet")
        elif not (tpl_dir / "main.j2").exists():
            problems.append(f"{job['host']}: no templates for {job['os']} yet "
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

    envs = {}
    outdir.mkdir(exist_ok=True)
    for job in jobs:
        if job["os"] not in envs:
            envs[job["os"]] = Environment(
                loader=FileSystemLoader(HERE / "templates" / job["os"]), trim_blocks=True,
                keep_trailing_newline=True, undefined=StrictUndefined)
        d = data[job["host"]]
        path = outdir / f"{job['output']}.cfg"
        path.write_text(envs[job["os"]].get_template("main.j2").render(blocks=job["blocks"], **d))
        parts = [b for b, on in job["blocks"].items() if on]
        print(f"written {path}  [{job['os']}: {', '.join(parts)}]")

    skipped = [h for h in data if h not in {j["host"] for j in jobs}]
    if skipped:
        print(f"not generated (not selected in Generate sheet): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
