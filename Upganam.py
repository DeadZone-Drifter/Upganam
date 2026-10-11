"""
Upganam — Android Phone Backup & Restore Tool
Single .exe after PyInstaller build. ADB is bundled.
Supports folder hierarchy preservation, encrypted/compressed backups,
and selective item restoration to any connected Android device.
"""
import os
import sys
import shutil
import posixpath
import shlex
import re
import json
import struct
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

# ── Cryptography & Archive Helpers ─────────────────────────────
try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    InvalidTag = Exception

MAGIC_HEADER = b"UPG_ENC1"
CHUNK_SIZE = 1024 * 1024  # 1 MB chunk for streaming AES-GCM

def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return kdf.derive(password.encode("utf-8"))

def encrypt_file(src_path: str, dst_path: str, password: str):
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("The 'cryptography' library is required for encrypted backups. Run: pip install cryptography")
    salt = os.urandom(16)
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    with open(src_path, "rb") as in_f, open(dst_path, "wb") as out_f:
        out_f.write(MAGIC_HEADER + salt)
        chunk_idx = 0
        while True:
            data = in_f.read(CHUNK_SIZE)
            is_last = 1 if len(data) < CHUNK_SIZE else 0
            nonce = os.urandom(12)
            aad = struct.pack(">IB", chunk_idx, is_last)
            ct = aesgcm.encrypt(nonce, data, aad)
            out_f.write(aad + nonce + struct.pack(">I", len(ct)) + ct)
            chunk_idx += 1
            if is_last:
                break

def decrypt_file(src_path: str, dst_path: str, password: str):
    if not HAS_CRYPTOGRAPHY:
        raise RuntimeError("The 'cryptography' library is required for encrypted backups. Run: pip install cryptography")
    with open(src_path, "rb") as in_f:
        magic = in_f.read(len(MAGIC_HEADER))
        if magic != MAGIC_HEADER:
            raise ValueError("Invalid file format. Not a recognized Upganam encrypted backup.")
        salt = in_f.read(16)
        if len(salt) < 16:
            raise ValueError("Corrupted backup file: header truncated.")
        key = derive_key(password, salt)
        aesgcm = AESGCM(key)
        with open(dst_path, "wb") as out_f:
            chunk_idx = 0
            while True:
                hdr = in_f.read(5)
                if not hdr:
                    break
                idx, is_last = struct.unpack(">IB", hdr)
                if idx != chunk_idx:
                    raise ValueError("Corrupted chunk order in encrypted backup.")
                nonce = in_f.read(12)
                ct_len_b = in_f.read(4)
                if len(ct_len_b) < 4:
                    raise ValueError("Truncated encrypted chunk.")
                ct_len = struct.unpack(">I", ct_len_b)[0]
                ct = in_f.read(ct_len)
                if len(ct) < ct_len:
                    raise ValueError("Truncated ciphertext payload.")
                pt = aesgcm.decrypt(nonce, ct, hdr)
                out_f.write(pt)
                chunk_idx += 1
                if is_last:
                    break

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

