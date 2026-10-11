# Upganam

Android phone backup tool — single `.exe`, ADB bundled.

## Features
- Browse internal storage, Android/data, and OBB via ADB
- Select files and pull them to your PC
- No separate ADB install required

## Usage
1. Enable USB Debugging on your phone
2. Connect via USB
3. Run `Upganam.exe` → Detect Device → browse → select → Start Backup

## Build from Source
```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name "Upganam" \
  --add-binary "platform-tools/adb.exe;." \
  --add-binary "platform-tools/AdbWinApi.dll;." \
  --add-binary "platform-tools/AdbWinUsbApi.dll;." \
  Upganam.py
