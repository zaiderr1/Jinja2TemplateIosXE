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

Both run locally, need no server, and only use `openpyxl` and `jinja2`.

## Contents

- [Installation](#installation)
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

```
git clone https://github.com/zaiderr1/Jinja2TemplateIosXE.git
cd Jinja2TemplateIosXE
pip install -r requirements.txt
```

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
├── netgen_input.xlsx        input workbook with a fictional example (README sheet inside)
├── requirements.txt         openpyxl, jinja2
├── templates/ios-xe/        master templates: main.j2, vrf.j2, interfaces.j2, bgp.j2
├── templates/ios-xr/        the same four blocks for IOS-XR
└── examples/
    ├── running-config-CE01.txt   fictional running-config
    ├── drafts/CE01/              conf2jinja.py output for it
    ├── output/                   generate.py output for netgen_input.xlsx
    └── mixed/                    IOS-XE CE + IOS-XR PE, one VRF link and one GRT link
```

## Network data and this repository

Everything in `examples/` and in the workbook is fictional: documentation
address ranges (RFC 5737) and private AS numbers.

Real running-configs, filled workbooks, `vars.yml` files and generated `.cfg`
files describe a real network. Keep them out of this repository; `.gitignore`
already excludes `output/`, `drafts/`, `*.cfg` and `vars.yml` outside `examples/`.

## Limitations

- IOS-XE and IOS-XR. `conf2jinja.py` reads IOS-XE running-configs only.
- The IOS-XR templates follow IOS-XR syntax rules but have not been committed
  on a real router by the author yet: test them in a lab first.
- IPv4 only.
- The `Vlan` column is read but not used by the templates yet.
- `conf2jinja.py` drafts need a review before they become master templates;
  they reflect one reference device.
