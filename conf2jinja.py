#!/usr/bin/env python3
"""
conf2jinja.py - turn a Cisco IOS / IOS-XE running-config into Jinja2 templates
with loops.

By default only the main blocks are templated: VRFs, interfaces and BGP.
ACLs, prefix-lists, route-maps, communities, SNMP, AAA... are left out.
Choose other blocks with --blocks (see the list below, or "all").

Items that share the same structure become ONE loop over a list in vars.yml:
    vrf definition ...          -> {% for v in vrfs %}
    interface X/Y/Z.<dot1q>     -> {% for i in subinterfaces %}
    interface X/Y/Z             -> {% for i in interfaces %}
    address-family ipv4 vrf ... -> {% for af in bgp_vrfs %}
      neighbor ...              ->   {% for n in af.neighbors %}
If items of one kind have different structures (e.g. one neighbor has a
route-map in, another doesn't), you get one loop per structure
(subinterfaces, subinterfaces_2, ...).

Rules:
  - The sub-interface number and its "encapsulation dot1Q" are always the same
    field ({{ i.dot1q }}). A warning is printed if a config has them different.
  - hostname, local BGP AS and router-id stay global variables, used everywhere
    (route-map names become RP-BGP{{ local_as }}OutBgp{{ n.remote_as }}-{{ af.vrf }}).
  - Interface filtering lines (ip access-group, ipv6 traffic-filter,
    access-class) are dropped, since ACLs are not part of the common config.
    Use --keep-filters to keep them.
  - Each block is checked: rendered with vars.yml it must give back the original
    lines of that block (round-trip check).

Output:
    <outdir>/<name>/main.j2      includes the blocks
    <outdir>/<name>/<block>.j2   one file per block
    <outdir>/<name>/vars.yml     global variables + lists
  or with --single-file:
    <outdir>/<name>.j2 and <outdir>/<name>_vars.yml

Templates need trim_blocks=True (Ansible's template module does this by default).

Blocks: base vrf vlans interfaces bgp igp static_routes prefix_lists
        bgp_lists route_maps acls services

Usage:
    python conf2jinja.py running-config.txt
    python conf2jinja.py running-config.txt -o models -n CE_model --redact-secrets
    python conf2jinja.py running-config.txt --blocks vrf,interfaces,bgp,static_routes
    python conf2jinja.py running-config.txt --blocks all --single-file

Needs: pip install jinja2   (for the round-trip check; pyyaml optional)
"""
import argparse
import ipaddress
import re
import sys
from collections import Counter
from pathlib import Path

IP = r"\d{1,3}(?:\.\d{1,3}){3}"
DEFAULT_BLOCKS = ["vrf", "interfaces", "bgp"]

# Lines that change at every "show run" and are not configuration
NOISE = [
    r"^Building configuration",
    r"^Current configuration\s*:",
    r"^! Last configuration change",
    r"^! NVRAM config last updated",
    r"^ntp clock-period",
]

# Top-level sections -> block. First match wins. Unmatched sections are
# "base" before the first matched block and "services" after it.
BLOCKS = [
    ("vrf", r"^(?:vrf definition|ip vrf) "),
    ("vlans", r"^vlan \d"),
    ("interfaces", r"^interface "),
    ("bgp", r"^router bgp "),
    ("igp", r"^router (?:ospf|ospfv3|isis|eigrp|rip)\b"),
    ("static_routes", r"^(?:ip|ipv6) route "),
    ("prefix_lists", r"^(?:ip|ipv6) prefix-list "),
    ("bgp_lists", r"^ip (?:community-list|extcommunity-list|as-path) "),
    ("route_maps", r"^route-map "),
    ("acls", r"^(?:ip access-list|ipv6 access-list|access-list) "),
]
BLOCKS = [(name, re.compile(rx)) for name, rx in BLOCKS]
ALL_BLOCKS = ["base"] + [b for b, _ in BLOCKS] + ["services"]
FLAT_BLOCKS = {"base", "services", "igp", "bgp"}   # not looped at section level

