"""netgen_core.py - headless backend logic for the Netgen desktop GUI.

This module provides testable, non-GUI helper functions for:
- Loading workbooks from disk and generating configs via generate.py
- Formatting device rows for the GUI list
- Safe configuration saving and file name validation
- User preference management (JSON whitelist)
- Threaded background job execution
"""

import json
import queue
import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from openpyxl import load_workbook

import dns_names
import generate

SETTINGS_FILE = Path(__file__).resolve().parent / "netgen_gui_settings.json"

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def generate_from_path(path, templates=None, strict=False):
    """Load workbook from disk and invoke generate.generate_configs().

    Always re-reads the workbook from disk (no caching).
    Never raises an exception; returns Result().stop(...) on error.
    """
    path_obj = Path(path)
    if path_obj.suffix.lower() not in (".xlsx", ".xlsm"):
        return generate.Result().stop(
            "Cannot read the Excel file:",
            ["Expected a .xlsx or .xlsm file, got: {}".format(path_obj.suffix)],
        )

    wb = None
    try:
        wb = load_workbook(path, data_only=True)
        return generate.generate_configs(wb, templates=templates, strict=strict)
    except Exception as e:
        return generate.Result().stop("Cannot read the Excel file:", [str(e)])
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass


def device_rows(res):
    """Build list of device row items for the GUI treeview.

    Ordered by: generated configs, failed devices, then skipped devices.
    Each item has:
        {"kind": "ok"|"left_out"|"failed"|"skipped",
         "label": str,
         "config": Optional[dict],
         "lines": List[str]}
    """
    if res is None:
        return []

    rows = []
    for cfg in getattr(res, "configs", []):
        kind = "left_out" if cfg.get("left_out") else "ok"
        label = cfg.get("output", cfg.get("host", ""))
        rows.append({
            "kind": kind,
            "label": label,
            "config": cfg,
            "lines": [],
        })

    for failed in getattr(res, "failed", []):
        rows.append({
            "kind": "failed",
            "label": failed.get("host", ""),
            "config": None,
            "lines": failed.get("lines", []),
        })

    for skipped in getattr(res, "skipped", []):
        msg = skipped if isinstance(skipped, str) else str(skipped)
        rows.append({
            "kind": "skipped",
            "label": msg,
            "config": None,
            "lines": [msg],
        })

    return rows


def unsafe_output_name(name):
    """Check if an output file name is safe to write on Windows.

    Returns None if safe, or a descriptive reason string if unsafe.
    """
    if not name or not isinstance(name, str):
        return "name is empty"
    if name.strip() != name:
        return "name cannot start or end with whitespace"
    if name.startswith(".") or name.endswith("."):
        return "name cannot start or end with a dot"
    if any(c in r'\/:*?"<>|' for c in name):
        return 'name contains forbidden characters (\\ / : * ? " < > |)'
    if any(ord(c) < 32 for c in name):
        return "name contains control characters"

    stem = name.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        return "reserved Windows device name ({})".format(stem)
    return None


def plan_save_all(folder, configs):
    """Plan saving configurations to folder.

    Returns (all_paths, existing_paths).
    """
    folder_path = Path(folder)
    all_paths = [folder_path / (cfg["output"] + ".cfg") for cfg in configs]
    existing = [p for p in all_paths if p.exists()]
    return all_paths, existing


