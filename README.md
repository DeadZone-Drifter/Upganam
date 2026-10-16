# Upganam 📱💾

**Upganam** is a modern, standalone desktop tool for backing up and restoring Android devices using ADB. It packages all required tools into a single portable `.exe` with zero installation required, no device rooting, and no third-party phone apps needed.

---

## ✨ Features

- **📶 Wireless ADB Connectivity**: Connect and back up over your local Wi-Fi network without USB cables. Fully supports Android 11+ Wi-Fi pairing with 6-digit PIN authentication.
- **🔒 Encrypted Containers (`.upg`)**: Military-grade authenticated streaming encryption using **AES-256-GCM** with **PBKDF2-HMAC-SHA256** (100,000 iterations) key derivation.
- **🗜️ Fast Compressed Archives (`.zip`)**: Lightweight, high-speed Deflate compression when password protection is not required.
- **📱 Third-Party App (APK) Backup & 1-Click Restore**: Scan non-system applications (`pm list packages -3`), extract their `.apk` installers, and batch-reinstall them onto any connected device with one click.
- **🛡️ Bit-for-Bit SHA-256 Integrity Verification**: Every backup records cryptographic file hashes in `manifest.json`. Verify entire archives on the fly without unpacking to disk.
- **📥 Selective Device Restoration**: Open and inspect `.upg` and `.zip` containers, selectively choose files, and restore them back to their original Android directories or a custom folder.
- **📁 Directory Hierarchy Preservation**: Recursively preserves subfolder structures during backup and restore.
- **⚡ Portable & Self-Contained**: Google Android Platform Tools (ADB) and cryptographic libraries are bundled directly into the executable.

---

## 🚀 Quick Start

### 1. Enable Developer Options on your Phone
1. Open phone **Settings** → **About Phone**.
2. Tap **Build Number** 7 times until Developer Mode is unlocked.
3. Go to **Settings** → **System / Additional Settings** → **Developer Options**.
4. Enable **USB Debugging** (and optionally **Wireless Debugging**).

### 2. Connect Your Phone
- **Via USB Cable**: Plug in the cable and tap **"Always allow from this computer"** when prompted on your phone screen.
- **Via Wi-Fi**: Click **📶 Connect Wi-Fi** in Upganam, enter your phone's IP address and port (e.g., `192.168.1.50:5555`), and click **Connect**.

### 3. Backing Up
1. Launch `Upganam.exe` and select your device in the top bar.
2. Select your **Backup Source**:
   - **📁 Files & Storage**: Browse folders like `DCIM`, `Download`, `Documents`, or `Pictures`.
   - **📱 Installed Apps (APKs)**: Check off the applications you want to preserve.
3. Choose your backup format:
   - **🔒 Encrypted Container (.upg)**: Enter a password to encrypt.
   - **🗜️ Compressed Archive (.zip)**: Fast archive without password.
   - **📁 Plain Folder**: Direct uncompressed file transfer.
4. Click **▶ Start Backup**.

### 4. Restoring
1. Switch to the **📥 Restore to Device** tab.
2. Click **Browse…** to select your `.upg` or `.zip` backup file.
3. Enter your password (if `.upg`) and click **🔓 Open & Inspect Backup**.
4. *(Optional)* Click **🛡️ Verify Integrity** to confirm all files match their SHA-256 checksums.
5. Check off the items you want to restore and click **▶ Start Restore**. Files will be pushed to storage and APKs will be automatically reinstalled!

---

## 🏗️ Project Architecture

Upganam is organized into clean, independent modules:

```text
Upganam/
├── Upganam.py                 # Lightweight application launcher & DPI initializer
├── requirements.txt           # Python dependencies (pyinstaller, cryptography)
├── .github/workflows/         # Automated GitHub Actions build workflow
│   └── build.yml
└── upganam/                   # Core Python package
    ├── __init__.py            # Package root & dynamic version metadata
    ├── core/                  # Core business logic (no GUI dependencies)
    │   ├── adb.py             # ADB process handling, Wi-Fi pairing & package manager
    │   ├── crypto.py          # Streaming AES-256-GCM chunked encryption & PBKDF2
    │   └── archive.py         # ZIP creation, manifest handling & SHA-256 verification
    └── ui/                    # Tkinter GUI components
        ├── app.py             # Main window, device combobox & wireless dialog
        ├── backup_tab.py      # Storage browser, APK picker & backup engine
        └── restore_tab.py     # Archive inspector, integrity checker & restore engine
```

🛠️ Building from Source
Prerequisites
Python 3.10+
Google Platform Tools (adb.exe, AdbWinApi.dll, AdbWinUsbApi.dll) placed inside a platform-tools/ folder.
Installation & Build
code
Bash
# 1. Clone repository
git clone https://github.com/DeadZone-Drifter/Upganam.git
cd Upganam

# 2. Install dependencies
pip install -r requirements.txt

# 3. Compile standalone Windows executable
pyinstaller --onefile --windowed --name "Upganam" \
  --collect-all cryptography \
  --add-binary "platform-tools/adb.exe;." \
  --add-binary "platform-tools/AdbWinApi.dll;." \
  --add-binary "platform-tools/AdbWinUsbApi.dll;." \
  Upganam.py
