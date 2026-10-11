"""
Upganam — Android Phone Backup Tool
Single .exe after PyInstaller build. ADB is bundled.
"""
import os
import sys
import shutil
import posixpath
import shlex
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── ADB resolution & subprocess helpers ────────────────────────
def _subp_kwargs():
    kwargs = {}
    if sys.platform == "win32":
        # Suppress flashing console windows in PyInstaller GUI/windowed mode
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs

def _adb_path():
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for name in ("adb.exe", "adb"):
        local = os.path.join(base, name)
        if os.path.isfile(local):
            return local
    found = shutil.which("adb")
    if found:
        return found
    return "adb"

ADB = _adb_path()

def adb(*args, serial=None):
    cmd = [ADB]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_subp_kwargs()
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except FileNotFoundError:
        return "", f"ADB executable not found at '{ADB}'.", -1
    except Exception as e:
        return "", str(e), -1

def adb_shell(cmd_str, serial=None):
    return adb("shell", cmd_str, serial=serial)

def get_devices():
    out, _, code = adb("devices", "-l")
    if code != 0:
        return []
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            state = parts[1]
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1]
            devices.append({"serial": serial, "state": state, "model": model})
    return devices

# Regex to parse Android toybox/toolbox 'ls -la' output:
# [perms] [links?] [owner] [group?] [size] [date] [time] [filename...]
LS_RE = re.compile(
    r'^([b-ls-][rwxstST-]{9}\+?)\s+'
    r'(?:\d+\s+)?'
    r'(?:\S+\s+){1,2}'
    r'(\d+)\s+'
    r'((?:\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})|(?:[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2})))\s+'
    r'(.+)$'
)

def parse_ls_output(out, base_path):
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("total ") or line.endswith(":"):
            continue
        m = LS_RE.match(line)
        if m:
            perm, size_str, _, name = m.groups()
            if perm.startswith('l') and ' -> ' in name:
                name = name.split(' -> ')[0].strip()
            if name in ('.', '..'):
                continue
            is_dir = perm.startswith('d') or perm.startswith('l')
            try:
                size_int = int(size_str)
            except ValueError:
                size_int = 0
            entries.append({
                "name": name,
                "path": posixpath.join(base_path, name),
                "is_dir": is_dir,
                "size": size_int
            })
        else:
            # Fallback for unexpected formats
            parts = line.split()
            if len(parts) >= 6 and len(parts[0]) >= 10 and parts[0][0] in "bcdl-sp":
                perm = parts[0]
                name = parts[-1]
                if name in ('.', '..'):
                    continue
                is_dir = perm.startswith('d') or perm.startswith('l')
                entries.append({
                    "name": name,
                    "path": posixpath.join(base_path, name),
                    "is_dir": is_dir,
                    "size": 0
                })
    return entries

def list_dir(path="/storage/emulated/0", serial=None):
    norm_path = posixpath.normpath(path)
    quoted = shlex.quote(norm_path)
    out, err, code = adb_shell(f'ls -la {quoted}', serial=serial)
    if code != 0 or "Permission denied" in err or "Permission denied" in out:
        error_msg = (err or out or "Permission denied").strip()
        return [], error_msg
    entries = parse_ls_output(out, norm_path)
    return entries, None

def format_size(bytes_val):
    try:
        b = float(bytes_val)
    except (ValueError, TypeError):
        return ""
    if b <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024.0 or unit == "TB":
            return f"{int(b)} B" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"

