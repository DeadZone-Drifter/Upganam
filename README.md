# Upganam

Android phone backup and restore tool — single `.exe`, ADB bundled.

## Features
- **Browse & Select**: Explore internal storage, `Android/data`, and `OBB` via ADB.
- **Hierarchy Preservation**: Pulls files and folders while maintaining the original directory tree (prevents name collisions).
- **Encrypted Backups (`.upg`)**: Strong password-protected AES-256-GCM authenticated encryption + ZIP Deflate compression.
- **Selective Restore**: Inspect backup archives, view stored files, and selectively restore items to any connected device (new or old).
- **Flexible Restore Destination**: Push restored files to original device locations or into a custom folder on the target phone.
- **Multi-Device Support**: Detect and switch between multiple connected Android devices with a dropdown selector.
- **Self-Contained**: Automatically bundles ADB binaries into a standalone executable.

## Usage

### Backup
1. Enable **Developer Options** and **USB Debugging** on your phone.
2. Connect your phone via USB and tap **Allow** on the debugging prompt.
3. Launch `Upganam.exe` and click **Detect Devices**.
4. In the **Backup** tab, navigate through directories, check files/folders, and choose:
   - **Encrypted Backup**: Check *"Compress & Encrypt Backup"*, enter a password, and save as a `.upg` file.
   - **Plain Backup**: Save to a local folder with folder hierarchy preserved.
5. Click **Start Backup**.

### Restore
1. Connect the destination phone (new or old) via USB with USB debugging enabled.
2. Switch to the **Restore to Device** tab.
3. Browse for your `.upg` backup container and enter your encryption password.
4. Click **Open & Inspect Backup** to view the archived file manifest.
5. Check the specific files or folders you wish to restore.
6. Choose whether to restore to the original paths or a custom target folder.
7. Click **Start Restore**.

## Build from Source

```bash
pip install -r requirements.txt

pyinstaller --onefile --windowed --name "Upganam" \
  --add-binary "platform-tools/adb.exe;." \
  --add-binary "platform-tools/AdbWinApi.dll;." \
  --add-binary "platform-tools/AdbWinUsbApi.dll;." \
  Upganam.py
