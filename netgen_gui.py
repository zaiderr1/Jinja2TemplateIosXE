"""Network Config Tools - desktop interface (Tkinter / ttk).

Three tabs:
  * "Config generator": Excel workbook -> Cisco configurations, through
    generate.generate_configs() (called via netgen_core.generate_kit_from_path),
    with, for each device, its rollback and verification commands
    (change_kit.py), and "Save change kit..." for all files at once.
  * "DNS names": Cisco running-configs -> DNS records, through dns_names.py.
  * "Rollback": running-config before + change -> rollback, through rollback.py.

Rules followed by this file:
  * No business logic here: generation, parsing, naming rules, exports and
    preferences live in netgen_core.py and dns_names.py.
  * Importing this module has no side effect: no window, no work, no write.
    Everything starts in main().
  * Long work runs in a background thread (netgen_core.BackgroundJob). The
    thread never touches a widget: the window polls it with after().
  * Nothing is written to disk except by the Save / Export buttons (to the
    place the user chose) and the small preferences file (no config data).
  * Every visible text is in the TEXT dictionary below, for translation.

Run with:  python netgen_gui.py   (or double-click netgen_gui.pyw)
"""

import os
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import change_kit
import dns_check
import dns_names
import netgen_core
import rollback


# ---------------------------------------------------------------------------
# All visible texts (English). Translate here only.
# ---------------------------------------------------------------------------
TEXT = {
    "app_title": "Network Config Tools",
    "tab_generator": "Config generator",
    "tab_dns": "DNS names",
    "browse": "Browse...",
    "clear": "Clear",
    "ready": "Ready",
    "working": "Working...",
    "settings_not_saved": "Preferences could not be saved (folder is read-only?).",
    "unexpected_error": "Unexpected error",

    # --- Config generator tab ---
    "excel_file": "Excel file:",
    "template_folder": "Template folder:",
    "template_hint": "(optional - empty = standard templates chosen by the OS column)",
    "strict": "Stop on values missing from Excel",
    "generate": "Generate",
    "generate_hint": "Ctrl+Enter",
    "pick_excel": "Choose the Excel workbook",
    "excel_types": "Excel workbook",
    "all_files": "All files",
    "pick_templates": "Choose a template folder",
    "no_excel": "Choose an Excel file first.",
    "n_generated": "{n} configurations generated",
    "one_generated": "1 configuration generated",
    "devices": "Devices",
    "col_device": "Device",
    "col_status": "Status",
    "st_generated": "generated",
    "st_left_out": "generated, values left out",
    "st_failed": "failed",
    "st_skipped": "not generated",
    "skipped_label": "(not generated)",
    "file_info": "File: {file}    OS: {os}    Blocks: {blocks}",
    "no_blocks": "none",
    "left_out": "Left out because the Excel file has no value for: {names}",
    "failed_title": "Template failed for {host}:",
    "copy": "Copy",
    "save_cfg": "Save .cfg...",
    "save_all": "Save all...",
    "copied": "Copied {name}",
    "saved": "Saved {name}",
    "saved_all": "Saved {n} file(s) in {folder}",
    "save_failed": "Could not save the file:\n{err}",
    "pick_save_folder": "Choose the folder for all configurations",
    "cfg_types": "Configuration",
    "overwrite_title": "Replace existing files?",
    "overwrite_msg": "These files already exist and will be replaced:\n\n{files}\n\nReplace them?",
    "save_cancelled": "Nothing saved.",
    "unsafe_names": "These configurations were not saved (invalid file name):\n\n{items}",
    "nothing_to_save": "No configuration to save.",
    "generating": "Reading the Excel file and generating...",
    "gen_done": "Done.",
    "gen_errors": "Fix the Excel file and click Generate again.",
    "running_folder": "Running-configs:",
    "running_hint": "(optional - folder of 'show running-config' taken before the change,"
                    " for exact rollbacks)",
    "pick_running": "Choose the folder of running-configs taken before the change",
    "view_config": "Config",
    "view_rollback": "Rollback",
    "view_checks": "Checks",
    "rb_from": "Rollback built from {file}",
    "rb_without": "Rollback built WITHOUT running-config: only sub-interfaces and BGP "
                  "neighbors are removed, the rest is in '! REVIEW' comments",
    "rb_reviews": "{n} point(s) to review - search for '! REVIEW' in the rollback",
    "save_kit": "Save change kit...",
    "pick_kit_folder": "Choose the folder for the change kit",
    "kit_saved": "Change kit: {n} file(s) saved in {folder}",
    "kit_problems": "Change kit:",

    # --- Rollback tab ---
    "tab_rollback": "Rollback",
    "rb_before": "Running-config BEFORE the change (optional):",
    "rb_change": "Change to apply (indented, as you paste it on the device):",
    "load_file": "Load file...",
    "generate_rb": "Generate rollback",
    "rb_result": "Rollback",
    "rb_no_change": "Paste or load the change first.",
    "rb_nothing": "Nothing to roll back: the change is already in the running-config.",
    "rb_done": "Rollback generated ({platform}).",
    "rb_done_reviews": "Rollback generated ({platform}) - {n} point(s) to review.",
    "rb_error": "The change cannot be read:",
    "rb_platform": "Platform:",
    "rb_auto": "auto",
    "save_rb": "Save...",
    "pick_text": "Choose a configuration file",
    "rb_types": "Configuration / text",

    # --- DNS names tab ---
    "add_files": "Add files...",
    "add_folder": "Add folder...",
    "remove": "Remove",
    "loaded_files": "Running-configs",
    "col_file": "File",
    "col_hostname": "Hostname",
    "col_platform": "Platform",
    "no_hostname": "(no hostname - skipped)",
    "paste_label": "Or paste one running-config:",
    "pasted_source": "(pasted)",
    "domain": "Domain:",
    "opt_secondary": "Give secondary addresses a record (-sec1, -sec2...)",
    "opt_shutdown": "Leave out shutdown interfaces",
    "opt_lower": "All lower case",
    "abbrev_button": "Interface types...",
    "leave_out": "Leave out (no DNS record):",
    "excl_patterns": "Interface names:",
    "excl_patterns_hint": "comma separated, * = any characters, e.g. MgmtEth*, *.4094",
    "excl_vrfs": "VRFs:",
    "excl_vrfs_hint": "comma separated, e.g. Mgmt-intf",
    "opt_unknown": "Leave out interface types that are not in the table",
    "excl_types_n": "Types left out: {types}",
    "excl_types_none": "Types left out: none (choose in Interface types...)",
    "show_excluded": "Show left-out interfaces",
    "regen_needed": "Settings changed: click Generate DNS names again.",
    "check_dns": "Check existing DNS",
    "dns_server": "DNS server:",
    "dns_server_hint": "(empty = this computer's DNS server)",
    "check_forward": "Also check the generated names",
    "check_running": "Checking DNS: {done} / {total}",
    "check_done": "DNS check done ({n} record(s)).",
    "check_cancelled": "DNS check cancelled after {n} record(s).",
    "check_nothing": "No record to check: generate DNS names first.",
    "check_bad_server": "DNS server not valid: {err}",
    "check_info": "The check only asks DNS (nslookup); it never creates or changes records.",
    "raw_title": "nslookup output",
    "raw_none": "This row has not been checked.",
    "generate_dns": "Generate DNS names",
    "pick_configs": "Choose running-config files",
    "config_types": "Text files",
    "pick_config_folder": "Choose a folder of running-configs",
    "loading": "Reading files...",
    "loaded_n": "{n} file(s) added",
    "already_loaded": "{n} file(s) already in the list were ignored",
    "no_input": "Add running-config files or paste a configuration first.",
    "export_csv": "Export CSV...",
    "export_ps": "Export PowerShell...",
    "copy_table": "Copy table",
    "csv_types": "CSV (UTF-8)",
    "ps_types": "PowerShell script",
    "exported": "Exported {name}",
    "table_copied": "Table copied ({n} rows)",
    "nothing_to_export": "Generate DNS names first.",
    "problems_title": "Problems:",
    "dns_working": "Generating DNS names...",

    # --- Abbreviation editor ---
    "abbrev_title": "Interface types",
    "abbrev_type": "Interface type in the config",
    "abbrev_short": "Abbreviation",
    "abbrev_include": "DNS record",
    "abbrev_toggle": "Include / leave out",
    "abbrev_hint": "Double-click the 'DNS record' column, or use the button, to leave a type out.",
    "yes_mark": "yes",
    "no_mark": "left out",
    "add": "Add...",
    "edit": "Change...",
    "reset": "Reset to defaults",
    "ok": "OK",
    "cancel": "Cancel",
    "abbrev_empty": "Both fields are required.",
    "abbrev_dup": "This interface type is already in the table.",
    "abbrev_row_title": "Abbreviation",
}

