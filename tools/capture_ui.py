"""Capture screenshots of the real running UI (visible window) for design
verification. Grabs each tab + each mode. Run from repo root:
    python tools/capture_ui.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk
from app import gui
from app.elevation import is_admin


def snap(root, name):
    root.update_idletasks()
    root.update()
    root.after(200, lambda: None)  # let animations settle
    root.update()
    # Windows: use PrintWindow via PIL-free approach — Tk's postscript only
    # works for Canvas. Simplest reliable route: use PowerShell screen grab
    # on the window region.
    x = root.winfo_rootx()
    y = root.winfo_rooty()
    w = root.winfo_width()
    h = root.winfo_height()
    out = pathlib.Path.cwd() / "shots"
    out.mkdir(exist_ok=True)
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        f"$b = New-Object System.Drawing.Bitmap {w},{h};"
        f"$g = [System.Drawing.Graphics]::FromImage($b);"
        f"$g.CopyFromScreen({x},{y},0,0,[System.Drawing.Size]::{w},{h});"  # noqa
        f"$b.Save('{(out / name).resolve()}.png');"
    )
    # build the size correctly
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        f"$sz = New-Object System.Drawing.Size({w},{h});"
        f"$b = New-Object System.Drawing.Bitmap({w},{h});"
        f"$g = [System.Drawing.Graphics]::FromImage($b);"
        f"$g.CopyFromScreen({x},{y},0,0,$sz);"
        f"$b.Save('{(out / name).resolve()}');"
    )
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
    print(f"captured {name}")


def main():
    root = tk.Tk()
    app = gui.Application(root)
    if not is_admin():
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                w._continue_limited()
                break

    root.deiconify()
    root.attributes("-topmost", True)   # keep above the terminal running us
    root.lift()
    root.geometry("980x720+60+40")
    root.update()
    root.after(400, lambda: None)
    root.update()

    snap(root, "01_clean_default")

    app.tabs["Clean"]._select_preset("Quick Clean")
    root.update()
    snap(root, "02_clean_quick")

    app.tabs["Clean"]._select_preset("Deep Clean")
    root.update()
    snap(root, "03_clean_deep")

    app._switch_to("Repair")
    app.tabs["Repair"]._select_preset("Deep Repair")
    root.update()
    snap(root, "04_repair_deep")

    app._switch_to("Tweak")
    app.tabs["Tweak"]._select_preset("Recommended")
    root.update()
    snap(root, "05_tweak_recommended")

    app.tabs["Tweak"]._enter_custom()
    root.update()
    snap(root, "06_tweak_custom")

    app.tabs["Tweak"]._enter_undo()
    root.update()
    snap(root, "07_tweak_undo")

    app._switch_to("Install")
    root.update()
    snap(root, "08_install_catalog")

    root.destroy()
    print("DONE")


if __name__ == "__main__":
    main()