# kind -> pool. Same value inside a pool = same variable.
POOL = {
    "hostname": "hostname", "local_as": "asn", "remote_as": "asn", "asn": "asn",
    "router_id": "ip", "neighbor_ip": "ip", "intf_ip": "ip", "ip": "ip",
    "prefix": "prefix", "mask": "mask", "dot1q": "dot1q", "vlan": "vlan",
    "vrf": "vrf", "vpn_id": "vpn_id", "rt": "rt", "intf": "intf",
    "description": "desc", "neighbor_desc": "desc", "secret": "secret",
}
SINGLE = {"hostname", "local_as", "router_id"}        # named without a number
GLOBAL_KINDS = {"hostname", "local_as", "router_id"}  # never a loop field

# field name used inside loop items
FIELD = {
    "intf": "intf", "dot1q": "dot1q", "vlan": "vlan", "description": "description",
    "neighbor_desc": "description", "vrf": "vrf", "intf_ip": "ip",
    "neighbor_ip": "ip", "ip": "ip", "mask": "mask", "remote_as": "remote_as",
    "asn": "asn", "vpn_id": "vpn_id", "rt": "rt", "prefix": "prefix",
    "secret": "secret",
}
LOOP_VAR = {"vrf": "v", "vlans": "vl", "interfaces": "i", "af": "af", "neighbor": "n"}

# Filtering lines dropped from the kept blocks (common config only).
# Kept with --keep-filters.
FILTER_LINES = [re.compile(rx) for rx in (
    r"^\s+ip access-group ",
    r"^\s+ipv6 traffic-filter ",
    r"^\s+access-class ",
)]

# Positional rules: (regex, kind of each capture group). Order = priority,
# so "router bgp" names the ASN local_as before the rd/route-target lines.
RULES = [
    (r"^hostname (\S+)$", ["hostname"]),
    (r"^router bgp (\d+)$", ["local_as"]),
    (rf"^\s*bgp router-id ({IP})$", ["router_id"]),
    (rf"neighbor ({IP}) remote-as (\d+)", ["neighbor_ip", "remote_as"]),
    (r"neighbor \S+ description (.+)$", ["neighbor_desc"]),
    (r"^\s+description (.+)$", ["description"]),
    (r"^(?:vrf definition|ip vrf) (\S+)$", ["vrf"]),
    (r"(?:ip )?vrf forwarding (\S+)", ["vrf"]),
    (r"address-family ipv4 vrf (\S+)", ["vrf"]),
    (r"^\s*rd (\d+):(\d+)$", ["asn", "vpn_id"]),
    (r"route-target (?:export|import|both) (\d+):(\d+)", ["asn", "rt"]),
    # sub-interface number and encapsulation dot1Q share the "dot1q" pool
    (r"^interface (\S+/\S+?)\.(\d+)$", ["intf", "dot1q"]),
    (r"^interface (\S+/\S+)$", ["intf"]),
    (r"(?i)encapsulation dot1q (\d+)", ["dot1q"]),
    (r"switchport access vlan (\d+)", ["vlan"]),
    (r"^vlan (\d+)$", ["vlan"]),
    (rf"ip address ({IP}) ({IP})", ["intf_ip", "mask"]),
    (rf"ip route (?:vrf \S+ )?{IP} ({IP})", ["mask"]),
    (rf"network {IP} mask ({IP})", ["mask"]),
    (r"(?:secret|password) (?:\d+ )?(\S+)$", ["secret"]),
    (r"\bkey (?:\d+ )?(\S+)$", ["secret"]),
    (r"snmp-server community (\S+)", ["secret"]),
]
RULES = [(re.compile(rx), kinds) for rx, kinds in RULES]

IP_TOKEN = re.compile(rf"(?<![\d.])({IP})(/\d{{1,2}})?(?![\d.])")
BANNER = re.compile(r"^banner \S+ (\S{1,2})")


