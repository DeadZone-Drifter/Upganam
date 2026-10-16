"""
Main Application Window for Upganam.
Houses global device detection, combobox selection, wireless ADB connectivity dialog,
and the tabbed notebook hosting BackupTab and RestoreTab.
"""
try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    tk = None
    ttk = None
    messagebox = None

from upganam.core.adb import (
    is_adb_available,
    get_devices,
    adb_connect,
    adb_disconnect,
    adb_pair
)
from upganam.ui.backup_tab import BackupTab
from upganam.ui.restore_tab import RestoreTab

class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Upganam — Android Phone Backup & Restore")
        self.root.geometry("1080x820")
        self.root.minsize(840, 600)

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
        self.device_combo = ttk.Combobox(dev_frame, textvariable=self.device_var, state="readonly", width=42)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(dev_frame, text="🔍 Detect Devices", command=self.check_devices).pack(side="left", padx=4)
        ttk.Button(dev_frame, text="📶 Connect Wi-Fi", command=self.open_wireless_dialog).pack(side="left", padx=4)

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
                "2. Enable USB Debugging (or Wireless Debugging)\n"
                "3. Connect via USB or click '📶 Connect Wi-Fi'\n"
                "4. Tap 'Allow debugging' on your phone screen\n"
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

    # ─────────────────────────────────────────────────────────────
    # Wireless ADB Connection Dialog
    # ─────────────────────────────────────────────────────────────
    def open_wireless_dialog(self):
        """Display modal dialog to pair and connect to Android devices over Wi-Fi."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Connect via Wireless ADB (Wi-Fi)")
        dialog.geometry("480x420")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        pad_opts = {"padx": 10, "pady": 6}

        # Header Info
        header_frame = ttk.Frame(dialog, padding=10)
        header_frame.pack(fill="x")
        ttk.Label(
            header_frame,
            text="Connect to your Android phone over Wi-Fi without a USB cable.\n"
                 "Make sure your phone and PC are on the same Wi-Fi network.",
            font=("", 9)
        ).pack(anchor="w")

        # Section 1: Direct Connect
        conn_frame = ttk.LabelFrame(dialog, text="1. Connect to IP & Port", padding=10)
        conn_frame.pack(fill="x", **pad_opts)

        ip_row = ttk.Frame(conn_frame)
        ip_row.pack(fill="x", pady=4)
        ttk.Label(ip_row, text="Phone IP:Port :", width=16).pack(side="left")
        ip_var = tk.StringVar(value="192.168.1. :5555")
        ip_entry = ttk.Entry(ip_row, textvariable=ip_var, width=28)
        ip_entry.pack(side="left", fill="x", expand=True)

        btn_row = ttk.Frame(conn_frame)
        btn_row.pack(fill="x", pady=(6, 2))

        def do_connect():
            target = ip_var.get().strip()
            if not target:
                messagebox.showerror("Error", "Please enter phone IP and port.", parent=dialog)
                return
            success, msg = adb_connect(target)
            if success:
                messagebox.showinfo("Connected", f"Successfully connected to {target}!\n\n{msg}", parent=dialog)
                dialog.destroy()
                self.check_devices()
            else:
                messagebox.showerror("Connection Failed", f"Could not connect to {target}.\n\nDetails:\n{msg}", parent=dialog)

        def do_disconnect():
            target = ip_var.get().strip()
            success, msg = adb_disconnect(target if ":" in target else None)
            messagebox.showinfo("Wireless ADB", msg, parent=dialog)
            self.check_devices()

        ttk.Button(btn_row, text="⚡ Connect", command=do_connect).pack(side="left", padx=4)
        ttk.Button(btn_row, text="🔌 Disconnect", command=do_disconnect).pack(side="left", padx=4)

        # Section 2: Android 11+ Pairing (Optional)
        pair_frame = ttk.LabelFrame(dialog, text="2. Android 11+ Wi-Fi Pairing (If Required)", padding=10)
        pair_frame.pack(fill="x", **pad_opts)

        pair_ip_row = ttk.Frame(pair_frame)
        pair_ip_row.pack(fill="x", pady=3)
        ttk.Label(pair_ip_row, text="Pairing IP:Port :", width=16).pack(side="left")
        pair_ip_var = tk.StringVar()
        ttk.Entry(pair_ip_row, textvariable=pair_ip_var, width=28).pack(side="left", fill="x", expand=True)

        code_row = ttk.Frame(pair_frame)
        code_row.pack(fill="x", pady=3)
        ttk.Label(code_row, text="6-Digit Code :", width=16).pack(side="left")
        code_var = tk.StringVar()
        ttk.Entry(code_row, textvariable=code_var, width=28).pack(side="left", fill="x", expand=True)

        def do_pair():
            p_target = pair_ip_var.get().strip()
            p_code = code_var.get().strip()
            if not p_target or not p_code:
                messagebox.showerror("Error", "Enter both pairing address (IP:Port) and 6-digit code.", parent=dialog)
                return
            success, msg = adb_pair(p_target, p_code)
            if success:
                messagebox.showinfo("Paired", f"Successfully paired!\n\nNow enter the main connection port in Section 1 and click Connect.", parent=dialog)
            else:
                messagebox.showerror("Pairing Failed", f"Pairing failed:\n\n{msg}", parent=dialog)

        ttk.Button(pair_frame, text="🔑 Pair Device", command=do_pair).pack(anchor="w", pady=(6, 2))

        # Close button
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(side="bottom", pady=10)