def is_adb_available():
    """Verify whether the ADB binary is present on the filesystem or PATH."""
    adb_bin = _adb_path()
    if os.path.isabs(adb_bin) and os.path.isfile(adb_bin):
        return True, adb_bin
    which = shutil.which(adb_bin)
    if which and os.path.isfile(which):
        return True, which
    return False, adb_bin

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
            is_symlink = perm.startswith('l')
            symlink_target = ""
            if is_symlink and ' -> ' in name:
                parts_name = name.split(' -> ', 1)
                name = parts_name[0].strip()
                symlink_target = parts_name[1].strip()
            if name in ('.', '..'):
                continue
            is_dir = perm.startswith('d')
            if is_symlink:
                clean_target = symlink_target.rstrip('/')
                is_dir = (
                    symlink_target.endswith('/') or
                    clean_target.endswith(('storage', 'sdcard', 'emulated', '0', 'media', 'Download', 'DCIM', 'Documents', 'Pictures'))
                )
            try:
                size_int = int(size_str)
            except ValueError:
                size_int = 0
            entries.append({
                "name": name,
                "path": posixpath.join(base_path, name),
                "is_dir": is_dir,
                "is_symlink": is_symlink,
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
                is_dir = perm.startswith('d')
                entries.append({
                    "name": name,
                    "path": posixpath.join(base_path, name),
                    "is_dir": is_dir,
                    "is_symlink": perm.startswith('l'),
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

def get_relative_backup_path(remote_path, base_root="/storage/emulated/0"):
    """Compute a relative destination path from remote Android path to preserve hierarchy."""
    clean_remote = posixpath.normpath(remote_path)
    clean_base = posixpath.normpath(base_root)
    if clean_remote == clean_base:
        return posixpath.basename(clean_remote) or "root"
    if clean_remote.startswith(clean_base + "/"):
        return clean_remote[len(clean_base) + 1:]
    for prefix in ("/storage/emulated/0/", "/sdcard/"):
        if clean_remote.startswith(prefix):
            return clean_remote[len(prefix):]
    return clean_remote.lstrip("/")

# ── GUI Application ──────────────────────────────────────────
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
        self.root.title("Upganam — Android Phone Backup & Restore")
        self.root.geometry("1040x780")
        self.root.minsize(800, 560)

        self.current_path = "/storage/emulated/0"
        self.selected_device_serial = None
        self.devices_cache = []

        # Backup state
        self.backup_selected_paths = {}  # Map path -> entry dict
        self.iid_to_backup_entry = {}
        self.is_backing_up = False
        self.cancel_backup_event = threading.Event()
        self.current_backup_proc = None

        # Restore state
        self.restore_archive_entries = []  # List of manifest dicts
        self.restore_selected_indices = set()
        self.iid_to_restore_idx = {}
        self.loaded_restore_zip_path = None
        self.loaded_restore_temp_dir = None
        self.is_restoring = False
        self.cancel_restore_event = threading.Event()
        self.current_restore_proc = None

        self._build_ui()
        self._check_device()

    def _build_ui(self):
        # Top Global Device Selection Frame
        dev_frame = ttk.LabelFrame(self.root, text="Connected Android Device", padding=6)
        dev_frame.pack(fill="x", padx=8, pady=(6, 4))

        ttk.Label(dev_frame, text="Active Device:").pack(side="left", padx=(5, 2))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(dev_frame, textvariable=self.device_var, state="readonly", width=46)
        self.device_combo.pack(side="left", padx=5)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(dev_frame, text="🔍 Detect Devices", command=self._check_device).pack(side="left", padx=5)

        self.global_device_status_var = tk.StringVar(value="Checking devices…")
        ttk.Label(dev_frame, textvariable=self.global_device_status_var, font=("", 9, "italic")).pack(side="right", padx=6)

        # Tabbed Interface: Backup and Restore
        self.notebook = ttk.Notebook(self.root)
        self.backup_tab = ttk.Frame(self.notebook)
        self.restore_tab = ttk.Frame(self.notebook)

        self.notebook.add(self.backup_tab, text="  📤 Backup to PC  ")
        self.notebook.add(self.restore_tab, text="  📥 Restore to Device  ")
        self.notebook.pack(fill="both", expand=True, padx=8, pady=4)

        self._build_backup_tab()
        self._build_restore_tab()

    # ─────────────────────────────────────────────────────────────
    # Backup Tab UI & Actions
    # ─────────────────────────────────────────────────────────────
    def _build_backup_tab(self):
        # 1. Navigation Bar
        nav_frame = ttk.Frame(self.backup_tab, padding=4)
        nav_frame.pack(fill="x", padx=4, pady=2)

        ttk.Button(nav_frame, text="⬆ Up", width=6, command=self._go_up).pack(side="left", padx=2)
        ttk.Button(nav_frame, text="🔄 Refresh", width=9, command=self._refresh_backup).pack(side="left", padx=2)

        ttk.Label(nav_frame, text="Device Path:").pack(side="left", padx=(6, 2))
        self.path_var = tk.StringVar(value=self.current_path)
        self.path_entry = ttk.Entry(nav_frame, textvariable=self.path_var)
        self.path_entry.pack(side="left", fill="x", expand=True, padx=4)
        self.path_entry.bind("<Return>", lambda e: self._cd())
        ttk.Button(nav_frame, text="Go", width=5, command=self._cd).pack(side="left", padx=2)

        # Quick Jump Bar
        bm_frame = ttk.Frame(self.backup_tab, padding=(4, 0, 4, 4))
        bm_frame.pack(fill="x", padx=4)
        ttk.Label(bm_frame, text="Quick Jump:", font=("", 9, "bold")).pack(side="left", padx=(5, 5))
        for path, label in self.ROOTS:
            ttk.Button(bm_frame, text=label, command=lambda p=path: self._jump(p)).pack(side="left", padx=2)

        # 2. Main Treeview
        mid = ttk.Frame(self.backup_tab, padding=4)
        mid.pack(fill="both", expand=True, padx=4, pady=2)

        cols = ("sel", "name", "size")
        self.backup_tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.backup_tree.heading("sel", text="✓", command=lambda: self._sort_backup_column("sel", False))
        self.backup_tree.heading("name", text="Name", command=lambda: self._sort_backup_column("name", False))
        self.backup_tree.heading("size", text="Size", command=lambda: self._sort_backup_column("size", False))

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

        self.backup_tree.bind("<Button-1>", self._on_backup_tree_click)
        self.backup_tree.bind("<Double-1>", self._on_backup_dblclick)
        self.backup_tree.bind("<space>", self._on_backup_space)

        # 3. Selection Actions
        sel_bar = ttk.Frame(self.backup_tab, padding=4)
        sel_bar.pack(fill="x", padx=4)

        ttk.Button(sel_bar, text="Select All", command=self._backup_sel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Deselect All", command=self._backup_desel_all).pack(side="left", padx=2)
        ttk.Button(sel_bar, text="Clear All Selections", command=self._backup_clear_all_selections).pack(side="left", padx=2)

        self.backup_sel_count_var = tk.StringVar(value="Selected: 0 item(s)")
        ttk.Label(sel_bar, textvariable=self.backup_sel_count_var, font=("", 9, "bold")).pack(side="left", padx=(15, 5))

        # 4. Backup Options & Destination Controls
        opts_frame = ttk.LabelFrame(self.backup_tab, text="Backup Settings & Security", padding=6)
        opts_frame.pack(fill="x", padx=4, pady=3)

        self.encrypt_backup_var = tk.BooleanVar(value=True)
        chk_enc = ttk.Checkbutton(
            opts_frame,
            text="🔒 Compress & Encrypt Backup (.upg container)",
            variable=self.encrypt_backup_var,
            command=self._on_backup_mode_toggled
        )
        chk_enc.grid(row=0, column=0, sticky="w", padx=5, pady=2)

        self.preserve_hierarchy_var = tk.BooleanVar(value=True)
        self.chk_preserve = ttk.Checkbutton(
            opts_frame,
            text="📁 Preserve folder hierarchy",
            variable=self.preserve_hierarchy_var
        )
        self.chk_preserve.grid(row=0, column=1, sticky="w", padx=10, pady=2)

        # Password row
        self.pass_label = ttk.Label(opts_frame, text="Encryption Password:")
        self.pass_label.grid(row=1, column=0, sticky="w", padx=5, pady=2)

        self.backup_pass_var = tk.StringVar()
        self.backup_pass_entry = ttk.Entry(opts_frame, textvariable=self.backup_pass_var, show="*", width=32)
        self.backup_pass_entry.grid(row=1, column=1, sticky="w", padx=5, pady=2)

        self.show_backup_pass_var = tk.BooleanVar(value=False)
        self.chk_show_pass = ttk.Checkbutton(
            opts_frame,
            text="Show Password",
            variable=self.show_backup_pass_var,
            command=lambda: self.backup_pass_entry.configure(show="" if self.show_backup_pass_var.get() else "*")
        )
        self.chk_show_pass.grid(row=1, column=2, sticky="w", padx=5, pady=2)

        # Destination row
        dst_row = ttk.Frame(opts_frame)
        dst_row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        opts_frame.columnconfigure(1, weight=1)

        ttk.Label(dst_row, text="Save to:").pack(side="left", padx=(5, 2))
        default_backup = os.path.join(os.path.expanduser("~"), "Upganam_Backup.upg")
        self.backup_dest_var = tk.StringVar(value=default_backup)
        self.entry_backup_dest = ttk.Entry(dst_row, textvariable=self.backup_dest_var, width=50)
        self.entry_backup_dest.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(dst_row, text="Browse…", command=self._browse_backup_dest).pack(side="left", padx=2)

        self.btn_backup_cancel = ttk.Button(dst_row, text="⏹ Cancel", command=self._cancel_backup, state="disabled")
        self.btn_backup_cancel.pack(side="right", padx=3)

        self.btn_backup_start = ttk.Button(dst_row, text="▶ Start Backup", command=self._start_backup)
        self.btn_backup_start.pack(side="right", padx=3)

        # 5. Progress and Status Bar
        self.backup_progress = ttk.Progressbar(self.backup_tab, mode="determinate")
        self.backup_progress.pack(fill="x", padx=6, pady=(3, 2))

        self.backup_status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.backup_tab, textvariable=self.backup_status_var, anchor="w", relief="sunken", padding=3).pack(fill="x", padx=4, pady=(0, 2))

        self._on_backup_mode_toggled()

    def _on_backup_mode_toggled(self):
        is_enc = self.encrypt_backup_var.get()
        curr_path = self.backup_dest_var.get()
        if is_enc:
            self.pass_label.configure(state="normal")
            self.backup_pass_entry.configure(state="normal")
            self.chk_show_pass.configure(state="normal")
            self.chk_preserve.configure(state="disabled")
            if not curr_path.endswith(".upg"):
                if os.path.isdir(curr_path):
                    self.backup_dest_var.set(os.path.join(curr_path, "Upganam_Backup.upg"))
                else:
                    self.backup_dest_var.set(os.path.splitext(curr_path)[0] + ".upg")
        else:
            self.pass_label.configure(state="disabled")
            self.backup_pass_entry.configure(state="disabled")
            self.chk_show_pass.configure(state="disabled")
            self.chk_preserve.configure(state="normal")
            if curr_path.endswith(".upg"):
                self.backup_dest_var.set(os.path.splitext(curr_path)[0])

    def _browse_backup_dest(self):
        if self.encrypt_backup_var.get():
            init_file = os.path.basename(self.backup_dest_var.get()) or "Upganam_Backup.upg"
            init_dir = os.path.dirname(self.backup_dest_var.get()) or os.path.expanduser("~")
            f = filedialog.asksaveasfilename(
                title="Select Encrypted Backup Container",
                initialdir=init_dir,
                initialfile=init_file,
                filetypes=[("Upganam Backup (*.upg)", "*.upg"), ("All files", "*.*")]
            )
            if f:
                if not f.endswith(".upg"):
                    f += ".upg"
                self.backup_dest_var.set(f)
        else:
            d = filedialog.askdirectory(initialdir=self.backup_dest_var.get() or os.path.expanduser("~"))
            if d:
                self.backup_dest_var.set(d)

    # ─────────────────────────────────────────────────────────────
    # Restore Tab UI & Actions
    # ─────────────────────────────────────────────────────────────
    def _build_restore_tab(self):
        # 1. File Selection & Decryption Bar
        f_frame = ttk.LabelFrame(self.restore_tab, text="Encrypted Backup Container (.upg)", padding=6)
        f_frame.pack(fill="x", padx=4, pady=4)

        row1 = ttk.Frame(f_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Backup File:").pack(side="left", padx=(5, 2))
        self.restore_file_var = tk.StringVar()
        self.restore_file_entry = ttk.Entry(row1, textvariable=self.restore_file_var, width=50)
        self.restore_file_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row1, text="Browse…", command=self._browse_restore_file).pack(side="left", padx=2)

        row2 = ttk.Frame(f_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="Password:").pack(side="left", padx=(5, 2))
        self.restore_pass_var = tk.StringVar()
        self.restore_pass_entry = ttk.Entry(row2, textvariable=self.restore_pass_var, show="*", width=32)
        self.restore_pass_entry.pack(side="left", padx=5)

        self.show_restore_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row2,
            text="Show Password",
            variable=self.show_restore_pass_var,
            command=lambda: self.restore_pass_entry.configure(show="" if self.show_restore_pass_var.get() else "*")
        ).pack(side="left", padx=5)

        self.btn_inspect_backup = ttk.Button(row2, text="🔓 Open & Inspect Backup", command=self._inspect_and_load_backup)
        self.btn_inspect_backup.pack(side="left", padx=10)

        # 2. Backup Content Inspection Treeview
        mid_res = ttk.LabelFrame(self.restore_tab, text="Contents of Backup (Choose files to restore)", padding=4)
        mid_res.pack(fill="both", expand=True, padx=4, pady=3)

        res_cols = ("sel", "name", "original_path", "size")
        self.restore_tree = ttk.Treeview(mid_res, columns=res_cols, show="headings", selectmode="extended")
        self.restore_tree.heading("sel", text="✓", command=lambda: self._sort_restore_column("sel", False))
        self.restore_tree.heading("name", text="File / Relative Path", command=lambda: self._sort_restore_column("name", False))
        self.restore_tree.heading("original_path", text="Original Android Path", command=lambda: self._sort_restore_column("original_path", False))
        self.restore_tree.heading("size", text="Size", command=lambda: self._sort_restore_column("size", False))

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

        self.restore_tree.bind("<Button-1>", self._on_restore_tree_click)
        self.restore_tree.bind("<Double-1>", self._on_restore_dblclick)
        self.restore_tree.bind("<space>", self._on_restore_space)

        # 3. Restore Selection Bar
        res_sel_bar = ttk.Frame(self.restore_tab, padding=4)
        res_sel_bar.pack(fill="x", padx=4)

        ttk.Button(res_sel_bar, text="Select All", command=self._restore_sel_all).pack(side="left", padx=2)
        ttk.Button(res_sel_bar, text="Deselect All", command=self._restore_desel_all).pack(side="left", padx=2)

        self.restore_sel_count_var = tk.StringVar(value="No backup file loaded.")
        ttk.Label(res_sel_bar, textvariable=self.restore_sel_count_var, font=("", 9, "bold")).pack(side="left", padx=(15, 5))

        # 4. Target Destination on Device & Actions
        target_frame = ttk.LabelFrame(self.restore_tab, text="Restore Destination on Device", padding=6)
        target_frame.pack(fill="x", padx=4, pady=3)

        self.restore_mode_var = tk.StringVar(value="original")
        rb_orig = ttk.Radiobutton(
            target_frame,
            text="Restore to original device paths (e.g. /storage/emulated/0/...)",
            variable=self.restore_mode_var,
            value="original"
        )
        rb_orig.pack(anchor="w", padx=5, pady=1)

        row_cust = ttk.Frame(target_frame)
        row_cust.pack(fill="x", padx=5, pady=2)
        rb_custom = ttk.Radiobutton(
            row_cust,
            text="Restore into custom device folder:",
            variable=self.restore_mode_var,
            value="custom"
        )
        rb_custom.pack(side="left")

        self.custom_restore_dir_var = tk.StringVar(value="/storage/emulated/0/Restored_Backup")
        self.entry_custom_restore = ttk.Entry(row_cust, textvariable=self.custom_restore_dir_var, width=40)
        self.entry_custom_restore.pack(side="left", fill="x", expand=True, padx=5)

        act_row = ttk.Frame(target_frame)
        act_row.pack(fill="x", pady=(4, 2))

        self.btn_restore_cancel = ttk.Button(act_row, text="⏹ Cancel", command=self._cancel_restore, state="disabled")
        self.btn_restore_cancel.pack(side="right", padx=3)

        self.btn_restore_start = ttk.Button(act_row, text="▶ Start Restore", command=self._start_restore)
        self.btn_restore_start.pack(side="right", padx=3)

        # 5. Restore Progress & Status
        self.restore_progress = ttk.Progressbar(self.restore_tab, mode="determinate")
        self.restore_progress.pack(fill="x", padx=6, pady=(3, 2))

        self.restore_status_var = tk.StringVar(value="Select an encrypted backup file and click 'Open & Inspect Backup'.")
        ttk.Label(self.restore_tab, textvariable=self.restore_status_var, anchor="w", relief="sunken", padding=3).pack(fill="x", padx=4, pady=(0, 2))

    def _browse_restore_file(self):
        f = filedialog.askopenfilename(
            title="Open Upganam Encrypted Backup",
            filetypes=[("Upganam Backup (*.upg)", "*.upg"), ("All files", "*.*")]
        )
        if f:
            self.restore_file_var.set(f)

    # ─────────────────────────────────────────────────────────────
    # Device Detection & State Handling
    # ─────────────────────────────────────────────────────────────
    def _check_device(self):
        avail, path_info = is_adb_available()
        if not avail:
            self.selected_device_serial = None
            self.device_combo["values"] = []
            self.device_var.set("ADB executable not found")
            self.global_device_status_var.set("ADB Not Found")
            self.backup_status_var.set(f"ADB not found at '{path_info}'.")
            self.restore_status_var.set(f"ADB not found at '{path_info}'.")
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
            self.backup_status_var.set("No Android device detected.")
            self.backup_tree.delete(*self.backup_tree.get_children())
            self.iid_to_backup_entry.clear()
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
            self.backup_status_var.set(f"Device {current_dev['serial']} unauthorized! Unlock phone to accept prompt.")
            messagebox.showwarning(
                "Device Unauthorized",
                f"Device '{current_dev['serial']}' is connected but unauthorized.\n\n"
                "Please unlock your phone and tap 'Allow' on the USB debugging prompt."
            )
            return

        self.global_device_status_var.set(f"Ready ({self.selected_device_serial})")
        self.backup_status_var.set(f"Connected: {self.selected_device_serial}")
        self._refresh_backup()

    def _on_device_selected(self, event=None):
        idx = self.device_combo.current()
        if 0 <= idx < len(self.devices_cache):
            self.selected_device_serial = self.devices_cache[idx]["serial"]
            self.global_device_status_var.set(f"Ready ({self.selected_device_serial})")
            self._refresh_backup()

    # ─────────────────────────────────────────────────────────────
    # Backup Treeview & Navigation Logic
    # ─────────────────────────────────────────────────────────────
    def _refresh_backup(self):
        if not self.selected_device_serial:
            self._check_device()
            return

        self.backup_tree.delete(*self.backup_tree.get_children())
        self.iid_to_backup_entry.clear()
        self.backup_status_var.set(f"Listing {self.current_path} …")
        self.root.config(cursor="watch")

        target_path = self.current_path
        serial = self.selected_device_serial

        def worker():
            entries, err = list_dir(target_path, serial=serial)
            self.root.after(0, lambda: self._on_backup_entries_loaded(target_path, entries, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_backup_entries_loaded(self, path, entries, err):
        self.root.config(cursor="")
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
            icon = "📁 " if e["is_dir"] else "📄 "
            size_disp = "<DIR>" if e["is_dir"] else format_size(e["size"])
            iid = self.backup_tree.insert("", "end", values=(mark, f"{icon}{e['name']}", size_disp))
            self.iid_to_backup_entry[iid] = e

        self.backup_status_var.set(f"{len(entries)} items in {self.current_path}")
        self._update_backup_selection_status()

    def _go_up(self):
        if self.current_path and self.current_path != "/":
            parent = posixpath.dirname(self.current_path.rstrip("/")) or "/"
            self.current_path = parent
            self.path_var.set(parent)
            self._refresh_backup()

    def _cd(self):
        entered = self.path_var.get().strip()
        if entered:
            self.current_path = posixpath.normpath(entered)
            self.path_var.set(self.current_path)
            self._refresh_backup()

    def _jump(self, path):
        self.current_path = path
        self.path_var.set(path)
        self._refresh_backup()

    def _toggle_backup_item(self, iid):
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
        self._update_backup_selection_status()

    def _on_backup_tree_click(self, event):
        region = self.backup_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.backup_tree.identify_column(event.x)
        item = self.backup_tree.identify_row(event.y)
        if item and col == "#1":
            self._toggle_backup_item(item)

    def _on_backup_dblclick(self, event):
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
            self._refresh_backup()
        else:
            self._toggle_backup_item(item)

    def _on_backup_space(self, event):
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
        self._update_backup_selection_status()
        return "break"

    def _backup_sel_all(self):
        for iid, entry in self.iid_to_backup_entry.items():
            self.backup_selected_paths[entry["path"]] = entry
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☑"
            self.backup_tree.item(iid, values=vals)
        self._update_backup_selection_status()

    def _backup_desel_all(self):
        for iid, entry in self.iid_to_backup_entry.items():
            self.backup_selected_paths.pop(entry["path"], None)
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☐"
            self.backup_tree.item(iid, values=vals)
        self._update_backup_selection_status()

    def _backup_clear_all_selections(self):
        self.backup_selected_paths.clear()
        for iid in self.backup_tree.get_children():
            vals = list(self.backup_tree.item(iid, "values"))
            vals[0] = "☐"
            self.backup_tree.item(iid, values=vals)
        self._update_backup_selection_status()

    def _update_backup_selection_status(self):
        self.backup_sel_count_var.set(f"Selected: {len(self.backup_selected_paths)} item(s)")

    def _sort_backup_column(self, col, reverse):
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
        self.backup_tree.heading(col, command=lambda: self._sort_backup_column(col, not reverse))

    # ─────────────────────────────────────────────────────────────
    # Backup Execution (Plain & Encrypted)
    # ─────────────────────────────────────────────────────────────
    def _start_backup(self):
        if not self.selected_device_serial:
            messagebox.showwarning("No Device", "Please select a connected Android device.")
            return

        if not self.backup_selected_paths:
            messagebox.showinfo("Nothing Selected", "Please select at least one file or folder to backup.")
            return

        dest = self.backup_dest_var.get().strip()
        if not dest:
            messagebox.showwarning("No Destination", "Please specify a backup destination.")
            return

        is_enc = self.encrypt_backup_var.get()
        password = self.backup_pass_var.get()
        if is_enc:
            if not HAS_CRYPTOGRAPHY:
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
            if not dest.endswith(".upg"):
                dest += ".upg"
                self.backup_dest_var.set(dest)

        dest_dir = os.path.dirname(dest) if is_enc else dest
        if dest_dir:
            try:
                os.makedirs(dest_dir, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to create destination folder:\n{e}")
                return

        self.is_backing_up = True
        self.cancel_backup_event.clear()
        self.btn_backup_start.configure(state="disabled")
        self.btn_backup_cancel.configure(state="normal")
        self.backup_progress.configure(value=0)

        targets = list(self.backup_selected_paths.values())
        preserve_hier = self.preserve_hierarchy_var.get()
        serial = self.selected_device_serial

        threading.Thread(
            target=self._backup_worker,
            args=(targets, dest, serial, is_enc, password, preserve_hier),
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

    def _backup_worker(self, targets, dest, serial, is_enc, password, preserve_hier):
        total = len(targets)
        successes = 0
        errors = []

        temp_dir = tempfile.mkdtemp(prefix="upganam_bk_") if is_enc else None
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

                self.root.after(0, lambda i=i, t=total, n=name:
                    self.backup_status_var.set(f"Pulling ({i}/{t}): {n} …"))
                self.root.after(0, lambda i=i, t=total:
                    self.backup_progress.configure(value=((i - 1) / total) * (75 if is_enc else 100)))

                if is_enc or preserve_hier:
                    local_target = os.path.join(staging_dir, os.path.normpath(rel_path))
                    os.makedirs(os.path.dirname(local_target), exist_ok=True)
                else:
                    local_target = staging_dir

                cmd = [ADB]
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
                        **_subp_kwargs()
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
                except Exception as e:
                    errors.append((name, str(e)))

            if is_enc and not self.cancel_backup_event.is_set() and successes > 0:
                self.root.after(0, lambda: self.backup_status_var.set("Compressing files into archive…"))
                self.root.after(0, lambda: self.backup_progress.configure(value=80))

                manifest_data = {
                    "version": "1.0",
                    "device_serial": serial,
                    "total_items": successes,
                    "entries": manifest_entries
                }
                manifest_path = os.path.join(temp_dir, "manifest.json")
                with open(manifest_path, "w", encoding="utf-8") as mf:
                    json.dump(manifest_data, mf, indent=2)

                raw_zip_path = os.path.join(temp_dir, "archive.zip")
                with zipfile.ZipFile(raw_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(manifest_path, "manifest.json")
                    for root_d, _, files in os.walk(staging_dir):
                        for f in files:
                            full_f = os.path.join(root_d, f)
                            arc_rel = os.path.relpath(full_f, staging_dir).replace("\\", "/")
                            zf.write(full_f, arc_rel)

                self.root.after(0, lambda: self.backup_status_var.set("Encrypting backup with AES-256-GCM…"))
                self.root.after(0, lambda: self.backup_progress.configure(value=90))

                encrypt_file(raw_zip_path, dest, password)

        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        self.root.after(0, lambda: self._on_backup_completed(total, successes, errors, dest, is_enc))

    def _on_backup_completed(self, total, successes, errors, dest, is_enc):
        self.is_backing_up = False
        self.current_backup_proc = None
        self.btn_backup_start.configure(state="normal")
        self.btn_backup_cancel.configure(state="disabled")

        if self.cancel_backup_event.is_set():
            self.backup_status_var.set(f"Backup cancelled. {successes}/{total} item(s) processed.")
            messagebox.showinfo(
                "Backup Cancelled",
                f"Backup was cancelled.\n\n{successes} item(s) were processed."
            )
            return

        self.backup_progress.configure(value=100)
        mode_text = "Encrypted container created" if is_enc else "Files saved"
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

    # ─────────────────────────────────────────────────────────────
    # Restore Inspection & Parsing
    # ─────────────────────────────────────────────────────────────
    def _inspect_and_load_backup(self):
        upg_path = self.restore_file_var.get().strip()
        if not upg_path:
            messagebox.showwarning("Missing File", "Please select an encrypted backup file (.upg).")
            return
        if not os.path.isfile(upg_path):
            messagebox.showerror("File Not Found", f"The file '{upg_path}' does not exist.")
            return

        password = self.restore_pass_var.get()
        if not password:
            messagebox.showwarning("Password Required", "Please enter the password used to encrypt this backup.")
            self.restore_pass_entry.focus_set()
            return

        if not HAS_CRYPTOGRAPHY:
            messagebox.showerror(
                "Missing Library",
                "The 'cryptography' Python package is required to decrypt backups.\n\n"
                "Please install it using:\npip install cryptography"
            )
            return

        self._cleanup_restore_temp()

        self.btn_inspect_backup.configure(state="disabled")
        self.restore_status_var.set("Decrypting and reading backup container…")
        self.restore_progress.configure(value=30)
        self.root.config(cursor="watch")

        def worker():
            try:
                temp_dir = tempfile.mkdtemp(prefix="upganam_dec_")
                decrypted_zip = os.path.join(temp_dir, "archive.zip")
                decrypt_file(upg_path, decrypted_zip, password)

                with zipfile.ZipFile(decrypted_zip, "r") as zf:
                    namelist = zf.namelist()
                    manifest = None
                    if "manifest.json" in namelist:
                        try:
                            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                        except Exception:
                            manifest = None

                    parsed_items = []
                    if manifest and "entries" in manifest:
                        info_map = {zi.filename: zi.file_size for zi in zf.infolist()}
                        for entry in manifest["entries"]:
                            rel_p = entry.get("rel_path", "")
                            size = info_map.get(rel_p, entry.get("size", 0))
                            parsed_items.append({
                                "name": rel_p,
                                "original_path": entry.get("remote_path", "/storage/emulated/0/" + rel_p),
                                "size": size,
                                "is_dir": entry.get("is_dir", False)
                            })
                    else:
                        for zi in zf.infolist():
                            if zi.filename == "manifest.json":
                                continue
                            parsed_items.append({
                                "name": zi.filename,
                                "original_path": "/storage/emulated/0/" + zi.filename,
                                "size": zi.file_size,
                                "is_dir": zi.is_dir()
                            })

                self.root.after(0, lambda: self._on_restore_loaded_success(temp_dir, decrypted_zip, parsed_items))
            except (InvalidTag, ValueError):
                self.root.after(0, lambda: self._on_restore_loaded_error(
                    "Decryption Failed",
                    "Incorrect password or corrupted backup file.\n\nPlease verify your password."
                ))
            except Exception as e:
                self.root.after(0, lambda e=e: self._on_restore_loaded_error("Error Reading Backup", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_restore_loaded_success(self, temp_dir, decrypted_zip, parsed_items):
        self.root.config(cursor="")
        self.btn_inspect_backup.configure(state="normal")
        self.restore_progress.configure(value=100)

        self.loaded_restore_temp_dir = temp_dir
        self.loaded_restore_zip_path = decrypted_zip
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
        self.restore_sel_count_var.set(
            f"Selected: {len(parsed_items)} of {len(parsed_items)} item(s) ({format_size(total_size)})"
        )
        self.restore_status_var.set(
            f"Loaded {len(parsed_items)} items from backup. Select items and click 'Start Restore'."
        )

    def _on_restore_loaded_error(self, title, msg):
        self.root.config(cursor="")
        self.btn_inspect_backup.configure(state="normal")
        self.restore_progress.configure(value=0)
        self._cleanup_restore_temp()
        self.restore_status_var.set(f"Failed to open backup: {title}")
        messagebox.showerror(title, msg)

    def _cleanup_restore_temp(self):
        if self.loaded_restore_temp_dir and os.path.exists(self.loaded_restore_temp_dir):
            shutil.rmtree(self.loaded_restore_temp_dir, ignore_errors=True)
        self.loaded_restore_temp_dir = None
        self.loaded_restore_zip_path = None

    # ─────────────────────────────────────────────────────────────
    # Restore Treeview Interaction
    # ─────────────────────────────────────────────────────────────
    def _toggle_restore_item(self, iid):
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
        self._update_restore_selection_status()

    def _on_restore_tree_click(self, event):
        region = self.restore_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        col = self.restore_tree.identify_column(event.x)
        item = self.restore_tree.identify_row(event.y)
        if item and col == "#1":
            self._toggle_restore_item(item)

    def _on_restore_dblclick(self, event):
        col = self.restore_tree.identify_column(event.x)
        if col == "#1":
            return
        item = self.restore_tree.identify_row(event.y)
        if item:
            self._toggle_restore_item(item)

    def _on_restore_space(self, event):
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
        self._update_restore_selection_status()
        return "break"

    def _restore_sel_all(self):
        self.restore_selected_indices = set(range(len(self.restore_archive_entries)))
        for iid in self.restore_tree.get_children():
            vals = list(self.restore_tree.item(iid, "values"))
            vals[0] = "☑"
            self.restore_tree.item(iid, values=vals)
        self._update_restore_selection_status()

    def _restore_desel_all(self):
        self.restore_selected_indices.clear()
        for iid in self.restore_tree.get_children():
            vals = list(self.restore_tree.item(iid, "values"))
            vals[0] = "☐"
            self.restore_tree.item(iid, values=vals)
        self._update_restore_selection_status()

    def _update_restore_selection_status(self):
        sel_count = len(self.restore_selected_indices)
        total = len(self.restore_archive_entries)
        sel_bytes = sum(self.restore_archive_entries[i]["size"] for i in self.restore_selected_indices)
        self.restore_sel_count_var.set(
            f"Selected: {sel_count} of {total} item(s) ({format_size(sel_bytes)})"
        )

    def _sort_restore_column(self, col, reverse):
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
        self.restore_tree.heading(col, command=lambda: self._sort_restore_column(col, not reverse))

    # ─────────────────────────────────────────────────────────────
    # Restore Execution to Device
    # ─────────────────────────────────────────────────────────────
    def _start_restore(self):
        if not self.selected_device_serial:
            messagebox.showwarning("No Device", "Please select a connected target Android device.")
            return

        if not self.loaded_restore_zip_path or not os.path.isfile(self.loaded_restore_zip_path):
            messagebox.showwarning("No Backup Loaded", "Please open and inspect an encrypted backup container first.")
            return

        if not self.restore_selected_indices:
            messagebox.showinfo("Nothing Selected", "Please select at least one item to restore to the device.")
            return

        mode = self.restore_mode_var.get()
        custom_base = self.custom_restore_dir_var.get().strip()
        if mode == "custom" and not custom_base:
            messagebox.showwarning("Custom Path Empty", "Please specify a destination folder path on the device.")
            return

        chosen_items = [self.restore_archive_entries[i] for i in sorted(self.restore_selected_indices)]

        self.is_restoring = True
        self.cancel_restore_event.clear()
        self.btn_restore_start.configure(state="disabled")
        self.btn_restore_cancel.configure(state="normal")
        self.restore_progress.configure(value=0)

        serial = self.selected_device_serial
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
            with zipfile.ZipFile(zip_path, "r") as zf:
                for i, item in enumerate(items, 1):
                    if self.cancel_restore_event.is_set():
                        break

                    name = item["name"]
                    self.root.after(0, lambda i=i, t=total, n=name:
                        self.restore_status_var.set(f"Restoring ({i}/{t}): {n} …"))
                    self.root.after(0, lambda i=i, t=total:
                        self.restore_progress.configure(value=((i - 1) / total) * 100))

                    if mode == "original":
                        target_remote = item["original_path"]
                    else:
                        target_remote = posixpath.join(custom_base, name.replace("\\", "/"))

                    try:
                        extracted_path = zf.extract(name, path=extract_tmp)
                    except Exception as e:
                        errors.append((name, f"Extraction failed: {e}"))
                        continue

                    remote_parent = posixpath.dirname(target_remote)
                    if remote_parent and remote_parent != "/":
                        adb_shell(f'mkdir -p {shlex.quote(remote_parent)}', serial=serial)

                    cmd = [ADB]
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
                            **_subp_kwargs()
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
                    except Exception as e:
                        errors.append((name, str(e)))

        finally:
            if os.path.exists(extract_tmp):
                shutil.rmtree(extract_tmp, ignore_errors=True)

        self.root.after(0, lambda: self._on_restore_completed(total, successes, errors))

    def _on_restore_completed(self, total, successes, errors):
        self.is_restoring = False
        self.current_restore_proc = None
        self.btn_restore_start.configure(state="normal")
        self.btn_restore_cancel.configure(state="disabled")

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
                f"Successfully restored all {total} item(s) to device '{self.selected_device_serial}'!"
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


if __name__ == "__main__":
    if tk is None:
        print("Error: Tkinter is required to run the Upganam GUI.", file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    BackupApp(root)
    root.mainloop()