# ----------------------------------------------------------------- helpers
def is_mask_like(value):
    """True for netmasks and wildcards (255.255.255.0, 0.0.0.255, 0.0.0.0...)."""
    try:
        ipaddress.ip_network(f"0.0.0.0/{value}")
        return True
    except ValueError:
        return False


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def has_jinja(text):
    return any(t in text for t in ("{{", "{%", "{#"))


def clean(text):
    noise = [re.compile(n) for n in NOISE]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [l.rstrip() for l in lines if not any(n.search(l) for n in noise)]
    while lines and lines[0] in ("", "!"):
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def banner_delim(line):
    """Delimiter of a 'banner xxx ^C' line and whether the banner continues."""
    m = BANNER.match(line)
    if not m:
        return None, False
    tok = m.group(1)
    delim = tok[:2] if tok.startswith("^") else tok[0]
    return delim, line.count(delim) < 2


def check_dot1q(lines):
    """Warn when a sub-interface number differs from its encapsulation dot1Q."""
    warnings, current = [], None
    for line in lines:
        m = re.match(r"^interface (\S+?)\.(\d+)$", line)
        if m:
            current = (line, m.group(2))
            continue
        if not line.startswith(" "):
            current = None
            continue
        e = re.search(r"(?i)encapsulation dot1q (\d+)", line)
        if current and e and e.group(1) != current[1]:
            warnings.append(f"{current[0]}: encapsulation dot1Q {e.group(1)} "
                            f"differs from sub-interface number {current[1]}")
    return warnings


# --------------------------------------------------------------- variables
class Vars:
    def __init__(self):
        self.by_value = {}   # (pool, value) -> var name
        self.values = {}     # var name -> value
        self.kind_of = {}    # var name -> kind
        self.context = {}    # var name -> first line it was seen on
        self.count = {}
        self._words = None

    def get(self, kind, value):
        return self.by_value.get((POOL[kind], value))

    def add(self, kind, value, line):
        key = (POOL[kind], value)
        if key in self.by_value:
            return self.by_value[key]
        if kind in SINGLE and kind not in self.values:
            name = kind
        else:
            self.count[kind] = self.count.get(kind, 0) + 1
            name = f"{kind}_{self.count[kind]}"
        self.by_value[key] = name
        self.values[name] = value
        self.kind_of[name] = kind
        self.context[name] = line.strip()
        return name

    def word_patterns(self):
        """Values safe to replace anywhere: VRFs, hostname, interfaces, 4+ digit ASNs."""
        if self._words is None:
            words = []
            for (pool, value), name in self.by_value.items():
                if pool in ("vrf", "hostname"):
                    pre, post = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
                elif pool == "intf":
                    pre, post = r"(?<![\w/.])", r"(?![\w/])"
                elif pool == "asn" and len(value) >= 4:
                    pre, post = r"(?<![\d.])", r"(?![\d.])"   # also RP-BGP64681Out...
                else:
                    continue
                words.append((re.compile(pre + re.escape(value) + post), name))
            self._words = words
        return self._words


def rule_spans(line, keep_interfaces):
    """Positions in the line to variabilize: list of (start, end, kind, value)."""
    spans = []
    for rx, kinds in RULES:
        m = rx.search(line)
        if not m:
            continue
        for i, kind in enumerate(kinds, start=1):
            value = m.group(i)
            if value is None:
                continue
            if kind == "intf" and keep_interfaces:
                continue
            if kind in ("neighbor_ip", "router_id", "intf_ip") and not is_ip(value):
                continue
            if kind == "mask" and value == "0.0.0.0":
                continue   # default route stays literal
            s, e = m.span(i)
            if any(s < pe and e > ps for ps, pe, _, _ in spans):
                continue   # an earlier (higher priority) rule already owns it
            spans.append((s, e, kind, value))
    return sorted(spans)