def write_config(path, text):
    """Write configuration text to path using UTF-8 and LF newlines."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def save_all(folder, configs, overwrite=False):
    """Save all configurations in folder.

    Refuses unsafe names by raising ValueError.
    Does not replace existing files unless overwrite is True.
    Returns list of written Path objects.
    """
    folder_path = Path(folder)
    folder_path.mkdir(parents=True, exist_ok=True)
    written = []
    for cfg in configs:
        out_name = cfg.get("output", "")
        reason = unsafe_output_name(out_name)
        if reason:
            raise ValueError("Unsafe output name '{}': {}".format(out_name, reason))
        dest = folder_path / (out_name + ".cfg")
        if dest.exists() and not overwrite:
            continue
        write_config(dest, cfg.get("text", ""))
        written.append(dest)
    return written


def default_settings():
    """Return default GUI preference dictionary."""
    return {
        "version": 1,
        "window": {"width": 1100, "height": 680},
        "last_dirs": {
            "excel": "",
            "templates": "",
            "save": "",
            "dns_input": "",
            "dns_export": "",
        },
        "generator": {"strict": False},
        "dns": {
            "domain": "win.net.intra",
            "include_secondary": False,
            "exclude_shutdown": False,
            "lowercase": False,
            "abbreviations": [[a, b] for a, b in dns_names.DEFAULT_ABBREVIATIONS],
        },
    }


def _is_valid_domain(d):
    if not isinstance(d, str) or not d or len(d) > 253:
        return False
    labels = d.split(".")
    for label in labels:
        if not label or len(label) > 63:
            return False
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label):
            return False
    return True


def _validate_abbreviations(abbrs):
    if not isinstance(abbrs, (list, tuple)):
        return None
    valid = []
    seen = set()
    for item in abbrs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        k, v = item
        if not isinstance(k, str) or not isinstance(v, str):
            return None
        k_s, v_s = k.strip(), v.strip()
        if not k_s or not v_s:
            return None
        if k_s.casefold() in seen:
            return None
        seen.add(k_s.casefold())
        valid.append([k_s, v_s])
    return valid


def _filter_settings(data):
    """Enforce whitelist and defaults on settings data."""
    defaults = default_settings()
    if not isinstance(data, dict):
        return defaults

    clean = {"version": 1}

    # window
    win = data.get("window")
    if isinstance(win, dict):
        w = win.get("width")
        h = win.get("height")
        try:
            w_int = max(900, int(w))
        except (TypeError, ValueError):
            w_int = defaults["window"]["width"]
        try:
            h_int = max(560, int(h))
        except (TypeError, ValueError):
            h_int = defaults["window"]["height"]
        clean["window"] = {"width": w_int, "height": h_int}
    else:
        clean["window"] = dict(defaults["window"])

    # last_dirs
    clean["last_dirs"] = dict(defaults["last_dirs"])
    dirs = data.get("last_dirs")
    if isinstance(dirs, dict):
        for k in clean["last_dirs"]:
            val = dirs.get(k)
            if isinstance(val, str):
                clean["last_dirs"][k] = val

    # generator
    clean["generator"] = {"strict": False}
    gen = data.get("generator")
    if isinstance(gen, dict):
        clean["generator"]["strict"] = bool(gen.get("strict"))

    # dns
    clean["dns"] = dict(defaults["dns"])
    dns_sec = data.get("dns")
    if isinstance(dns_sec, dict):
        dom = dns_sec.get("domain")
        if _is_valid_domain(dom):
            clean["dns"]["domain"] = dom
        clean["dns"]["include_secondary"] = bool(dns_sec.get("include_secondary"))
        clean["dns"]["exclude_shutdown"] = bool(dns_sec.get("exclude_shutdown"))
        clean["dns"]["lowercase"] = bool(dns_sec.get("lowercase"))
        abbrs = _validate_abbreviations(dns_sec.get("abbreviations"))
        if abbrs is not None:
            clean["dns"]["abbreviations"] = abbrs

    return clean


def load_settings(path=None):
    """Load settings from path, returning default settings on error."""
    if path is None:
        path = SETTINGS_FILE
    p = Path(path)
    if not p.is_file():
        return default_settings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _filter_settings(data)
    except Exception:
        return default_settings()


def save_settings(settings, path=None):
    """Save whitelisted settings to path. Never raises; returns bool."""
    if path is None:
        path = SETTINGS_FILE
    try:
        p = Path(path)
        clean = _filter_settings(settings)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


class BackgroundJob:
    """Run a callable in a daemon thread and communicate results via a queue."""

    def __init__(self, func, *args):
        self.func = func
        self.args = args
        self.queue = queue.Queue()
        self._thread = None

    def start(self):
        def worker():
            try:
                res = self.func(*self.args)
                self.queue.put(("done", res))
            except Exception as e:
                self.queue.put(("error", "{}: {}".format(type(e).__name__, str(e))))

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def poll(self):
        """Non-blocking poll. Returns None, ('done', res), or ('error', msg)."""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None
