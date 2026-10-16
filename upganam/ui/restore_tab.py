"""
Restore Tab Component for Upganam.
Handles loading, inspecting, and selectively restoring files from
encrypted (.upg) and unencrypted (.zip) backup containers to connected Android devices.
"""
import os
import posixpath
import shlex
import tempfile
import zipfile
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
    adb_shell,
    format_size
)
from upganam.core.crypto import (
    is_crypto_available,
    is_encrypted_container,
    decrypt_file,
    InvalidTag
)
from upganam.core.archive import (
    inspect_archive,
    extract_file_from_archive
)

class RestoreTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app  # Reference to parent BackupApp

        self.restore_archive_entries = []  # List of item dicts
        self.restore_selected_indices = set()
        self.iid_to_restore_idx = {}
        self.loaded_restore_zip_path = None
        self.loaded_restore_temp_dir = None
        self.is_restoring = False
        self.cancel_restore_event = threading.Event()
        self.current_restore_proc = None

        self._build_ui()

    def _build_ui(self):
        # 1. File Selection & Decryption Bar
        f_frame = ttk.LabelFrame(self, text="Backup Container (.upg or .zip)", padding=6)
        f_frame.pack(fill="x", padx=4, pady=4)

        row1 = ttk.Frame(f_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Backup Archive:").pack(side="left", padx=(5, 2))
        self.restore_file_var = tk.StringVar()
        self.restore_file_entry = ttk.Entry(row1, textvariable=self.restore_file_var, width=50)
        self.restore_file_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="Browse…", command=self._browse_archive).pack(side="left", padx=2)

        row2 = ttk.Frame(f_frame)
        row2.pack(fill="x", pady=2)
        self.restore_pass_lbl = ttk.Label(row2, text="Password:")
        self.restore_pass_lbl.pack(side="left", padx=(5, 2))
        self.restore_pass_var = tk.StringVar()
        self.restore_pass_entry = ttk.Entry(row2, textvariable=self.restore_pass_var, show="*", width=26)
        self.restore_pass_entry.pack(side="left", padx=5)

        self.show_restore_pass_var = tk.BooleanVar(value=False)
        self.chk_restore_show_pass = ttk.Checkbutton(
            row2,
            text="Show Password",
            variable=self.show_restore_pass_var,
            command=lambda: self.restore_pass_entry.configure(show="" if self.show_restore_pass_var.get() else "*")
        )
        self.chk_restore_show_pass.pack(side="left", padx=5)

        self.restore_pass_note_var = tk.StringVar(value="(Required only for .upg encrypted containers)")
        ttk.Label(row2, textvariable=self.restore_pass_note_var, font=("", 8, "italic")).pack(side="left", padx=8)

        self.btn_inspect_backup = ttk.Button(row2, text="🔓 Open & Inspect Backup", command=self._inspect_and_load_backup)
        self.btn_inspect_backup.pack(side="right", padx=10)

        # 2. Backup Content Inspection Treeview
        mid_res = ttk.LabelFrame(self, text="Contents of Backup (Choose files to restore)", padding=4)
        mid_res.pack(fill="both", expand=True, padx=4, pady=3)

        res_cols = ("sel", "name", "original_path", "size")
        self.restore_tree = ttk.Treeview(mid_res, columns=res_cols, show="headings", selectmode="extended")
        self.restore_tree.heading("sel", text="✓", command=lambda: self._sort_column("sel", False))
        self.restore_tree.heading("name", text="File / Relative Path", command=lambda: self._sort_column("name", False))
        self.restore_tree.heading("original_path", text="Original Android Path", command=lambda: self._sort_column("original_path", False))
        self.restore_tree.heading("size", text="Size", command=lambda: self._sort_column("size", False))

        self.restore_tree.column("sel", width=45, minwidth=40, anchor="center", stretch=False)
        self.restore_tree.column("name", width=340, minwidth=180, anchor="w", stretch=True)
        self.restore_tree.column("original_path", width=360, minwidth=180, anchor="w", stretch=True)
        self.restore_tree.column("size", width=100, minwidth=70, anchor="e", stretch=False)

        vsb_res = ttk.Scrollbar(mid_res, orient="vertical", command=self.restore_tree.yview)
        hsb_res = ttk.Scrollbar(mid_res, orient="horizontal", command=self.restore_tree.xview)
        self.restore_tree.configure(yscrollcommand=vsb_res.set, xscrollcommand=hsb_res.set)

        self.restore_tree.grid(row=0, column=0, sticky="nsew")
        vsb_res.grid(row=0, column=1, sticky="ns")
        hsb_res.grid(row=1, column=0, sticky="ew")
        mid_res.rowconfigure(0, weight=1)
        mid_res.columnconfigure(0, weight=1)

        self.restore_tree.bind("<Button-1>", self._on_tree_click)
        self.restore_tree.bind("<Double-1>", self._on_dblclick)
        self.restore_tree.bind("<space>", self._on_space)

        # 3. Restore Selection Bar
        res_sel_bar = ttk.Frame(self, padding=4)
        res_sel_bar.pack(fill="x", padx=4)

        ttk.Button(res_sel_bar, text="Select All", command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(res_sel_bar, text="Deselect All", command=self._desel_all).pack(side="left", padx=2)

        self.restore_sel_count_var = tk.StringVar(value="No backup file loaded.")
        ttk.Label(res_sel_bar, textvariable=self.restore_sel_count_var, font=("", 9, "bold")).pack(side="left", padx=(15, 5))

        # 4. Target Destination on Device & Actions
        target_frame = ttk.LabelFrame(self, text="Restore Destination on Device", padding=6)
        target_frame.pack(fill="x", padx=4, pady=3)

        self.restore_mode_var = tk.StringVar(value="original")
        ttk.Radiobutton(
            target_frame,
            text="Restore to original device paths (e.g. /storage/emulated/0/...)",
            variable=self.restore_mode_var,
            value="original"
        ).pack(anchor="w", padx=5, pady=1)

        row_cust = ttk.Frame(target_frame)
        row_cust.pack(fill="x", padx=5, pady=2)
        ttk.Radiobutton(
            row_cust,
            text="Restore into custom device folder:",
            variable=self.restore_mode_var,
            value="custom"
        ).pack(side="left")

        self.custom_restore_dir_var = tk.StringVar(value="/storage/emulated/0/Restored_Backup")
        self.entry_custom_restore = ttk.Entry(row_cust, textvariable=self.custom_restore_dir_var, width=40)
        self.entry_custom_restore.pack(side="left", fill="x", expand=True, padx=5)

        act_row = ttk.Frame(target_frame)
        act_row.pack(fill="x", pady=(4, 2))

        self.btn_cancel = ttk.Button(act_row, text="⏹ Cancel", command=self._cancel_restore, state="disabled")
        self.btn_cancel.pack(side="right", padx=3)

        self.btn_start = ttk.Button(act_row, text="▶ Start Restore", command=self._start_restore)
        self.btn_start.pack(side="right", padx=3)

        # 5. Restore Progress & Status
        self.restore_progress = ttk.Progressbar(self, mode="determinate")
        self.restore_progress.pack(fill="x", padx=6, pady=(3, 2))

        self.restore_status_var = tk.StringVar(value="Select a backup file (.upg or .zip) and click 'Open & Inspect Backup'.")
        ttk.Label(self, textvariable=self.restore_status_var, anchor="w", relief="sunken", padding=3).pack(fill="x", padx=4, pady=(0, 2))

    # ─────────────────────────────────────────────────────────────
    # File Selection & Inspection
    # ─────────────────────────────────────────────────────────────
    def _browse_archive(self):
        f = filedialog.askopenfilename(
            title="Open Upganam Backup Archive",
            filetypes=[
                ("Upganam Backups (*.upg, *.zip)", "*.upg;*.zip"),
                ("Encrypted Containers (*.upg)", "*.upg"),
                ("ZIP Archives (*.zip)", "*.zip"),
                ("All files", "*.*")
            ]
        )
        if f:
            self.restore_file_var.set(f)
            if f.lower().endswith(".zip"):
                self.restore_pass_lbl.configure(state="disabled")
                self.restore_pass_entry.configure(state="disabled")
                self.chk_restore_show_pass.configure(state="disabled")
                self.restore_pass_note_var.set("(ZIP selected — No password required)")
            else:
                self.restore_pass_lbl.configure(state="normal")
                self.restore_pass_entry.configure(state="normal")
                self.chk_restore_show_pass.configure(state="normal")
                self.restore_pass_note_var.set("(Password required for .upg container)")

    def _inspect_and_load_backup(self):
        archive_path = self.restore_file_var.get().strip()
        if not archive_path:
            messagebox.showwarning("Missing File", "Please select a backup container (.upg or .zip).")
            return
        if not os.path.isfile(archive_path):
            messagebox.showerror("File Not Found", f"The file '{archive_path}' does not exist.")
            return

        is_encrypted = is_encrypted_container(archive_path)
        password = self.restore_pass_var.get()

        if is_encrypted:
            if not password:
                messagebox.showwarning("Password Required", "This backup is encrypted (.upg). Please enter your password.")
                self.restore_pass_entry.focus_set()
                return
            if not is_crypto_available():
                messagebox.showerror(
                    "Missing Library",
                    "The 'cryptography' Python package is required to decrypt backups.\n\n"
                    "Please install it using:\npip install cryptography"
                )
                return

        self._cleanup_temp()

        self.btn_inspect_backup.configure(state="disabled")
        status_msg = "Decrypting and reading backup container…" if is_encrypted else "Reading backup archive…"
        self.restore_status_var.set(status_msg)
        self.restore_progress.configure(value=30)
        self.winfo_toplevel().config(cursor="watch")

        def worker():
            try:
                temp_dir = None
                if is_encrypted:
                    temp_dir = tempfile.mkdtemp(prefix="upganam_dec_")
                    target_zip = os.path.join(temp_dir, "archive.zip")
                    decrypt_file(archive_path, target_zip, password)
                else:
                    target_zip = archive_path

                parsed_items = inspect_archive(target_zip)
                self.after(0, lambda: self._on_loaded_success(temp_dir, target_zip, parsed_items, is_encrypted))
            except (InvalidTag, ValueError):
                self.after(0, lambda: self._on_loaded_error(
                    "Decryption Failed",
                    "Incorrect password or corrupted backup file.\n\nPlease verify your password."
                ))
            except Exception as e:
                self.after(0, lambda e=e: self._on_loaded_error("Error Reading Backup", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded_success(self, temp_dir, target_zip, parsed_items, is_encrypted):
        self.winfo_toplevel().config(cursor="")
        self.btn_inspect_backup.configure(state="normal")
        self.restore_progress.configure(value=100)

        self.loaded_restore_temp_dir = temp_dir
        self.loaded_restore_zip_path = target_zip
        self.restore_archive_entries = parsed_items
        self.restore_selected_indices = set(range(len(parsed_items)))

        self.restore_tree.delete(*self.restore_tree.get_children())
        self.iid_to_restore_idx.clear()

        for idx, item in enumerate(parsed_items):
            mark = "☑"
            icon = "📁 " if item["is_dir"] else "📄 "
            size_disp = "<DIR>" if item["is_dir"] else format_size(item["size"])
            iid = self.restore_tree.insert(
                "", "end",
                values=(mark, f"{icon}{item['name']}", item["original_path"], size_disp)
            )
            self.iid_to_restore_idx[iid] = idx

        total_size = sum(item["size"] for item in parsed_items if not item["is_dir"])
        container_type = "Encrypted container" if is_encrypted else "ZIP archive"
        self.restore_sel_count_var.set(
            f"Selected: {len(parsed_items)} of {len(parsed_items)} item(s) ({format_size(total_size)})"
        )
        self.restore_status_var.set(
            f"{container_type} loaded ({len(parsed_items)} items). Choose items and click 'Start Restore'."
        )

    def _on_loaded_error(self, title, msg):
        self.winfo_toplevel().config(cursor="")
        self.btn_inspect_backup.configure(state="normal")
        self.restore_progress.configure(value=0)
        self._cleanup_temp()
        self.restore_status_var.set(f"Failed to open backup: {title}")
        messagebox.showerror(title, msg)

    def _cleanup_temp(self):
        if self.loaded_restore_temp_dir and os.path.exists(self.loaded_restore_temp_dir):
            import shutil
            shutil.rmtree(self.loaded_restore_temp_dir, ignore_errors=True)
        self.loaded_restore_temp_dir = None
        self.loaded_restore_zip_path = None

    # ─────────────────────────────────────────────────────────────
    # Treeview Interaction
    # ─────────────────────────────────────────────────────────────
    def _toggle_item(self, iid):
        idx = self.iid_to_restore_idx.get(iid)
        if idx is None:
            return
        if idx in self.restore_selected_indices:
            self.restore_selected_indices.remove(idx)
            new_mark = "☐"
        else:
            self.restore_selected_indices.add(idx)
            new_mark = "☑"
        vals = list(self.restore_tree.item(iid, "values"))
        vals[0] = new_mark
        self.restore_tree.item(iid, values=vals)
        self._update_selection_status()

    def _on_tree_click(self, event):
        region = self.restore_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.restore_tree.identify_column(event.x)
        item = self.restore_tree.identify_row(event.y)
        if item and col == "#1":
            self._toggle_item(item)

    def _on_dblclick(self, event):
        col = self.restore_tree.identify_column(event.x)
        if col == "#1":
            return
        item = self.restore_tree.identify_row(event.y)
        if item:
            self._toggle_item(item)

    def _on_space(self, event):
        selected_iids = self.restore_tree.selection()
        if not selected_iids:
            return "break"
        any_unchecked = any(
            self.iid_to_restore_idx[i] not in self.restore_selected_indices
            for i in selected_iids if i in self.iid_to_restore_idx
        )
        for i in selected_iids:
            if i not in self.iid_to_restore_idx:
                continue
            idx = self.iid_to_restore_idx[i]
            if any_unchecked:
                self.restore_selected_indices.add(idx)
                mark = "☑"
            else:
                self.restore_selected_indices.discard(idx)
                mark = "☐"
            vals = list(self.restore_tree.item(i, "values"))
            vals[0] = mark
            self.restore_tree.item(i, values=vals)
        self._update_selection_status()
        return "break"

    def _sel_all(self):
        self.restore_selected_indices = set(range(len(self.restore_archive_entries)))
        for iid in self.restore_tree.get_children():
            vals = list(self.restore_tree.item(iid, "values"))
            vals[0] = "☑"
            self.restore_tree.item(iid, values=vals)
        self._update_selection_status()

    def _desel_all(self):
        self.restore_selected_indices.clear()
        for iid in self.restore_tree.get_children():
            vals = list(self.restore_tree.item(iid, "values"))
            vals[0] = "☐"
            self.restore_tree.item(iid, values=vals)
        self._update_selection_status()

    def _update_selection_status(self):
        sel_count = len(self.restore_selected_indices)
        total = len(self.restore_archive_entries)
        sel_bytes = sum(self.restore_archive_entries[i]["size"] for i in self.restore_selected_indices)
        self.restore_sel_count_var.set(
            f"Selected: {sel_count} of {total} item(s) ({format_size(sel_bytes)})"
        )

    def _sort_column(self, col, reverse):
        items = [(self.restore_tree.item(k, "values"), k) for k in self.restore_tree.get_children("")]
        def sort_key(element):
            val, iid = element
            idx = self.iid_to_restore_idx.get(iid)
            entry = self.restore_archive_entries[idx] if idx is not None else None
            is_dir = entry["is_dir"] if entry else False
            if col == "name":
                return (not is_dir, val[1].lower())
            elif col == "original_path":
                return (not is_dir, val[2].lower())
            elif col == "size":
                size = entry["size"] if entry else 0
                return (not is_dir, size)
            elif col == "sel":
                return (val[0] != "☑", entry["name"].lower() if entry else "")
            return val[0]

        items.sort(key=sort_key, reverse=reverse)
        for idx, (_, k) in enumerate(items):
            self.restore_tree.move(k, "", idx)
        self.restore_tree.heading(col, command=lambda: self._sort_column(col, not reverse))

    # ─────────────────────────────────────────────────────────────
    # Restore Execution to Device
    # ─────────────────────────────────────────────────────────────
    def _start_restore(self):
        serial = self.app.get_selected_serial()
        if not serial:
            messagebox.showwarning("No Device", "Please select a connected target Android device.")
            return

        if not self.loaded_restore_zip_path or not os.path.isfile(self.loaded_restore_zip_path):
            messagebox.showwarning("No Backup Loaded", "Please open and inspect a backup archive (.upg or .zip) first.")
            return

        if not self.restore_selected_indices:
            messagebox.showinfo("Nothing Selected", "Please select at least one item to restore to the device.")
            return

        mode = self.restore_mode_var.get()
        custom_base = self.custom_restore_dir_var.get().strip().replace("\\", "/")
        if mode == "custom" and not custom_base:
            messagebox.showwarning("Custom Path Empty", "Please specify a destination folder path on the device.")
            return

        chosen_items = [self.restore_archive_entries[i] for i in sorted(self.restore_selected_indices)]

        self.is_restoring = True
        self.cancel_restore_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.restore_progress.configure(value=0)

        zip_path = self.loaded_restore_zip_path

        threading.Thread(
            target=self._restore_worker,
            args=(chosen_items, zip_path, serial, mode, custom_base),
            daemon=True
        ).start()

    def _cancel_restore(self):
        if self.is_restoring:
            if messagebox.askyesno("Cancel Restore", "Are you sure you want to cancel the restore in progress?"):
                self.cancel_restore_event.set()
                self.restore_status_var.set("Cancelling restore...")
                if self.current_restore_proc:
                    try:
                        self.current_restore_proc.kill()
                    except Exception:
                        pass

    def _restore_worker(self, items, zip_path, serial, mode, custom_base):
        total = len(items)
        successes = 0
        errors = []

        extract_tmp = tempfile.mkdtemp(prefix="upganam_rest_extract_")

        try:
            for i, item in enumerate(items, 1):
                if self.cancel_restore_event.is_set():
                    break

                name = item["name"]
                self.after(0, lambda i=i, t=total, n=name:
                    self.restore_status_var.set(f"Restoring ({i}/{t}): {n} …"))
                self.after(0, lambda i=i, t=total:
                    self.restore_progress.configure(value=((i - 1) / total) * 100))

                if mode == "original":
                    target_remote = item["original_path"]
                else:
                    target_remote = posixpath.join(custom_base, name.replace("\\", "/"))

                try:
                    extracted_path = extract_file_from_archive(zip_path, name, extract_tmp)
                except Exception as e:
                    errors.append((name, f"Extraction failed: {e}"))
                    continue

                remote_parent = posixpath.dirname(target_remote)
                if remote_parent and remote_parent != "/":
                    adb_shell(f"mkdir -p {shlex.quote(remote_parent)}", serial=serial)

                cmd = [ADB_BIN]
                if serial:
                    cmd.extend(["-s", serial])
                cmd.extend(["push", extracted_path, target_remote])

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
                    self.current_restore_proc = proc
                    out, _ = proc.communicate()
                    self.current_restore_proc = None

                    if self.cancel_restore_event.is_set():
                        break

                    if proc.returncode == 0:
                        successes += 1
                    else:
                        err_msg = out.strip() if out else f"Failed with exit code {proc.returncode}"
                        errors.append((name, err_msg))
                        if any(fatal in err_msg.lower() for fatal in ("device not found", "device offline", "no devices/emulators")):
                            errors.append((name, "Device disconnected. Aborting restore."))
                            break
                except Exception as e:
                    errors.append((name, str(e)))

        finally:
            if os.path.exists(extract_tmp):
                import shutil
                shutil.rmtree(extract_tmp, ignore_errors=True)

        self.after(0, lambda: self._on_restore_completed(total, successes, errors))

    def _on_restore_completed(self, total, successes, errors):
        self.is_restoring = False
        self.current_restore_proc = None
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")

        if self.cancel_restore_event.is_set():
            self.restore_status_var.set(f"Restore cancelled. {successes}/{total} item(s) restored.")
            messagebox.showinfo(
                "Restore Cancelled",
                f"Restore was cancelled.\n\n{successes} item(s) were restored to the device."
            )
            return

        self.restore_progress.configure(value=100)
        if not errors:
            self.restore_status_var.set(f"Done! {total} item(s) successfully restored to device.")
            messagebox.showinfo(
                "Restore Complete",
                f"Successfully restored all {total} item(s) to device '{self.app.get_selected_serial()}'!"
            )
        else:
            self.restore_status_var.set(f"Finished with warnings: {successes} succeeded, {len(errors)} failed.")
            detail_lines = [f"• {n}: {err}" for n, err in errors[:10]]
            if len(errors) > 10:
                detail_lines.append(f"... and {len(errors) - 10} more.")
            messagebox.showwarning(
                "Restore Completed with Warnings",
                f"{successes} of {total} item(s) restored successfully.\n\n"
                f"Failed items:\n" + "\n".join(detail_lines)
            )