def collect(lines, keep_interfaces):
    v = Vars()
    for rx, kinds in RULES:            # rule by rule, so priority decides naming
        for line in lines:
            if not rx.search(line):
                continue
            for s, e, kind, value in rule_spans(line, keep_interfaces):
                if kind in kinds:
                    v.add(kind, value, line)
    for line in lines:                 # every other IPv4 address / prefix
        for m in IP_TOKEN.finditer(line):
            ip, plen = m.group(1), m.group(2)
            if not is_ip(ip):
                continue
            if plen:
                v.add("prefix", ip + plen, line)
            elif not is_mask_like(ip):
                v.add("ip", ip, line)
    return v


# ------------------------------------------------------------ tokenizing
# A line becomes a list of tokens: plain text (str), ("var", name) or ("raw", text)
def scan_literal(seg, v):
    if not seg:
        return []
    if has_jinja(seg):
        return [("raw", seg)]
    cands = []
    for m in IP_TOKEN.finditer(seg):
        ip, plen = m.group(1), m.group(2)
        if plen and v.get("prefix", ip + plen):
            cands.append((m.start(), m.end(), v.get("prefix", ip + plen)))
        elif v.get("ip", ip):
            cands.append((m.start(1), m.end(1), v.get("ip", ip)))
    for rx, name in v.word_patterns():
        for m in rx.finditer(seg):
            cands.append((m.start(), m.end(), name))
    cands.sort(key=lambda c: (c[0], c[0] - c[1]))
    out, pos = [], 0
    for s, e, name in cands:
        if s < pos:
            continue
        if s > pos:
            out.append(seg[pos:s])
        out.append(("var", name))
        pos = e
    if pos < len(seg):
        out.append(seg[pos:])
    return out


def tokenize_all(lines, v, keep_interfaces):
    result, in_banner, delim = [], False, None
    for line in lines:
        if in_banner:                         # banner body: untouched
            result.append([("raw", line)] if has_jinja(line) else [line])
            if delim in line:
                in_banner = False
            continue
        d, cont = banner_delim(line)
        if d:
            delim, in_banner = d, cont
            result.append([("raw", line)] if has_jinja(line) else [line])
            continue
        toks, pos = [], 0
        for s, e, kind, value in rule_spans(line, keep_interfaces):
            toks += scan_literal(line[pos:s], v)
            toks.append(("var", v.get(kind, value)))
            pos = e
        toks += scan_literal(line[pos:], v)
        result.append(toks)
    return result


def render_tokens(tokens, resolve):
    out = []
    for t in tokens:
        if isinstance(t, str):
            out.append(t)
        elif t[0] == "raw":
            out.append("{% raw %}" + t[1] + "{% endraw +%}")
        else:
            out.append("{{ " + resolve(t[1]) + " }}")
    return "".join(out)


# ------------------------------------------------------------- structure
def split_sections(lines):
    """Top-level sections with their block name: [(block, [line indexes])].
    A trailing 'end' is left out (main.j2 adds it)."""
    sections, i, n, seen_block = [], 0, len(lines), False
    while i < n:
        line = lines[i]
        if line == "end" and i == n - 1:
            break
        idx = [i]
        d, cont = banner_delim(line)
        i += 1
        if d and cont:                      # banner body until its delimiter
            while i < n:
                idx.append(i)
                i += 1
                if d in lines[idx[-1]]:
                    break
        while i < n and (lines[i].startswith(" ") or lines[i] in ("!", "")):
            idx.append(i)
            i += 1
        block = next((name for name, rx in BLOCKS if rx.match(line)), None)
        if block:
            seen_block = True
        else:
            block = "services" if seen_block else "base"
        sections.append((block, idx))
    return sections


class Unit:
    """One item that can be looped: a section, a BGP address-family, a neighbor.
    elements: ("line", raw, tokens) or ("run", kind, [Unit, ...])"""
    def __init__(self, kind, elements):
        self.kind = kind
        self.elements = elements
        self.header = next((e[1] for e in elements
                            if e[0] == "line" and e[1].strip() not in ("!", "")), "")


def add_child(elements, kind, unit):
    if elements and elements[-1][0] == "run" and elements[-1][1] == kind:
        elements[-1][2].append(unit)
    else:
        elements.append(("run", kind, [unit]))


