#!/usr/bin/env python3
"""
rollback.py - build the rollback of a Cisco change from the running-config
taken before it.

Why
---
A change is undone by undoing each modification, not by reloading the device:
old values must be put back, added lines removed, deleted lines restored, each
in the right section and in the right order. This script works that out by
comparing the change with the running-config saved before the change.

    python rollback.py running-before.txt change.cfg            (prints the rollback)
    python rollback.py running-before.txt change.cfg -o rb.cfg  (writes it to a file)

What it does, line by line of the change
----------------------------------------
    the change...                         the running-config had...      rollback
    adds a section (interface X.100)      nothing                        no interface X.100
    changes a value (description NEW)     description OLD                description OLD
    adds a line (ip address ... sec.)     nothing                        no ip address ... secondary
    shutdown                              no shutdown (no "shutdown")    no shutdown
    no <something>                        <something> (+ its lines)      <something> put back
    an identical line                     the same line                  nothing

Rules that keep the rollback safe:
  - Sections are entered properly (router bgp -> address-family ipv4 vrf X).
  - Sections are undone in reverse order: BGP before interfaces before VRFs.
  - A new BGP neighbor is removed with one "no neighbor <ip>" in the section
    where its remote-as is, instead of one line per setting.
  - Anything it cannot reverse with certainty becomes a "! REVIEW:" comment,
    never a guessed command. It never writes an active "no router ...".
  - Without the running-config (generated change kits, see change_kit.py),
    VRFs, routing processes and address-families are assumed to exist already:
    only what is clearly specific to the change (sub-interfaces, neighbors) is
    removed, the rest is suggested as REVIEW comments.

Input format: both files as in "show running-config", indented. A change file
typed without indentation is refused (its hierarchy can't be known). Commands
must be written in full, as in the running-config ("interface GigabitEthernet1",
not "int gi1").

IOS-XE and IOS-XR. Standard library only.
"""
import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Lines that are not configuration (or are only section terminators).
NOISE = re.compile(
    r"^(Building configuration|Current configuration|! Last configuration change"
    r"|! NVRAM config last updated|end$|exit$|exit-[\w-]+$|commit\b)", re.I)
BANNER = re.compile(r"^banner\s+\S+\s+(\S{1,2})")

# Lines that open a section even when nothing is indented below them.
# ("neighbor" is a section only on IOS-XR, where it has lines below it.)
SECTION_START = re.compile(
    r"^(interface|router|vrf|ip vrf|address-family|route-map|ip access-list"
    r"|ipv6 access-list|class-map|policy-map|line|route-policy|prefix-set"
    r"|community-set|extcommunity-set|as-path-set|key chain|controller|template"
    r"|track|ip sla|object-group|monitor session)\b", re.I)

# Interfaces that can be deleted with "no interface". Others are physical.
REMOVABLE_INTERFACE = re.compile(
    r"^interface\s+(\S+\.\d+|Loopback\S*|Tunnel\S*|Vlan\S*|Port-channel\S*"
    r"|Bundle-Ether\S*|BDI\S*|BVI\S*)$", re.I)
# Abbreviated interface names in a change file ("interface Gi1/0/1").
ABBREVIATED_INTERFACE = re.compile(
    r"^interface\s+(Gi|Te|Fa|Twe|Tw|Fo|Hu|Lo|Po|Tu|Vl|BE)\d", re.I)

# Global (GRT) IPv4 address-family under "router bgp": shared by everything.
GLOBAL_AF = re.compile(r"^address-family ipv4(\s+unicast)?\s*$", re.I)

# Containers assumed to exist when no running-config is given (change kits).
ASSUMED_CONTAINER = re.compile(r"^(router|vrf|ip vrf|address-family)\b", re.I)