# Colours used for statuses (text colour, never the only signal).
GREEN = "#1b7a2b"
AMBER = "#9a6000"
AMBER_BG = "#fff4d6"
RED = "#b00020"
RED_BG = "#fde7ea"
GREY = "#6b6b6b"

POLL_MS = 100  # how often the window checks a background job


# ---------------------------------------------------------------------------
# Plain functions (no window needed: usable from tests)
# ---------------------------------------------------------------------------
def run_generation(excel_path, template_folder="", strict=False):
    """Re-read the workbook from disk and run generate_configs().

    The real work is netgen_core.generate_from_path(): it opens the file with
    load_workbook(data_only=True) on every call, closes it, and calls
    generate.generate_configs(). "strict" only matters with a template folder,
    exactly like the command line.
    """
    templates = (template_folder or "").strip() or None
    return netgen_core.generate_from_path(
        excel_path, templates=templates, strict=bool(strict and templates))


def run_generation_kit(excel_path, template_folder="", strict=False,
                       running_folder="", dns_settings=None):
    """Worker function: like run_generation(), plus the change kit
    (rollback, checks, DNS records) of every device.
    Returns (result, {host: KitEntry}, problems)."""
    templates = (template_folder or "").strip() or None
    return netgen_core.generate_kit_from_path(
        excel_path, templates=templates, strict=bool(strict and templates),
        running_folder=(running_folder or "").strip(), dns_settings=dns_settings)


def rollback_info(entry):
    """Line shown above a rollback in the generator tab."""
    lines = [TEXT["rb_from"].format(file=entry.running_source) if entry.running_source
             else TEXT["rb_without"]]
    if entry.rollback is not None and entry.rollback.reviews:
        lines.append(TEXT["rb_reviews"].format(n=len(entry.rollback.reviews)))
    return "\n".join(lines)


def make_rollback(before_text, change_text, platform=""):
    """Worker function for the Rollback tab: returns a rollback.RollbackResult.
    Raises rollback.FormatError when the change is not indented."""
    return rollback.build_rollback(before_text if before_text.strip() else None,
                                   change_text, before_name="running-config (before)",
                                   change_name="change", platform=platform or None)


def config_header(config):
    """Text shown above a configuration: file name, OS and blocks."""
    blocks = [name for name, on in (config.get("blocks") or {}).items() if on]
    return TEXT["file_info"].format(
        file=config["output"] + ".cfg",
        os=config.get("os", ""),
        blocks=", ".join(blocks) if blocks else TEXT["no_blocks"])


def dns_settings_from_prefs(prefs):
    """Build a dns_names.DnsSettings object from the "dns" preferences."""
    d = prefs.get("dns", {})
    return dns_names.DnsSettings(
        domain=d.get("domain") or "example.intra",
        include_secondary=bool(d.get("include_secondary")),
        exclude_shutdown=bool(d.get("exclude_shutdown")),
        lowercase=bool(d.get("lowercase")),
        abbreviations=[tuple(p) for p in d.get("abbreviations")
                       or dns_names.DEFAULT_ABBREVIATIONS],
        excluded_types=list(d.get("excluded_types") or []),
        exclude_unknown_types=bool(d.get("exclude_unknown_types")),
        exclude_patterns=list(d.get("exclude_patterns") or []),
        exclude_vrfs=list(d.get("exclude_vrfs") or []))


def split_list(text):
    """'MgmtEth*, *.4094' -> ['MgmtEth*', '*.4094'] (commas or semicolons)."""
    return [p.strip() for p in re.split(r"[,;]", text or "") if p.strip()]


def check_dns(records, server, forward, cancel, progress):
    """Worker function: read-only DNS check of the records (dns_check.py).
    Returns how many records were checked."""
    return dns_check.check_records(records, server, forward=forward, cancel=cancel,
                                   progress=progress)


def load_paths(paths):
    """Worker function: read and parse files (no Tk here)."""
    return [dns_names.load_file(p) for p in paths]


def load_folder(folder):
    """Worker function: list the text files of a folder and parse them."""
    files = dns_names.find_text_files(folder)
    return [(str(p), dns_names.load_file(p)) for p in files]


def build_dns(loaded, pasted_text, settings):
    """Worker function: files (+ pasted text) -> DnsResult."""
    files = list(loaded)
    if pasted_text.strip():
        files.append(dns_names.parse_config(pasted_text, TEXT["pasted_source"]))
    return dns_names.build_records(files, settings)


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------
class StatusBar(ttk.Frame):
    """One line at the bottom of the window for short confirmations."""

    def __init__(self, master):
        super().__init__(master, relief="sunken", padding=(6, 2))
        self.var = tk.StringVar(value=TEXT["ready"])
        ttk.Label(self, textvariable=self.var, anchor="w").pack(fill="x")

    def set(self, message):
        self.var.set(message)


def make_scrolled_text(master, height=6, font=None):
    """A read-only Text widget with a vertical scrollbar, in a frame."""
    frame = ttk.Frame(master)
    text = tk.Text(frame, height=height, wrap="none", font=font,
                   state="disabled", relief="solid", borderwidth=1)
    ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    text.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, text


def set_text(widget, content, tag=None):
    """Replace the content of a read-only Text widget."""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    if content:
        widget.insert("1.0", content, tag or ())
    widget.configure(state="disabled")


class JobRunner:
    """Run one background job at a time for a tab and poll it with after().

    A newer job replaces an older one: the result of the old job is ignored.
    Callbacks run on the Tk main thread only.
    """

    def __init__(self, widget):
        self.widget = widget
        self.job_id = 0
        self.busy = False

    def run(self, func, args, on_done, on_error):
        self.job_id += 1
        my_id = self.job_id
        job = netgen_core.BackgroundJob(func, *args)
        job.start()
        self.busy = True

        def poll():
            if my_id != self.job_id:
                return  # replaced by a newer job
            msg = job.poll()
            if msg is None:
                self.widget.after(POLL_MS, poll)
                return
            self.busy = False
            kind, value = msg
            if kind == "done":
                on_done(value)
            else:
                on_error(value)

        self.widget.after(POLL_MS, poll)