def parse_children(entries, depth):
    """Inside router bgp (depth 1) or an address-family (depth 2): group
    address-families and per-neighbor lines into child units."""
    pad = " " * depth
    nb_rx = re.compile("^" + pad + r"neighbor (\S+) ")
    elements, i, n = [], 0, len(entries)
    while i < n:
        raw = entries[i][0]
        starts_af = depth == 1 and (
            raw.startswith(" address-family ")
            or (raw == " !" and i + 1 < n and entries[i + 1][0].startswith(" address-family ")))
        if starts_af:
            j = i
            while j < n and entries[j][0] != " exit-address-family":
                j += 1
            af = entries[i:j + 1]
            # header lines of the AF stay lines, its neighbors become children
            k = 1 if af[0][0] == " !" else 0
            af_elements = [("line", r, t) for r, t in af[:k + 1]]
            af_elements += parse_children(af[k + 1:], 2)
            add_child(elements, "af", Unit("af", af_elements))
            i = j + 1
            continue
        m = nb_rx.match(raw)
        if m:
            j = i
            while j < n and (nb_rx.match(entries[j][0]) or [None, None])[1] == m.group(1):
                j += 1
            add_child(elements, "neighbor",
                      Unit("neighbor", [("line", r, t) for r, t in entries[i:j]]))
            i = j
            continue
        elements.append(("line", raw, entries[i][1]))
        i += 1
    return elements


def make_unit(block, entries):
    if block == "bgp":
        return Unit("bgp", [("line",) + tuple(entries[0])] + parse_children(entries[1:], 1))
    return Unit(block, [("line", r, t) for r, t in entries])


def list_name(unit, depth_owner):
    k, h = unit.kind, unit.header
    if k == "interfaces":
        if re.match(r"^interface \S+\.\d+", h):
            return "subinterfaces"
        for prefix, name in (("Loopback", "loopbacks"), ("Vlan", "svis"),
                             ("Tunnel", "tunnels"), ("Port-channel", "port_channels")):
            if h.startswith("interface " + prefix):
                return name
        return "interfaces"
    if k == "af":
        return "bgp_vrfs" if "vrf" in h else "bgp_afs"
    if k == "neighbor":
        return "neighbors" if depth_owner else "bgp_neighbors"
    return {"vrf": "vrfs", "vlans": "vlans", "static_routes": "static_routes",
            "prefix_lists": "prefix_list_entries", "route_maps": "route_map_entries",
            "acls": "acls", "bgp_lists": "bgp_list_entries"}.get(k, k)


# ---------------------------------------------------------- template build
class Builder:
    def __init__(self, v, redact):
        self.v = v
        self.redact = redact
        self.globals = {}     # global var name -> value (in order of use)
        self.lists = {}       # top-level lists

    def value(self, name):
        if self.redact and self.v.kind_of[name] == "secret":
            return "CHANGE_ME"
        return self.v.values[name]

    def unit(self, unit, scope, loop_var):
        """Template lines + data dict for one unit. loop_var None = not looped."""
        slots, data, lines = {}, {}, []
        for pos, el in enumerate(unit.elements):
            if el[0] == "line":
                def resolve(name, in_header=(pos == 0)):
                    if name in scope:
                        return scope[name]
                    kind = self.v.kind_of[name]
                    if loop_var is None or kind in GLOBAL_KINDS:
                        self.globals.setdefault(name, self.value(name))
                        return name
                    if name not in slots:
                        field = FIELD[kind]
                        if in_header and unit.kind in ("vrf", "interfaces") \
                                and kind in ("vrf", "intf") and "name" not in data:
                            field = "name"
                        base, n = field, 2
                        while field in data:
                            field, n = f"{base}_{n}", n + 1
                        slots[name] = field
                        data[field] = self.value(name)
                    return f"{loop_var}.{slots[name]}"
                lines.append(render_tokens(el[2], resolve))
            else:
                child_scope = dict(scope)
                if loop_var:
                    child_scope.update({n: f"{loop_var}.{f}" for n, f in slots.items()})
                container = data if loop_var else self.lists
                owner = f"{loop_var}." if loop_var else ""
                lines += self.run(el[2], child_scope, owner, container)
        return lines, data

    def run(self, units, scope, owner, container):
        """Group units by structure; each group becomes one loop."""
        groups = {}
        for u in units:
            lv = LOOP_VAR.get(u.kind, "e")
            tl, data = self.unit(u, scope, lv)
            groups.setdefault("\n".join(tl), [tl, [], u])[1].append(data)
        out = []
        for tl, datas, u0 in groups.values():
            if not any(datas):            # identical items with nothing variable
                for _ in datas:
                    out += tl
                continue
            base = list_name(u0, owner)
            name, n = base, 2
            while name in container:
                name, n = f"{base}_{n}", n + 1
            container[name] = datas
            out.append("{%% for %s in %s%s %%}" % (LOOP_VAR.get(u0.kind, "e"), owner, name))
            out += tl
            out.append("{% endfor %}")
        return out

    def block(self, block, units):
        if block in FLAT_BLOCKS:
            lines = []
            for u in units:
                lines += self.unit(u, {}, None)[0]
            return lines
        return self.run(units, {}, "", self.lists)