# Settings that hold one value per section: a new value replaces the old one,
# so the rollback puts the old value back. Each entry: regex -> key template.
SINGLE_VALUE = [
    (r"^description\b", "description"),
    (r"^ip address \S+ \S+$", "ip address"),
    (r"^ip address \S+/\d+$", "ip address"),
    (r"^ipv4 address \S+ \S+$", "ipv4 address"),
    (r"^ipv4 address \S+/\d+$", "ipv4 address"),
    (r"^encapsulation dot1q\b", "encapsulation dot1q"),
    (r"^(ip )?vrf forwarding\b", "vrf forwarding"),
    (r"^(mtu|ip mtu|ipv4 mtu|bandwidth|speed|duplex|load-interval|hostname|rd"
     r"|bgp router-id|remote-as|update-source|ip ospf cost)\b", r"\1"),
    (r"^service-policy (input|output)\b", r"service-policy \1"),
    (r"^neighbor (\S+) (remote-as|description|update-source|password|timers"
     r"|ebgp-multihop|local-as|peer-group|version|ttl-security)\b", r"neighbor \1 \2"),
    (r"^neighbor (\S+) (route-map|prefix-list|filter-list) \S+ (in|out)$",
     r"neighbor \1 \2 \3"),
    (r"^neighbor (\S+) maximum-prefix\b", r"neighbor \1 maximum-prefix"),
    (r"^route-policy \S+ (in|out)$", r"route-policy \1"),
    (r"^redistribute (\S+)", r"redistribute \1"),
    (r"^ip prefix-list (\S+) seq (\d+)\b", r"ip prefix-list \1 seq \2"),
    (r"^(\d+) (permit|deny|remark)\b", r"seq \1"),
    (r"^match (ip address|ipv6 address|community|as-path|tag|interface)\b", r"match \1"),
    (r"^set (\S+)", r"set \1"),
]
SINGLE_VALUE = [(re.compile(rx, re.I), key) for rx, key in SINGLE_VALUE]


# =============================================================================
# Parsing
# =============================================================================

@dataclass
class Node:
    """One configuration line and the lines indented below it."""
    text: str
    children: List["Node"] = field(default_factory=list)
    top: bool = False             # at the top level of the configuration

    @property
    def is_section(self):
        """Has lines below it, or opens a section at the top level. Nested lines
        with nothing below them are settings, even "route-policy X in" or an
        IOS-XR interface's "vrf X"."""
        return bool(self.children) or (self.top and bool(SECTION_START.match(self.text)))

    def find(self, text):
        """Child section whose line is exactly `text` (spaces normalized)."""
        for c in self.children:
            if c.is_section and c.text == text:
                return c
        return None

    def leaves(self):
        return [c for c in self.children if not c.is_section]


class FormatError(Exception):
    """The input can't be read as an indented configuration."""


def normalize(line):
    return re.sub(r"\s+", " ", line.strip())


def parse(text, name="configuration"):
    """Indented configuration text -> tree of Node (the root has text "")."""
    root = Node("")
    stack = [(-1, root)]          # (indent, node)
    banner_end = None
    indented = 0
    for raw in text.replace("\r\n", "\n").replace("\t", " ").split("\n"):
        if banner_end is not None:            # banner body: ignored
            if banner_end in raw:
                banner_end = None
            continue
        line = normalize(raw)
        if not line or line.startswith("!") or NOISE.match(line):
            continue
        b = BANNER.match(line)
        if b:
            delim = b.group(1)[:2] if b.group(1).startswith("^") else b.group(1)[0]
            if line.count(delim) < 2:
                banner_end = delim
            continue
        indent = len(raw.replace("\t", " ")) - len(raw.replace("\t", " ").lstrip(" "))
        indented += indent > 0
        while stack[-1][0] >= indent:
            stack.pop()
        node = Node(line, top=stack[-1][1] is root)
        stack[-1][1].children.append(node)
        stack.append((indent, node))
    # A change typed without indentation: "interface X" followed by its
    # settings on the same level. Its hierarchy can't be known safely.
    if not indented:
        tops = [c.text for c in root.children]
        if any(SECTION_START.match(t) for t in tops) and \
                any(not SECTION_START.match(t) for t in tops[1:]):
            raise FormatError(
                f"{name} has no indentation: write it as in 'show running-config' "
                "(settings indented under their section).")
    return root


def detect_platform(*roots):
    """'ios-xr' if any of the trees looks like IOS-XR, else 'ios-xe'."""
    def walk(node):
        for c in node.children:
            yield c
            yield from walk(c)
    for root in roots:
        for n in walk(root):
            t = n.text.lower()
            if t.startswith("ipv4 address") or t.startswith("route-policy") \
                    or re.match(r"interface \S+\d+/\d+/\d+/\d+", t):
                return "ios-xr"
    return "ios-xe"


# =============================================================================
# Rollback tree (what we will print)
# =============================================================================