# ---------------------------------------------------------------------------
# Tab 1 - Config generator
# ---------------------------------------------------------------------------
class GeneratorTab(ttk.Frame):
    """Excel workbook -> configurations, with preview, copy and save."""

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.jobs = JobRunner(self)
        self.result = None
        self.rows = []  # netgen_core.device_rows(result)
        self.kit = {}   # {host: change_kit.KitEntry}
        self.kit_domain = "example.intra"

        prefs = app.prefs
        self.excel_var = tk.StringVar()
        self.tpl_var = tk.StringVar()
        self.running_var = tk.StringVar()
        self.view_var = tk.StringVar(value="config")
        self.strict_var = tk.BooleanVar(
            value=bool(prefs.get("generator", {}).get("strict")))

        self._build_inputs()
        self._build_results()
        self.tpl_var.trace_add("write", lambda *a: self._update_strict())
        self._update_strict()
        self._show_nothing()

    # ---- layout ----------------------------------------------------------
    def _build_inputs(self):
        box = ttk.Frame(self)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text=TEXT["excel_file"]).grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.excel_var).grid(
            row=0, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(box, text=TEXT["browse"], command=self._browse_excel).grid(
            row=0, column=2, pady=2)

        ttk.Label(box, text=TEXT["template_folder"]).grid(row=1, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.tpl_var).grid(
            row=1, column=1, sticky="ew", padx=4, pady=2)
        tb = ttk.Frame(box)
        tb.grid(row=1, column=2, pady=2)
        ttk.Button(tb, text=TEXT["browse"], command=self._browse_templates).pack(side="left")
        ttk.Button(tb, text=TEXT["clear"], command=lambda: self.tpl_var.set("")).pack(
            side="left", padx=(4, 0))
        ttk.Label(box, text=TEXT["template_hint"], foreground=GREY).grid(
            row=2, column=1, sticky="w", padx=4)

        ttk.Label(box, text=TEXT["running_folder"]).grid(row=3, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.running_var).grid(
            row=3, column=1, sticky="ew", padx=4, pady=2)
        rb = ttk.Frame(box)
        rb.grid(row=3, column=2, pady=2)
        ttk.Button(rb, text=TEXT["browse"], command=self._browse_running).pack(side="left")
        ttk.Button(rb, text=TEXT["clear"], command=lambda: self.running_var.set("")).pack(
            side="left", padx=(4, 0))
        ttk.Label(box, text=TEXT["running_hint"], foreground=GREY).grid(
            row=4, column=1, sticky="w", padx=4)

        line = ttk.Frame(box)
        line.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.strict_check = ttk.Checkbutton(
            line, text=TEXT["strict"], variable=self.strict_var,
            command=self.app.save_prefs_quietly)
        self.strict_check.pack(side="left")
        self.gen_button = ttk.Button(line, text=TEXT["generate"], command=self.generate)
        self.gen_button.pack(side="right")
        ttk.Label(line, text=TEXT["generate_hint"], foreground=GREY).pack(
            side="right", padx=6)

    def _build_results(self):
        # Red (errors) or amber (notes) message area.
        self.msg_frame, self.msg_text = make_scrolled_text(self, height=5)
        self.msg_text.configure(wrap="word")
        self.msg_text.tag_configure("title", font=("Segoe UI", 10, "bold"))

        self.summary_var = tk.StringVar()
        self.summary = ttk.Label(self, textvariable=self.summary_var,
                                 font=("Segoe UI", 10, "bold"))

        # Device list (left) / preview (right).
        self.paned = ttk.PanedWindow(self, orient="horizontal")
        left = ttk.Frame(self.paned)
        self.dev_tree = ttk.Treeview(left, columns=("device", "status"),
                                     show="headings", selectmode="browse")
        self.dev_tree.heading("device", text=TEXT["col_device"])
        self.dev_tree.heading("status", text=TEXT["col_status"])
        self.dev_tree.column("device", width=150, stretch=True)
        self.dev_tree.column("status", width=150, stretch=True)
        for tag, colour in (("ok", GREEN), ("left_out", AMBER),
                            ("failed", RED), ("skipped", GREY)):
            self.dev_tree.tag_configure(tag, foreground=colour)
        ys = ttk.Scrollbar(left, orient="vertical", command=self.dev_tree.yview)
        self.dev_tree.configure(yscrollcommand=ys.set)
        self.dev_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.dev_tree.bind("<<TreeviewSelect>>", lambda e: self._show_selected())

        right = ttk.Frame(self.paned)
        views = ttk.Frame(right)
        views.pack(fill="x", pady=(0, 2))
        for value, key in (("config", "view_config"), ("rollback", "view_rollback"),
                           ("checks", "view_checks")):
            ttk.Radiobutton(views, text=TEXT[key], value=value, variable=self.view_var,
                            command=self._show_selected).pack(side="left", padx=(0, 8))
        self.info_var = tk.StringVar()
        ttk.Label(right, textvariable=self.info_var, justify="left").pack(fill="x")
        self.left_out_var = tk.StringVar()
        self.left_out_label = ttk.Label(right, textvariable=self.left_out_var,
                                        foreground=AMBER, wraplength=700)
        self.left_out_label.pack(fill="x")
        frame, self.preview = make_scrolled_text(right, height=10,
                                                 font=("Consolas", 10))
        self.preview.tag_configure("failed", foreground=RED)
        self.preview.tag_configure("skipped", foreground=GREY)
        frame.pack(fill="both", expand=True, pady=(2, 4))
        buttons = ttk.Frame(right)
        buttons.pack(fill="x")
        self.copy_button = ttk.Button(buttons, text=TEXT["copy"], command=self.copy)
        self.save_button = ttk.Button(buttons, text=TEXT["save_cfg"], command=self.save_one)
        self.save_all_button = ttk.Button(buttons, text=TEXT["save_all"],
                                          command=self.save_all)
        self.save_kit_button = ttk.Button(buttons, text=TEXT["save_kit"],
                                          command=self.save_kit)
        self.copy_button.pack(side="left")
        self.save_button.pack(side="left", padx=4)
        self.save_all_button.pack(side="left")
        self.save_kit_button.pack(side="left", padx=4)

        self.paned.add(left, weight=1)
        self.paned.add(right, weight=3)

    # ---- inputs ----------------------------------------------------------
    def _update_strict(self):
        """The strict option only makes sense with a custom template folder."""
        if self.tpl_var.get().strip():
            self.strict_check.state(["!disabled"])
        else:
            self.strict_check.state(["disabled"])

    def _browse_excel(self):
        path = filedialog.askopenfilename(
            parent=self, title=TEXT["pick_excel"],
            initialdir=self.app.last_dir("excel"),
            filetypes=[(TEXT["excel_types"], "*.xlsx *.xlsm"), (TEXT["all_files"], "*.*")])
        if path:
            self.excel_var.set(path)
            self.app.remember_dir("excel", os.path.dirname(path))

    def _browse_templates(self):
        path = filedialog.askdirectory(parent=self, title=TEXT["pick_templates"],
                                       initialdir=self.app.last_dir("templates"))
        if path:
            self.tpl_var.set(path)
            self.app.remember_dir("templates", path)

    def _browse_running(self):
        path = filedialog.askdirectory(parent=self, title=TEXT["pick_running"],
                                       initialdir=self.app.last_dir("running"))
        if path:
            self.running_var.set(path)
            self.app.remember_dir("running", path)

    # ---- generation ------------------------------------------------------
    def generate(self):
        excel = self.excel_var.get().strip()
        if not excel:
            self.app.status.set(TEXT["no_excel"])
            return
        if self.jobs.busy:
            return
        self.gen_button.state(["disabled"])
        self.app.status.set(TEXT["generating"])
        settings = self.app.dns.current_settings()
        self.kit_domain = settings.domain    # the DNS names of the kit use it
        self.jobs.run(run_generation_kit,
                      (excel, self.tpl_var.get(), self.strict_var.get(),
                       self.running_var.get(), settings),
                      self._on_result, self._on_crash)

    def _on_crash(self, message):
        self.gen_button.state(["!disabled"])
        self._show_errors(TEXT["unexpected_error"], [message])
        self.app.status.set(TEXT["unexpected_error"])

    def _on_result(self, value):
        res, kit, problems = value
        self.gen_button.state(["!disabled"])
        if res.errors:
            self._show_errors(res.error_title, res.errors)
            self.app.status.set(TEXT["gen_errors"])
            return
        self._show_result(res, kit, problems)
        self.app.status.set(TEXT["gen_done"])

    # ---- display ---------------------------------------------------------
    def _clear_results(self):
        self.result = None
        self.rows = []
        self.kit = {}
        self.dev_tree.delete(*self.dev_tree.get_children())
        self.info_var.set("")
        self.left_out_var.set("")
        set_text(self.preview, "")
        self.summary_var.set("")
        self.msg_frame.pack_forget()
        self.summary.pack_forget()
        self.paned.pack_forget()
        self._update_buttons()

    def _show_nothing(self):
        self._clear_results()

    def _show_errors(self, title, errors):
        """Blocking problems: only the red area, previous results erased."""
        self._clear_results()
        self.msg_text.configure(background=RED_BG, foreground=RED)
        set_text(self.msg_text, "")
        self.msg_text.configure(state="normal")
        self.msg_text.insert("end", (title or "") + "\n", "title")
        self.msg_text.insert("end", "\n".join(errors))
        self.msg_text.configure(state="disabled", height=min(20, len(errors) + 2))
        self.msg_frame.pack(fill="both", expand=True, pady=(8, 0))

    def _show_result(self, res, kit=None, problems=()):
        self._clear_results()
        self.result = res
        self.kit = kit or {}
        n = len(res.configs)
        self.summary_var.set(TEXT["one_generated"] if n == 1
                             else TEXT["n_generated"].format(n=n))
        self.summary.pack(fill="x", pady=(8, 2))
        notes = list(res.notes) + [TEXT["kit_problems"] + " " + p for p in problems]
        if notes:
            self.msg_text.configure(background=AMBER_BG, foreground=AMBER,
                                    height=min(5, len(notes)))
            set_text(self.msg_text, "\n".join(notes))
            self.msg_frame.pack(fill="x", pady=(0, 4))
        self.paned.pack(fill="both", expand=True, pady=(4, 0))

        self.rows = netgen_core.device_rows(res)
        status_text = {"ok": TEXT["st_generated"], "left_out": TEXT["st_left_out"],
                       "failed": TEXT["st_failed"], "skipped": TEXT["st_skipped"]}
        for i, row in enumerate(self.rows):
            label = row["label"]
            status = status_text[row["kind"]]
            if row["kind"] == "skipped":
                label, status = TEXT["skipped_label"], row["label"]
            self.dev_tree.insert("", "end", iid=str(i), values=(label, status),
                                 tags=(row["kind"],))
        if self.rows:
            self.dev_tree.selection_set("0")
            self.dev_tree.focus("0")
        self._update_buttons()

    def _selected_row(self):
        sel = self.dev_tree.selection()
        if not sel:
            return None
        return self.rows[int(sel[0])]

    def _show_selected(self):
        row = self._selected_row()
        self.info_var.set("")
        self.left_out_var.set("")
        if row is None:
            set_text(self.preview, "")
        elif row["config"] is not None:
            cfg = row["config"]
            view = self.view_var.get()
            entry = self.kit.get(cfg["host"])
            if view == "config" or entry is None:
                self.info_var.set(config_header(cfg))
                if cfg.get("left_out"):
                    self.left_out_var.set(TEXT["left_out"].format(
                        names=", ".join(cfg["left_out"])))
                set_text(self.preview, cfg["text"])
            elif view == "rollback":
                self.info_var.set(config_header(cfg))
                self.left_out_var.set(rollback_info(entry))
                set_text(self.preview, entry.rollback_text)
            else:
                self.info_var.set(config_header(cfg))
                set_text(self.preview, entry.files[entry.output + "_checks.txt"])
        elif row["kind"] == "failed":
            set_text(self.preview, TEXT["failed_title"].format(host=row["label"])
                     + "\n" + "\n".join(row["lines"]), "failed")
        else:
            set_text(self.preview, "\n".join(row["lines"]), "skipped")
        self._update_buttons()

    def _update_buttons(self):
        row = self._selected_row() if self.rows else None
        has_cfg = row is not None and row["config"] is not None
        for b in (self.copy_button, self.save_button):
            b.state(["!disabled"] if has_cfg else ["disabled"])
        has_any = bool(self.result and self.result.configs)
        self.save_all_button.state(["!disabled"] if has_any else ["disabled"])
        self.save_kit_button.state(["!disabled"] if has_any and self.kit else ["disabled"])

    # ---- copy / save -----------------------------------------------------
    def _shown(self):
        """(file name, text) of what the preview shows for the selected device."""
        row = self._selected_row()
        if not row or not row["config"]:
            return None, None
        cfg = row["config"]
        entry = self.kit.get(cfg["host"])
        view = self.view_var.get()
        if view == "config" or entry is None:
            return cfg["output"] + ".cfg", cfg["text"]
        name = entry.output + ("_rollback.cfg" if view == "rollback" else "_checks.txt")
        return name, entry.files[name]

    def copy(self):
        name, text = self._shown()
        if text is None:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.status.set(TEXT["copied"].format(name=name))

    def save_one(self):
        name, text = self._shown()
        if text is None:
            return
        ext = os.path.splitext(name)[1]
        path = filedialog.asksaveasfilename(
            parent=self, title=TEXT["save_cfg"], defaultextension=ext,
            initialdir=self.app.last_dir("save"), initialfile=name,
            filetypes=[(TEXT["cfg_types"], "*" + ext), (TEXT["all_files"], "*.*")])
        if not path:
            return  # the dialog asked for replacement itself
        try:
            netgen_core.write_config(Path(path), text)
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e),
                                 parent=self)
            return
        self.app.remember_dir("save", os.path.dirname(path))
        self.app.status.set(TEXT["saved"].format(name=os.path.basename(path)))

    def save_all(self):
        if not self.result or not self.result.configs:
            self.app.status.set(TEXT["nothing_to_save"])
            return
        folder = filedialog.askdirectory(parent=self, title=TEXT["pick_save_folder"],
                                         initialdir=self.app.last_dir("save"))
        if not folder:
            return
        self.app.remember_dir("save", folder)

        safe, unsafe = [], []
        for cfg in self.result.configs:
            reason = netgen_core.unsafe_output_name(cfg["output"])
            if reason:
                unsafe.append("{}: {}".format(cfg["output"], reason))
            else:
                safe.append(cfg)
        if unsafe:
            messagebox.showwarning(TEXT["app_title"],
                                   TEXT["unsafe_names"].format(items="\n".join(unsafe)),
                                   parent=self)
        if not safe:
            return
        _all, existing = netgen_core.plan_save_all(Path(folder), safe)
        overwrite = False
        if existing:
            listing = "\n".join(str(p.name) for p in existing)
            if not messagebox.askyesno(TEXT["overwrite_title"],
                                       TEXT["overwrite_msg"].format(files=listing),
                                       parent=self, icon="warning"):
                self.app.status.set(TEXT["save_cancelled"])
                return
            overwrite = True
        try:
            written = netgen_core.save_all(Path(folder), safe, overwrite=overwrite)
        except (OSError, ValueError) as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e),
                                 parent=self)
            return
        self.app.status.set(TEXT["saved_all"].format(n=len(written), folder=folder))

    def save_kit(self):
        """Config + rollback + checks of every device, and the DNS files."""
        if not self.kit:
            self.app.status.set(TEXT["nothing_to_save"])
            return
        folder = filedialog.askdirectory(parent=self, title=TEXT["pick_kit_folder"],
                                         initialdir=self.app.last_dir("save"))
        if not folder:
            return
        self.app.remember_dir("save", folder)
        safe, unsafe = [], []
        for entry in self.kit.values():
            reason = netgen_core.unsafe_output_name(entry.output)
            if reason:
                unsafe.append("{}: {}".format(entry.output, reason))
            else:
                safe.append(entry)
        if unsafe:
            messagebox.showwarning(TEXT["app_title"],
                                   TEXT["unsafe_names"].format(items="\n".join(unsafe)),
                                   parent=self)
        if not safe:
            return
        domain = self.kit_domain
        _all, existing = change_kit.plan(folder, safe, domain)
        if existing:
            listing = "\n".join(p.name for p in existing)
            if not messagebox.askyesno(TEXT["overwrite_title"],
                                       TEXT["overwrite_msg"].format(files=listing),
                                       parent=self, icon="warning"):
                self.app.status.set(TEXT["save_cancelled"])
                return
        try:
            written = change_kit.write_kit(folder, safe, domain, overwrite=True)
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e),
                                 parent=self)
            return
        self.app.status.set(TEXT["kit_saved"].format(n=len(written), folder=folder))

    def collect_prefs(self, prefs):
        prefs.setdefault("generator", {})["strict"] = bool(self.strict_var.get())


