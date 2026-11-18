#!/usr/bin/env python3
"""
dependencies.py - what a generated configuration uses but does not create.

A configuration from generate.py refers to objects it does not contain: the
route-maps / route-policies of the BGP neighbors, the physical port of each
sub-interface, a VRF when the VRF block is not generated, and so on. They must
already exist on the device, or the change fails (or worse: an IOS-XR eBGP
session comes up with no routes).

    find(text)              -> [{"kind", "name", "used_by": [...]}]
    slot(deps, host)        -> the comment block put at the top of the config
    missing(deps, running)  -> the dependencies a running-config doesn't have

The scan reads the configuration text itself, so it works for every template
(standard ones, conf2jinja.py drafts) without knowing it. Anything defined in
the same configuration is not a dependency.

netgen.html has a JavaScript copy of this file (section "dependencies.py
port"): change both together.
"""
import re
import sys

# Kinds in the order they are listed, with how each one is defined.
# "top": the definition must be a top-level line (not indented).
# {n} is replaced by the escaped name.
KINDS = [
    ("interface", True, [r"interface {n}"]),
    ("vrf", True, [r"vrf definition {n}", r"ip vrf {n}", r"vrf {n}"]),
    ("peer-group", False, [r"neighbor {n} peer-group"]),
    ("peer-session", False, [r"template peer-session {n}"]),
    ("peer-policy", False, [r"template peer-policy {n}"]),
    ("neighbor-group", False, [r"neighbor-group {n}"]),
    ("session-group", False, [r"session-group {n}"]),
    ("af-group", False, [r"af-group {n} .*"]),
    ("route-map", True, [r"route-map {n}(?: .*)?"]),
    ("route-policy", True, [r"route-policy {n}(?:\(.*\))?"]),
    ("prefix-list", True, [r"ip prefix-list {n} .*", r"prefix-set {n}"]),
    ("access-list", True, [r"ip access-list (?:standard |extended )?{n}",
                           r"access-list {n} .*", r"ipv4 access-list {n}"]),
    ("policy-map", True, [r"policy-map (?:type \S+ )?{n}"]),
]
ORDER = [k for k, _, _ in KINDS]
MAX_USERS = 3

# References: (regex on the stripped line, kind, group of the name,
# "top" = only on top-level lines / "sub" = only on indented lines / None = both,
# group of the text describing the user, or None = the section).
REFERENCES = [
    (r"interface (\S+)\.(\d+)", "interface", 1, "top", "sub"),
    (r"(?:ip )?vrf forwarding (\S+)", "vrf", 1, "sub", None),
    (r"neighbor (\S+) route-map (\S+) (in|out)", "route-map", 2, "sub", "neighbor"),
    (r"neighbor (\S+) prefix-list (\S+) (in|out)", "prefix-list", 2, "sub", "neighbor"),
    (r"neighbor (\S+) peer-group (\S+)", "peer-group", 2, "sub", "neighbor1"),
    (r"neighbor (\S+) inherit peer-session (\S+)", "peer-session", 2, "sub", "neighbor1"),
    (r"neighbor (\S+) inherit peer-policy (\S+)", "peer-policy", 2, "sub", "neighbor1"),
    (r"neighbor (\S+) update-source (\S+)", "interface", 2, "sub", "neighbor1"),
    (r"update-source (\S+)", "interface", 1, "sub", None),
    (r"use (neighbor-group|session-group|af-group) (\S+)", None, 2, "sub", None),
    (r"(?:ip |ipv4 )?access-group (\S+)(?: (?:in|out|ingress|egress))?", "access-list", 1,
     "sub", None),
    (r"access-class (\S+) .*", "access-list", 1, "sub", None),
    (r"service-policy (input|output) (\S+)", "policy-map", 2, "sub", None),
    (r"route-policy (\S+) (in|out)", "route-policy", 1, "sub", "direction"),
    (r"(.*?)\broute-policy (\S+?)(?:\(.*\))?(?: .*)?", "route-policy", 2, "sub", "before"),
    (r"(.*?)\broute-map (\S+)(?: .*)?", "route-map", 2, "sub", "before"),
    (r"(.*?)\bprefix-list (\S+)(?: .*)?", "prefix-list", 2, "sub", "before"),
    (r"vrf (\S+)", "vrf", 1, "sub", None),
    (r"address-family ipv4 vrf (\S+)", "vrf", 1, "sub", None),
]
REFERENCES = [(re.compile(r"^" + p + r"$", re.I), k, g, where, user)
              for p, k, g, where, user in REFERENCES]
NEIGHBOR = re.compile(r"^neighbor (\S+)$", re.I)
VRF_CONTEXT = re.compile(r"^(?:vrf|address-family ipv4 vrf) (\S+)$", re.I)