# ------------------------------------------------------------------ output
def yaml_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_items(obj, indent):
    lines, pad = [], " " * indent
    for key, val in obj.items():
        if isinstance(val, list):
            lines.append(f"{pad}{key}:")
            for item in val:
                sub = yaml_items(item, indent + 4)
                sub[0] = " " * (indent + 2) + "- " + sub[0].lstrip()
                lines += sub
        else:
            lines.append(f"{pad}{key}: {yaml_str(val)}")
    return lines


def write_vars(path, b, source):
    out = [f"# Variables extracted from {source}", ""]
    if b.globals:
        width = max(len(n) for n in b.globals)
        for name, value in b.globals.items():
            ctx = b.v.context[name]
            if b.redact and b.v.kind_of[name] == "secret":
                ctx = ctx.replace(b.v.values[name], "****")
            out.append(f"{name + ':':<{width + 1}} {yaml_str(value)}   # {ctx[:70]}")
        out.append("")
    for key, val in b.lists.items():
        out += yaml_items({key: val}, 0)
        out.append("")
    path.write_text("\n".join(out))


def load_vars(path, b):
    """Prefer re-reading vars.yml (checks the YAML too); fall back to memory."""
    try:
        import yaml
        return yaml.safe_load(path.read_text())
    except ImportError:
        return {**b.globals, **b.lists}


def compare(expected, got):
    """'exact', 'regrouped' (same lines, other order) or a diff message."""
    if got == expected:
        return "exact"
    e, g = Counter(expected.split("\n")), Counter(got.split("\n"))
    if e == g:
        return "regrouped"
    missing = list((e - g).elements())[:3]
    extra = list((g - e).elements())[:3]
    return f"missing {missing} / unexpected {extra}"


