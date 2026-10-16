"""
Upganam — Android Phone Backup & Restore Tool
Entry point and launcher for Upganam.
"""
import sys

def main():
    try:
        import tkinter as tk
    except ImportError:
        if sys.stderr:
            sys.stderr.write("Error: Tkinter is required to run the Upganam GUI.\n")
        sys.exit(1)

    # Windows High-DPI awareness
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    from upganam.ui.app import BackupApp

    root = tk.Tk()
    BackupApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