# ---------------------------------------------------------------------------
# Abbreviation editor (modal dialog)
# ---------------------------------------------------------------------------
class AbbreviationDialog(tk.Toplevel):
    """Edit the interface types: abbreviation, and whether they get a DNS record.

    result = (pairs, excluded short names) or None when cancelled. A type is
    left out by its abbreviation, so TenGigE and TenGigabitEthernet (both "Te")
    are included or left out together."""

    def __init__(self, master, pairs, excluded_types=()):
        super().__init__(master)
        self.title(TEXT["abbrev_title"])
        self.transient(master.winfo_toplevel())
        self.result = None
        self.excluded = {t.casefold() for t in excluded_types}
        self.geometry("560x460")
        self.minsize(440, 320)

        body = ttk.Frame(self, padding=8)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=TEXT["abbrev_hint"], foreground=GREY).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.tree = ttk.Treeview(body, columns=("type", "abbr", "incl"), show="headings",
                                 selectmode="browse")
        self.tree.heading("type", text=TEXT["abbrev_type"])
        self.tree.heading("abbr", text=TEXT["abbrev_short"])
        self.tree.heading("incl", text=TEXT["abbrev_include"])
        self.tree.column("type", width=240)
        self.tree.column("abbr", width=100)
        self.tree.column("incl", width=90)
        self.tree.tag_configure("out", foreground=GREY)
        ys = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._double_click)

        side = ttk.Frame(body)
        side.grid(row=0, column=2, sticky="n", padx=(6, 0))
        for key, cmd in (("add", self._add), ("edit", self._edit),
                         ("abbrev_toggle", self._toggle),
                         ("remove", self._remove), ("reset", self._reset)):
            ttk.Button(side, text=TEXT[key], command=cmd).pack(fill="x", pady=2)

        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")
        ttk.Button(bottom, text=TEXT["cancel"], command=self.destroy).pack(side="right")
        ttk.Button(bottom, text=TEXT["ok"], command=self._ok).pack(side="right", padx=4)

        self._fill(pairs)
        self.grab_set()
        self.bind("<Escape>", lambda e: self.destroy())

    def _fill(self, pairs):
        self.tree.delete(*self.tree.get_children())
        for long_name, short in pairs:
            self.tree.insert("", "end", values=(long_name, short, ""))
        self._refresh_marks()

    def _refresh_marks(self):
        """Show, on every row, whether its abbreviation is left out."""
        for iid in self.tree.get_children():
            long_name, short = self.tree.item(iid, "values")[:2]
            out = short.casefold() in self.excluded
            self.tree.item(iid, values=(long_name, short,
                                        TEXT["no_mark"] if out else TEXT["yes_mark"]),
                           tags=("out",) if out else ())

    def _pairs(self):
        return [tuple(self.tree.item(i, "values"))[:2] for i in self.tree.get_children()]

    def _double_click(self, event):
        if self.tree.identify_column(event.x) == "#3":
            self._toggle()
        else:
            self._edit()

    def _toggle(self):
        sel = self.tree.selection()
        if not sel:
            return
        short = self.tree.item(sel[0], "values")[1].casefold()
        if short in self.excluded:
            self.excluded.discard(short)
        else:
            self.excluded.add(short)
        self._refresh_marks()

    def _ask_row(self, long_name="", short=""):
        """Small dialog with two fields; returns (type, abbr) or None."""
        win = tk.Toplevel(self)
        win.title(TEXT["abbrev_row_title"])
        win.transient(self)
        out = {}
        f = ttk.Frame(win, padding=8)
        f.pack(fill="both", expand=True)
        v1, v2 = tk.StringVar(value=long_name), tk.StringVar(value=short)
        ttk.Label(f, text=TEXT["abbrev_type"]).grid(row=0, column=0, sticky="w")
        e1 = ttk.Entry(f, textvariable=v1, width=30)
        e1.grid(row=0, column=1, pady=2)
        ttk.Label(f, text=TEXT["abbrev_short"]).grid(row=1, column=0, sticky="w")
        ttk.Entry(f, textvariable=v2, width=30).grid(row=1, column=1, pady=2)

        def ok():
            a, b = v1.get().strip(), v2.get().strip()
            if not a or not b:
                messagebox.showwarning(TEXT["abbrev_title"], TEXT["abbrev_empty"], parent=win)
                return
            out["row"] = (a, b)
            win.destroy()

        bf = ttk.Frame(f)
        bf.grid(row=2, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(bf, text=TEXT["ok"], command=ok).pack(side="left", padx=4)
        ttk.Button(bf, text=TEXT["cancel"], command=win.destroy).pack(side="left")
        win.bind("<Return>", lambda e: ok())
        win.bind("<Escape>", lambda e: win.destroy())
        e1.focus_set()
        win.grab_set()
        self.wait_window(win)
        self.grab_set()
        return out.get("row")

    def _exists(self, name, ignore_iid=None):
        for iid in self.tree.get_children():
            if iid != ignore_iid and self.tree.item(iid, "values")[0].lower() == name.lower():
                return True
        return False

    def _add(self):
        row = self._ask_row()
        if row:
            if self._exists(row[0]):
                messagebox.showwarning(TEXT["abbrev_title"], TEXT["abbrev_dup"], parent=self)
                return
            iid = self.tree.insert("", "end", values=row + ("",))
            self._refresh_marks()
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _edit(self):
        sel = self.tree.selection()
        if not sel:
            return
        old = self.tree.item(sel[0], "values")
        row = self._ask_row(old[0], old[1])
        if row:
            if self._exists(row[0], ignore_iid=sel[0]):
                messagebox.showwarning(TEXT["abbrev_title"], TEXT["abbrev_dup"], parent=self)
                return
            self.tree.item(sel[0], values=row + ("",))
            self._refresh_marks()

    def _remove(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)

    def _reset(self):
        self.excluded = set()
        self._fill(dns_names.DEFAULT_ABBREVIATIONS)

    def _ok(self):
        pairs = [list(p) for p in self._pairs()]
        shorts = {}
        for _, short in pairs:                 # keep the table's spelling
            shorts.setdefault(short.casefold(), short)
        self.result = (pairs, sorted(shorts[t] for t in self.excluded if t in shorts))
        self.destroy()


# ---------------------------------------------------------------------------
# Tab 2 - DNS names
# ---------------------------------------------------------------------------
class DnsTab(ttk.Frame):
    """Running-configs -> DNS records table, with exports."""

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.jobs = JobRunner(self)
        self.loaded = []        # list of (key, LoadedFile), in memory only
        self.result = None
        self.rows = []          # list of (record, row tuple) in display order
        self.sort_state = (None, False)

        d = app.prefs.get("dns", {})
        self.domain_var = tk.StringVar(value=d.get("domain") or "example.intra")
        self.sec_var = tk.BooleanVar(value=bool(d.get("include_secondary")))
        self.shut_var = tk.BooleanVar(value=bool(d.get("exclude_shutdown")))
        self.lower_var = tk.BooleanVar(value=bool(d.get("lowercase")))
        self.abbreviations = [list(p) for p in (d.get("abbreviations")
                                                or dns_names.DEFAULT_ABBREVIATIONS)]
        self.excluded_types = list(d.get("excluded_types") or [])
        self.unknown_var = tk.BooleanVar(value=bool(d.get("exclude_unknown_types")))
        self.patterns_var = tk.StringVar(value=", ".join(d.get("exclude_patterns") or []))
        self.vrfs_var = tk.StringVar(value=", ".join(d.get("exclude_vrfs") or []))
        self.types_var = tk.StringVar()
        self.show_excluded_var = tk.BooleanVar(value=False)
        self.server_var = tk.StringVar()          # not saved: session only
        self.forward_var = tk.BooleanVar(value=bool(d.get("check_forward", True)))
        self.check_cancel = None                  # threading.Event while checking
        self.check_progress = [0, 0]              # done, total (written by workers)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=2)
        paned.add(right, weight=5)
        self._build_inputs(left)
        self._build_results(right)
        self._update_export_buttons()

    # ---- layout ----------------------------------------------------------
    def _build_inputs(self, box):
        ttk.Label(box, text=TEXT["loaded_files"], font=("Segoe UI", 9, "bold")).pack(anchor="w")
        tf = ttk.Frame(box)
        tf.pack(fill="both", expand=True)
        self.file_tree = ttk.Treeview(tf, columns=("file", "host", "platform"),
                                      show="headings", height=6, selectmode="extended")
        for col, key, w in (("file", "col_file", 150), ("host", "col_hostname", 100),
                            ("platform", "col_platform", 70)):
            self.file_tree.heading(col, text=TEXT[key])
            self.file_tree.column(col, width=w, stretch=True)
        self.file_tree.tag_configure("problem", foreground=RED)
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=ys.set)
        self.file_tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        self.file_tree.bind("<Delete>", lambda e: self._remove_files())

        fb = ttk.Frame(box)
        fb.pack(fill="x", pady=(2, 6))
        self.add_files_button = ttk.Button(fb, text=TEXT["add_files"], command=self._add_files)
        self.add_folder_button = ttk.Button(fb, text=TEXT["add_folder"], command=self._add_folder)
        self.add_files_button.pack(side="left")
        self.add_folder_button.pack(side="left", padx=4)
        ttk.Button(fb, text=TEXT["remove"], command=self._remove_files).pack(side="left")
        ttk.Button(fb, text=TEXT["clear"], command=self._clear_files).pack(side="left", padx=4)

        ttk.Label(box, text=TEXT["paste_label"]).pack(anchor="w")
        pf = ttk.Frame(box)
        pf.pack(fill="both", expand=True, pady=(0, 6))
        self.paste = tk.Text(pf, height=5, wrap="none", font=("Consolas", 9),
                             relief="solid", borderwidth=1, undo=True)
        pys = ttk.Scrollbar(pf, orient="vertical", command=self.paste.yview)
        self.paste.configure(yscrollcommand=pys.set)
        self.paste.grid(row=0, column=0, sticky="nsew")
        pys.grid(row=0, column=1, sticky="ns")
        pf.rowconfigure(0, weight=1)
        pf.columnconfigure(0, weight=1)

        opts = ttk.Frame(box)
        opts.pack(fill="x")
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text=TEXT["domain"]).grid(row=0, column=0, sticky="w")
        ttk.Entry(opts, textvariable=self.domain_var).grid(row=0, column=1, sticky="ew", padx=4)
        for r, (key, var) in enumerate((("opt_secondary", self.sec_var),
                                        ("opt_shutdown", self.shut_var),
                                        ("opt_lower", self.lower_var)), start=1):
            ttk.Checkbutton(opts, text=TEXT[key], variable=var,
                            command=self.app.save_prefs_quietly).grid(
                row=r, column=0, columnspan=2, sticky="w")
        out = ttk.LabelFrame(box, text=TEXT["leave_out"], padding=6)
        out.pack(fill="x", pady=(6, 0))
        out.columnconfigure(1, weight=1)
        ttk.Label(out, textvariable=self.types_var, foreground=GREY, wraplength=320,
                  justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(out, text=TEXT["opt_unknown"], variable=self.unknown_var,
                        command=self._settings_changed).grid(
            row=1, column=0, columnspan=2, sticky="w")
        for r, (key, hint, var) in enumerate((
                ("excl_patterns", "excl_patterns_hint", self.patterns_var),
                ("excl_vrfs", "excl_vrfs_hint", self.vrfs_var)), start=2):
            ttk.Label(out, text=TEXT[key]).grid(row=r * 2, column=0, sticky="w")
            entry = ttk.Entry(out, textvariable=var)
            entry.grid(row=r * 2, column=1, sticky="ew", padx=4)
            entry.bind("<FocusOut>", lambda e: self._settings_changed())
            ttk.Label(out, text=TEXT[hint], foreground=GREY).grid(
                row=r * 2 + 1, column=1, sticky="w", padx=4)
        self._show_types()

        bl = ttk.Frame(box)
        bl.pack(fill="x", pady=(6, 0))
        ttk.Button(bl, text=TEXT["abbrev_button"], command=self._edit_abbreviations).pack(side="left")
        self.gen_button = ttk.Button(bl, text=TEXT["generate_dns"], command=self.generate)
        self.gen_button.pack(side="right")

    def _build_results(self, box):
        self.summary_var = tk.StringVar()
        ttk.Label(box, textvariable=self.summary_var,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.problem_var = tk.StringVar()
        self.problem_label = ttk.Label(box, textvariable=self.problem_var,
                                       foreground=AMBER, wraplength=800, justify="left")
        self.problem_label.pack(anchor="w", fill="x")

        tf = ttk.Frame(box)
        tf.pack(fill="both", expand=True, pady=(2, 4))
        cols = [str(i) for i in range(len(dns_names.TABLE_COLUMNS))]
        self.table = ttk.Treeview(tf, columns=cols, show="headings", selectmode="extended")
        widths = (260, 110, 80, 170, 55, 90, 160, 170)
        for i, title in enumerate(dns_names.TABLE_COLUMNS):
            self.table.heading(str(i), text=title,
                               command=lambda c=i: self._sort_by(c))
            self.table.column(str(i), width=widths[i] if i < len(widths) else 100,
                              stretch=True, anchor="w")
        self.table.tag_configure("ok", foreground=GREEN)
        self.table.tag_configure("warn", foreground=AMBER)
        self.table.tag_configure("norecord", foreground=GREY)
        self.table.tag_configure("excluded", foreground=GREY, background="#f0f0f0")
        self.table.tag_configure("conflict", foreground=RED)
        self.table.bind("<Double-1>", lambda e: self._show_lookup())
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.table.yview)
        xs = ttk.Scrollbar(tf, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)

        ttk.Checkbutton(box, text=TEXT["show_excluded"], variable=self.show_excluded_var,
                        command=self._fill_table).pack(anchor="w")

        cf = ttk.LabelFrame(box, text=TEXT["check_dns"], padding=6)
        cf.pack(fill="x", pady=(2, 4))
        ttk.Label(cf, text=TEXT["dns_server"]).pack(side="left")
        ttk.Entry(cf, textvariable=self.server_var, width=18).pack(side="left", padx=4)
        ttk.Label(cf, text=TEXT["dns_server_hint"], foreground=GREY).pack(side="left")
        ttk.Checkbutton(cf, text=TEXT["check_forward"], variable=self.forward_var,
                        command=self.app.save_prefs_quietly).pack(side="left", padx=8)
        self.cancel_button = ttk.Button(cf, text=TEXT["cancel"], command=self._cancel_check)
        self.cancel_button.pack(side="right")
        self.check_button = ttk.Button(cf, text=TEXT["check_dns"], command=self.check_existing)
        self.check_button.pack(side="right", padx=4)
        self.progress = ttk.Progressbar(cf, length=120, mode="determinate")
        self.progress.pack(side="right", padx=4)
        self.cancel_button.state(["disabled"])
        ttk.Label(box, text=TEXT["check_info"], foreground=GREY).pack(anchor="w")

        eb = ttk.Frame(box)
        eb.pack(fill="x")
        self.csv_button = ttk.Button(eb, text=TEXT["export_csv"], command=self.export_csv)
        self.ps_button = ttk.Button(eb, text=TEXT["export_ps"], command=self.export_ps)
        self.copy_button = ttk.Button(eb, text=TEXT["copy_table"], command=self.copy_table)
        self.csv_button.pack(side="left")
        self.ps_button.pack(side="left", padx=4)
        self.copy_button.pack(side="left")

    # ---- loaded files ----------------------------------------------------
    def _refresh_files(self):
        self.file_tree.delete(*self.file_tree.get_children())
        for i, (key, lf) in enumerate(self.loaded):
            host = lf.hostname or TEXT["no_hostname"]
            tags = ("problem",) if (lf.problems or not lf.hostname) else ()
            self.file_tree.insert("", "end", iid=str(i),
                                  values=(lf.source or os.path.basename(key), host, lf.platform),
                                  tags=tags)

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(str(path)))

    def _set_loading(self, busy):
        state = ["disabled"] if busy else ["!disabled"]
        for b in (self.add_files_button, self.add_folder_button, self.gen_button,
                  self.check_button):
            b.state(state)

    def _add_files(self):
        paths = filedialog.askopenfilenames(
            parent=self, title=TEXT["pick_configs"], initialdir=self.app.last_dir("dns_input"),
            filetypes=[(TEXT["all_files"], "*.*"), (TEXT["config_types"], "*.txt *.cfg *.log")])
        if not paths or self.jobs.busy:
            return
        self.app.remember_dir("dns_input", os.path.dirname(paths[0]))
        known = {k for k, _ in self.loaded}
        new = [p for p in paths if self._key(p) not in known]
        ignored = len(paths) - len(new)
        if not new:
            self.app.status.set(TEXT["already_loaded"].format(n=ignored))
            return
        self._set_loading(True)
        self.app.status.set(TEXT["loading"])
        self.jobs.run(load_paths, (new,),
                      lambda files: self._files_loaded(list(zip(new, files)), ignored),
                      self._on_crash)

    def _add_folder(self):
        folder = filedialog.askdirectory(parent=self, title=TEXT["pick_config_folder"],
                                         initialdir=self.app.last_dir("dns_input"))
        if not folder or self.jobs.busy:
            return
        self.app.remember_dir("dns_input", folder)
        self._set_loading(True)
        self.app.status.set(TEXT["loading"])
        self.jobs.run(load_folder, (folder,),
                      lambda pairs: self._files_loaded(pairs, 0), self._on_crash)

    def _files_loaded(self, pairs, ignored):
        self._set_loading(False)
        known = {k for k, _ in self.loaded}
        added = 0
        for path, lf in pairs:
            key = self._key(path)
            if key in known:
                ignored += 1
                continue
            known.add(key)
            if not lf.source:
                lf.source = os.path.basename(str(path))
            self.loaded.append((key, lf))
            added += 1
        self._refresh_files()
        msg = TEXT["loaded_n"].format(n=added)
        if ignored:
            msg += " - " + TEXT["already_loaded"].format(n=ignored)
        self.app.status.set(msg)

    def _remove_files(self):
        sel = {int(i) for i in self.file_tree.selection()}
        if sel:
            self.loaded = [x for i, x in enumerate(self.loaded) if i not in sel]
            self._refresh_files()

    def _clear_files(self):
        self.loaded = []
        self._refresh_files()
        self.paste.delete("1.0", "end")

    def _edit_abbreviations(self):
        dlg = AbbreviationDialog(self, self.abbreviations, self.excluded_types)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.abbreviations, self.excluded_types = dlg.result
            self._show_types()
            self._settings_changed()

    def _show_types(self):
        self.types_var.set(TEXT["excl_types_n"].format(types=", ".join(self.excluded_types))
                           if self.excluded_types else TEXT["excl_types_none"])

    def _settings_changed(self):
        """Save the settings; the table shows old results until Generate."""
        self.app.save_prefs_quietly()
        if self.result is not None:
            self.app.status.set(TEXT["regen_needed"])

    # ---- generation ------------------------------------------------------
    def current_settings(self):
        return dns_settings_from_prefs({"dns": self._dns_prefs()})

    def _dns_prefs(self):
        return {"domain": self.domain_var.get().strip() or "example.intra",
                "include_secondary": bool(self.sec_var.get()),
                "exclude_shutdown": bool(self.shut_var.get()),
                "lowercase": bool(self.lower_var.get()),
                "abbreviations": [list(p) for p in self.abbreviations],
                "excluded_types": list(self.excluded_types),
                "exclude_unknown_types": bool(self.unknown_var.get()),
                "exclude_patterns": split_list(self.patterns_var.get()),
                "exclude_vrfs": split_list(self.vrfs_var.get()),
                "check_forward": bool(self.forward_var.get())}

    def generate(self):
        pasted = self.paste.get("1.0", "end")
        if not self.loaded and not pasted.strip():
            self.app.status.set(TEXT["no_input"])
            return
        if self.jobs.busy:
            return
        self.app.save_prefs_quietly()
        self._set_loading(True)
        self.app.status.set(TEXT["dns_working"])
        files = [lf for _, lf in self.loaded]
        self.jobs.run(build_dns, (files, pasted, self.current_settings()),
                      self._on_result, self._on_crash)

    def _on_crash(self, message):
        self.check_cancel = None
        self.cancel_button.state(["disabled"])
        self._set_loading(False)
        self.problem_var.set(TEXT["unexpected_error"] + ": " + message)
        self.app.status.set(TEXT["unexpected_error"])

    def _on_result(self, result):
        self._set_loading(False)
        self.result = result
        self.rows = [(r, dns_names.record_row(r)) for r in result.records]
        self.sort_state = (None, False)
        self._fill_table()
        self.summary_var.set(dns_names.summary_text(result))
        problems = list(result.problems)
        self.problem_var.set(TEXT["problems_title"] + "\n" + "\n".join(problems)
                             if problems else "")
        self.app.status.set(TEXT["gen_done"])
        self._update_export_buttons()

    def _fill_table(self):
        self.table.delete(*self.table.get_children())
        show_excluded = bool(self.show_excluded_var.get())
        for i, (record, row) in enumerate(self.rows):
            if record.excluded:
                if not show_excluded:
                    continue
                tag = "excluded"
            elif not record.is_record:
                tag = "norecord"
            elif record.existing_kind == "conflict":
                tag = "conflict"
            elif record.warnings or record.existing_kind in ("no answer", "unknown"):
                tag = "warn"
            else:
                tag = "ok"
            self.table.insert("", "end", iid=str(i), values=row, tags=(tag,))

    # ---- DNS check (read-only nslookup) ------------------------------------
    def check_existing(self):
        if self.jobs.busy:
            return
        records = dns_check.checkable(self._records())
        if not records:
            self.app.status.set(TEXT["check_nothing"])
            return
        server = self.server_var.get().strip()
        try:
            dns_check.valid_server(server)
        except ValueError as e:
            messagebox.showwarning(TEXT["app_title"],
                                   TEXT["check_bad_server"].format(err=e), parent=self)
            return
        self.app.save_prefs_quietly()
        self.check_cancel = threading.Event()
        self.check_progress = [0, len(records)]
        self.progress.configure(maximum=len(records), value=0)
        self._set_loading(True)
        self.cancel_button.state(["!disabled"])

        def progress(done, total):          # worker threads: plain data only
            self.check_progress[0] = done

        self.jobs.run(check_dns, (self._records(), server, bool(self.forward_var.get()),
                                  self.check_cancel, progress),
                      self._on_checked, self._on_crash)
        self._poll_progress()

    def _poll_progress(self):
        if self.check_cancel is None:
            return
        done, total = self.check_progress
        self.progress.configure(value=done)
        self.app.status.set(TEXT["check_running"].format(done=done, total=total))
        self.after(200, self._poll_progress)

    def _cancel_check(self):
        if self.check_cancel is not None:
            self.check_cancel.set()

    def _on_checked(self, n):
        cancelled = self.check_cancel is not None and self.check_cancel.is_set()
        self.check_cancel = None
        self.cancel_button.state(["disabled"])
        self._set_loading(False)
        self.progress.configure(value=n)
        self.rows = [(r, dns_names.record_row(r)) for r, _ in self.rows]
        self._fill_table()
        self.summary_var.set(dns_names.summary_text(self.result))
        self.app.status.set((TEXT["check_cancelled"] if cancelled
                             else TEXT["check_done"]).format(n=n))

    def _show_lookup(self):
        """Double-click: the raw nslookup output of the row."""
        sel = self.table.selection()
        if not sel:
            return
        record = self.rows[int(sel[0])][0]
        win = tk.Toplevel(self)
        win.title(TEXT["raw_title"] + " - " + (record.fqdn or record.ip))
        win.transient(self.winfo_toplevel())
        win.geometry("640x360")
        frame, text = make_scrolled_text(win, height=16, font=("Consolas", 9))
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        set_text(text, record.lookup_raw or TEXT["raw_none"])
        ttk.Button(win, text=TEXT["ok"], command=win.destroy).pack(pady=(0, 8))
        win.bind("<Escape>", lambda e: win.destroy())

    def _sort_by(self, col):
        last_col, desc = self.sort_state
        desc = (not desc) if last_col == col else False
        self.sort_state = (col, desc)
        self.rows.sort(key=lambda rr: dns_names.table_sort_key(col, rr[1][col]),
                       reverse=desc)
        self._fill_table()
        for i, title in enumerate(dns_names.TABLE_COLUMNS):
            mark = (" ▼" if desc else " ▲") if i == col else ""
            self.table.heading(str(i), text=title + mark)

    def _update_export_buttons(self):
        state = ["!disabled"] if self.rows else ["disabled"]
        for b in (self.csv_button, self.ps_button, self.copy_button):
            b.state(state)

    # ---- exports ---------------------------------------------------------
    def _records(self):
        return [r for r, _ in self.rows]

    def _ask_save(self, ext, type_key, name):
        path = filedialog.asksaveasfilename(
            parent=self, title=TEXT["export_csv"] if ext == ".csv" else TEXT["export_ps"],
            defaultextension=ext, initialfile=name, initialdir=self.app.last_dir("dns_export"),
            filetypes=[(TEXT[type_key], "*" + ext), (TEXT["all_files"], "*.*")])
        if path:
            self.app.remember_dir("dns_export", os.path.dirname(path))
        return path

    def export_csv(self):
        if not self.rows:
            self.app.status.set(TEXT["nothing_to_export"])
            return
        path = self._ask_save(".csv", "csv_types", "dns_names.csv")
        if not path:
            return
        try:
            Path(path).write_bytes(dns_names.to_csv_bytes(self._records()))
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e), parent=self)
            return
        self.app.status.set(TEXT["exported"].format(name=os.path.basename(path)))

    def export_ps(self):
        if not self.rows:
            self.app.status.set(TEXT["nothing_to_export"])
            return
        path = self._ask_save(".ps1", "ps_types", "dns_names.ps1")
        if not path:
            return
        domain = self.current_settings().domain
        text = dns_names.to_powershell(self._records(), domain)
        try:
            # UTF-8 with BOM; to_powershell already ends lines with CRLF.
            Path(path).write_bytes(text.encode("utf-8-sig"))
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e), parent=self)
            return
        self.app.status.set(TEXT["exported"].format(name=os.path.basename(path)))

    def copy_table(self):
        if not self.rows:
            self.app.status.set(TEXT["nothing_to_export"])
            return
        self.clipboard_clear()
        self.clipboard_append(dns_names.to_tsv(self._records(), header=True))
        self.app.status.set(TEXT["table_copied"].format(
            n=len(dns_names.exported(self._records()))))

    def collect_prefs(self, prefs):
        prefs["dns"] = self._dns_prefs()


