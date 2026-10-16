"""
Backup Tab Component for Upganam.
Handles phone file browsing, selection, hierarchy preservation,
and dispatching backups into encrypted containers, ZIP archives, or plain folders.
"""
import os
import posixpath
import tempfile
import subprocess
import threading
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    tk = None
    ttk = None
    filedialog = None
    messagebox = None

from upganam.core.adb import (
    ADB_BIN,
    get_subp_kwargs,
    list_dir,
    get_relative_backup_path,
    format_size
)
from upganam.core.crypto import (
    is_crypto_available,
    encrypt_file
)
from upganam.core.archive import (
    create_manifest,
    build_zip_archive
)

class BackupTab(ttk.Frame):
    ROOTS = [
        ("/storage/emulated/0", "Storage"),
        ("/storage/emulated/0/DCIM", "Photos/DCIM"),
        ("/storage/emulated/0/Download", "Downloads"),
        ("/storage/emulated/0/Documents", "Documents"),
        ("/storage/emulated/0/Pictures", "Pictures"),
        ("/storage/emulated/0/Music", "Music"),
    ]

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app  # Reference to parent BackupApp

        self.current_path = "/storage/emulated/0"
        self.backup_selected_paths = {}  # Map remote path -> entry dict
        self.iid_to_backup_entry = {}
        self.is_backing_up = False
        self.cancel_backup_event = threading.Event()
        self.current_backup_proc = None

        self._build_ui()

    def _build_ui(self):
        # 1. Navigation Bar
        nav_frame = ttk.Frame(self, padding=4)
        nav_frame.pack(fill="x", padx=4, pady=2)

        ttk.Button(nav_frame, text="⬆ Up", width=6, command=self._go_up).pack(side="left", padx=2)
        ttk.Button(nav_frame, text="🔄 Refresh", width=9, command=self.refresh).pack(side="left", padx=2)

        ttk.Label(nav_frame, text="Device Path:").pack(side="left", padx=(6, 2))
        self.path_var = tk.StringVar(value=self.current_path)
        self.path_entry = ttk.Entry(nav_frame, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.path_entry.bind("<Return>", lambda e: self._cd())
        ttk.Button(nav_frame, text="Go", width=5, command=self._cd).pack(side="left", padx=2)

        # Quick Jump Bar
        bm_frame = ttk.Frame(self, padding=(4, 0, 4, 4))
        bm_frame.pack(fill="x", padx=4)
        ttk.Label(bm_frame, text="Quick Jump:", font=("", 9, "bold")).pack(side="left", padx=(5, 5))
        for path, label in self.ROOTS:
            ttk.Button(bm_frame, text=label, command=lambda p=path: self._jump(p)).pack(side="left", padx=2)

        # 2. Main Treeview
        mid = ttk.Frame(self, padding=4)
        mid.pack(fill="both", expand=True, padx=4, pady=2)

        cols = ("sel", "name", "size")
        self.backup_tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.backup_tree.heading("sel", text="✓", command=lambda: self._sort_column("sel", False))
        self.backup_tree.heading("name", text="Name", command=lambda: self._sort_column("name", False))
        self.backup_tree.heading("size", text="Size", command=lambda: self._sort_column("size", False))

        self.backup_tree.column("sel", width=45, minwidth=40, anchor="center", stretch=False)
        self.backup_tree.column("name", width=620, minwidth=250, anchor="w", stretch=True)
        self.backup_tree.column("size", width=120, minwidth=80, anchor="e", stretch=False)

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.backup_tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.backup_tree.xview)
        self.backup_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        self.backup_tree.bind("<Button-1>", self._on_tree_click)
        self.backup_tree.bind("<Double-1>", self._on_dblclick)
        self.backup_tree.bind("<space>", self._on_space)

        # 3. Selection Actions Bar
        sel_bar = ttk.Frame(self, padding=4)
        sel_bar.pack(fill="x", padx=4)

        ttk.Button(sel_bar, text="Select All", command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Deselect All", command=self._desel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Clear All Selections", command=self._clear_all_selections).pack(side="left", padx=2)

        self.backup_sel_count_var = tk.StringVar(value="Selected: 0 item(s)")
        ttk.Label(sel_bar, textvariable=self.backup_sel_count_var, font=("", 9, "bold")).pack(side="left", padx=(15, 5))

        # 4. Backup Options & Format Selection Controls
        opts_frame = ttk.LabelFrame(self, text="Backup Settings & Format", padding=6)
        opts_frame.pack(fill="x", padx=4, pady=3)

        fmt_row = ttk.Frame(opts_frame)
        fmt_row.pack(fill="x", padx=2, pady=2)
        ttk.Label(fmt_row, text="Format:", font=("", 9, "bold")).pack(side="left", padx=(5, 8))

        self.backup_format_var = tk.StringVar(value="encrypted")
        ttk.Radiobutton(
            fmt_row,
            text="🔒 Encrypted Container (.upg) — Password protected",
            variable=self.backup_format_var,
            value="encrypted",
            command=self._on_format_toggled
        ).pack(side="left", padx=6)

        ttk.Radiobutton(
            fmt_row,
            text="🗜️ Compressed Archive Only (.zip) — Fast, no password",
            variable=self.backup_format_var,
            value="zip",
            command=self._on_format_toggled
        ).pack(side="left", padx=6)

        ttk.Radiobutton(
            fmt_row,
            text="📁 Plain Folder — Direct pull, uncompressed",
            variable=self.backup_format_var,
            value="plain",
            command=self._on_format_toggled
        ).pack(side="left", padx=6)

        # Password row (for encrypted mode)
        pass_row = ttk.Frame(opts_frame)
        pass_row.pack(fill="x", padx=2, pady=2)

        self.pass_label = ttk.Label(pass_row, text="Password:")
        self.pass_label.pack(side="left", padx=(5, 2))

        self.backup_pass_var = tk.StringVar()
        self.backup_pass_entry = ttk.Entry(pass_row, textvariable=self.backup_pass_var, show="*", width=18)
        self.backup_pass_entry.pack(side="left", padx=4)

        self.pass_confirm_label = ttk.Label(pass_row, text="Confirm:")
        self.pass_confirm_label.pack(side="left", padx=(8, 2))

        self.backup_pass_confirm_var = tk.StringVar()
        self.backup_pass_confirm_entry = ttk.Entry(pass_row, textvariable=self.backup_pass_confirm_var, show="*", width=18)
        self.backup_pass_confirm_entry.pack(side="left", padx=4)

        self.show_backup_pass_var = tk.BooleanVar(value=False)
        self.chk_show_pass = ttk.Checkbutton(
            pass_row,
            text="Show",
            variable=self.show_backup_pass_var,
            command=self._toggle_show_pass
        )
        self.chk_show_pass.pack(side="left", padx=6)

        self.preserve_hierarchy_var = tk.BooleanVar(value=True)
        self.chk_preserve = ttk.Checkbutton(
            pass_row,
            text="📁 Preserve folder hierarchy",
            variable=self.preserve_hierarchy_var
        )
        self.chk_preserve.pack(side="left", padx=16)

        # Destination row
        dst_row = ttk.Frame(opts_frame)
        dst_row.pack(fill="x", padx=2, pady=(4, 2))

        ttk.Label(dst_row, text="Save to:").pack(side="left", padx=(5, 2))
        default_backup = os.path.join(os.path.expanduser("~"), "Upganam_Backup.upg")
        self.backup_dest_var = tk.StringVar(value=default_backup)
        self.entry_backup_dest = ttk.Entry(dst_row, textvariable=self.backup_dest_var, width=50)
        self.entry_backup_dest.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(dst_row, text="Browse…", command=self._browse_dest).pack(side="left", padx=2)

        self.btn_cancel = ttk.Button(dst_row, text="⏹ Cancel", command=self._cancel_backup, state="disabled")
        self.btn_cancel.pack(side="right", padx=3)

        self.btn_start = ttk.Button(dst_row, text="▶ Start Backup", command=self._start_backup)
        self.btn_start.pack(side="right", padx=3)

        # 5. Progress and Status Bar
        self.backup_progress = ttk.Progressbar(self, mode="determinate")
        self.backup_progress.pack(fill="x", padx=6, pady=(3, 2))

        self.backup_status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.backup_status_var, anchor="w", relief="sunken", padding=3).pack(fill="x", padx=4, pady=(0, 2))

        self._on_format_toggled()

    # ─────────────────────────────────────────────────────────────
    # Format and Password Toggles
    # ─────────────────────────────────────────────────────────────
    def _toggle_show_pass(self):
        show_char = "" if self.show_backup_pass_var.get() else "*"
        self.backup_pass_entry.configure(show=show_char)
        self.backup_pass_confirm_entry.configure(show=show_char)

    def _on_format_toggled(self):
        fmt = self.backup_format_var.get()
        curr_path = self.backup_dest_var.get().strip()

        if fmt == "encrypted":
            self.pass_label.configure(state="normal")
            self.backup_pass_entry.configure(state="normal")
            self.pass_confirm_label.configure(state="normal")
            self.backup_pass_confirm_entry.configure(state="normal")
            self.chk_show_pass.configure(state="normal")
            self.chk_preserve.configure(state="disabled")
            if not curr_path.endswith(".upg"):
                base_no_ext = os.path.splitext(curr_path)[0]
                self.backup_dest_var.set(base_no_ext + ".upg")
        elif fmt == "zip":
            self.pass_label.configure(state="disabled")
            self.backup_pass_entry.configure(state="disabled")
            self.pass_confirm_label.configure(state="disabled")
            self.backup_pass_confirm_entry.configure(state="disabled")
            self.chk_show_pass.configure(state="disabled")
            self.chk_preserve.configure(state="disabled")
            if not curr_path.endswith(".zip"):
                base_no_ext = os.path.splitext(curr_path)[0]
                self.backup_dest_var.set(base_no_ext + ".zip")
        else:  # plain folder
            self.pass_label.configure(state="disabled")
            self.backup_pass_entry.configure(state="disabled")
            self.pass_confirm_label.configure(state="disabled")
            self.backup_pass_confirm_entry.configure(state="disabled")
            self.chk_show_pass.configure(state="disabled")
            self.chk_preserve.configure(state="normal")
            if curr_path.endswith((".upg", ".zip")):
                self.backup_dest_var.set(os.path.splitext(curr_path)[0])

    def _browse_dest(self):
        fmt = self.backup_format_var.get()
        if fmt == "encrypted":
            init_file = os.path.basename(self.backup_dest_var.get()) or "Upganam_Backup.upg"
            init_dir = os.path.dirname(self.backup_dest_var.get()) or os.path.expanduser("~")
            f = filedialog.asksaveasfilename(
                title="Select Encrypted Backup Container",
                initialdir=init_dir,
                initialfile=init_file,
                filetypes=[("Upganam Encrypted Backup (*.upg)", "*.upg"), ("All files", "*.*")]
            )
            if f:
                if not f.endswith(".upg"):
                    f += ".upg"
                self.backup_dest_var.set(f)
        elif fmt == "zip":
            init_file = os.path.basename(self.backup_dest_var.get()) or "Upganam_Backup.zip"
            init_dir = os.path.dirname(self.backup_dest_var.get()) or os.path.expanduser("~")
            f = filedialog.asksaveasfilename(
                title="Select Compressed ZIP Backup Destination",
                initialdir=init_dir,
                initialfile=init_file,
                filetypes=[("ZIP Archive (*.zip)", "*.zip"), ("All files", "*.*")]
            )
            if f:
                if not f.endswith(".zip"):
                    f += ".zip"
                self.backup_dest_var.set(f)
        else:
            d = filedialog.askdirectory(initialdir=self.backup_dest_var.get() or os.path.expanduser("~"))
            if d:
                self.backup_dest_var.set(d)

    # ─────────────────────────────────────────────────────────────
    # Directory Listing & Navigation
    # ─────────────────────────────────────────────────────────────
    def refresh(self):
        serial = self.app.get_selected_serial()
        if not serial:
            self.app.check_devices()
            return

        self.backup_tree.delete(*self.backup_tree.get_children())
        self.iid_to_backup_entry.clear()
        self.backup_status_var.set(f"Listing {self.current_path} …")
        self.winfo_toplevel().config(cursor="watch")

        target_path = self.current_path

        def worker():
            entries, err = list_dir(target_path, serial=serial)
            self.after(0, lambda: self._on_entries_loaded(target_path, entries, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_entries_loaded(self, path, entries, err):
        self.winfo_toplevel().config(cursor="")
        if path != self.current_path:
            return

        if err:
            self.backup_status_var.set(f"Error reading {path}: {err}")
            parent = posixpath.dirname(path.rstrip("/")) or "/"
            self.current_path = parent
            self.path_var.set(parent)
            messagebox.showwarning("Access Error", f"Could not list contents of:\n{path}\n\n{err}")
            return

        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        for e in entries:
            is_checked = e["path"] in self.backup_selected_paths
            mark = "☑" if is_checked else "☐"
            icon = "📁 " if e["is_dir"] else ("🔗 " if e.get("is_symlink") else "📄 ")
            size_disp = "<DIR>" if e["is_dir"] else format_size(e["size"])
            iid = self.backup_tree.insert("", "end", values=(mark, f"{icon}{e['name']}", size_disp))
            self.iid_to_backup_entry[iid] = e

        self.backup_status_var.set(f"{len(entries)} items in {self.current_path}")
        self._update_selection_status()

    def _go_up(self):
        if self.current_path and self.current_path != "/":
            parent = posixpath.dirname(self.current_path.rstrip("/")) or "/"
            self.current_path = parent
            self.path_var.set(parent)
            self.refresh()

    def _cd(self):
        entered = self.path_var.get().strip().replace("\\", "/")
        if entered:
            self.current_path = posixpath.normpath(entered)
            self.path_var.set(self.current_path)
            self.refresh()

    def _jump(self, path):
        self.current_path = path
        self.path_var.set(path)
        self.refresh()

    # ─────────────────────────────────────────────────────────────
    # Treeview Interaction & Multi-Selection
    # ─────────────────────────────────────────────────────────────
    def _toggle_item(self, iid):
        entry = self.iid_to_backup_entry.get(iid)
        if not entry:
            return
        path = entry["path"]
        if path in self.backup_selected_paths:
            del self.backup_selected_paths[path]
            new_mark = "☐"
        else:
            self.backup_selected_paths[path] = entry
            new_mark = "☑"
        vals = list(self.backup_tree.item(iid, "values"))
        vals[0] = new_mark
        self.backup_tree.item(iid, values=vals)
        self._update_selection_status()

    def _on_tree_click(self, event):
        region = self.backup_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.backup_tree.identify_column(event.x)
        item = self.backup_tree.identify_row(event.y)
        if item and col == "#1":
            self._toggle_item(item)

    def _on_dblclick(self, event):
        col = self.backup_tree.identify_column(event.x)
        if col == "#1":
            return
        item = self.backup_tree.identify_row(event.y)
        if not item:
            return
        entry = self.iid_to_backup_entry.get(item)
        if not entry:
            return
        if entry["is_dir"]:
            self.current_path = entry["path"]
            self.path_var.set(self.current_path)
            self.refresh()
        else:
            self._toggle_item(item)

    def _on_space(self, event):
        selected_iids = self.backup_tree.selection()
        if not selected_iids:
            return "break"
        any_unchecked = any(
            self.iid_to_backup_entry[i]["path"] not in self.backup_selected_paths
            for i in selected_iids if i in self.iid_to_backup_entry
        )
        for i in selected_iids:
            if i not in self.iid_to_backup_entry:
                continue
            entry = self.iid_to_backup_entry[i]
            path = entry["path"]
            if any_unchecked:
                self.backup_selected_paths[path] = entry
                mark = "☑"
            else:
                self.backup_selected_paths.pop(path, None)
                mark = "☐"
            vals = list(self.backup_tree.item(i, "values"))
            vals[0] = mark
            self.backup_tree.item(i, values=vals)
        self._update_selection_status()
        return "break"

    def _sel_all(self):
        for iid, entry in self.iid_to_backup_entry.items():
            self.backup_selected_paths[entry["path"]] = entry
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☑"
            self.backup_tree.item(iid, values=vals)
        self._update_selection_status()

    def _desel_all(self):
        for iid, entry in self.iid_to_backup_entry.items():
            self.backup_selected_paths.pop(entry["path"], None)
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☐"
            self.backup_tree.item(iid, values=vals)
        self._update_selection_status()

    def _clear_all_selections(self):
        self.backup_selected_paths.clear()
        for iid in self.backup_tree.get_children():
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☐"
            self.backup_tree.item(iid, values=vals)
        self._update_selection_status()

    def _update_selection_status(self):
        self.backup_sel_count_var.set(f"Selected: {len(self.backup_selected_paths)} item(s)")

    def _sort_column(self, col, reverse):
        items = [(self.backup_tree.item(k, "values"), k) for k in self.backup_tree.get_children("")]
        def sort_key(element):
            val, iid = element
            entry = self.iid_to_backup_entry.get(iid)
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
            self.backup_tree.move(k, "", idx)
        self.backup_tree.heading(col, command=lambda: self._sort_column(col, not reverse))

    # ─────────────────────────────────────────────────────────────
    # Backup Execution & Worker
    # ─────────────────────────────────────────────────────────────
    def _start_backup(self):
        serial = self.app.get_selected_serial()
        if not serial:
            messagebox.showwarning("No Device", "Please select a connected Android device.")
            return

        if not self.backup_selected_paths:
            messagebox.showinfo("Nothing Selected", "Please select at least one file or folder to backup.")
            return

        dest = self.backup_dest_var.get().strip()
        if not dest:
            messagebox.showwarning("No Destination", "Please specify a backup destination.")
            return

        fmt = self.backup_format_var.get()
        password = self.backup_pass_var.get()

        if fmt == "encrypted":
            if not is_crypto_available():
                messagebox.showerror(
                    "Missing Library",
                    "The 'cryptography' Python package is required for encrypted backups.\n\n"
                    "Please install it using:\npip install cryptography"
                )
                return
            if not password:
                messagebox.showwarning("Password Required", "Please enter a password to encrypt your backup container.")
                self.backup_pass_entry.focus_set()
                return
            confirm = self.backup_pass_confirm_var.get()
            if password != confirm:
                messagebox.showerror("Password Mismatch", "The entered passwords do not match. Please re-enter them carefully.")
                self.backup_pass_confirm_entry.focus_set()
                return
            if not dest.endswith(".upg"):
                dest += ".upg"
                self.backup_dest_var.set(dest)
        elif fmt == "zip":
            if not dest.endswith(".zip"):
                dest += ".zip"
                self.backup_dest_var.set(dest)

        dest_dir = os.path.dirname(dest) if fmt in ("encrypted", "zip") else dest
        if dest_dir:
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to create destination folder:\n{e}")
                return

        self.is_backing_up = True
        self.cancel_backup_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.backup_progress.configure(value=0)

        targets = list(self.backup_selected_paths.values())
        preserve_hier = self.preserve_hierarchy_var.get()

        threading.Thread(
            target=self._backup_worker,
            args=(targets, dest, serial, fmt, password, preserve_hier),
            daemon=True
        ).start()

    def _cancel_backup(self):
        if self.is_backing_up:
            if messagebox.askyesno("Cancel Backup", "Are you sure you want to cancel the backup in progress?"):
                self.cancel_backup_event.set()
                self.backup_status_var.set("Cancelling backup...")
                if self.current_backup_proc:
                    try:
                        self.current_backup_proc.kill()
                    except Exception:
                        pass

    def _backup_worker(self, targets, dest, serial, fmt, password, preserve_hier):
        total = len(targets)
        successes = 0
        errors = []

        is_archive = (fmt in ("encrypted", "zip"))
        temp_dir = tempfile.mkdtemp(prefix="upganam_bk_") if is_archive else None
        staging_dir = os.path.join(temp_dir, "content") if temp_dir else dest
        if temp_dir:
            os.makedirs(staging_dir, exist_ok=True)

        manifest_entries = []

        try:
            for i, entry in enumerate(targets, 1):
                if self.cancel_backup_event.is_set():
                    break

                remote = entry["path"]
                name = posixpath.basename(remote.rstrip("/")) or "item"
                rel_path = get_relative_backup_path(remote)

                self.after(0, lambda i=i, t=total, n=name:
                    self.backup_status_var.set(f"Pulling ({i}/{t}): {n} …"))
                scale = 75 if is_archive else 100
                self.after(0, lambda i=i, t=total, s=scale:
                    self.backup_progress.configure(value=((i - 1) / total) * s))

                if is_archive or preserve_hier:
                    local_target = os.path.join(staging_dir, os.path.normpath(rel_path))
                    os.makedirs(os.path.dirname(local_target), exist_ok=True)
                else:
                    local_target = staging_dir

                cmd = [ADB_BIN]
                if serial:
                    cmd.extend(["-s", serial])
                cmd.extend(["pull", remote, local_target])

                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        **get_subp_kwargs()
                    )
                    self.current_backup_proc = proc
                    out, _ = proc.communicate()
                    self.current_backup_proc = None

                    if self.cancel_backup_event.is_set():
                        break

                    if proc.returncode == 0:
                        successes += 1
                        manifest_entries.append({
                            "remote_path": remote,
                            "rel_path": rel_path.replace("\\", "/"),
                            "is_dir": entry["is_dir"],
                            "size": entry["size"]
                        })
                    else:
                        err_msg = out.strip() if out else f"Failed with exit code {proc.returncode}"
                        errors.append((name, err_msg))
                        if any(fatal in err_msg.lower() for fatal in ("device not found", "device offline", "no devices/emulators")):
                            errors.append((name, "Device disconnected. Aborting backup."))
                            break
                except Exception as e:
                    errors.append((name, str(e)))

            # If archive mode (zip or encrypted), package staging into zip
            if is_archive and not self.cancel_backup_event.is_set() and successes > 0:
                self.after(0, lambda: self.backup_status_var.set("Packaging and compressing files into ZIP archive…"))
                self.after(0, lambda: self.backup_progress.configure(value=80))

                manifest_data = create_manifest(manifest_entries, serial=serial, backup_format=fmt)
                raw_zip_path = dest if fmt == "zip" else os.path.join(temp_dir, "archive.zip")
                build_zip_archive(staging_dir, raw_zip_path, manifest_data)

                if fmt == "encrypted":
                    self.after(0, lambda: self.backup_status_var.set("Encrypting archive with AES-256-GCM…"))
                    self.after(0, lambda: self.backup_progress.configure(value=90))
                    encrypt_file(raw_zip_path, dest, password)

        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        self.after(0, lambda: self._on_backup_completed(total, successes, errors, dest, fmt))

    def _on_backup_completed(self, total, successes, errors, dest, fmt):
        self.is_backing_up = False
        self.current_backup_proc = None
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

        if self.cancel_backup_event.is_set():
            self.backup_status_var.set(f"Backup cancelled. {successes}/{total} item(s) processed.")
            messagebox.showinfo(
                "Backup Cancelled",
                f"Backup was cancelled.\n\n{successes} item(s) were processed."
            )
            return

        self.backup_progress.configure(value=100)
        mode_names = {"encrypted": "Encrypted container created", "zip": "ZIP archive created", "plain": "Files saved"}
        mode_text = mode_names.get(fmt, "Files saved")
        if not errors:
            self.backup_status_var.set(f"Done! {total} item(s) successfully backed up → {dest}")
            messagebox.showinfo(
                "Backup Complete",
                f"Successfully backed up {total} item(s)!\n\n"
                f"{mode_text}:\n{dest}"
            )
        else:
            self.backup_status_var.set(f"Finished with warnings: {successes} succeeded, {len(errors)} failed.")
            detail_lines = [f"• {n}: {err}" for n, err in errors[:10]]
            if len(errors) > 10:
                detail_lines.append(f"... and {len(errors) - 10} more.")
            messagebox.showwarning(
                "Backup Completed with Warnings",
                f"{successes} of {total} item(s) backed up successfully.\n\n"
                f"Failed items:\n" + "\n".join(detail_lines) + f"\n\nDestination: {dest}"
            )
