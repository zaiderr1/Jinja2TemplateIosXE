# Network Config Tools: project briefing

> **To the AI reading this:** this document describes a set of offline tools
> built by a network engineer for daily run and change work. Read it in full
> before answering. Your proposals must respect **every constraint in section 2**;
> an idea that breaks one of them (a server, a new package, sending data
> anywhere) is not useful here, however good it is. Section 11 says what is
> asked of you and how to answer.
>
> All names, addresses and AS numbers in this document are fictional
> (RFC 5737 addresses, private AS numbers, `example.intra`).

---

## 1. Context

- **Who:** a network engineer on a large enterprise network, mostly **run and
  changes** (not design). Cisco **IOS-XE** (CE side) and **IOS-XR** (PE side),
  **BGP** per VRF, **dot1Q sub-interfaces**, **VRFs** (and links in the global
  routing table).
- **Biggest time sinks:**
  - writing change configurations;
  - preparing verifications and rollbacks;
  - troubleshooting.
- **Already in place at the company:** **NetBrain**. It does the pre/post
  change comparison and runbooks, so the tools must feed it (command lists),
  not replace it.
- **Rollbacks:** a device cannot be reloaded on its old running-config. A
  rollback is always "undo the commands one by one".
- **Repository:** `zaiderr1/Jinja2TemplateIosXE` (public, so it holds fictional
  data only).

## 2. Hard constraints (non-negotiable)

| # | Constraint | Consequence for any proposal |
|---|---|---|
| C1 | **Closed network, strict DLP.** Nothing can leave the company laptop: no cloud, no AI API, no upload, no telemetry. DLP detects "really anything". | Everything runs **offline** on local files. No workflow that requires copying configs or outputs to an external service, even sanitized. |
| C2 | **No internal data may leak in any way.** | No real hostnames, IPs, AS numbers, VRF names or domains in the repo, examples, tests or docs. The tools never write anything except what the user explicitly saves. |
| C3 | **Stack is fixed: Python 3 + `openpyxl` + `jinja2` + Tkinter** (Tkinter ships with the Windows Python installer). | No new package (no pandas, Streamlit, Flask, rich, netmiko, ttp, textfsm, Ansible, ...). No build step. Standard library is fine. |
| C4 | **No web server, no service, no `.bat` launcher.** The user explicitly rolled back a web-server idea. | Desktop GUI = Tkinter. A single offline HTML file (`netgen.html`, opened by double-click, network blocked by CSP) also exists. |
| C5 | **Offline files only:** the tools read files the user saved (Excel workbooks, `show running-config` text files). They don't connect to devices. | No SSH, SNMP, API or NetBrain integration. Inputs are text and xlsx files. |
| C6 | Rollback is done by undoing modifications (no config replace / reload). | Rollback logic must produce explicit `no ...` / restore commands. |
| C7 | NetBrain already does pre/post comparison. | Don't rebuild diffing of show outputs; produce **command lists** for NetBrain runbooks instead. |
| C8 | Corporate Windows laptop. | Paths, encodings (UTF-8 with BOM for CSV/PS1, CRLF for PowerShell) and file dialogs must work on Windows. |