def _definition(kind, name):
    top, patterns = next((t, p) for k, t, p in KINDS if k == kind)
    n = re.escape(name)
    return top, re.compile("^(?:" + "|".join(p.format(n=n) for p in patterns) + r")\s*$", re.I)


def _lines(text):
    """(indentation, stripped line) of every meaningful line."""
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("!") or s.lower().startswith("description "):
            continue
        yield len(raw) - len(raw.lstrip(" ")), s


def is_defined(kind, name, text):
    """True if `text` defines this object (a configuration or running-config)."""
    top, rx = _definition(kind, name)
    return any(rx.match(s) for indent, s in _lines(text) if not top or indent == 0)


def find(text):
    """Dependencies of a configuration, in display order.
    Returns [{"kind": "route-map", "name": "RM-X", "used_by": ["neighbor 192.0.2.1 out"]}]"""
    found = {}                       # (kind, name) -> dependency
    stack = []                       # [(indent, line)] sections enclosing the line
    for indent, s in _lines(text):
        while stack and stack[-1][0] >= indent:
            stack.pop()
        for rx, kind, group, where, user in REFERENCES:
            if (where == "top") != (indent == 0):
                continue
            m = rx.match(s)
            if not m:
                continue
            k = kind or m.group(1).lower()
            name = m.group(group)
            used = _user(m, user, stack, s)
            dep = found.setdefault((k, name), {"kind": k, "name": name, "used_by": []})
            if used not in dep["used_by"]:
                dep["used_by"].append(used)
            break
        stack.append((indent, s))
    deps = [d for d in found.values() if not is_defined(d["kind"], d["name"], text)]
    deps.sort(key=lambda d: ORDER.index(d["kind"]))      # stable: first use first
    return deps


def _user(m, user, stack, line):
    """Short text saying what uses the object, e.g. 'neighbor 192.0.2.1 out (vrf A)'."""
    vrf = next((VRF_CONTEXT.match(s).group(1) for _, s in reversed(stack)
                if VRF_CONTEXT.match(s)), None)
    neighbor = next((NEIGHBOR.match(s).group(1) for _, s in reversed(stack)
                     if NEIGHBOR.match(s)), None)
    section = stack[0][1] if stack else line
    if user == "sub":
        return "sub-interface ." + m.group(2)
    if user == "neighbor":
        text = f"neighbor {m.group(1)} {m.group(3)}"
    elif user == "neighbor1":
        text = f"neighbor {m.group(1)}"
    elif user == "direction":
        text = (f"neighbor {neighbor} " if neighbor else "") + m.group(2)
    elif user == "before":
        if section.lower().startswith(("route-map ", "route-policy ")):
            text = section
        else:
            text = (f"neighbor {neighbor} " if neighbor else "") + \
                (m.group(1).strip() or section)
    else:
        text = f"neighbor {neighbor}" if neighbor else section
    if vrf and text != section and "vrf" not in text.split():
        text += f" (vrf {vrf})"
    return text


def users_text(dep):
    """'used_by' as one short line."""
    users = dep["used_by"]
    if all(u.startswith("sub-interface .") for u in users) and len(users) > 1:
        shown = "sub-interfaces " + ", ".join(u[len("sub-interface "):]
                                              for u in users[:MAX_USERS])
    else:
        shown = ", ".join(users[:MAX_USERS])
    if len(users) > MAX_USERS:
        shown += f" +{len(users) - MAX_USERS} more"
    return shown


def slot(deps, host):
    """Comment block listing the dependencies, put at the top of the config."""
    rule = "! " + "-" * 75
    if not deps:
        return (f"{rule}\n! DEPENDENCIES of {host}: none - everything this configuration "
                f"uses is in it.\n{rule}\n!\n")
    width = min(55, max(len(d["kind"]) + 1 + len(d["name"]) for d in deps))
    lines = [rule,
             f"! DEPENDENCIES of {host}: NOT generated here, must already exist "
             "on the device",
             "! (check them before the change)"]
    for d in deps:
        obj = f"{d['kind']} {d['name']}"
        lines.append(f"!   {obj.ljust(width)}  <- {users_text(d)}")
    lines += [rule, "!"]
    return "\n".join(lines) + "\n"


def missing(deps, running_text):
    """Dependencies not found in a running-config."""
    return [d for d in deps if not is_defined(d["kind"], d["name"], running_text)]


def main(argv=None):
    """python dependencies.py file.cfg  -> prints the dependency slot."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python dependencies.py <configuration file>")
        return 1
    with open(argv[0], encoding="utf-8", errors="replace") as f:
        print(slot(find(f.read()), argv[0]), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