def main():
    ap = argparse.ArgumentParser(description="Cisco running-config -> Jinja2 templates with loops")
    ap.add_argument("config", help="running-config text file")
    ap.add_argument("-o", "--outdir", default=".", help="output folder (default: .)")
    ap.add_argument("-n", "--name", help="output name (default: config file name)")
    ap.add_argument("--blocks", default=",".join(DEFAULT_BLOCKS),
                    help=f"blocks to template, comma separated, or 'all' "
                         f"(default: {','.join(DEFAULT_BLOCKS)})")
    ap.add_argument("--single-file", action="store_true",
                    help="one .j2 file instead of one file per block")
    ap.add_argument("--keep-filters", action="store_true",
                    help="keep interface ACL filters (ip access-group...)")
    ap.add_argument("--keep-interfaces", action="store_true",
                    help="keep physical interface names literal")
    ap.add_argument("--redact-secrets", action="store_true",
                    help="write CHANGE_ME instead of passwords/keys in the vars file")
    args = ap.parse_args()

    wanted = ALL_BLOCKS if args.blocks == "all" else [b.strip() for b in args.blocks.split(",")]
    unknown = [b for b in wanted if b not in ALL_BLOCKS]
    if unknown:
        ap.error(f"unknown block(s) {unknown}. Valid: {', '.join(ALL_BLOCKS)}")

    src = Path(args.config)
    lines = clean(src.read_text(errors="replace"))
    for w in check_dot1q(lines):
        print(f"warning: {w}")

    sections = split_sections(lines)
    dropped = 0
    if not args.keep_filters:
        filtered = []
        for blk, idx in sections:
            keep = [i for i in idx if not (blk in wanted and
                                           any(rx.match(lines[i]) for rx in FILTER_LINES))]
            dropped += len(idx) - len(keep)
            filtered.append((blk, keep))
        sections = filtered
    kept_idx = [i for blk, idx in sections if blk in wanted for i in idx]
    kept_lines = [lines[i] for i in kept_idx]
    v = collect(kept_lines, args.keep_interfaces)          # only from kept blocks
    tokens = dict(zip(kept_idx, tokenize_all(kept_lines, v, args.keep_interfaces)))

    order, units, skipped = [], {}, Counter()
    for blk, idx in sections:
        if blk not in wanted:
            skipped[blk] += len(idx)
            continue
        if blk not in units:
            order.append(blk)
            units[blk] = []
        units[blk].append(make_unit(blk, [(lines[i], tokens[i]) for i in idx]))

    b = Builder(v, args.redact_secrets)
    templates = {blk: b.block(blk, units[blk]) for blk in order}
    expected = {blk: "\n".join(lines[i] for _, idx in sections if _ == blk for i in idx)
                for blk in order}

    if args.single_file:
        folder = Path(args.outdir)
        name = args.name or src.stem
        files = {f"{name}.j2": "\n".join(l for blk in order for l in templates[blk]) + "\nend\n"}
        vars_path = folder / f"{name}_vars.yml"
    else:
        folder = Path(args.outdir) / (args.name or src.stem)
        files = {f"{blk}.j2": "\n".join(templates[blk]) + "\n" for blk in order}
        files["main.j2"] = "".join("{%% include '%s.j2' %%}\n" % blk for blk in order) + "end\n"
        vars_path = folder / "vars.yml"
    folder.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (folder / fname).write_text(text)
    write_vars(vars_path, b, src.name)

    print(f"output   : {folder}")
    for fname in files:
        print(f"  {fname}")
    print(f"  {vars_path.name:<22} {len(b.globals)} globals, lists: "
          f"{', '.join(f'{k} ({len(x)})' for k, x in b.lists.items()) or 'none'}")
    if dropped:
        print(f"dropped  : {dropped} interface filter line(s) (use --keep-filters to keep them)")
    if skipped:
        print("skipped  : " + ", ".join(f"{k} ({n} lines)" for k, n in skipped.items()))

    if args.redact_secrets:
        print("round-trip check: skipped (secrets redacted)")
        return
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError:
        print("round-trip check: skipped (pip install jinja2 to enable it)")
        return
    env = Environment(loader=FileSystemLoader(str(folder)), trim_blocks=True,
                      keep_trailing_newline=True, undefined=StrictUndefined)
    data = load_vars(vars_path, b)
    results = {}
    for blk in order:
        tpl = env.from_string("\n".join(templates[blk]) + "\n")
        results[blk] = compare(expected[blk] + "\n", tpl.render(**data))
    bad = {k: r for k, r in results.items() if r not in ("exact", "regrouped")}
    if bad:
        for k, r in bad.items():
            print(f"round-trip check: MISMATCH in {k}: {r}")
        sys.exit(1)
    regrouped = [k for k, r in results.items() if r == "regrouped"]
    print("round-trip check: OK - every block reproduces its config lines")
    if regrouped:
        print(f"  note: in {', '.join(regrouped)} items are grouped by loop, so they come")
        print("  out in a different order than in the running-config (same lines).")


if __name__ == "__main__":
    main()