# ── GUI ──────────────────────────────────────────────────────
class BackupApp:
    ROOTS = [
        ("/storage/emulated/0", "Storage"),
        ("/storage/emulated/0/DCIM", "Photos/DCIM"),
        ("/storage/emulated/0/Download", "Downloads"),
        ("/storage/emulated/0/Documents", "Documents"),
        ("/storage/emulated/0/Pictures", "Pictures"),
        ("/storage/emulated/0/Music", "Music"),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("Upganam — Android Phone Backup")
        self.root.geometry("980x720")
        self.root.minsize(750, 500)

        self.current_path = "/storage/emulated/0"
        self.selected_device_serial = None
        self.devices_cache = []
        self.selected_paths = set()  # Retains selections across folder navigation
        self.iid_to_entry = {}       # Map item ID -> entry dict
        self.is_backing_up = False
        self.cancel_requested = threading.Event()
        self.current_proc = None

        self._build_ui()
        self._check_device()

    def _build_ui(self):
        # 1. Device Selection Frame
        dev_frame = ttk.LabelFrame(self.root, text="Connected Device", padding=5)
        dev_frame.pack(fill="x", padx=6, pady=(4, 2))

        ttk.Label(dev_frame, text="Device:").pack(side="left", padx=(5, 2))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(dev_frame, textvariable=self.device_var, state="readonly", width=42)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(dev_frame, text="🔍 Detect Devices", command=self._check_device).pack(side="left", padx=5)

        # 2. Navigation Bar
        nav_frame = ttk.Frame(self.root, padding=5)
        nav_frame.pack(fill="x", padx=6, pady=2)

        ttk.Button(nav_frame, text="⬆ Up", width=6, command=self._go_up).pack(side="left", padx=2)
        ttk.Button(nav_frame, text="🔄 Refresh", width=9, command=self._refresh).pack(side="left", padx=2)

        ttk.Label(nav_frame, text="Path:").pack(side="left", padx=(6, 2))
        self.path_var = tk.StringVar(value=self.current_path)
        self.path_entry = ttk.Entry(nav_frame, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.path_entry.bind("<Return>", lambda e: self._cd())
        ttk.Button(nav_frame, text="Go", width=5, command=self._cd).pack(side="left", padx=2)

        # Quick Jump Bar
        bm_frame = ttk.Frame(self.root, padding=(5, 0, 5, 2))
        bm_frame.pack(fill="x", padx=6)
        ttk.Label(bm_frame, text="Quick Jump:", font=("", 9, "bold")).pack(side="left", padx=(5, 5))
        for path, label in self.ROOTS:
            ttk.Button(bm_frame, text=label, command=lambda p=path: self._jump(p)).pack(side="left", padx=2)

        # 3. Main Treeview
        mid = ttk.Frame(self.root, padding=5)
        mid.pack(fill="both", expand=True, padx=6, pady=2)

        cols = ("sel", "name", "size")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("sel", text="✓", command=lambda: self._sort_column("sel", False))
        self.tree.heading("name", text="Name", command=lambda: self._sort_column("name", False))
        self.tree.heading("size", text="Size", command=lambda: self._sort_column("size", False))

        self.tree.column("sel", width=45, minwidth=40, anchor="center", stretch=False)
        self.tree.column("name", width=600, minwidth=250, anchor="w", stretch=True)
        self.tree.column("size", width=120, minwidth=80, anchor="e", stretch=False)

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_dblclick)
        self.tree.bind("<space>", self._on_space)

        # 4. Selection Actions
        sel_bar = ttk.Frame(self.root, padding=5)
        sel_bar.pack(fill="x", padx=6)

        ttk.Button(sel_bar, text="Select All", command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Deselect All", command=self._desel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Clear All Selections", command=self._clear_all_selections).pack(side="left", padx=2)

        self.sel_count_var = tk.StringVar(value="Selected: 0 item(s)")
        ttk.Label(sel_bar, textvariable=self.sel_count_var, font=("", 9, "bold")).pack(side="left", padx=(15, 5))

        # 5. Destination & Backup Controls
        bot = ttk.LabelFrame(self.root, text="Backup Destination & Actions", padding=6)
        bot.pack(fill="x", padx=6, pady=4)

        ttk.Label(bot, text="Save to:").pack(side="left", padx=(5, 2))
        default_backup = os.path.join(os.path.expanduser("~"), "Upganam_Backup")
        self.backup_dir = tk.StringVar(value=default_backup)
        ttk.Entry(bot, textvariable=self.backup_dir, width=45).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(bot, text="Browse…", command=self._browse).pack(side="left", padx=2)

        self.btn_cancel = ttk.Button(bot, text="⏹ Cancel", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="right", padx=3)

        self.btn_start = ttk.Button(bot, text="▶ Start Backup", command=self._start)
        self.btn_start.pack(side="right", padx=3)

        # 6. Progress and Status Bar
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(2, 2))

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken", padding=3).pack(fill="x", padx=6, pady=(0, 4))

    def _check_device(self):
        devices = get_devices()
        self.devices_cache = devices
        if not devices:
            self.selected_device_serial = None
            self.device_combo["values"] = []
            self.device_var.set("No device found")
            self.status_var.set("No Android device detected.")
            self.tree.delete(*self.tree.get_children())
            self.iid_to_entry.clear()
            messagebox.showwarning(
                "No Device",
                "No Android device detected.\n\n"
                "1. Enable Developer Options on your phone\n"
                "2. Enable USB Debugging\n"
                "3. Connect your phone via USB\n"
                "4. Tap 'Allow USB debugging' on your phone screen\n"
                "5. Click 'Detect Devices'"
            )
            return

        combo_labels = []
        default_serial = None
        for d in devices:
            label = d["serial"]
            if d["model"]:
                label += f" ({d['model']})"
            if d["state"] != "device":
                label += f" [{d['state']}]"
            combo_labels.append(label)
            if d["state"] == "device" and not default_serial:
                default_serial = d["serial"]

        self.device_combo["values"] = combo_labels

        serials = [d["serial"] for d in devices]
        if self.selected_device_serial in serials:
            idx = serials.index(self.selected_device_serial)
        else:
            self.selected_device_serial = default_serial or devices[0]["serial"]
            idx = serials.index(self.selected_device_serial)

        self.device_combo.current(idx)

        current_dev = devices[idx]
        if current_dev["state"] == "unauthorized":
            self.status_var.set(f"Device {current_dev['serial']} unauthorized! Unlock phone to accept prompt.")
            messagebox.showwarning(
                "Device Unauthorized",
                f"Device '{current_dev['serial']}' is connected but unauthorized.\n\n"
                "Please unlock your phone and tap 'Allow' or 'Always allow' on the USB debugging prompt."
            )
            return

        self.status_var.set(f"Connected: {self.selected_device_serial}")
        self._refresh()

    def _on_device_selected(self, event=None):
        idx = self.device_combo.current()
        if 0 <= idx < len(self.devices_cache):
            self.selected_device_serial = self.devices_cache[idx]["serial"]
            self._refresh()

    def _refresh(self):
        if not self.selected_device_serial:
            self._check_device()
            return

        self.tree.delete(*self.tree.get_children())
        self.iid_to_entry.clear()
        self.status_var.set(f"Listing {self.current_path} …")
        self.root.config(cursor="watch")

        target_path = self.current_path
        serial = self.selected_device_serial

        def worker():
            entries, err = list_dir(target_path, serial=serial)
            self.root.after(0, lambda: self._on_entries_loaded(target_path, entries, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_entries_loaded(self, path, entries, err):
        self.root.config(cursor="")
        if path != self.current_path:
            return

        if err:
            self.status_var.set(f"Error reading {path}: {err}")
            messagebox.showwarning("Access Error", f"Could not list contents of:\n{path}\n\n{err}")
            return

        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        for e in entries:
            is_checked = e["path"] in self.selected_paths
            mark = "☑" if is_checked else "☐"
            icon = "📁 " if e["is_dir"] else "📄 "
            size_disp = "<DIR>" if e["is_dir"] else format_size(e["size"])
            iid = self.tree.insert("", "end", values=(mark, f"{icon}{e['name']}", size_disp))
            self.iid_to_entry[iid] = e

        self.status_var.set(f"{len(entries)} items in {self.current_path}")
        self._update_selection_status()

    def _go_up(self):
        if self.current_path and self.current_path != "/":
            parent = posixpath.dirname(self.current_path.rstrip("/")) or "/"
            self.current_path = parent
            self.path_var.set(parent)
            self._refresh()

    def _cd(self):
        entered = self.path_var.get().strip()
        if entered:
            self.current_path = posixpath.normpath(entered)
            self.path_var.set(self.current_path)
            self._refresh()

    def _jump(self, path):
        self.current_path = path
        self.path_var.set(path)
        self._refresh()

    def _toggle_item(self, iid):
        entry = self.iid_to_entry.get(iid)
        if not entry:
            return
        path = entry["path"]
        if path in self.selected_paths:
            self.selected_paths.remove(path)
            new_mark = "☐"
        else:
            self.selected_paths.add(path)
            new_mark = "☑"
        vals = list(self.tree.item(iid, "values"))
        vals[0] = new_mark
        self.tree.item(iid, values=vals)
        self._update_selection_status()

    def _on_tree_click(self, event):
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        # Toggle checkbox only when clicking column #1 (the checkbox column)
        if item and col == "#1":
            self._toggle_item(item)

    def _on_dblclick(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        entry = self.iid_to_entry.get(item)
        if not entry:
            return
        if entry["is_dir"]:
            self.current_path = entry["path"]
            self.path_var.set(self.current_path)
            self._refresh()
        else:
            self._toggle_item(item)

    def _on_space(self, event):
        # Spacebar toggles checkboxes for all highlighted items
        selected_iids = self.tree.selection()
        if not selected_iids:
            return "break"
        any_unchecked = any(
            self.iid_to_entry[i]["path"] not in self.selected_paths
            for i in selected_iids if i in self.iid_to_entry
        )
        for i in selected_iids:
            if i not in self.iid_to_entry:
                continue
            path = self.iid_to_entry[i]["path"]
            if any_unchecked:
                self.selected_paths.add(path)
                mark = "☑"
            else:
                self.selected_paths.discard(path)
                mark = "☐"
            vals = list(self.tree.item(i, "values"))
            vals[0] = mark
            self.tree.item(i, values=vals)
        self._update_selection_status()
        return "break"

    def _sel_all(self):
        for iid, entry in self.iid_to_entry.items():
            self.selected_paths.add(entry["path"])
            vals = list(self.tree.item(iid, "values"))
            vals[0] = "☑"
            self.tree.item(iid, values=vals)
        self._update_selection_status()

    def _desel_all(self):
        for iid, entry in self.iid_to_entry.items():
            self.selected_paths.discard(entry["path"])
            vals = list(self.tree.item(iid, "values"))
            vals[0] = "☐"
            self.tree.item(iid, values=vals)
        self._update_selection_status()

    def _clear_all_selections(self):
        self.selected_paths.clear()
        for iid in self.tree.get_children():
            vals = list(self.tree.item(iid, "values"))
            vals[0] = "☐"
            self.tree.item(iid, values=vals)
        self._update_selection_status()

    def _update_selection_status(self):
        self.sel_count_var.set(f"Selected: {len(self.selected_paths)} item(s)")

    def _sort_column(self, col, reverse):
        items = [(self.tree.item(k, "values"), k) for k in self.tree.get_children("")]
        def sort_key(element):
            val, iid = element
            entry = self.iid_to_entry.get(iid)
            is_dir = entry["is_dir"] if entry else False
            if col == "name":
                return (not is_dir, val[1].lower())
            elif col == "size":
                size = entry["size"] if entry else 0
                return (not is_dir, size)
            elif col == "sel":
                return (val[0] != "☑", entry["name"].lower() if entry else "")
            return val[0]

        items.sort(key=sort_key, reverse=reverse)
        for idx, (_, k) in enumerate(items):
            self.tree.move(k, "", idx)
        self.tree.heading(col, command=lambda: self._sort_column(col, not reverse))

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.backup_dir.get())
        if d:
            self.backup_dir.set(d)

    def _start(self):
        if not self.selected_device_serial:
            messagebox.showwarning("No Device", "Please select a connected Android device.")
            return

        if not self.selected_paths:
            messagebox.showinfo("Nothing Selected", "Please select at least one file or folder to backup.")
            return

        dest = self.backup_dir.get().strip()
        if not dest:
            messagebox.showwarning("No Destination", "Please specify a backup destination folder.")
            return

        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to create backup destination directory:\n{e}")
            return

        self.is_backing_up = True
        self.cancel_requested.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(value=0)

        targets = list(self.selected_paths)
        threading.Thread(
            target=self._do_backup_worker,
            args=(targets, dest, self.selected_device_serial),
            daemon=True
        ).start()

    def _cancel(self):
        if self.is_backing_up:
            if messagebox.askyesno("Cancel Backup", "Are you sure you want to cancel the backup in progress?"):
                self.cancel_requested.set()
                self.status_var.set("Cancelling backup...")
                if self.current_proc:
                    try:
                        self.current_proc.kill()
                    except Exception:
                        pass

    def _do_backup_worker(self, targets, dest, serial):
        total = len(targets)
        successes = 0
        errors = []

        for i, remote in enumerate(targets, 1):
            if self.cancel_requested.is_set():
                break

            name = posixpath.basename(remote.rstrip("/")) or "item"
            self.root.after(0, lambda i=i, t=total, n=name:
                self.status_var.set(f"Pulling ({i}/{t}): {n} …"))
            self.root.after(0, lambda i=i, t=total:
                self.progress.configure(value=((i - 1) / t) * 100))

            cmd = [ADB]
            if serial:
                cmd.extend(["-s", serial])
            cmd.extend(["pull", remote, dest])

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **_subp_kwargs()
                )
                self.current_proc = proc
                out, _ = proc.communicate()
                self.current_proc = None

                if self.cancel_requested.is_set():
                    break

                if proc.returncode == 0:
                    successes += 1
                else:
                    err_msg = out.strip() if out else f"Failed with exit code {proc.returncode}"
                    errors.append((name, err_msg))
            except Exception as e:
                errors.append((name, str(e)))

            self.root.after(0, lambda i=i, t=total:
                self.progress.configure(value=(i / t) * 100))

        self.root.after(0, lambda: self._backup_done(total, successes, errors, dest))

    def _backup_done(self, total, successes, errors, dest):
        self.is_backing_up = False
        self.current_proc = None
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

        if self.cancel_requested.is_set():
            self.status_var.set(f"Backup cancelled. {successes}/{total} item(s) saved.")
            messagebox.showinfo(
                "Backup Cancelled",
                f"Backup was cancelled.\n\n{successes} item(s) were saved to:\n{dest}"
            )
            return

        self.progress.configure(value=100)
        if not errors:
            self.status_var.set(f"Done! {total} item(s) successfully backed up → {dest}")
            messagebox.showinfo(
                "Backup Complete",
                f"Successfully backed up {total} item(s) to:\n{dest}"
            )
        else:
            self.status_var.set(f"Backup finished with warnings: {successes} succeeded, {len(errors)} failed.")
            detail_lines = [f"• {n}: {err}" for n, err in errors[:10]]
            if len(errors) > 10:
                detail_lines.append(f"... and {len(errors) - 10} more.")
            messagebox.showwarning(
                "Backup Completed with Warnings",
                f"{successes} of {total} item(s) backed up successfully.\n\n"
                f"Failed items:\n" + "\n".join(detail_lines) + f"\n\nSaved to: {dest}"
            )


if __name__ == "__main__":
    root = tk.Tk()
    BackupApp(root)
    root.mainloop()