# ---------------------------------------------------------------------------
# Tab 3 - Rollback
# ---------------------------------------------------------------------------
def make_editable_text(master, height=8):
    """An editable Text widget with scrollbars, in a frame."""
    frame = ttk.Frame(master)
    text = tk.Text(frame, height=height, wrap="none", font=("Consolas", 9),
                   relief="solid", borderwidth=1, undo=True)
    ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
    text.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    text.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, text


class RollbackTab(ttk.Frame):
    """Running-config before + change -> rollback (rollback.py)."""

    PLATFORMS = ("auto", "ios-xe", "ios-xr")

    def __init__(self, master, app):
        super().__init__(master, padding=8)
        self.app = app
        self.jobs = JobRunner(self)
        self.result = None
        self.platform_var = tk.StringVar(value="auto")

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 6, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=1)

        self.before_text = self._input(left, "rb_before")
        self.change_text = self._input(left, "rb_change")
        line = ttk.Frame(left)
        line.pack(fill="x", pady=(4, 0))
        ttk.Label(line, text=TEXT["rb_platform"]).pack(side="left")
        ttk.Combobox(line, textvariable=self.platform_var, values=self.PLATFORMS,
                     state="readonly", width=8).pack(side="left", padx=4)
        self.gen_button = ttk.Button(line, text=TEXT["generate_rb"], command=self.generate)
        self.gen_button.pack(side="right")
        ttk.Label(line, text=TEXT["generate_hint"], foreground=GREY).pack(side="right", padx=6)

        ttk.Label(right, text=TEXT["rb_result"], font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.info_var = tk.StringVar()
        ttk.Label(right, textvariable=self.info_var, foreground=AMBER, wraplength=500,
                  justify="left").pack(fill="x")
        frame, self.output = make_scrolled_text(right, height=10, font=("Consolas", 10))
        self.output.tag_configure("review", foreground=AMBER)
        self.output.tag_configure("error", foreground=RED)
        frame.pack(fill="both", expand=True, pady=(2, 4))
        buttons = ttk.Frame(right)
        buttons.pack(fill="x")
        self.copy_button = ttk.Button(buttons, text=TEXT["copy"], command=self.copy)
        self.save_button = ttk.Button(buttons, text=TEXT["save_rb"], command=self.save)
        self.copy_button.pack(side="left")
        self.save_button.pack(side="left", padx=4)
        self._update_buttons()

    def _input(self, box, label_key):
        head = ttk.Frame(box)
        head.pack(fill="x", pady=(4, 0))
        ttk.Label(head, text=TEXT[label_key]).pack(side="left")
        frame, text = make_editable_text(box)
        ttk.Button(head, text=TEXT["clear"],
                   command=lambda: text.delete("1.0", "end")).pack(side="right")
        ttk.Button(head, text=TEXT["load_file"],
                   command=lambda: self._load_into(text)).pack(side="right", padx=4)
        frame.pack(fill="both", expand=True)
        return text

    def _load_into(self, widget):
        path = filedialog.askopenfilename(
            parent=self, title=TEXT["pick_text"], initialdir=self.app.last_dir("rollback"),
            filetypes=[(TEXT["rb_types"], "*.txt *.cfg *.conf *.log"),
                       (TEXT["all_files"], "*.*")])
        if not path:
            return
        try:
            content = dns_names.read_text_file(Path(path))
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], str(e), parent=self)
            return
        self.app.remember_dir("rollback", os.path.dirname(path))
        widget.delete("1.0", "end")
        widget.insert("1.0", content)

    def generate(self):
        change = self.change_text.get("1.0", "end-1c")
        if not change.strip():
            self.app.status.set(TEXT["rb_no_change"])
            return
        if self.jobs.busy:
            return
        before = self.before_text.get("1.0", "end-1c")
        platform = self.platform_var.get()
        self.gen_button.state(["disabled"])
        self.app.status.set(TEXT["working"])
        self.jobs.run(make_rollback, (before, change, "" if platform == "auto" else platform),
                      self._on_result, self._on_error)

    def _on_error(self, message):
        """Worker raised: an unreadable change (FormatError) or a bug."""
        self.gen_button.state(["!disabled"])
        self.result = None
        self.info_var.set("")
        set_text(self.output, TEXT["rb_error"] + "\n" + message, "error")
        self.app.status.set(TEXT["rb_error"])
        self._update_buttons()

    def _on_result(self, res):
        self.gen_button.state(["!disabled"])
        self.result = res
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        for line in res.text.splitlines(True):
            self.output.insert("end", line, "review" if "REVIEW" in line else ())
        self.output.configure(state="disabled")
        if res.empty:
            self.info_var.set(TEXT["rb_nothing"])
            self.app.status.set(TEXT["rb_nothing"])
        elif res.reviews:
            self.info_var.set(TEXT["rb_reviews"].format(n=len(res.reviews)))
            self.app.status.set(TEXT["rb_done_reviews"].format(platform=res.platform,
                                                               n=len(res.reviews)))
        else:
            self.info_var.set("")
            self.app.status.set(TEXT["rb_done"].format(platform=res.platform))
        self._update_buttons()

    def _update_buttons(self):
        state = ["!disabled"] if self.result is not None else ["disabled"]
        self.copy_button.state(state)
        self.save_button.state(state)

    def copy(self):
        if self.result is None:
            return
        self.clipboard_clear()
        self.clipboard_append(self.result.text)
        self.app.status.set(TEXT["copied"].format(name=TEXT["rb_result"]))

    def save(self):
        if self.result is None:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title=TEXT["save_rb"], defaultextension=".cfg",
            initialdir=self.app.last_dir("rollback"), initialfile="rollback.cfg",
            filetypes=[(TEXT["cfg_types"], "*.cfg"), (TEXT["all_files"], "*.*")])
        if not path:
            return
        try:
            netgen_core.write_config(Path(path), self.result.text)
        except OSError as e:
            messagebox.showerror(TEXT["app_title"], TEXT["save_failed"].format(err=e),
                                 parent=self)
            return
        self.app.remember_dir("rollback", os.path.dirname(path))
        self.app.status.set(TEXT["saved"].format(name=os.path.basename(path)))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class NetgenApp:
    """The window: three tabs, a status bar, and the preferences."""

    def __init__(self, root):
        self.root = root
        self.prefs = netgen_core.load_settings()
        root.title(TEXT["app_title"])
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        win = self.prefs.get("window", {})
        w = min(int(win.get("width", 1100)), max(900, root.winfo_screenwidth() - 40))
        h = min(int(win.get("height", 680)), max(560, root.winfo_screenheight() - 80))
        root.geometry("{}x{}".format(w, h))
        root.minsize(900, 560)

        self.status = StatusBar(root)
        self.status.pack(side="bottom", fill="x")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)
        self.generator = GeneratorTab(self.notebook, self)
        self.dns = DnsTab(self.notebook, self)
        self.rollback = RollbackTab(self.notebook, self)
        self.notebook.add(self.generator, text=TEXT["tab_generator"])
        self.notebook.add(self.dns, text=TEXT["tab_dns"])
        self.notebook.add(self.rollback, text=TEXT["tab_rollback"])

        root.bind_all("<Control-Return>", self._ctrl_enter)
        root.bind_all("<Control-KP_Enter>", self._ctrl_enter)
        root.protocol("WM_DELETE_WINDOW", self.close)

    def _ctrl_enter(self, event=None):
        """Ctrl+Enter runs the action of the visible tab."""
        if self.notebook.select() == str(self.dns):
            self.dns.generate()
        elif self.notebook.select() == str(self.rollback):
            self.rollback.generate()
        else:
            self.generator.generate()
        return "break"

    # ---- preferences (folders, window size, DNS settings only) ----------
    def last_dir(self, key):
        d = self.prefs.get("last_dirs", {}).get(key, "")
        return d if d and os.path.isdir(d) else ""

    def remember_dir(self, key, folder):
        self.prefs.setdefault("last_dirs", {})[key] = str(folder)
        self.save_prefs_quietly()

    def save_prefs_quietly(self):
        """Save preferences; a failure only shows a message in the status bar."""
        self.generator.collect_prefs(self.prefs) if hasattr(self, "generator") else None
        self.dns.collect_prefs(self.prefs) if hasattr(self, "dns") else None
        try:
            ok = netgen_core.save_settings(self.prefs)
        except Exception:  # never block the user because of preferences
            ok = False
        if not ok:
            self.status.set(TEXT["settings_not_saved"])

    def close(self):
        if self.root.state() == "normal":
            self.prefs["window"] = {"width": self.root.winfo_width(),
                                    "height": self.root.winfo_height()}
        self.save_prefs_quietly()
        self.root.destroy()


def main():
    """Open the window (the only place where Tk is started)."""
    root = tk.Tk()
    NetgenApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