**Rejected or parked ideas (don't propose them again):**

- A local web server or browser app needing a server.
- Extra Python packages.
- Workflows built around sending data to an AI. The user found these bad and time-consuming.
- Generating route-maps, route-policies and prefix-lists. The user writes those by hand; the tools only *list* them as dependencies.
- Rebuilding pre/post comparison (NetBrain does it).

## 3. What exists today

```
                 conf2jinja.py                         generate.py
running-config ───────────────► Jinja2 templates ◄──────────────── Excel inventory
                (once per standard)        │           (every deployment)
                                           ▼
                one .cfg per device (with a DEPENDENCIES block at the top)
                                           │ change_kit.py
                                           ▼
         config + rollback + verification commands + DNS records, per device
```

| File | Role |
|---|---|
| `generate.py` | Excel → configurations (engine `generate_configs()` + CLI) |
| `conf2jinja.py` | running-config → draft Jinja2 templates with loops |
| `dependencies.py` | lists what a config uses but doesn't create |
| `rollback.py` | running-config before + change → rollback |
| `change_kit.py` | per device: config, rollback, checks, DNS records |
| `dns_names.py` | running-configs → DNS A records (CSV / PowerShell), with exclusions |
| `dns_check.py` | read-only nslookup check: does each record already exist? |
| `netgen_core.py` | GUI backend (no Tk): load/generate, save, preferences, background jobs |
| `netgen_gui.py` / `.pyw` | Tkinter desktop window, 3 tabs |
| `netgen.html` + `build_html.py` | same generator + DNS tool in one offline HTML page (JavaScript port) |
| `templates/ios-xe/`, `templates/ios-xr/` | `main.j2`, `vrf.j2`, `interfaces.j2`, `bgp.j2`, `checks.j2` |
| `netgen_input.xlsx` | input workbook with a fictional example and a README sheet |
| `examples/` | fictional running-config, drafts, reference outputs (incl. mixed XE/XR + GRT) |
| `tests/` | ~70 unittest tests, all fictional fixtures |
| `docs/netgen-gui-prompt.md` | the specification the GUI was built from |

### 3.1 `generate.py`: Excel → configs

- One Links row = one link, and **both ends are generated**:
  - Device A gets `PortA.DOT1Q` with IP A and BGP neighbor IP B.
  - Device B gets the mirror: `PortB.DOT1Q` with IP B and neighbor IP A.
- **Blocks:** VRF definitions, dot1Q sub-interfaces, BGP (one address-family per VRF, one neighbor per link).
- **Empty VRF cell (or `GRT` / `global`) = global routing table:**
  - no `vrf forwarding` line;
  - the neighbor goes into the global `address-family ipv4`;
  - policy names use the label `GRT`;
  - no `redistribute connected` in the global table.
- **The whole workbook is validated before anything is written.** If any check fails, no file is written. Each error is listed with its sheet and row:
  - IP inside its subnet;
  - subnet given with its prefix length;
  - dot1Q between 1 and 4094;
  - same sub-interface used twice on a device;
  - missing devices or VRFs;
  - empty required cells;
  - Generate sheet values.
- **The workbook is forgiving:**
  - headers matched ignoring case, spaces, `_` and `-`;
  - columns in any order, extra columns ignored;
  - the header row can be anywhere in the first 10 rows;
  - merged cells apply to every row they cover.
- **CLI:** `python generate.py [xlsx] [outdir] [--templates DIR] [--strict] [--debug]`
  - `--templates` uses drafts from conf2jinja.
  - Without `--strict`, values the Excel doesn't provide are left out and listed.
  - `--debug` prints the data each template receives.
- **Engine API:** `generate_configs(wb, templates=None, strict=False)` returns a `Result`:
  - `errors`, `notes`, `failed`, `skipped`, `ok`;
  - `configs`: one dict per device with `host`, `os`, `blocks`, `output`, `source`, `text`, `data`, `dependencies` and `left_out`.

### 3.2 `conf2jinja.py`: running-config → templates

- Turns a good production config into draft templates. It produces **loops**, not one copy per instance:
  - VRFs;
  - sub-interfaces;
  - BGP address-families and neighbors.
- One `.j2` per block, or `--single-file`.
- Options:
  - `--blocks vrf,interfaces,bgp[,static_routes|all]`;
  - `--keep-filters`;
  - `--keep-interfaces`;
  - `--redact-secrets`.
- Common config the user doesn't want templated (ACLs, filters) is left out by default.
- Reads IOS-XE only.

### 3.3 `dependencies.py`: the DEPENDENCIES slot

Every generated config starts with a comment-only block:

```
! ---------------------------------------------------------------------------
! DEPENDENCIES of CE01: NOT generated here, must already exist on the device
! (check them before the change)
!   interface TwentyFiveGigE1/0/1              <- sub-interface .100
!   route-map RP-BGP65001OutBgp65100-underlay  <- neighbor 192.0.2.1 out (vrf underlay)
!   route-map RP-BGP65100InBgp65001-underlay   <- neighbor 192.0.2.1 in (vrf underlay)
! ---------------------------------------------------------------------------
```

- It scans the **rendered text**, so it works with any template. Kinds it reports:
  - route-maps (IOS-XE) and route-policies (IOS-XR);
  - parent interfaces of the sub-interfaces;
  - VRFs, when the VRF block is off;
  - prefix-lists, ACLs and policy-maps;
  - peer-groups, peer-session and peer-policy templates;
  - neighbor-, session- and af-groups;
  - update-source interfaces.
- Objects defined in the same config are excluded.
- `missing(deps, running_text)` lists the dependencies a running-config lacks.

### 3.4 `rollback.py`: rollback generator

`python rollback.py running-before.txt change.cfg -o rollback.cfg`, or the GUI Rollback tab.

| In the change | In the rollback |
|---|---|
| a setting whose value changed (description, ip address, rd, remote-as, ...) | the old value |
| a new line | `no <line>` |
| a new sub-interface / loopback / tunnel | `no interface ...` |
| a new BGP neighbor | one `no neighbor <ip>` |
| `no ...` of something that existed | the old lines put back |
| VRF changed on an interface | the old VRF, then its old addresses |
| a deleted VRF | the VRF and its interfaces |

- **Safety rules:**
  - Uncertain items become `! REVIEW:` comments with the suggested command commented out.
  - It never writes an active `no router ...`.
  - A physical interface missing from the running-config is never removed.
  - Abbreviated interface names are flagged.
  - A new global `address-family ipv4` is only suggested, never removed automatically.
  - A flat (non-indented) change file is refused.
- **Without a running-config (change kit fallback):**
  - only change-specific items are removed (sub-interfaces, neighbors);
  - VRFs and address-families are listed as REVIEW suggestions.
- **Platform:** XE and XR are detected automatically. The XE rollback ends with `end`. The XR rollback ends with a reminder to check `show configuration`, then commit (never an automatic commit).

### 3.5 `change_kit.py`: everything a change request needs

`python change_kit.py netgen_input.xlsx kit --running FOLDER --domain example.intra [--templates DIR] [--force]`

| File (per device) | Content |
|---|---|
| `<device>.cfg` | the config (identical to generate.py) |
| `<device>_rollback.cfg` | its rollback |
| `<device>_checks.txt` | verification commands with real names, for NetBrain runbooks (see below) |
| `dns_records.csv`, `dns_records.ps1` | DNS records of the new sub-interfaces (all devices together) |

- **What the checks contain:**
  - dependencies first: `show route-map`, `show rpl route-policy`, `show interfaces`, ...;
  - then sub-interfaces;
  - then BGP: summary, ping, neighbor state, routes received and advertised.
- **`--running` is a folder of `show running-config` files taken before the change.** Files are matched by their `hostname` line, or by file name. Two files for the same device are both ignored and reported. With it:
  - the rollback is exact;
  - any dependency missing from the device is reported.
- Existing files are never replaced without `--force` (or a confirmation in the GUI).
- `checks.j2` is user-editable to match their runbooks.

### 3.6 `dns_names.py`: DNS names from running-configs

- **Name rule:** `Device_Interface_dot1q.<domain>` with a short interface name, e.g. `CE01_Twe1-0-1_100.example.intra`. In the interface part, `/` becomes `-`.
- **Editable abbreviations:** TenGigabitEthernet / TenGigE → `Te`, TwentyFiveGig… → `Twe`, FortyGig → `Fo`, HundredGig → `Hu`, GigabitEthernet → `Gi`, Loopback → `Lo`, Bundle-Ether → `BE`, …
- **Options:**
  - include secondary addresses (`-sec1`, …);
  - exclude shutdown interfaces;
  - lowercase.
- **Warnings:** shutdown, secondary, duplicate name or IP, name too long, dot1q ≠ sub-interface number, unknown interface type.
- **Leaving interfaces out:** by type (a *DNS record* tick per abbreviation; unknown types optional), by interface-name pattern (`*` wildcard) and by VRF. Excluded interfaces get no record, don't count in duplicates and are never exported; they can be shown greyed out with the reason.
- **Existing DNS check** (`dns_check.py`, desktop app and `--check-dns` only): read-only `nslookup` PTR + A per record (8 in parallel, 5 s timeout, argument list, validated values, English/French output). Results: to create / same / conflict (other PTR, several PTR, name elsewhere, half record) / no answer / unknown. The PowerShell export keeps only records to create as active lines.
- **Exports:**
  - CSV (UTF-8 with BOM);
  - PowerShell `Add-DnsServerResourceRecordA ... -CreatePtr` (warnings as commented lines; UTF-8 BOM, CRLF);
  - TSV copy.
- **CLI:** `python dns_names.py files... --domain example.intra --csv out.csv --powershell out.ps1`

### 3.7 GUI (`netgen_gui.py`, Tkinter)

- **Config generator tab:**
  - inputs: Excel file, optional template folder, optional running-configs folder;
  - device list with status (generated / values left out / failed / not generated);
  - a **Config / Rollback / Checks** view switch;
  - buttons: Copy, Save .cfg..., Save all..., **Save change kit...**;
  - amber notes, including missing dependencies.
- **DNS names tab:** add files, a folder or a pasted config; options; abbreviation editor; sortable table; exports.
- **Rollback tab:** paste or load "before" and "change", pick the platform (auto/XE/XR), Generate, then Copy or Save. REVIEW lines are highlighted.
- Ctrl+Enter runs the visible tab.
- Preferences are saved in a whitelisted JSON next to the program: window size, last folders, DNS options. **Never** config contents, hostnames or IPs.

### 3.8 `netgen.html`

- One file, opened offline. A Content-Security-Policy blocks all network access.
- Contains:
  - a JavaScript port of the generator (with its own small Jinja-subset engine and xlsx reader);
  - the dependency scanner;
  - the DNS tool.
- `build_html.py` embeds `templates/`; `tests/test_html.py` fails if they drift.
- **The JS output is identical to Python on the examples.**
- Does **not** include the rollback or the change kit.

## 4. Data model

### Excel sheets

| Sheet | Columns | Notes |
|---|---|---|
| Links | VRF, Subnet, DOT1Q, Vlan, DEVICE A, Port, IP, Device B, Port, IP | A plain `Port`/`IP` belongs to the device column on its left. Vlan is read but unused. Port B may be empty if Device B isn't generated. |
| VRFs | VRF, VPN ID | RD = `<local AS>:<VPN ID>` |
| Devices | Device, BGP AS, Router ID (optional) | every device of Links, both sides |
| Generate (optional) | Device, Generate, OS (`ios-xe`/`ios-xr`), VRF, Interfaces, BGP, Output name | empty = yes; lets you build partial changes, e.g. VRF=no for a new link on a device already in service |

### Template variables (per device)

- `local_as`, `router_id` (optional), `grt_label` (`"GRT"`)
- `blocks.vrf`, `blocks.interfaces`, `blocks.bgp`
- `vrfs[]`: `name`, `vpn_id`
- `subinterfaces[]`: `name`, `dot1q`, `vrf`, `ip`, `mask`, `description`
- `bgp_vrfs[]`: `vrf`, `vpn_id`, `neighbors[]` (each with `ip`, `remote_as`, `device_b`, `description`)
- `bgp_global[]`: neighbors of GRT links
- `checks.j2` also gets `host` and `dependencies[]` (`kind`, `name`, `used_by`)

## 5. Naming standards (company conventions)

| Object | Standard |
|---|---|
| Sub-interface | `<port>.<dot1q>`. **The dot1Q value always equals the sub-interface number.** |
| Interface description | `# <remote host> - <remote port>.<dot1q> - ( <VRF> ) #` |
| BGP neighbor description | `<remote host>-(vrf <VRF>)` |
| RD | `<local AS>:<VPN ID>` |
| Route-map / route-policy out | `RP-BGP<local AS>OutBgp<remote AS>-<VRF or GRT>` |
| Route-map / route-policy in | `RP-BGP<remote AS>InBgp<local AS>-<VRF or GRT>` |
| DNS record | `<Device>_<ShortIf>_<dot1q>.<domain>` |

## 6. Platform specifics

- **IOS-XE:**
  - `vrf definition` with the RD and `address-family ipv4`;
  - `vrf forwarding`;
  - `ip address`;
  - BGP `address-family ipv4 vrf X` with `redistribute connected` and `activate`;
  - neighbor route-maps in and out;
  - the config ends with `end`.
- **IOS-XR:**
  - `vrf X` with `address-family ipv4 unicast`;
  - the RD goes under `router bgp / vrf X`;
  - `ipv4 address`, `encapsulation dot1q`;
  - global `address-family vpnv4 unicast` is required when there are VRF neighbors, and `address-family ipv4 unicast` for GRT neighbors;
  - route-policy in **and** out is mandatory for eBGP;
  - no `activate`;
  - **no `end`/`commit` in output** (the user commits after `show configuration`).
- The XR templates follow syntax rules but haven't been lab-committed yet.

## 7. Engineering guidelines (how things are built here)

1. **Engines hold the logic; interfaces only display.**
   - `generate.py`, `rollback.py`, `dependencies.py`, `dns_names.py` and `change_kit.py` are importable and have no side effects.
   - The GUI holds no business logic.
   - Every engine also has a CLI.
2. **Nothing is written except on an explicit Save/Export to a place the user chose.**
   - Existing files are never replaced without asking (`--force` on the CLI).
3. **Validate everything first, and fail with the full list.**
   - Errors name the sheet, row and value.
   - On an error, no partial output.
4. **When unsure, don't act: explain.**
   - Generated text never contains an active destructive command it isn't sure of.
   - Doubt becomes a `! REVIEW:` comment with the suggestion commented out.
   - Generated annotations are `!` comment lines only, so pasting them is harmless.
5. **Read the rendered config, not the template,** when analysing output (dependencies, rollback, DNS). That way, templates stay freely editable.
6. **Templates are the user's:**
   - standards live in `.j2` files with comment headers documenting their variables;
   - `checks.j2` is meant to be edited.
7. **Naming standard in one place:** the description, route-map and DNS rules above are applied consistently across tools. The DNS names in the change kit come from `dns_names.py` itself.
8. **The HTML page mirrors Python:**
   - any rule change in `generate.py`, `dns_names.py` or `dependencies.py` must be ported to `netgen.html`;
   - templates are embedded with `build_html.py`.
9. **GUI rules:**
   - every visible text lives in a `TEXT` dict;
   - long work runs in a background thread that never touches widgets (the window polls with `after()`);
   - preferences are whitelisted.
10. **Tests:**
    - `python -m unittest discover -s tests -t .`;
    - fixtures are fictional only: RFC 5737 IPs (192.0.2.x, 198.51.100.x, 203.0.113.x), private ASes (65001, 65100), hosts CE01/PE01, domain `example.intra`;
    - reference outputs are compared byte for byte.
11. **Repo hygiene:**
    - `.gitignore` excludes `output/`, `drafts/`, `running/`, `kit/`, `*.cfg` and `vars.yml`, except for examples and test data;
    - every change goes through a branch, a PR and a merge;
    - before every push, a scan checks for real-looking data.
12. **Writing style:** plain, short English in messages and docs. A beginner network engineer must understand errors and comments.

## 8. Known gaps / status

- IPv4 only. The `Vlan` column is unused.
- The IOS-XR templates haven't been tested on a real router.
- `conf2jinja.py` reads IOS-XE only.
- `netgen.html` has no rollback and no change kit.
- **Day-0 config generator for new devices** (following the company's STS/standard templates): wanted, waiting for an anonymized STS skeleton.
- The GUI was smoke-tested headless; it still has to be tried on the Windows laptop.

## 9. Ideas already proposed (backlog, not started)

1. **Conflict check against the running-config.** Before sending, flag items that must *not* exist yet:
   - an existing sub-interface;
   - a dot1q already used on the port;
   - an IP or subnet that overlaps an existing interface;
   - a neighbor IP already in BGP;
   - a VRF that exists with a different RD.
2. **Delta config.** Drop the lines already present in the running-config, so the change and its rollback contain only effective lines.
3. **Reverse generator: running-configs → Excel.** Pair sub-interfaces across devices by their /31 to rebuild the Links sheet. This makes existing links reproducible and exposes end-to-end mismatches (dot1q, VRF, description).
4. **Description audit and fixer.** Check descriptions against the standard and generate the correcting config, using the pairing from idea 3.
5. **Orphan finder.** Find objects nothing uses: route-maps, prefix-lists and ACLs that nothing references, VRFs with no interfaces, sub-interfaces with no IP, admin-down neighbors. Output as CSV.
6. **Decommission generator.** Excel rows marked `remove` give removal config plus its rollback. Only objects owned by that link are removed; shared ones get a warning.
7. **MOP / implementation plan text** (`mop.j2`), ready to paste into the change ticket: order of devices, steps, waits, verification and rollback steps.

## 10. Glossary

- **CE / PE:** customer edge (IOS-XE) / provider edge (IOS-XR).
- **GRT:** global routing table (no VRF).
- **Change kit:** the files produced per change by `change_kit.py`.
- **REVIEW line:** a `! REVIEW:` comment the engineer must decide on by hand.
- **STS:** the company's standard technical specification for device configs.

## 11. What I want from you

Given all of the above, please:

1. **Propose new tools or features** that would save time in daily run and change work. For each one give:
   - the problem it solves and how often it happens;
   - inputs and outputs (files only);
   - how it fits the existing engines (reuse `dependencies.py`, `rollback.py`, `dns_names.py`, templates, change kit, ...);
   - effort (S/M/L) and risk;
   - how it stays within constraints C1–C8.
2. **Rank the backlog in section 9** together with your own ideas, and say which three you would build first and why.
3. **Point out weaknesses or risks** in what exists: rollback edge cases, IOS-XR specifics, validation gaps, Windows/DLP pitfalls.
4. Ask me questions if something about my environment is unclear, rather than assuming.

Don't propose anything that needs a server, a new Python package, a connection to devices or to any external service, or that sends data outside the laptop.
