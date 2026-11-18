# Prompt: desktop GUI for the network config generator and a DNS name generator

> **Before sending:** review section 5 ("DNS naming rules — to confirm") and the
> default exclusions in 5.2, and adjust anything that doesn't match our guideline.
> Everything else can be sent as is.
> This prompt contains no real company data: all examples are fictional
> (RFC 5737 documentation addresses, private AS numbers, hostnames CE01/PE01).
> In this public copy, the DNS domain is the placeholder `example.intra`:
> replace it with your own domain.

---

## 1. Context

I am a network engineer. I already have a working Python command-line project that
generates Cisco IOS-XE and IOS-XR configurations from an Excel workbook. It is in
this public repository:

**https://github.com/zaiderr1/Jinja2TemplateIosXE**

If you cannot access it, stop and ask me to attach the files. Do not rewrite it
from this description.

I want you to build, on top of that project:

1. **A desktop graphical interface** so colleagues who are not developers can
   generate configurations without the command line.
2. **A new tool, in the same interface**, that reads Cisco running-configs and
   produces the DNS names of every routed interface, following our naming
   guideline `Device_Interface_dot1q.example.intra`. The user chooses which
   interface types to leave out, and can check with `nslookup` which records
   already exist in DNS before creating anything.

## 2. Hard constraints (non-negotiable)

The tool runs on a company Windows laptop inside a **closed network** with strict
data-loss prevention. Only what the existing project already uses is approved.

- **Python 3.8+**, **standard library only**, plus **`openpyxl`** and **`jinja2`**.
  No other package: no PyQt, no wxPython, no Flask, no Streamlit, no pandas,
  no requests, nothing to `pip install` beyond those two.
- **GUI: Tkinter (`tkinter` / `tkinter.ttk`)**, which ships with Python on Windows.
- **No web server, no browser, no local port, no network access** — with one
  single exception: the **"Check existing DNS"** button of the DNS tab runs the
  Windows `nslookup` command (see section 5.3). Nothing else may touch the
  network: no socket opened by the program itself, no URL, no update check, no
  telemetry. Everything except that button must work with the network cable
  unplugged, and the DNS check must fail cleanly (status `no answer`) when there
  is no network.
- **No data stored behind the user's back:** no cache, no history, no logs
  containing configurations, IP addresses or hostnames. Files are written only
  where and when the user explicitly chooses (Save / Export buttons).
  Remembering UI preferences (last folder used, window size, DNS settings) in a
  small settings file next to the program is fine.
- **Start by double-click:** provide `netgen_gui.pyw` (opens without a console
  window). `python netgen_gui.py` must also work.
- **Do not break the existing command line.** `python generate.py netgen_input.xlsx`
  and `python conf2jinja.py …` must behave exactly as today. The existing
  outputs in `examples/` must be reproduced byte for byte.
- Plain, readable code with docstrings and comments: I will maintain it myself
  and I am not a professional developer.

## 3. What already exists (reuse it, don't duplicate it)

```
generate.py              engine + command line (Excel -> configs)
conf2jinja.py            running-config -> draft Jinja2 templates
netgen_input.xlsx        input workbook (sheets: README, Links, Generate, VRFs, Devices)
templates/ios-xe/        main.j2, vrf.j2, interfaces.j2, bgp.j2
templates/ios-xr/        the same four blocks for IOS-XR
examples/                fictional inputs and expected outputs
```

The engine is already separated from the command line. **The GUI must call it,
not reimplement it:**

```python
from openpyxl import load_workbook
from generate import generate_configs

wb = load_workbook(path, data_only=True)
res = generate_configs(wb, templates=None, strict=False)
```

`res` (class `Result`) has:

| Field | Content |
|---|---|
| `res.errors`, `res.error_title` | blocking problems; when not empty, **no** config was produced |
| `res.notes` | information to show (e.g. "no interface description because Port B is empty") |
| `res.configs` | one dict per device: `host`, `os`, `blocks`, `output` (file name without .cfg), `text` (the config), `data`, `left_out` |
| `res.failed` | devices whose templates failed: `{host, lines}` |
| `res.skipped` | messages about devices not generated |
| `res.ok` | True when every selected device was generated |

