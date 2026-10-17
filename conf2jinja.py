#!/usr/bin/env python3
"""
conf2jinja.py - turn a Cisco IOS / IOS-XE running-config into Jinja2 templates.

Every per-device value (hostname, ASNs, IPs, masks, dot1q, VLANs, VRFs, RDs,
route-targets, physical interfaces, descriptions, secrets) becomes a Jinja2 variable.

Output (default: one file per config block):
    <outdir>/<name>/
        main.j2           includes every block, in config order
        base.j2           hostname, services, AAA, users... (before the first block)
        vrf.j2            vrf definition / ip vrf
        vlans.j2          vlan N
        interfaces.j2     interface ...
        bgp.j2            router bgp
        igp.j2            router ospf / isis / eigrp / rip
        static_routes.j2  ip route / ipv6 route
        prefix_lists.j2   ip prefix-list
        bgp_lists.j2      ip community-list / extcommunity-list / as-path
        route_maps.j2     route-map
        acls.j2           access-lists
        services.j2       everything else after the first block (snmp, tacacs, line, banner...)
        vars.yml          the variables, shared by all blocks
Only the blocks present in the config are written.

With --single-file you get the old behaviour: <name>.j2 + <name>_vars.yml.

Rules worth knowing:
  - A sub-interface number and its "encapsulation dot1Q" always use the same
    variable ({{ dot1q_N }}). A warning is printed if a config has them different.
  - Each block is checked: rendered with vars.yml it must give back exactly the
    original lines of that block (round-trip check).

Usage:
    python conf2jinja.py running-config.txt
    python conf2jinja.py running-config.txt -o models -n CE_model --redact-secrets
    python conf2jinja.py running-config.txt --single-file

Needs: pip install jinja2   (only for the round-trip check)
"""
import argparse
import ipaddress
import re
import sys
from pathlib import Path

IP = r"\d{1,3}(?:\.\d{1,3}){3}"

# Lines that change at every "show run" and are not configuration
NOISE = [
    r"^Building configuration",
    r"^Current configuration\s*:",
    r"^! Last configuration change",
    r"^! NVRAM config last updated",
    r"^ntp clock-period",
]

# Top-level sections -> block file. First match wins.
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

# kind -> pool. Values are shared inside a pool: if 64681 is local_as, every
# later 64681 (rd, route-target, remote-as) re-uses {{ local_as }}.
POOL = {
    "hostname": "hostname", "local_as": "asn", "remote_as": "asn", "asn": "asn",
    "router_id": "ip", "neighbor_ip": "ip", "intf_ip": "ip", "ip": "ip",
    "prefix": "prefix", "mask": "mask", "dot1q": "dot1q", "vlan": "vlan",
    "vrf": "vrf", "vpn_id": "vpn_id", "rt": "rt", "intf": "intf",
    "description": "desc", "neighbor_desc": "desc", "secret": "secret",
}
SINGLE = {"hostname", "local_as", "router_id"}   # named without a number

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
    # sub-interface number and encapsulation dot1Q share the "dot1q" pool,
    # so <intf>.110 and "encapsulation dot1Q 110" are the same variable
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


class Vars:
    def __init__(self):
        self.by_value = {}   # (pool, value) -> var name
        self.values = {}     # var name -> value (insertion order = YAML order)
        self.context = {}    # var name -> first line it was seen on
        self.count = {}

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
        self.context[name] = line.strip()
        return name


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
    # 1) positional rules, rule by rule so priority decides the naming
    for rx, kinds in RULES:
        for line in lines:
            if not rx.search(line):
                continue
            for s, e, kind, value in rule_spans(line, keep_interfaces):
                if kind in kinds:
                    v.add(kind, value, line)
    # 2) every other IPv4 address / prefix
    for line in lines:
        for m in IP_TOKEN.finditer(line):
            ip, plen = m.group(1), m.group(2)
            if not is_ip(ip):
                continue
            if plen:
                v.add("prefix", ip + plen, line)
            elif not is_mask_like(ip):
                v.add("ip", ip, line)
    return v


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


