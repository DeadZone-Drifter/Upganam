"""
Main Application Window for Upganam.
Houses global device detection, combobox selection, and the tabbed notebook
hosting BackupTab and RestoreTab.
"""
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    tk = None
    ttk = None
    messagebox = None

from upganam.core.adb import is_adb_available, get_devices
from upganam.ui.backup_tab import BackupTab
from upganam.ui.restore_tab import RestoreTab

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Upganam — Android Phone Backup & Restore")
        self.root.geometry("1060x800")
        self.root.minsize(820, 580)

        self.selected_device_serial = None
        self.devices_cache = []

        self._build_ui()
        self.check_devices()

    def _build_ui(self):
        # 1. Global Device Selection Frame (Shared by all tabs)
        dev_frame = ttk.LabelFrame(self.root, text="Connected Android Device", padding=6)
        dev_frame.pack(fill="x", padx=8, pady=(6, 4))

        ttk.Label(dev_frame, text="Active Device:").pack(side="left", padx=(5, 2))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(dev_frame, textvariable=self.device_var, state="readonly", width=46)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(dev_frame, text="🔍 Detect Devices", command=self.check_devices).pack(side="left", padx=5)

        self.global_device_status_var = tk.StringVar(value="Checking devices…")
        ttk.Label(dev_frame, textvariable=self.global_device_status_var, font=("", 9, "italic")).pack(side="right", padx=6)

        # 2. Main Tabbed Notebook
        self.notebook = ttk.Notebook(self.root)
        self.backup_tab = BackupTab(self.notebook, app=self)
        self.restore_tab = RestoreTab(self.notebook, app=self)

        self.notebook.add(self.backup_tab, text="  📤 Backup to PC  ")
        self.notebook.add(self.restore_tab, text="  📥 Restore to Device  ")
        self.notebook.pack(fill="both", expand=True, padx=8, pady=4)

    # ─────────────────────────────────────────────────────────────
    # Global Device Management
    # ─────────────────────────────────────────────────────────────
    def get_selected_serial(self):
        """Return the serial number of the currently selected Android device."""
        return self.selected_device_serial

    def check_devices(self):
        """Scan for connected Android devices and update selector dropdown."""
        avail, path_info = is_adb_available()
        if not avail:
            self.selected_device_serial = None
            self.device_combo["values"] = []
            self.device_var.set("ADB executable not found")
            self.global_device_status_var.set("ADB Not Found")
            self.backup_tab.backup_status_var.set(f"ADB not found at '{path_info}'.")
            self.restore_tab.restore_status_var.set(f"ADB not found at '{path_info}'.")
            messagebox.showerror(
                "ADB Not Found",
                f"Could not locate the ADB executable ('{path_info}').\n\n"
                "Please verify that ADB is installed on your system or place "
                "'adb.exe' (or 'adb') in the Upganam directory."
            )
            return

        devices = get_devices()
        self.devices_cache = devices

        if not devices:
            self.selected_device_serial = None
            self.device_combo["values"] = []
            self.device_var.set("No device found")
            self.global_device_status_var.set("No Device")
            self.backup_tab.backup_status_var.set("No Android device detected.")
            self.backup_tab.backup_tree.delete(*self.backup_tab.backup_tree.get_children())
            self.backup_tab.iid_to_backup_entry.clear()
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
            self.global_device_status_var.set("Unauthorized")
            self.backup_tab.backup_status_var.set(f"Device {current_dev['serial']} unauthorized! Unlock phone to accept prompt.")
            messagebox.showwarning(
                "Device Unauthorized",
                f"Device '{current_dev['serial']}' is connected but unauthorized.\n\n"
                "Please unlock your phone and tap 'Allow' on the USB debugging prompt."
            )
            return

        self.global_device_status_var.set(f"Ready ({self.selected_device_serial})")
        self.backup_tab.backup_status_var.set(f"Connected: {self.selected_device_serial}")
        self.backup_tab.refresh()

    def _on_device_selected(self, event=None):
        idx = self.device_combo.current()
        if 0 <= idx < len(self.devices_cache):
            self.selected_device_serial = self.devices_cache[idx]["serial"]
            self.global_device_status_var.set(f"Ready ({self.selected_device_serial})")
            self.backup_tab.refresh()
