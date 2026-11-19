# Jinja2 Template IOS-XE

Generate Cisco IOS-XE and IOS-XR configurations from an Excel link inventory,
and turn an existing running-config into Jinja2 templates.

```
                 conf2jinja.py                         generate.py
running-config ───────────────► Jinja2 templates ◄──────────────── Excel inventory
                (once per standard)        │           (every deployment)
                                           ▼
                                  one .cfg per device
```

- **`generate.py`** reads one Excel row per link and writes the configuration of
  **both ends**: VRF definitions, dot1Q sub-interfaces and BGP per VRF.
- **`conf2jinja.py`** reads a running-config and produces Jinja2 templates with
  loops (VRFs, sub-interfaces, BGP address-families and neighbors), so a good
  production config becomes the starting point of a template.
- **`rollback.py`** builds the rollback of a change from the running-config
  taken before it: old values are put back, additions are removed.
- **`change_kit.py`** gives, for every device of the workbook, the config, its
  rollback, the verification commands and the DNS records of the new
  sub-interfaces: everything a change request needs.

Both run locally, need no server, and only use `openpyxl` and `jinja2`.

## Contents

- [Installation](#installation)
- [Graphical interface](#graphical-interface)
- [DNS names](#dns-names)
- [Rollback](#rollback)
- [Change kit](#change-kit)
- [Generate configurations from Excel](#generate-configurations-from-excel)
- [The Excel workbook](#the-excel-workbook)
- [IOS-XR](#ios-xr)
- [GRT links (no VRF)](#grt-links-no-vrf)
- [Create templates from a running-config](#create-templates-from-a-running-config)
- [Templates and naming standards](#templates-and-naming-standards)
- [Repository layout](#repository-layout)
- [Network data and this repository](#network-data-and-this-repository)
- [Limitations](#limitations)

## Installation

Python 3.8 or later.

The approved runtime is Python with Tkinter (included with the Windows Python
installer), `openpyxl` and `jinja2` as listed in `requirements.txt`. No other
package, service, or build step is used.

## Graphical interface

On Windows, double-click `netgen_gui.pyw`; alternatively run `python
netgen_gui.py` from the project folder. The window has **Config generator** and
**DNS names** and **Rollback** tabs. In Config generator, choose an `.xlsx` or `.xlsm` workbook
and optionally a template folder. An empty folder field uses the standard
IOS-XE/IOS-XR templates selected by the workbook's Generate sheet. **Stop on
values missing from Excel** applies only with a custom template folder. Click
**Generate** (or Ctrl+Enter); the workbook is reread on each run. Blocking
workbook errors are shown without configurations. Otherwise the results show
generated, failed, and skipped devices, notes, and any values left out. Select
a device to preview its configuration, then use **Copy**, **Save .cfg...**, or
**Save all...**. Save all lists existing files and asks before replacing them.

The optional **Running-configs** folder is used for exact rollbacks (see
[Change kit](#change-kit)). Above the preview, **Config / Rollback / Checks**
switches between the configuration, its rollback and its verification
commands; Copy and Save act on what is shown. **Save change kit...** writes
all of them for every device, plus the DNS files (the DNS tab's domain and
options are used).

## DNS names

In **DNS names**, add running-config files, add a folder of text files, or paste
one config. Files without a hostname are reported and skipped. Set the domain
(the default `example.intra` is a placeholder: type your own domain once, it is
remembered in the settings saved next to the program), choose whether to include secondary addresses,
exclude shutdown interfaces, or lowercase names, and edit the interface
types with **Interface types...**. Generate names to preview the
sortable FQDN, IPv4, device, interface, dot1q, VRF, status and Existing DNS columns.
Warnings include shutdown, secondary, duplicate name/IP, name too long, dot1q
mismatch, and unknown interface type. **Export CSV...** writes UTF-8 with BOM;
**Export PowerShell...** writes `Add-DnsServerResourceRecordA` commands for
clean records and commented commands for warnings; **Copy table** copies TSV.
Exports happen only after choosing a destination.

### Leaving interfaces out

Three rules, any of which leaves an interface out (no record, not counted in
duplicates, not checked in DNS, never exported):

| Rule | Where | Example |
|---|---|---|
| By type | **Interface types...**: double-click the *DNS record* column (or *Include / leave out*) | leave out `Lo` = every Loopback. TenGigE and TenGigabitEthernet share `Te`, so they go together. Option: leave out types that are not in the table. |
| By interface name | *Interface names*, comma separated, `*` = any characters, case-insensitive | `MgmtEth*, *.4094, Tunnel1*` |
| By VRF | *VRFs*, comma separated | `Mgmt-intf` |

Tick **Show left-out interfaces** to see them greyed out with the reason, e.g.
`excluded (type Lo)`, `excluded (pattern *.4094)`, `excluded (VRF Mgmt-intf)`.
The rules are saved with the other DNS settings and also apply to the DNS
records of the [change kit](#change-kit).

### Checking existing DNS records

**Check existing DNS** asks DNS, record by record, with the Windows `nslookup`
command (read-only: nothing is created, changed or deleted):

- a reverse lookup of the IP: `nslookup -type=PTR <ip> [server]`;
- if *Also check the generated names* is ticked, a lookup of the name:
  `nslookup -type=A <name>. [server]`.

*DNS server* empty = the computer's own DNS server. Up to 8 lookups run at the
same time, 5 seconds each at most; **Cancel** stops starting new ones. The
server you type is not saved. Double-click a row to see the raw `nslookup`
output. English and French Windows outputs are understood.

| Existing DNS | Meaning | In the PowerShell export |
|---|---|---|
| *(empty)* | not in DNS | active `Add-...` line |
| `same` | the PTR and the name already match | comment: already in DNS |
| `PTR: <name>` | the IP already has another name | comment: conflict |
| `multiple PTR: ...` | the IP has several names | comment: conflict |
| `name -> <ip>` | the name already points elsewhere | comment: conflict |
| `A only (no PTR)` / `PTR only (no A)` | half of the record exists | comment: conflict |
| `no answer` | timeout, server unreachable, no nslookup | comment: not checked |
| `unknown` | output not understood | comment: check manually |

The summary line then shows how many records are to create, already correct,
in conflict, or without answer. The browser page (`netgen.html`) has the
exclusions but not the DNS check: a page opened from a file cannot query DNS.

Command line (`--exclude-*` options can be repeated):

```
python dns_names.py <files...> --domain example.intra --csv out.csv --powershell out.ps1 \
    --exclude-type Loopback --exclude-interface "MgmtEth*" --exclude-vrf Mgmt-intf \
    --check-dns [--dns-server 192.0.2.53] [--no-forward]
python dns_check.py 192.0.2.1 CE01_Lo0.example.intra      # try one record by hand
```

Both tools work offline. Workbook and running-config contents are kept in
memory; only explicit Save/Export actions write output files. The adjacent
`netgen_gui_settings.json` stores window size, last-used folders, strict mode,
DNS options, domain, abbreviations and the rules for leaving interfaces out.
It does not store the DNS server typed for the check, workbook/config
contents, hostnames, IP addresses, or loaded-file history. The test command is:

```
python -m unittest discover -s tests -t .
```

## Rollback

You can't reload a device on its old running-config, so a rollback is the
list of commands that undoes the change. `rollback.py` compares the change
with the running-config taken **before** it and writes that list:

```
python rollback.py running-before.txt change.cfg -o rollback.cfg
```

or, in the window, the **Rollback** tab: load or paste both, click
**Generate rollback**, then Copy or Save.

| In the change | In the rollback |
|---|---|
| a setting whose value changed (`description`, `ip address`, `rd`, `remote-as`...) | the old value |
| a new line | `no <line>` |
| a new sub-interface, loopback, tunnel... | `no interface ...` |
| a new BGP neighbor | one `no neighbor <ip>` |
| `no ...` of something that existed | the old lines, put back |
| `vrf forwarding` changed on an interface | the old VRF, then its old addresses |
| a deleted VRF | the VRF and the interfaces it had |

Lines starting with `! REVIEW` are not applied: they explain something to
decide by hand, with the suggested command commented below. For example a
physical interface or a router process missing from the running-config is
never removed, an abbreviated interface name (`Gi0/0/1`) is reported, and a
new global `address-family ipv4` under `router bgp` is only suggested. The
change must be indented as in `show running-config` (a flat file is refused).
IOS-XE and IOS-XR are detected automatically; the IOS-XR rollback ends with a
reminder to check `show configuration` and commit.

Read the rollback before using it: it is built from text, not from the device.

## Change kit

```
python change_kit.py netgen_input.xlsx kit --running running --domain example.intra
```

For every device generated from the workbook, in the `kit` folder:

| File | Content |
|---|---|
| `<device>.cfg` | the configuration (same as `generate.py`) |
| `<device>_rollback.cfg` | its rollback |
| `<device>_checks.txt` | verification commands with the real names (interfaces, VRF ping, BGP neighbor, routes received and advertised), to run before and after the change, e.g. in a NetBrain runbook |
| `dns_records.csv`, `dns_records.ps1` | DNS records of the new sub-interfaces, same rules as the DNS tab |

`--running` is a folder with one `show running-config` per device, taken
before the change; files are matched by their `hostname` line (or file name).
With it, the rollback is exact and the dependencies missing from the device
(a route-map not created yet, for example) are listed. Without it, the rollback only removes what is
clearly specific to the change (sub-interfaces and BGP neighbors) and lists
VRFs and address-families as `! REVIEW` suggestions, because they may already
exist on the device. Existing files are kept unless `--force` is given.

The commands come from `templates/<os>/checks.j2`: edit them to match your
own runbooks (a custom template folder may have its own `checks.j2`).

## Browser interface (netgen.html)

`netgen.html` does the same two jobs as the Tkinter window (Config generator and DNS names) in your web browser. It is a single file: double-click it, Edge or Chrome opens it. Nothing to install, no Python needed, no server, no network: a security rule inside the page blocks every network request, and nothing is saved unless you click Save or Export.

- **Config generator**: Browse… (or drop) the Excel file, then Generate. The file is read again at every Generate (Ctrl+Enter), so you can edit it in Excel and regenerate. Template folder: optional, pick a draft folder made by `conf2jinja.py`. Save .cfg… opens a save dialog; Save all… writes every config into a folder you choose (and asks before overwriting), or downloads a `configs.zip` if the browser can't write to a folder.
- **DNS names**: same rules, options, exclusions and exports as `dns_names.py` (CSV with BOM, PowerShell, Copy table), but no check of existing records (a page cannot query DNS). The options, the interface types and the exclusion rules are remembered in the browser (they never contain configurations, IPs or hostnames).

The page carries its own copy of `templates/`. **After changing a template, run `python build_html.py`** to copy it into `netgen.html` (`tests/test_html.py` fails until you do). The page reproduces `generate.py` and `dns_names.py` in JavaScript: when you change a rule in one of them, the same change must be made in the engine part of `netgen.html` (each function names the Python function it copies).

The rollback and the change kit are only in the Python tools, not in the page.

Limits: Excel cells formatted as dates are read as numbers; drafts may use the template features of `conf2jinja.py` and the standard templates (if/for/include/raw, filters default, upper, lower, join, length…), not every Jinja2 feature.

## Generate configurations from Excel

1. Fill `netgen_input.xlsx`. Row 2 of each sheet is a fictional example; overwrite it.
2. Run:
   ```
   python generate.py netgen_input.xlsx
   ```
3. Review the files written to `output/` before applying them.

With the example workbook, one Links row:

| VRF | Subnet | DOT1Q | DEVICE A | Port | IP | Device B | Port | IP |
|---|---|---|---|---|---|---|---|---|
| underlay | 192.0.2.0/31 | 100 | CE01 | TwentyFiveGigE1/0/1 | 192.0.2.0 | PE01 | GigabitEthernet0/0/1 | 192.0.2.1 |

gives `CE01.cfg` (and the mirrored `PE01.cfg`, see [examples/output](examples/output)):

```
! ---------------------------------------------------------------------------
! DEPENDENCIES of CE01: NOT generated here, must already exist on the device
! (check them before the change)
!   interface TwentyFiveGigE1/0/1              <- sub-interface .100
!   route-map RP-BGP65001OutBgp65100-underlay  <- neighbor 192.0.2.1 out (vrf underlay)
!   route-map RP-BGP65100InBgp65001-underlay   <- neighbor 192.0.2.1 in (vrf underlay)
! ---------------------------------------------------------------------------
!
vrf definition underlay
 rd 65001:100
 !
 address-family ipv4
 exit-address-family
!
interface TwentyFiveGigE1/0/1.100
 description # PE01 - GigabitEthernet0/0/1.100 - ( underlay ) #
 encapsulation dot1Q 100
 vrf forwarding underlay
 ip address 192.0.2.0 255.255.255.254
!
router bgp 65001
 !
 address-family ipv4 vrf underlay
  redistribute connected
  neighbor 192.0.2.1 remote-as 65100
  neighbor 192.0.2.1 description PE01-(vrf underlay)
  neighbor 192.0.2.1 activate
  neighbor 192.0.2.1 route-map RP-BGP65001OutBgp65100-underlay out
  neighbor 192.0.2.1 route-map RP-BGP65100InBgp65001-underlay in
 exit-address-family
!
end
```

### Dependencies

Every configuration starts with a **DEPENDENCIES** block: what the
configuration uses but does not create, so it must already exist on the
device. With the standard templates that is the route-maps (IOS-XE) or
route-policies (IOS-XR) of each BGP neighbor and the physical port of each
sub-interface, plus a VRF when the VRF block is set to `no`. Other templates
(conf2jinja.py drafts) can also bring prefix-lists, access-lists,
policy-maps, peer-groups / neighbor-groups and update-source interfaces.
Anything the same configuration defines is not listed. The block is only
`!` comments: pasting it on the device does nothing.

`dependencies.py` builds it from the configuration text, so it follows any
template change. `python dependencies.py some.cfg` prints it for any file.
In the [change kit](#change-kit), the checks start with a `show` command per
dependency, and with `--running` every dependency missing from the device's
running-config is reported.

### Checks before anything is written

The whole workbook is checked first. If anything is wrong, **no file is written**
and every problem is listed with its sheet and row:

```
Fix these errors in the Excel file first:
  - Links row 5: IP B 192.0.2.9 is not in 192.0.2.0/31
  - Links row 7: TwentyFiveGigE1/0/1.100 already used on CE01 (Links row 2)
  - Links row 8: empty cell(s): Port A
```

Checked: IPs inside their subnet, subnet written with its prefix length, dot1Q
between 1 and 4094, the same sub-interface used twice on a device, devices and
VRFs missing from their sheets, empty required cells, and the Generate sheet values.

### Options

| Option | Effect |
|---|---|
| `python generate.py [xlsx] [output_dir]` | Excel file and output folder (defaults: `netgen_input.xlsx`, `output/`) |
| `--debug` | Print, for each device, the data the templates receive |
| `--templates FOLDER` | Use another template folder, such as drafts from `conf2jinja.py` (see below) |
| `--strict` | With `--templates`: stop on a value the Excel doesn't provide instead of leaving it out |

## The Excel workbook

Sheet names and headers are matched without caring about case, spaces, `_` or
`-`; columns can be in any order; extra columns are ignored; a title row above
the headers is fine; merged cells apply to every row they cover.

| Sheet | Columns | Purpose |
|---|---|---|
| **Links** | VRF, Subnet, DOT1Q, Vlan, DEVICE A, Port, IP, Device B, Port, IP | One row per link. A plain `Port` / `IP` belongs to the device column on its left; `Port A`, `IP B`… also work. An empty VRF (or `GRT` / `global`) means the [global routing table](#grt-links-no-vrf). |
| **VRFs** | VRF, VPN ID | RD = `<local AS>:<VPN ID>`. Not needed for GRT links. |
| **Devices** | Device, BGP AS, Router ID (optional) | Every device of the Links sheet, on both sides |
| **Generate** (optional) | Device, Generate, OS, VRF, Interfaces, BGP, Output name | Which devices to produce, their OS (`ios-xe` or `ios-xr`, empty = `ios-xe`), which blocks, and the file name. Empty cells mean *yes*. Without this sheet, every device is generated, IOS-XE, with all blocks. |

Each Links row configures **both ends**: DEVICE A gets `Port A.DOT1Q` with IP A
and neighbor IP B; Device B gets `Port B.DOT1Q` with IP B and neighbor IP A.
Port B can stay empty when Device B is not generated (the A side then has no
interface description, since the facing interface is unknown).

The Generate sheet makes partial changes easy. For example, to add a link to a
device already in service, set `VRF = no` to get only the sub-interface and the
BGP neighbor.

## IOS-XR

Set **OS = ios-xr** for a device in the Generate sheet: the same Excel data
gives the IOS-XR equivalent, with the same naming standards. A link can have an
IOS-XE device on one end and an IOS-XR device on the other; see
[examples/mixed](examples/mixed).

| | IOS-XE | IOS-XR |
|---|---|---|
| VRF | `vrf definition X` with `rd` | `vrf X`; the RD goes under `router bgp / vrf X` |
| Sub-interface | `vrf forwarding`, `ip address`, `encapsulation dot1Q` | `vrf`, `ipv4 address`, `encapsulation dot1q` |
| BGP | `address-family ipv4 vrf X`, `neighbor … activate`, `route-map` | `address-family vpnv4 unicast` globally (required), `vrf X / neighbor …`, `route-policy` |
| End of file | `end` | nothing: paste in configure mode, check `show configuration`, then `commit` |

The route-policies (RPL) are referenced, not created: they must exist on the
device. eBGP on IOS-XR exchanges no routes without a policy in and out.

## GRT links (no VRF)

An empty VRF cell (or `GRT` / `global`) puts the link in the global routing table:

- no VRF definition and no `vrf forwarding` / `vrf` on the sub-interface;
- the BGP neighbor goes in the global address-family (`address-family ipv4` on
  IOS-XE, `address-family ipv4 unicast` on IOS-XR);
- descriptions and policies use `GRT`: `# PE01 - Gi0/0/1.100 - ( GRT ) #`,
  `PE01-(GRT)`, `RP-BGP65001OutBgp65100-GRT`. Change `GRT_LABEL` in
  `generate.py` if your standard uses another name;
- no row needed in the VRFs sheet; a VRF name that is filled in but missing
  from the VRFs sheet is still an error;
- no `redistribute connected` in the global table: it would announce every
  connected network to every global BGP peer, route reflectors included.

## Create templates from a running-config

`conf2jinja.py` turns a running-config into **draft** templates: one file per
block plus a `vars.yml` with the values it found.

```
python conf2jinja.py examples/running-config-CE01.txt -o drafts -n CE01
```

```
drafts/CE01/
├── main.j2         includes the blocks
├── vrf.j2          {% for v in vrfs %}
├── interfaces.j2   {% for i in subinterfaces %} … one loop per interface type
├── bgp.j2          {% for af in bgp_vrfs %} → {% for n in af.neighbors %}
└── vars.yml        the values of this config
```

Each block is checked: rendered with `vars.yml`, it must give back exactly the
lines of the original config. See [examples/drafts/CE01](examples/drafts/CE01)
for the result on the example config.

What it does:
- templates only the common blocks by default (VRF, interfaces, BGP); ACLs,
  prefix-lists, route-maps, SNMP, AAA… are left out. Choose others with
  `--blocks vrf,interfaces,bgp,static_routes` or `--blocks all`;
- groups items with the same structure into one loop;
- binds the sub-interface number and `encapsulation dot1Q` to the same value;
- reuses a value everywhere it appears, including inside names
  (`RP-BGP{{ local_as }}OutBgp{{ n.remote_as }}-{{ af.vrf }}`);
- drops interface ACL filters (`--keep-filters` to keep them);
- `--redact-secrets` writes `CHANGE_ME` instead of passwords and keys.

### From draft to master template

A draft describes the reference device; the Excel describes links. Two ways to
use a draft:

- **Directly**: `python generate.py netgen_input.xlsx --templates drafts/CE01`.
  Everything the Excel provides is filled in; lines that need values the Excel
  doesn't have (loopbacks, router-id, parent interfaces…) are left out and listed.
- **As a master template**: review it, merge variant loops (`bgp_vrfs_2`…) into
  one loop with `{% if %}` for optional lines, and copy it to
  `templates/ios-xe/`. Run `generate.py --debug` to see the variable names the
  Excel provides.

## Templates and naming standards

The master templates are in [templates/ios-xe](templates/ios-xe) and
[templates/ios-xr](templates/ios-xr). Each one starts with a comment listing the
variables it uses.

| Item | Format |
|---|---|
| Sub-interface | `<Port>.<DOT1Q>` with `encapsulation dot1Q <DOT1Q>` |
| Interface description | `# <remote host> - <remote port>.<DOT1Q> - ( <VRF> ) #` (`GRT` for GRT links) |
| RD | `<local AS>:<VPN ID>` |
| BGP neighbor description | `<remote host>-(vrf <VRF>)` |
| Route-map / route-policy out | `RP-BGP<local AS>OutBgp<remote AS>-<VRF>` |
| Route-map / route-policy in | `RP-BGP<remote AS>InBgp<local AS>-<VRF>` |

Route-maps (IOS-XE) and route-policies (IOS-XR) are referenced, not created:
they must exist on the device.

To change the configuration style, edit the `.j2` files. To add an Excel
column to the configuration: add it to `LINKS_COLS` in `generate.py`, put it in
the dictionary built in `add_side()`, and use it in a template.

## Repository layout

```
├── generate.py              Excel → configurations (engine + command line)
├── conf2jinja.py            running-config → draft templates
├── rollback.py              running-config before + change → rollback
├── change_kit.py            Excel → config + rollback + checks + DNS per device
├── dependencies.py          what a config uses but doesn't create (route-maps...)
├── dns_names.py             running-configs → DNS records
├── dns_check.py             read-only nslookup check: does each record already exist?
├── netgen_gui.py(w)         desktop window (Tkinter); netgen_core.py its backend
├── netgen.html              the generator and DNS tool in a browser page
├── netgen_input.xlsx        input workbook with a fictional example (README sheet inside)
├── requirements.txt         openpyxl, jinja2
├── templates/ios-xe/        master templates: main.j2, vrf.j2, interfaces.j2, bgp.j2,
│                            checks.j2 (verification commands)
├── templates/ios-xr/        the same four blocks for IOS-XR
└── examples/
    ├── running-config-CE01.txt   fictional running-config
    ├── drafts/CE01/              conf2jinja.py output for it
    ├── output/                   generate.py output for netgen_input.xlsx
    └── mixed/                    IOS-XE CE + IOS-XR PE, one VRF link and one GRT link
docs/
└── netgen-gui-prompt.md     specification of a planned desktop GUI and DNS name generator
```

## Network data and this repository

Everything in `examples/` and in the workbook is fictional: documentation
address ranges (RFC 5737) and private AS numbers.

Real running-configs, filled workbooks, `vars.yml` files, generated `.cfg`
files, rollbacks and change kits describe a real network. Keep them out of this repository; `.gitignore`
already excludes `output/`, `drafts/`, `running/`, `kit/`, `*.cfg` and `vars.yml` outside `examples/` and `tests/data/`.

## Limitations

- IOS-XE and IOS-XR. `conf2jinja.py` reads IOS-XE running-configs only.
- The IOS-XR templates follow IOS-XR syntax rules but have not been committed
  on a real router by the author yet: test them in a lab first.
- IPv4 only.
- The `Vlan` column is read but not used by the templates yet.
- `conf2jinja.py` drafts need a review before they become master templates;
  they reflect one reference device.