def global_subst(segment, v):
    """Replace values that are safe to replace anywhere (IPs, prefixes, VRFs,
    hostname, interfaces, 4+ digit ASNs) inside a literal piece of text."""
    def ip_repl(m):
        ip, plen = m.group(1), m.group(2) or ""
        if plen and v.get("prefix", ip + plen):
            return "{{ %s }}" % v.get("prefix", ip + plen)
        name = v.get("ip", ip)
        return ("{{ %s }}" % name + plen) if name else m.group(0)

    out = IP_TOKEN.sub(ip_repl, segment)

    words = []
    for (pool, value), name in v.by_value.items():
        if pool in ("vrf", "hostname"):
            words.append((value, name, r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"))
        elif pool == "intf":
            words.append((value, name, r"(?<![\w/.])", r"(?![\w/])"))
        elif pool == "asn" and len(value) >= 4:
            # also inside names: RP-BGP64681OutBgp64510-xxx
            words.append((value, name, r"(?<![\d.])", r"(?![\d.])"))
    for value, name, pre, post in sorted(words, key=lambda w: -len(w[0])):
        out = re.sub(pre + re.escape(value) + post,
                     lambda _m, n=name: "{{ %s }}" % n, out)
    return out


def has_jinja(text):
    return any(t in text for t in ("{{", "{%", "{#"))


def protect(segment):
    """Literal text that already contains Jinja delimiters must stay raw."""
    return "{% raw %}" + segment + "{% endraw %}" if has_jinja(segment) else segment


def literal(segment, v):
    """Variabilize a literal piece of text, unless it holds Jinja delimiters."""
    return protect(segment) if has_jinja(segment) else global_subst(segment, v)


def templatize(lines, v, keep_interfaces):
    out, in_banner, delim = [], False, None
    for line in lines:
        # banners: keep untouched
        if in_banner:
            out.append(protect(line))
            if delim in line:
                in_banner = False
            continue
        d, cont = banner_delim(line)
        if d:
            delim, in_banner = d, cont
            out.append(protect(line))
            continue

        pos, parts = 0, []
        for s, e, kind, value in rule_spans(line, keep_interfaces):
            parts.append(literal(line[pos:s], v))
            parts.append("{{ %s }}" % v.get(kind, value))
            pos = e
        parts.append(literal(line[pos:], v))
        out.append("".join(parts))
    return out


def split_sections(lines):
    """Cut the config into top-level sections and give each one a block name.
    Returns [(block, [line indexes])]. 'end' is left out (main.j2 adds it)."""
    sections, i, n = [], 0, len(lines)
    seen_block = False
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
        # indented lines and "!" separators belong to this section
        while i < n and (lines[i].startswith(" ") or lines[i] == "!" or lines[i] == ""):
            idx.append(i)
            i += 1
        block = next((name for name, rx in BLOCKS if rx.match(line)), None)
        if block:
            seen_block = True
        else:
            block = "services" if seen_block else "base"
        sections.append((block, idx))
    return sections


def yaml_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_vars(path, v, source, redact):
    lines = [f"# Variables extracted from {source}", ""]
    width = max((len(n) for n in v.values), default=0)
    for name, value in v.values.items():
        ctx = v.context[name]
        if redact and name.startswith("secret_"):
            ctx = ctx.replace(value, "****")
            value = "CHANGE_ME"
        ctx = ctx[:70]
        lines.append(f"{name + ':':<{width + 1}} {yaml_str(value)}   # {ctx}")
    path.write_text("\n".join(lines) + "\n")


def first_diff(expected, got):
    a, b = expected.split("\n"), got.split("\n")
    for i, (x, y) in enumerate(zip(a, b), start=1):
        if x != y:
            return f"line {i}:\n    config:   {x}\n    rendered: {y}"
    return f"{len(a)} lines in config, {len(b)} rendered"


def jinja_env(folder=None):
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except ImportError:
        return None
    loader = FileSystemLoader(str(folder)) if folder else None
    return Environment(loader=loader, undefined=StrictUndefined)


def run_single(args, src, lines, v, tpl_lines):
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    name = args.name or src.stem
    tpl_path, vars_path = outdir / f"{name}.j2", outdir / f"{name}_vars.yml"
    template = "\n".join(tpl_lines)
    tpl_path.write_text(template + "\n")
    write_vars(vars_path, v, src.name, args.redact_secrets)
    print(f"template : {tpl_path}")
    print(f"variables: {vars_path}  ({len(v.values)} variables)")
    if args.redact_secrets:
        print("round-trip check: skipped (secrets redacted)")
        return True
    env = jinja_env()
    if env is None:
        print("round-trip check: skipped (pip install jinja2 to enable it)")
        return True
    rendered = env.from_string(template).render(**v.values)
    if rendered == "\n".join(lines):
        print("round-trip check: OK - template + vars reproduce the config exactly")
        return True
    print("round-trip check: MISMATCH at " + first_diff("\n".join(lines), rendered))
    return False


def run_blocks(args, src, lines, v, tpl_lines):
    folder = Path(args.outdir) / (args.name or src.stem)
    folder.mkdir(parents=True, exist_ok=True)
    sections = split_sections(lines)

    order, by_block = [], {}
    for block, idx in sections:
        if block not in by_block:
            order.append(block)
            by_block[block] = []
        by_block[block].extend(idx)

    for block in order:
        text = "\n".join(tpl_lines[i] for i in by_block[block])
        (folder / f"{block}.j2").write_text(text + "\n")
    main = "\n".join("{%% include '%s.j2' %%}" % b for b in order) + "\nend\n"
    (folder / "main.j2").write_text(main)
    write_vars(folder / "vars.yml", v, src.name, args.redact_secrets)

    print(f"folder   : {folder}")
    for block in order:
        print(f"  {block + '.j2':<18} {len(by_block[block]):>4} lines")
    print(f"  {'main.j2':<18} includes {len(order)} blocks")
    print(f"  {'vars.yml':<18} {len(v.values):>4} variables")

    if args.redact_secrets:
        print("round-trip check: skipped (secrets redacted)")
        return True
    env = jinja_env(folder)
    if env is None:
        print("round-trip check: skipped (pip install jinja2 to enable it)")
        return True

    ok = True
    for block in order:
        expected = "\n".join(lines[i] for i in by_block[block])
        got = env.get_template(f"{block}.j2").render(**v.values)
        if got != expected:
            print(f"round-trip check: MISMATCH in {block}.j2 at " + first_diff(expected, got))
            ok = False
    if not ok:
        return False

    full = env.get_template("main.j2").render(**v.values)
    original = "\n".join(lines)
    if full == original:
        print("round-trip check: OK - every block and main.j2 reproduce the config exactly")
    elif sorted(full.split("\n")) == sorted(original.split("\n")):
        print("round-trip check: OK - every block reproduces its part of the config exactly.")
        print("  note: main.j2 groups sections by block, so some sections come out in a")
        print("  different order than in the running-config (same lines, nothing lost).")
    else:
        print("round-trip check: MISMATCH in main.j2 - lines differ from the config")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Cisco running-config -> Jinja2 templates")
    ap.add_argument("config", help="running-config text file")
    ap.add_argument("-o", "--outdir", default=".", help="output folder (default: .)")
    ap.add_argument("-n", "--name", help="output name (default: config file name)")
    ap.add_argument("--single-file", action="store_true",
                    help="one .j2 file instead of one file per config block")
    ap.add_argument("--keep-interfaces", action="store_true",
                    help="keep physical interface names literal")
    ap.add_argument("--redact-secrets", action="store_true",
                    help="write CHANGE_ME instead of passwords/keys in the vars file")
    args = ap.parse_args()

    src = Path(args.config)
    lines = clean(src.read_text(errors="replace"))
    for w in check_dot1q(lines):
        print(f"warning: {w}")
    v = collect(lines, args.keep_interfaces)
    tpl_lines = templatize(lines, v, args.keep_interfaces)

    run = run_single if args.single_file else run_blocks
    if not run(args, src, lines, v, tpl_lines):
        sys.exit(1)


if __name__ == "__main__":
    main()