Behaviour the GUI must preserve (all implemented in `generate.py` already):
both ends of each Links row get a config; empty VRF cell = global routing table
(GRT); the Generate sheet selects devices, OS (`ios-xe` / `ios-xr`) and blocks
(VRF / Interfaces / BGP); column order and header spelling are flexible; merged
cells are expanded; the whole workbook is checked first and nothing is produced
if any row is wrong, with every problem listed by sheet and row.

If a small change to `generate.py` is really needed (for example to accept a
file path or bytes), keep it minimal, keep the command line identical, and tell
me exactly what you changed and why.

## 4. The interface

One window, title **"Network Config Tools"**, two tabs. Sober, readable,
professional; Windows-native look (`ttk` with the default Windows theme). It
must stay usable at 1366×768. All text in **English**, kept in one place
(a dictionary at the top of the GUI file) so it can be translated later.

### Tab 1 — "Config generator"

**Inputs**
- *Excel file*: path field + **Browse…** button (`.xlsx`, `.xlsm`).
- *Template folder*: optional, empty by default (= standard templates chosen by
  the OS column). **Browse…** to pick another folder (e.g. drafts made with
  `conf2jinja.py`). A checkbox **"Stop on values missing from Excel"** maps to
  `strict=True`; disabled when no template folder is chosen.
- **Generate** button (also `Ctrl+Enter`). Re-running on the same file after
  editing it in Excel must re-read it from disk.

**Results**
- If `res.errors`: a clearly visible red message area with `res.error_title`
  and one line per error. Nothing else is shown. The user fixes Excel and
  clicks Generate again.
- Otherwise:
  - a summary line: "N configurations generated";
  - `res.notes` in an amber message area;
  - on the left, the **list of devices**, each with a status mark:
    green = generated, amber = generated with values left out (`left_out` not empty),
    red = failed (`res.failed`), grey = not generated (`res.skipped`, with the reason);
  - on the right, the **selected device's configuration** in a read-only,
    monospace text area (Consolas), with its file name, OS and blocks above it,
    and, if `left_out` is not empty, an amber line
    "Left out because the Excel file has no value for: …";
  - buttons: **Copy** (whole config to clipboard, confirmation in the status bar),
    **Save .cfg…** (save dialog, default name `<output>.cfg`),
    **Save all…** (choose a folder; write one `<output>.cfg` per device; if
    files already exist, list them and ask before overwriting).
- A status bar at the bottom for short confirmations ("Copied CE01.cfg").
- Long operations must not freeze the window (use a worker thread and update
  the UI with `after()`).

### Tab 2 — "DNS names"

**Inputs**
- **Add files…** (one or several running-config text files: `.txt`, `.cfg`,
  `.log`, any extension) and **Add folder…** (all text files inside).
  The list of loaded files is shown with, for each, the detected hostname and
  platform (IOS-XE / IOS-XR), and a **Remove** / **Clear** option.
- A **Paste** area as an alternative: the user can paste one running-config.
- *Domain*: text field, default `example.intra`.
- Naming options (see section 5.1), shown as simple controls and remembered
  between sessions.
- **Interface types** settings (section 5.2): the abbreviation table with an
  **Include** checkbox per type, plus the exclusion lists (interface name
  patterns, VRFs). Remembered between sessions.
- **Generate DNS names** button.

**Results**
- A table (`ttk.Treeview`), sortable by clicking a column header:
  `FQDN | IPv4 address | Device | Interface | dot1q | VRF | Status | Existing DNS`.
  Status is `OK`, or a short warning (`shutdown`, `secondary`, `duplicate name`,
  `duplicate IP`, `name too long`, `dot1q ≠ sub-interface number`,
  `unknown interface type`). *Existing DNS* is empty until the DNS check runs.