@dataclass
class Out:
    """A section of the rollback: its header and the lines to put in it."""
    header: str
    items: list = field(default_factory=list)   # str lines or Out sections
    keep: bool = False            # printed even when empty (restored section)

    def sub(self, header):
        for it in self.items:
            if isinstance(it, Out) and it.header == header:
                return it
        o = Out(header)
        self.items.append(o)
        return o

    def add(self, line):
        if line not in self.items:              # no duplicate lines
            self.items.append(line)

    def review(self, message, command=None):
        self.items.append(("REVIEW", message, command))

    def copy_of(self, node):
        """Put back a whole section (or line) of the running-config."""
        if node.children or node.text.lower().startswith("address-family"):
            o = self.sub(node.text)
            o.keep = True
            for c in node.children:
                o.copy_of(c)
        else:
            self.add(node.text)

    def empty(self):
        if self.keep:
            return False
        return not any(not isinstance(i, Out) or not i.empty() for i in self.items)


# =============================================================================
# Comparison
# =============================================================================

def key_of(text, parent_text=""):
    """What setting a line is about: lines with the same key replace each other."""
    if parent_text.lower().startswith("interface") and re.match(r"^vrf \S+$", text, re.I):
        return "vrf forwarding"          # IOS-XR "vrf X" under an interface
    if text.lower() == "shutdown":
        return "shutdown"
    for rx, key in SINGLE_VALUE:
        m = rx.match(text)
        if m:
            return m.expand(key).lower()
    return text                           # presence: the line itself


def negate(text):
    """The command that removes a line."""
    if text.lower() == "shutdown":
        return "no shutdown"
    return "no " + text


def all_lines(node):
    for c in node.children:
        yield c
        yield from all_lines(c)


