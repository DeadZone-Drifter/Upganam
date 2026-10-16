"""
Core ADB management, subprocess execution, and directory parsing.
Handles device detection, ADB binary location, shell commands, wireless connectivity,
and Android package manager (APK) operations.
"""
import os
import sys
import shutil
import posixpath
import shlex
import re
import subprocess

# ── Subprocess & Binary Resolution Helpers ───────────────────────

def get_subp_kwargs():
    """Return platform-specific kwargs to suppress console windows on Windows."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs

def get_adb_path():
    """Locate the bundled or system ADB binary."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    for name in ("adb.exe", "adb"):
        local = os.path.join(base, name)
        if os.path.isfile(local):
            return local
        # Check platform-tools subfolder if present
        pt_local = os.path.join(base, "platform-tools", name)
        if os.path.isfile(pt_local):
            return pt_local

    found = shutil.which("adb")
    if found:
        return found
    return "adb"

ADB_BIN = get_adb_path()

def is_adb_available():
    """Verify whether the ADB binary is present on the filesystem or system PATH."""
    adb_bin = get_adb_path()
    if os.path.isabs(adb_bin) and os.path.isfile(adb_bin):
        return True, adb_bin
    which = shutil.which(adb_bin)
    if which and os.path.isfile(which):
        return True, which
    return False, adb_bin

# ── Command Execution ────────────────────────────────────────────

def run_adb(*args, serial=None, timeout=30):
    """
    Execute an ADB command with optional device targeting and timeout.
    Returns: (stdout: str, stderr: str, returncode: int)
    """
    cmd = [ADB_BIN]
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
            timeout=timeout,
            **get_subp_kwargs()
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except FileNotFoundError:
        return "", f"ADB executable not found at '{ADB_BIN}'.", -1
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout} seconds.", -1
    except Exception as e:
        return "", str(e), -1

def adb_shell(cmd_str, serial=None, timeout=30):
    """Execute a shell command on the target Android device."""
    return run_adb("shell", cmd_str, serial=serial, timeout=timeout)

def get_devices():
    """
    Query connected devices via `adb devices -l`.
    Returns list of dicts: [{"serial": "...", "state": "...", "model": "..."}]
    """
    out, _, code = run_adb("devices", "-l")
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

# ── Wireless ADB Connectivity ────────────────────────────────────

def adb_connect(ip_port):
    """
    Connect to an Android device over TCP/IP via `adb connect <ip:port>`.
    Defaults to port 5555 if no port is specified.
    Returns: (success: bool, message: str)
    """
    target = ip_port.strip()
    if not target:
        return False, "IP address cannot be empty."
    if ":" not in target:
        target += ":5555"

    out, err, code = run_adb("connect", target, timeout=15)
    combined = f"{out} {err}".strip()
    if "connected to" in combined.lower() and "cannot" not in combined.lower() and "failed" not in combined.lower():
        return True, combined
    return False, combined or "Failed to connect to wireless device."

def adb_disconnect(ip_port=None):
    """
    Disconnect a specific wireless device or all wireless sessions.
    Returns: (success: bool, message: str)
    """
    args = ["disconnect"]
    if ip_port:
        args.append(ip_port.strip())
    out, err, code = run_adb(*args, timeout=10)
    return (code == 0), (out or err or "Disconnected").strip()

def adb_pair(ip_port, pairing_code):
    """
    Pair with an Android 11+ device using a 6-digit Wi-Fi pairing code via `adb pair <ip:port> <code>`.
    Returns: (success: bool, message: str)
    """
    target = ip_port.strip()
    code_str = str(pairing_code).strip()
    if not target:
        return False, "Pairing address cannot be empty."
    if not code_str:
        return False, "Pairing code cannot be empty."

    out, err, code = run_adb("pair", target, code_str, timeout=15)
    combined = f"{out} {err}".strip()
    if "successfully paired" in combined.lower():
        return True, combined
    return False, combined or "Pairing failed. Verify IP, port, and code."

# ── Package Manager (APK) Operations ─────────────────────────────

def list_installed_packages(serial=None, third_party_only=True):
    """
    List installed package identifiers on the device via `pm list packages`.
    Defaults to non-system 3rd-party packages (-3).
    Returns: list[str] of package names
    """
    flag = "-3" if third_party_only else ""
    cmd = f"pm list packages {flag}".strip()
    out, err, code = adb_shell(cmd, serial=serial, timeout=25)
    if code != 0:
        return []
    packages = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkg = line[len("package:"):].strip()
            if pkg:
                packages.append(pkg)
    packages.sort()
    return packages