- A checkbox **Show excluded**: off by default. When on, excluded interfaces
  appear greyed out with status `excluded (<reason>)`; they are never exported.
- A summary line: "N records from M devices, K warnings, X excluded" and, after a
  DNS check, "… A to create, B already correct, C conflicts, D no answer".
- **Check existing DNS** (section 5.3): a *DNS server* field (empty = the
  laptop's default DNS server), a checkbox **Also check the generated names**
  (on by default), a progress bar "checked n / N", and a **Cancel** button.
  The window stays responsive while it runs. Double-clicking a row shows the
  raw `nslookup` output for that row, so the user can see why a result is
  `unknown`.
- Export buttons (save dialogs, never automatic):
  - **Export CSV…** — columns as in the table, UTF-8 with BOM so Excel opens it correctly;
  - **Export PowerShell…** — for Windows DNS:
    `Add-DnsServerResourceRecordA -ZoneName "<domain>" -Name "<name>" -IPv4Address "<ip>" -CreatePtr`
    for each record with status OK. If the DNS check was run: only records with
    *Existing DNS* = `none` get an active line; `same` becomes a comment
    `# already in DNS: …`; any conflict or `no answer` becomes a comment with
    the reason. Records with a warning status are always commented out.
    **Never generate commands that remove or change existing records.**
  - **Copy table** — tab-separated, to paste into Excel.

## 5. DNS naming rules — to confirm

> Defaults below. **Adjust them to our guideline before sending this prompt.**
> In the tool, each rule marked *(option)* must be a setting in the DNS tab.

### 5.1 Names

**Which interfaces get a record** (before the exclusions of 5.2)
- Every interface with an IPv4 address: IOS-XE `ip address A.B.C.D M.M.M.M`,
  IOS-XR `ipv4 address A.B.C.D M.M.M.M` or `ipv4 address A.B.C.D/NN`.
- Skipped: `no ip address`, `ip unnumbered`, `ip address dhcp`, `ipv4 unnumbered`,
  interfaces with no IP.
- Secondary addresses: not given a record by default, listed with status
  `secondary` *(option: include them, with suffix `-sec1`, `-sec2`…)*.
- Shutdown interfaces: included, with status `shutdown` *(option: exclude them)*.
- Management interfaces (e.g. `GigabitEthernet0/0` in `Mgmt-intf`,
  `MgmtEth0/RP0/CPU0/0`) are included like any other, unless excluded in 5.2.

**Name format:** `<Device>_<Interface>_<dot1q>.<domain>`
- `<Device>`: the value of the `hostname` line, as written.
- `<Interface>`: the **short** interface name, without the sub-interface number,
  with `/` replaced by `-` and any other character outside `A-Z a-z 0-9 -`
  replaced by `-`. The interface type is replaced by its abbreviation, the
  numbers are kept: `TwentyFiveGigE1/0/1.100` → `Twe1-0-1`,
  `TenGigE0/0/0/1.100` → `Te0-0-0-1`, `Loopback0` → `Lo0`.

  Abbreviation table (both the long and the short spellings Cisco uses in
  running-configs must be recognized, case-insensitively):

  | Interface type in the config | Abbreviation | Include (5.2) |
  |---|---|---|
  | `TenGigabitEthernet`, `TenGigE` | `Te` | yes |
  | `TwentyFiveGigabitEthernet`, `TwentyFiveGigE` | `Twe` | yes |
  | `FortyGigabitEthernet`, `FortyGigE` | `Fo` | yes |
  | `HundredGigabitEthernet`, `HundredGigE` | `Hu` | yes |
  | `GigabitEthernet` | `Gi` | yes |
  | `FastEthernet` | `Fa` | yes |
  | `Ethernet` | `Et` | yes |
  | `Loopback` | `Lo` | yes |
  | `Vlan` | `Vl` | yes |
  | `Tunnel` | `Tu` | yes |
  | `Port-channel` | `Po` | yes |
  | `Bundle-Ether` | `BE` | yes |
  | `MgmtEth` | `Mg` | yes |

  The table must be **editable in the DNS tab settings** (add, change or remove
  a row, tick or untick *Include*) and saved with the other settings. Match the **longest** type name
  first, so `TenGigabitEthernet` is not read as `Ethernet`. An interface type
  that is not in the table keeps its full name and gets the status
  `unknown interface type`, so the user can add it to the table.
- `<dot1q>`: for a sub-interface, the value of `encapsulation dot1Q <n>`
  (IOS-XE) / `encapsulation dot1q <n>` (IOS-XR); if that line is missing,
  the sub-interface number. If both exist and differ, use the encapsulation
  value and set status `dot1q ≠ sub-interface number`.
- Interfaces that are not sub-interfaces (Loopback, Vlan, Tunnel, physical,
  Port-channel, Bundle-Ether…): **no `_<dot1q>` part**, e.g. `CE01_Lo0.example.intra`.
- Case: kept as in the configuration *(option: all lower case)*.
- Each label (part between dots) must be 63 characters or fewer; longer names
  get status `name too long`.

**Duplicates:** the same FQDN generated twice, or the same IP address on two
interfaces (across all loaded files), both get a warning status on every
affected row. The VRF column is informational only (IOS-XE `vrf forwarding X`,
IOS-XR `vrf X`; empty = global table).

### 5.2 Interfaces to leave out

The user decides which interfaces get no DNS record. Three independent rules;
an interface matching any of them is **excluded**:

1. **By type** — the *Include* checkbox of each row of the abbreviation table.
   Unticking `Loopback` excludes every `Loopback…` interface. Interfaces whose
   type is not in the table are included (and flagged `unknown interface type`)
   *(option: exclude unknown types)*.
2. **By interface name** — a list of patterns, one per line, with `*` as
   wildcard, matched case-insensitively against the full interface name as
   written in the config (`GigabitEthernet0/0`, `MgmtEth*`, `*.4094`,
   `Tunnel1*`). Empty by default.
3. **By VRF** — a list of VRF names (`Mgmt-intf`, `mgmt`…): interfaces in those
   VRFs are excluded. Empty by default.

Excluded interfaces get no record, are not counted in duplicates, are not
checked in DNS, and are never exported. With **Show excluded** on, they appear
greyed out with status `excluded (type Lo)`, `excluded (pattern MgmtEth*)` or
`excluded (VRF Mgmt-intf)`, so the user can check the rules do what they expect.

### 5.3 Checking existing DNS records (nslookup)

**Read-only.** The tool only asks DNS; it never creates, changes or deletes a
record. It runs only when the user clicks **Check existing DNS**, for the
records currently in the table (not excluded, not `secondary` unless included).

For each record:

1. **Reverse lookup of the IP:** run
   `nslookup -type=PTR <ip> [<dns server>]`.
2. **Forward lookup of the generated name** (if *Also check the generated
   names* is ticked): run `nslookup -type=A <fqdn> [<dns server>]`.

How to run `nslookup`:
- `subprocess.run([...], capture_output=True, text=True, timeout=5)` with an
  **argument list, never `shell=True`**; on Windows pass
  `creationflags=subprocess.CREATE_NO_WINDOW` so no console window flashes.
- At most **8 lookups at a time**; the **Cancel** button stops launching new
  ones. Timeout per lookup: 5 seconds (setting).
- Values are validated before being passed to `nslookup`: the IP with
  `ipaddress`, the FQDN against `[A-Za-z0-9_.-]`, the DNS server as an IP or a
  host name. Nothing else ever goes on the command line.

How to read the output (Windows may be in English or French, so do not rely on
localized words except where listed; keep these word lists in one place in the
code so they can be extended):
- **PTR:** every line containing `name =` gives a PTR name (text after `=`,
  trailing dot removed). This part is not translated by Windows.
- **A:** skip everything up to the line that contains the queried FQDN
  (case-insensitive); every IPv4 address after that line is an answer. The
  lines before it describe the DNS server itself and must be ignored.
- **Not found:** no answer and the output contains `Non-existent domain`,
  `NXDOMAIN`, `can't find` or `ne parvient pas à trouver`.
- **No answer:** the 5-second timeout, `timed out` / `expiré` in the output, or
  `nslookup` not available.
- Anything else: `unknown` (the raw output is kept in memory so the user can
  look at it by double-clicking the row; it is never written to disk unless
  exported).

Result shown in the **Existing DNS** column:

| Value | Meaning | PowerShell export |
|---|---|---|
| `none` | no PTR for the IP, and the name doesn't exist | active `Add-…` line |
| `same` | the PTR is the generated FQDN (case-insensitive) and the name points to this IP | comment: already in DNS |
| `PTR: <other name>` | the IP already has another name | comment: conflict |
| `name → <other IP>` | the generated name already points to another IP | comment: conflict |
| `multiple PTR: …` | several names for the IP | comment: conflict |
| `no answer` | timeout, no network, DNS server unreachable | comment: not checked |
| `unknown` | output not understood | comment: check manually |

When forward checking is off, `same` / `none` are based on the PTR only.

### 5.4 Parsing

**Parsing:** a running-config is plain text; sections start with a line that has
no leading space (`interface …`) and contain the indented lines below it. Ignore
the "Building configuration…" / "Current configuration" header lines, banners
(text between `banner xxx ^C` and the closing delimiter), and `!` lines.
Detect IOS-XR by `ipv4 address` lines or interface names in `0/0/0/0` form.
A file without a `hostname` line is reported and skipped.

## 6. Data safety

- No network access, no telemetry, no update check (see section 2). The only
  exception is `nslookup`, run only on the user's click, only towards the DNS
  server configured on the laptop or the one typed in the *DNS server* field.
- Loaded configurations and Excel data stay in memory only. Nothing is written
  except through the user's explicit Save / Export actions.
- Never print configuration content, IPs or hostnames to a log file. Errors
  shown to the user may contain them (they are on screen only).
- Passwords and keys found in running-configs are never displayed or exported
  by the DNS tool (it only reads hostnames, interfaces and addresses).

## 7. Quality and tests

Use only `unittest` (standard library). All test data must be fictional.

**Config generator** (through the GUI's code path, without opening a window):
- Loading `netgen_input.xlsx` and generating must reproduce
  `examples/output/CE01.cfg` and `PE01.cfg` byte for byte.
- `examples/mixed/netgen_input_xe_xr_grt.xlsx` must reproduce
  `examples/mixed/output/*.cfg` byte for byte (IOS-XE + IOS-XR + GRT).
- A workbook with an IP outside its subnet must give errors and no config.

**DNS tool** — this input:

```
hostname CE01
!
interface Loopback0
 ip address 203.0.113.1 255.255.255.255
!
interface TwentyFiveGigE1/0/1
 no ip address
!
interface TwentyFiveGigE1/0/1.100
 description # PE01 - TenGigE0/0/0/1.100 - ( underlay ) #
 encapsulation dot1Q 100
 vrf forwarding underlay
 ip address 192.0.2.0 255.255.255.254
!
interface TwentyFiveGigE1/0/2.300
 encapsulation dot1Q 300
 ip address 198.51.100.0 255.255.255.254
 shutdown
!
interface Vlan10
 ip address 198.51.100.65 255.255.255.192
 ip address 198.51.100.126 255.255.255.192 secondary
!
end
```

with default settings and domain `example.intra` must give:

| FQDN | IPv4 | Interface | dot1q | VRF | Status |
|---|---|---|---|---|---|
| CE01_Lo0.example.intra | 203.0.113.1 | Loopback0 | | | OK |
| CE01_Twe1-0-1_100.example.intra | 192.0.2.0 | TwentyFiveGigE1/0/1.100 | 100 | underlay | OK |
| CE01_Twe1-0-2_300.example.intra | 198.51.100.0 | TwentyFiveGigE1/0/2.300 | 300 | | shutdown |
| CE01_Vl10.example.intra | 198.51.100.65 | Vlan10 | | | OK |
| *(not a record)* | 198.51.100.126 | Vlan10 | | | secondary |

and this IOS-XR input:

```
hostname PE01
interface TenGigE0/0/0/1.100
 description # CE01 - TwentyFiveGigE1/0/1.100 - ( underlay ) #
 vrf underlay
 ipv4 address 192.0.2.1 255.255.255.254
 encapsulation dot1q 100
!
interface Bundle-Ether10.200
 ipv4 address 198.51.100.129/31
 encapsulation dot1q 201
!
```

must give `PE01_Te0-0-0-1_100.example.intra → 192.0.2.1` (VRF `underlay`, OK)
and `PE01_BE10_201.example.intra → 198.51.100.129` with status
`dot1q ≠ sub-interface number`.

Exclusions (5.2): unticking `Loopback` excludes `CE01_Lo0` (status
`excluded (type Lo)`, not exported, not in duplicates); the pattern
`TwentyFiveGigE1/0/2*` excludes the shutdown sub-interface; the VRF `underlay`
excludes `CE01_Twe1-0-1_100`; an unknown type is included by default and
excluded when *exclude unknown types* is set.

DNS check (5.3): **tests never run a real DNS query.** The lookup function must
be injectable, so tests replace it with fake `nslookup` outputs. Test at least:
- English PTR found: a line `1.113.0.203.in-addr.arpa	name = CE01_Lo0.example.intra`
  → `same` when it equals the generated FQDN, `PTR: <name>` otherwise;
- French outputs (`Serveur :`, `Nom :`, `Adresse :` lines) with the same
  `name =` line → same results;
- not found (`*** dns01 can't find 203.0.113.9: Non-existent domain`, and the
  French `ne parvient pas à trouver`) → `none`;
- A lookup where the DNS server's own address (`Address:  198.51.100.53`)
  appears before the queried name: it must **not** be taken as the answer;
- generated name resolving to another IP → `name → <ip>`; two PTR lines →
  `multiple PTR`; timeout → `no answer`;
- the PowerShell export after a check: only `none` rows are active lines;
- an IP or name containing anything unexpected is refused before reaching
  `nslookup`.

Also test: the long and short spellings of each type in the abbreviation table
(`TenGigabitEthernet0/1` and `TenGigE0/1` both give `Te0-1`), an interface type
missing from the table (full name kept, status `unknown interface type`),
duplicate FQDN, duplicate IP across two files, a 64-character label, a file
without hostname, the PowerShell export lines, the CSV export opened as UTF-8
with BOM.

## 8. Deliverables

```
netgen_gui.py            the GUI (both tabs)
netgen_gui.pyw           double-click launcher (imports and runs netgen_gui)
dns_names.py             DNS engine: parse configs -> records, exclusions,
                         nslookup check (lookup function injectable); no GUI code.
                         Usable alone:
                           python dns_names.py <files...> --domain example.intra --csv out.csv
                           [--exclude-type Lo --exclude-type Tu] [--exclude-name "MgmtEth*"]
                           [--exclude-vrf Mgmt-intf] [--check-dns] [--dns-server 198.51.100.53]
tests/                   unittest tests described above
README.md                updated: a "Graphical interface" section and a "DNS names" section
```

Keep `generate.py`, `conf2jinja.py`, the templates and the examples unchanged
unless section 3 applies. Update `requirements.txt` only if needed — it should
stay `openpyxl` and `jinja2`.

## 9. Don't

- Don't add any dependency, framework, build step, installer or web technology.
- Don't reimplement the config generation logic: call `generate_configs()`.
- Don't invent DNS rules beyond section 5: if something is ambiguous, ask me.
- Don't create, modify or delete DNS records from the tool: it only reads DNS
  and writes PowerShell for a human to review and run.
- Don't use `shell=True`, `os.system` or any network access other than `nslookup`.
- Don't use real-looking company data in code, tests or examples.