class Builder:
    def __init__(self, before_known, platform):
        self.before_known = before_known
        self.platform = platform
        self.reviews = []                 # (section path, message)

    # ---- helpers ------------------------------------------------------------
    def review(self, out, path, message, command=None):
        out.review(message, command)
        self.reviews.append((" / ".join(p for p in path if p), message))

    @staticmethod
    def neighbor_ip(text):
        m = re.match(r"^neighbor (\S+)", text, re.I)
        return m.group(1) if m else None

    # ---- main walk ----------------------------------------------------------
    def section(self, change, before, out, path, router_before=None, new_neighbors=None):
        """Undo the content of one change section, `before` being the same
        section in the running-config (None when it didn't exist)."""
        if change.text.lower().startswith("router "):
            router_before = before
            new_neighbors = self.find_new_neighbors(change, before)

        leaves = [c for c in change.children if not c.is_section]
        sections = [c for c in change.children if c.is_section]
        # Leaves in order (VRF before addresses), sub-sections in reverse order.
        self.leaves(leaves, change, before, out, path, new_neighbors)
        for child in reversed(sections):
            self.child_section(child, before, out, path, router_before, new_neighbors)

    def child_section(self, child, before, out, path, router_before, new_neighbors):
        text = child.text
        # "no <section>" written as a section header with nothing below.
        if text.lower().startswith("no "):
            self.negation(child, before, out, path, new_neighbors)
            return
        b = before.find(text) if before is not None else None
        if b is not None:
            self.section(child, b, out.sub(text), path + [text], router_before, new_neighbors)
            return
        if not self.before_known and ASSUMED_CONTAINER.match(text):
            self.assumed_container(child, out, path, router_before, new_neighbors)
            return
        self.new_section(child, before, out, path, new_neighbors)

    def assumed_container(self, child, out, path, router_before, new_neighbors):
        """No running-config: treat VRF / router / address-family as existing."""
        text = child.text
        if not path and re.match(r"^(vrf|ip vrf)\b", text, re.I):
            # A VRF definition: one suggestion for the whole VRF.
            self.review(out, path, "no running-config given: remove this VRF only if "
                        "the change created it", negate(text))
            return
        sub = out.sub(text)
        self.section(child, None, sub, path + [text], router_before, new_neighbors)
        # One suggestion per VRF / address-family directly under the routing
        # process; nothing for what is nested inside them.
        outermost = bool(path) and path[-1].lower().startswith("router")
        if outermost and not text.lower().startswith("router"):
            self.review(out, path + [text],
                        "no running-config given: remove this only if the change "
                        "created it", negate(text))

    def new_section(self, child, before, out, path, new_neighbors):
        """A section that did not exist before the change."""
        text = child.text
        low = text.lower()
        ip = self.neighbor_ip(text)
        if ip and new_neighbors is not None and ip not in new_neighbors \
                and self.before_known:
            # IOS-XR neighbor that existed elsewhere: undo line by line.
            self.section(child, None, out.sub(text), path + [text])
            return
        if low.startswith("interface"):
            if REMOVABLE_INTERFACE.match(text):
                out.add(negate(text))
            elif ABBREVIATED_INTERFACE.match(text) and self.before_known:
                self.review(out, path, f"'{text}' is not in the running-config: write the "
                            "interface name in full, as in 'show running-config'")
            else:
                self.review(out, path, f"'{text}' is not in the running-config and a "
                            "physical interface can't be removed: reset it by hand if "
                            "it was unused", "default " + text)
            return
        if low.startswith("router"):
            self.review(out, path, f"'{text}' is not in the running-config: check you used "
                        "the right running-config file", negate(text))
            return
        if GLOBAL_AF.match(text):
            # The global IPv4 address-family can carry neighbors and networks
            # that are not in this change: never removed automatically.
            self.review(out, path, f"'{text}' was added by the change: its new neighbors are "
                        "removed; remove the address-family itself only if nothing "
                        "else uses it", negate(text))
            return
        out.add(negate(text))

    def leaves(self, leaves, change, before, out, path, new_neighbors):
        before_leaves = before.leaves() if before is not None else []
        vrf_removed = False
        for leaf in leaves:
            text = leaf.text
            if text.lower().startswith("no "):
                self.negation(leaf, before, out, path, new_neighbors)
                continue
            ip = self.neighbor_ip(text)
            if ip and new_neighbors is not None and ip in new_neighbors:
                # New neighbor: one "no neighbor" where its remote-as is.
                if re.match(r"^neighbor \S+ remote-as\b", text, re.I):
                    out.add("no neighbor " + ip)
                continue
            key = key_of(text, change.text)
            same = [b for b in before_leaves if key_of(b.text, change.text) == key]
            if any(b.text == text for b in same):
                continue                                  # unchanged
            if same and key != text:                      # value changed
                for b in same:
                    out.add(b.text)
                if key == "vrf forwarding":
                    if self.platform == "ios-xr":
                        self.review(out, path, "IOS-XR: remove the IPv4 address before "
                                    "changing the VRF of an interface")
                    self.restore_addresses(before_leaves, out)
                continue
            if not self.before_known and before is None and path and \
                    ASSUMED_CONTAINER.match(path[-1]):
                # Inside a VRF / routing process assumed to exist (no
                # running-config): its settings (rd, redistribute...) are left
                # alone; the suggestion on the VRF or address-family covers them.
                continue
            if key == "vrf forwarding":                    # VRF added on an interface
                out.add(negate(text))
                vrf_removed = True
                self.restore_addresses(before_leaves, out)
                continue
            if vrf_removed and key in ("ip address", "ipv4 address"):
                continue                                  # cleared with the VRF
            out.add(negate(text))

    @staticmethod
    def restore_addresses(before_leaves, out):
        """Changing an interface's VRF clears its addresses: put them back."""
        for b in before_leaves:
            if re.match(r"^(ip|ipv4) address\b", b.text, re.I):
                out.add(b.text)

    def negation(self, node, before, out, path, new_neighbors):
        """'no X' in the change: put back what X removed."""
        target = node.text[3:].strip()
        if before is None:
            return
        if target.lower() == "shutdown":
            if any(b.text.lower() == "shutdown" for b in before.leaves()):
                out.add("shutdown")
            return
        # A whole section removed: put the whole section back.
        sec = before.find(target)
        if sec is not None:
            out.copy_of(sec)
            m = re.match(r"^(vrf definition|ip vrf|vrf) (\S+)$", target, re.I)
            if m and not before.text:           # top-level VRF: its interfaces
                self.restore_vrf_interfaces(before, m.group(2), out, path)
            return
        # "no neighbor X" (IOS-XE): every line of that neighbor, in its sections.
        if re.match(r"^neighbor \S+$", target, re.I):
            prefix = target + " "
            for b in before.children:
                if not b.is_section and (b.text + " ").startswith(prefix):
                    out.add(b.text)
                elif b.is_section:
                    lines = [c.text for c in b.children
                             if not c.is_section and (c.text + " ").startswith(prefix)]
                    if lines:
                        sub = out.sub(b.text)
                        for line in lines:
                            sub.add(line)
            return
        key = key_of(target, before.text)
        for b in before.leaves():
            if key_of(b.text, before.text) == key:
                out.add(b.text)

    def restore_vrf_interfaces(self, root, vrf, out, path):
        """Deleting a VRF also deleted the addresses of its interfaces."""
        found = False
        for intf in root.children:
            if not intf.text.lower().startswith("interface"):
                continue
            lines = [c.text for c in intf.children if not c.is_section]
            if any(re.match(rf"^((ip )?vrf forwarding|vrf) {re.escape(vrf)}$", t, re.I)
                   for t in lines):
                sub = out.sub(intf.text)
                for t in lines:
                    if re.match(r"^((ip )?vrf forwarding|vrf|ip address|ipv4 address)\b",
                                t, re.I):
                        sub.add(t)
                found = True
        if found:
            self.review(out, path, f"VRF {vrf} was deleted by the change: its interfaces' "
                        "VRF and addresses are put back above; check nothing else was lost")

    def find_new_neighbors(self, change, before):
        """Neighbors configured by the change that don't exist anywhere in the
        same routing process of the running-config."""
        if change is None:
            return set()
        in_change = {self.neighbor_ip(n.text) for n in all_lines(change)
                     if self.neighbor_ip(n.text) and not n.text.lower().startswith("no ")}
        in_before = set()
        if before is not None:
            in_before = {self.neighbor_ip(n.text) for n in all_lines(before)
                         if self.neighbor_ip(n.text)}
        return {ip for ip in in_change if ip not in in_before}


