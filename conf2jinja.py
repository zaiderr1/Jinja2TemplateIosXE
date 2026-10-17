#!/usr/bin/env python3
"""
conf2jinja.py - turn a Cisco IOS / IOS-XE running-config into a Jinja2 template.

Every per-device value (hostname, ASNs, IPs, masks, VLANs, VRFs, RDs, route-targets,
physical interfaces, descriptions, secrets) is replaced by a Jinja2 variable.
Two files are written:
    <name>.j2         the template
    <name>_vars.yml   the variables with the values found in this config
The script then renders the template with those values and checks that the result
is identical to the original config (round-trip check).

Usage:
    python conf2jinja.py running-config.txt
    python conf2jinja.py running-config.txt -o models --redact-secrets
    python conf2jinja.py running-config.txt --keep-interfaces

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

# kind -> pool. Values are shared inside a pool: if 64681 is local_as, every
# later 64681 (rd, route-target, remote-as) re-uses {{ local_as }}.
POOL = {
    "hostname": "hostname", "local_as": "asn", "remote_as": "asn", "asn": "asn",
    "router_id": "ip", "neighbor_ip": "ip", "intf_ip": "ip", "ip": "ip",
    "prefix": "prefix", "mask": "mask", "vlan": "vlan", "vrf": "vrf",
    "vpn_id": "vpn_id", "rt": "rt", "intf": "intf", "description": "desc",
    "neighbor_desc": "desc", "secret": "secret",
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
    (r"^interface (\S+/\S+?)\.(\d+)$", ["intf", "vlan"]),
    (r"^interface (\S+/\S+)$", ["intf"]),
    (r"encapsulation dot1q (\d+)", ["vlan"]),
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
    return lines


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


def protect(segment):
    """Literal text that already contains Jinja delimiters must stay raw."""
    if any(t in segment for t in ("{{", "{%", "{#")):
        return "{% raw %}" + segment + "{% endraw %}"
    return segment


def literal(segment, v):
    """Variabilize a literal piece of text, unless it holds Jinja delimiters."""
    if any(t in segment for t in ("{{", "{%", "{#")):
        return protect(segment)
    return global_subst(segment, v)


def templatize(lines, v, keep_interfaces):
    out, in_banner, delim = [], False, None
    for line in lines:
        # banners: keep untouched
        if in_banner:
            out.append(protect(line))
            if delim in line:
                in_banner = False
            continue
        bm = re.match(r"^banner \S+ (\S{1,2})", line)
        if bm:
            delim = bm.group(1)[:2] if bm.group(1).startswith("^") else bm.group(1)[0]
            in_banner = line.count(delim) < 2
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


def roundtrip(template, v, original):
    try:
        from jinja2 import Environment, StrictUndefined
    except ImportError:
        return "skipped (pip install jinja2 to enable it)"
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    rendered = env.from_string(template).render(**v.values)
    if rendered == original:
        return "OK - template + vars reproduce the config exactly"
    a, b = original.split("\n"), rendered.split("\n")
    for i, (x, y) in enumerate(zip(a, b), start=1):
        if x != y:
            return f"MISMATCH at line {i}:\n  config:   {x}\n  rendered: {y}"
    return f"MISMATCH: {len(a)} lines in config, {len(b)} rendered"


def main():
    ap = argparse.ArgumentParser(description="Cisco running-config -> Jinja2 template")
    ap.add_argument("config", help="running-config text file")
    ap.add_argument("-o", "--outdir", default=".", help="output folder (default: .)")
    ap.add_argument("-n", "--name", help="output base name (default: config file name)")
    ap.add_argument("--keep-interfaces", action="store_true",
                    help="keep physical interface names literal")
    ap.add_argument("--redact-secrets", action="store_true",
                    help="write CHANGE_ME instead of passwords/keys in the vars file")
    args = ap.parse_args()

    src = Path(args.config)
    lines = clean(src.read_text(errors="replace"))
    v = collect(lines, args.keep_interfaces)
    tpl_lines = templatize(lines, v, args.keep_interfaces)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    name = args.name or src.stem
    tpl_path, vars_path = outdir / f"{name}.j2", outdir / f"{name}_vars.yml"
    template = "\n".join(tpl_lines)
    tpl_path.write_text(template)
    write_vars(vars_path, v, src.name, args.redact_secrets)

    print(f"template : {tpl_path}")
    print(f"variables: {vars_path}  ({len(v.values)} variables)")
    if args.redact_secrets:
        print("round-trip check: skipped (secrets redacted)")
    else:
        result = roundtrip(template, v, "\n".join(lines))
        print(f"round-trip check: {result}")
        if result.startswith("MISMATCH"):
            sys.exit(1)


if __name__ == "__main__":
    main()