def get_package_apk_path(serial, package_name):
    """
    Retrieve the remote filesystem path to an installed application's primary base APK.
    Returns: remote APK path as str, or None if not found
    """
    clean_pkg = package_name.strip()
    out, err, code = adb_shell(f"pm path {shlex.quote(clean_pkg)}", serial=serial, timeout=15)
    if code != 0 or not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            apk_path = line[len("package:"):].strip()
            if apk_path.endswith(".apk"):
                return apk_path
    return None

def install_apk(serial, local_apk_path, reinstall=True):
    """
    Install a local APK onto the target device via `adb install -r`.
    Returns: (success: bool, message: str)
    """
    if not os.path.isfile(local_apk_path):
        return False, f"Local APK file not found at '{local_apk_path}'."
    args = ["install"]
    if reinstall:
        args.append("-r")
    args.append(local_apk_path)
    out, err, code = run_adb(*args, serial=serial, timeout=180)
    combined = f"{out} {err}".strip()
    if "success" in combined.lower():
        return True, "Application installed successfully."
    return False, combined or "Installation failed."

# ── Android Directory Parsing ────────────────────────────────────

# Regex to parse Android toybox/toolbox 'ls -la' output:
# [perms] [links?] [owner] [group?] [size] [date] [time] [filename...]
LS_RE = re.compile(
    r"^([b-ls-][rwxstST-]{9}\+?)\s+"
    r"(?:\d+\s+)?"
    r"(?:\S+\s+){1,2}"
    r"(\d+)\s+"
    r"((?:\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})|(?:[A-Za-z]{3}\s+\d{1,2}\s+(?:\d{4}|\d{2}:\d{2})))\s+"
    r"(.+)$"
)

def parse_ls_output(out, base_path):
    """Parse raw Android ls -la stdout into structured file/folder entries."""
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("total ") or line.endswith(":"):
            continue
        m = LS_RE.match(line)
        if m:
            perm, size_str, _, name = m.groups()
            is_symlink = perm.startswith("l")
            symlink_target = ""
            if is_symlink and " -> " in name:
                parts_name = name.split(" -> ", 1)
                name = parts_name[0].strip()
                symlink_target = parts_name[1].strip()
            if name in (".", ".."):
                continue
            is_dir = perm.startswith("d")
            if is_symlink:
                clean_target = symlink_target.rstrip("/")
                is_dir = (
                    symlink_target.endswith("/") or
                    clean_target.endswith((
                        "storage", "sdcard", "emulated", "0", "media",
                        "Download", "DCIM", "Documents", "Pictures"
                    ))
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
            # Fallback for non-standard formats
            parts = line.split()
            if len(parts) >= 6 and len(parts[0]) >= 10 and parts[0][0] in "bcdl-sp":
                perm = parts[0]
                name = parts[-1]
                if name in (".", ".."):
                    continue
                is_dir = perm.startswith("d")
                entries.append({
                    "name": name,
                    "path": posixpath.join(base_path, name),
                    "is_dir": is_dir,
                    "is_symlink": perm.startswith("l"),
                    "size": 0
                })
    return entries

def list_dir(path="/storage/emulated/0", serial=None):
    """
    List contents of an Android directory via `adb shell ls -la`.
    Returns: (entries: list[dict], error_msg: str | None)
    """
    clean_path = path.strip().replace("\\", "/")
    norm_path = posixpath.normpath(clean_path)
    quoted = shlex.quote(norm_path)
    out, err, code = adb_shell(f"ls -la {quoted}", serial=serial)
    if code != 0 or "Permission denied" in err or "Permission denied" in out:
        error_msg = (err or out or "Permission denied").strip()
        return [], error_msg
    entries = parse_ls_output(out, norm_path)
    return entries, None

def get_relative_backup_path(remote_path, base_root="/storage/emulated/0"):
    """Compute relative destination path from remote Android path to preserve folder hierarchy."""
    clean_remote = posixpath.normpath(remote_path.replace("\\", "/"))
    clean_base = posixpath.normpath(base_root)
    if clean_remote == clean_base:
        return posixpath.basename(clean_remote) or "root"
    if clean_remote.startswith(clean_base + "/"):
        return clean_remote[len(clean_base) + 1:]
    for prefix in ("/storage/emulated/0/", "/sdcard/"):
        if clean_remote.startswith(prefix):
            return clean_remote[len(prefix):]
    return clean_remote.lstrip("/")

def format_size(bytes_val):
    """Convert raw byte count into human-readable representation."""
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