# =============================================================================
# Output
# =============================================================================

@dataclass
class RollbackResult:
    text: str
    reviews: List[tuple]          # (section path, message)
    platform: str
    empty: bool


def render(out, platform, depth=0):
    lines = []
    pad = " " * depth
    for it in out.items:
        if isinstance(it, Out):
            if it.empty():
                continue
            lines.append(pad + it.header)
            lines.extend(render(it, platform, depth + 1))
            if platform == "ios-xe" and it.header.lower().startswith("address-family"):
                lines.append(pad + "exit-address-family")
            if depth == 0:
                lines.append("!")
        elif isinstance(it, tuple):
            _, message, command = it
            lines.append(pad + "! REVIEW: " + message)
            if command:
                lines.append(pad + "!   " + command)
        else:
            lines.append(pad + it)
    return lines


def build_rollback(before_text, change_text, before_name="running-config",
                   change_name="change", platform=None):
    """Rollback of `change_text`. `before_text` = None when no running-config
    is available (generated change kits): see the module docstring."""
    change = parse(change_text, change_name)
    before = parse(before_text, before_name) if before_text is not None else None
    platform = platform or detect_platform(change, *( [before] if before else []))
    b = Builder(before is not None, platform)
    out = Out("")
    # Top level: same rules as inside a section.
    b.section(change, before if before is not None else Node(""), out, [])
    body = render(out, platform)
    head = [f"! Rollback of: {change_name}"]
    if before is not None:
        head.append(f"! Built from the running-config taken before the change: {before_name}")
    else:
        head.append("! Built WITHOUT the running-config: VRFs, routing processes and "
                    "address-families are assumed to exist already")
    head.append(f"! Platform: {platform.upper()}")
    if b.reviews:
        head.append(f"! {len(b.reviews)} point(s) to review: search for 'REVIEW'")
    head.append("!")
    empty = not body
    if empty:
        body = ["! Nothing to roll back: the change makes no difference to the "
                "running-config."]
    tail = ["end"] if platform == "ios-xe" else [
        "! IOS-XR: check with 'show configuration', then commit"]
    text = "\n".join(head + body + ([] if empty else tail)) + "\n"
    return RollbackResult(text, b.reviews, platform, empty)


# =============================================================================
# Command line
# =============================================================================

def read_text(path):
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the rollback of a Cisco change from the running-config "
                    "taken before it.")
    ap.add_argument("running", help="running-config saved BEFORE the change")
    ap.add_argument("change", help="the change (configuration lines, indented)")
    ap.add_argument("-o", "--output", help="write the rollback to this file "
                    "(default: print it)")
    args = ap.parse_args(argv)
    try:
        res = build_rollback(read_text(args.running), read_text(args.change),
                             Path(args.running).name, Path(args.change).name)
    except (OSError, FormatError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.output:
        Path(args.output).write_text(res.text, encoding="utf-8")
        print(f"written {args.output}" + (f"  ({len(res.reviews)} point(s) to review)"
                                          if res.reviews else ""))
    else:
        sys.stdout.write(res.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
