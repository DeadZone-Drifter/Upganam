"""
Upganam — Android Phone Backup Tool
Single .exe after PyInstaller build. ADB is bundled.
"""
import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── ADB resolution ───────────────────────────────────────────
def _adb_path():
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(base, "adb.exe")
    if os.path.exists(local):
        return local
    return "adb"

ADB = _adb_path()

def adb(*args):
    cmd = [ADB] + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode

def adb_shell(cmd):
    return adb("shell", cmd)

def get_device():
    out, _ = adb("devices")
    return [l.split()[0] for l in out.splitlines()[1:]
            if l.strip() and "device" in l]

def list_dir(path="/storage/emulated/0"):
    out, _ = adb_shell(f'ls -la "{path}"')
    entries = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        perm, size, name = parts[0], parts[4], parts[5]
        if name in (".", ".."):
            continue
        entries.append({"name": name, "path": f"{path}/{name}",
                        "is_dir": perm.startswith("d"), "size": size})
    return entries

def pull_file(remote, local):
    return subprocess.run([ADB, "pull", remote, local],
                          capture_output=True, text=True)

# ── GUI ──────────────────────────────────────────────────────
class BackupApp:
    ROOTS = [
        ("/storage/emulated/0", "Internal Storage"),
        ("/sdcard/Android/data", "Android Data (per-app)"),
        ("/sdcard/Android/obb", "OBB (game assets)"),
    ]

    def __init__(self, root):
        self.root = root
        self.root.title("Upganam")
        self.root.geometry("950x680")
        self.current_path = "/storage/emulated/0"
        self.selected = {}
        self._build_ui()
        self._check_device()

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=5)
        top.pack(fill="x")

        ttk.Button(top, text="🔍 Detect Device", command=self._check_device).pack(side="left", padx=2)
        ttk.Button(top, text="⬆ Up", command=self._go_up).pack(side="left", padx=2)
        ttk.Button(top, text="🔄 Refresh", command=self._refresh).pack(side="left", padx=2)

        self.path_var = tk.StringVar(value=self.current_path)
        ttk.Entry(top, textvariable=self.path_var, width=45).pack(side="left", padx=5)
        ttk.Button(top, text="Go", command=self._cd).pack(side="left", padx=2)

        for path, label in self.ROOTS:
            ttk.Button(top, text=label,
                       command=lambda p=path: self._jump(p)).pack(side="left", padx=1)

        mid = ttk.Frame(self.root, padding=5)
        mid.pack(fill="both", expand=True)

        cols = ("name", "size", "sel")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("sel", text="✓")
        self.tree.column("name", width=450)
        self.tree.column("size", width=100)
        self.tree.column("sel", width=50, anchor="center")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.bind("<Double-1>", self._on_dblclick)
        self.tree.bind("<Button-1>", self._on_click)

        bot = ttk.Frame(self.root, padding=5)
        bot.pack(fill="x")

        ttk.Button(bot, text="Select All", command=self._sel_all).pack(side="left", padx=2)
        ttk.Button(bot, text="Deselect All", command=self._desel_all).pack(side="left", padx=2)

        self.backup_dir = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Upganam_Backup"))
        ttk.Label(bot, text="Backup to:").pack(side="left", padx=(15, 2))
        ttk.Entry(bot, textvariable=self.backup_dir, width=30).pack(side="left", padx=2)
        ttk.Button(bot, text="Browse…", command=self._browse).pack(side="left", padx=2)
        ttk.Button(bot, text="▶ Start Backup", command=self._start).pack(side="right", padx=2)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", padx=5, pady=(0, 3))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", padx=5, pady=(0, 5))

    def _check_device(self):
        devs = get_device()
        if devs:
            self.status_var.set(f"Connected: {devs[0]}")
            self._refresh()
        else:
            self.status_var.set("No device found.")
            messagebox.showwarning("No Device",
                "No Android device detected.\n\n"
                "1. Enable Developer Options\n"
                "2. Enable USB Debugging\n"
                "3. Connect via USB\n"
                "4. Click Detect Device")

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.selected.clear()
        self.status_var.set(f"Listing {self.current_path} …")
        self.root.update_idletasks()
        entries = list_dir(self.current_path)
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        for e in entries:
            icon = "📁" if e["is_dir"] else "📄"
            iid = self.tree.insert("", "end",
                values=(f"{icon} {e['name']}", e["size"], "☐"))
            self.tree.item(iid, tags=(e["path"], e["is_dir"]))
            self.selected[e["path"]] = False
        self.status_var.set(f"{len(entries)} items in {self.current_path}")

    def _go_up(self):
        p = os.path.dirname(self.current_path.rstrip("/"))
        if p:
            self.current_path = p
            self.path_var.set(p)
            self._refresh()

    def _cd(self):
        self.current_path = self.path_var.get().strip()
        self._refresh()

    def _jump(self, path):
        self.current_path = path
        self.path_var.set(path)
        self._refresh()

    def _on_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item: return
        tags = self.tree.item(item, "tags")
        if not tags or tags[1]: return
        path = tags[0]
        self.selected[path] = not self.selected[path]
        vals = list(self.tree.item(item, "values"))
        vals[2] = "☑" if self.selected[path] else "☐"
        self.tree.item(item, values=vals)

    def _on_dblclick(self, event):
        item = self.tree.identify_row(event.y)
        if not item: return
        tags = self.tree.item(item, "tags")
        if not tags: return
        path, is_dir = tags
        if is_dir:
            self.current_path = path
            self.path_var.set(path)
            self._refresh()

    def _sel_all(self):
        for item in self.tree.get_children():
            tags = self.tree.item(item, "tags")
            if tags and not tags[1]:
                self.selected[tags[0]] = True
                vals = list(self.tree.item(item, "values"))
                vals[2] = "☑"
                self.tree.item(item, values=vals)

    def _desel_all(self):
        for item in self.tree.get_children():
            tags = self.tree.item(item, "tags")
            if tags and not tags[1]:
                self.selected[tags[0]] = False
                vals = list(self.tree.item(item, "values"))
                vals[2] = "☐"
                self.tree.item(item, values=vals)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.backup_dir.get())
        if d: self.backup_dir.set(d)

    def _start(self):
        files = [p for p, s in self.selected.items() if s]
        if not files:
            messagebox.showinfo("Nothing selected", "Check at least one file.")
            return
        dest = self.backup_dir.get()
        os.makedirs(dest, exist_ok=True)
        self.progress.configure(value=0)
        threading.Thread(target=self._do_backup, args=(files, dest), daemon=True).start()

    def _do_backup(self, files, dest):
        total = len(files)
        for i, remote in enumerate(files, 1):
            self.root.after(0, lambda i=i, r=remote, t=total:
                self.status_var.set(f"Pulling {i}/{t}: {os.path.basename(r)}"))
            local = os.path.join(dest, os.path.basename(remote))
            pull_file(remote, local)
            self.root.after(0, lambda i=i, t=total:
                self.progress.configure(value=i / t * 100))
        self.root.after(0, lambda: self.status_var.set(
            f"Done! {total} item(s) → {dest}"))
        self.root.after(0, lambda: messagebox.showinfo(
            "Backup Complete", f"{total} item(s) saved to:\n{dest}"))


if __name__ == "__main__":
    root = tk.Tk()
    BackupApp(root)
    root.mainloop()
