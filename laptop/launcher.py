r"""
Console3DS launcher (built into Console3DS.exe with PyInstaller)

Double-click Console3DS.exe and it:
  1. checks the Mobile Hotspot (192.168.137.1) and the Mosquitto service
  2. opens 2 windows: azahar_bridge.py (controls + touch) and screen_stream.py (bottom screen)
  3. starts Azahar directly in the game
  4. when you close Azahar, it closes the 2 windows too

Rebuild the .exe after editing this file (from Console3DS\laptop):
  python -m PyInstaller --onefile --noconsole --name Console3DS --distpath .. launcher.py
"""

import ctypes
import glob
import os
import shutil
import socket
import subprocess
import sys
import time

# ---------- Paths (edit GAMES_DIR / GAME_HINT for your PC, then rebuild) ----------
# The scripts are found next to this file, or in "laptop\" next to Console3DS.exe
if getattr(sys, "frozen", False):
    LAPTOP_DIR = os.path.join(os.path.dirname(sys.executable), "laptop")
else:
    LAPTOP_DIR = os.path.dirname(os.path.abspath(__file__))
AZAHAR_EXE = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Azahar\azahar.exe")   # default install
GAMES_DIR = r"C:\Users\chato\OneDrive - Cegep de Sorel-Tracy\Automne 2025\Desktop\3ds-games"
GAME_HINT = "Pokemon Ultra Moon"        # picks the game whose file name contains this
HOTSPOT_IP = "192.168.137.1"

TITLE = "Console3DS"
MB_OK, MB_OKCANCEL, MB_ICONWARN, MB_ICONERR, IDOK = 0x0, 0x1, 0x30, 0x10, 1
CREATE_NEW_CONSOLE = 0x10


def msg(text, flags=MB_OK):
    return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)


def find_game():
    files = [f for ext in ("*.cci", "*.3ds", "*.cxi") for f in glob.glob(os.path.join(GAMES_DIR, ext))]
    preferred = [f for f in files if GAME_HINT.lower() in os.path.basename(f).lower()]
    return (preferred or files or [None])[0]


def hotspot_on():
    # The hotspot IP only exists on the laptop while the Mobile Hotspot is on
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((HOTSPOT_IP, 0))
        s.close()
        return True
    except OSError:
        return False


def mosquitto_running():
    try:
        s = socket.create_connection(("127.0.0.1", 1883), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def process_running(exe_name):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                         capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return exe_name.lower() in out.lower()


def open_console(title, script):
    """New visible window that keeps showing errors if the script crashes (cmd /k).
    Runs from LAPTOP_DIR so the script name has no spaces (no quoting trouble)."""
    cmd = f"cmd.exe /k title {title} && python {script}"
    return subprocess.Popen(cmd, cwd=LAPTOP_DIR, creationflags=CREATE_NEW_CONSOLE)


def kill_tree(proc):
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    # --- checks ---
    python = shutil.which("python")
    if not python:
        msg("Python was not found (python is not in PATH).", MB_ICONERR)
        return
    if not os.path.exists(AZAHAR_EXE):
        msg(f"Azahar not found:\n{AZAHAR_EXE}", MB_ICONERR)
        return
    game = find_game()
    if not game:
        msg(f"No .cci / .3ds game found in:\n{GAMES_DIR}", MB_ICONERR)
        return
    if process_running("azahar.exe"):
        msg("Azahar is already running. Close it first, then start Console3DS again.", MB_ICONWARN)
        return

    while not hotspot_on():
        os.startfile("ms-settings:network-mobilehotspot")
        if msg("The Mobile Hotspot is OFF, so the ESP32 can't connect.\n\n"
               "Turn it on in the Settings window that just opened, then press OK.\n"
               "(Cancel = quit)", MB_OKCANCEL | MB_ICONWARN) != IDOK:
            return

    if not mosquitto_running():
        if msg("Mosquitto (MQTT broker) is not running on port 1883.\n"
               "Start it: services.msc > mosquitto > Start.\n\n"
               "OK = continue anyway, Cancel = quit", MB_OKCANCEL | MB_ICONWARN) != IDOK:
            return

    # --- start everything ---
    bridge = open_console("Console3DS - Bridge - controls + touch", "azahar_bridge.py")
    stream = open_console("Console3DS - Screen stream", "screen_stream.py")
    time.sleep(1.5)                       # virtual controller exists before Azahar starts
    azahar = subprocess.Popen([AZAHAR_EXE, game])

    # --- when Azahar closes, close the 2 windows ---
    azahar.wait()
    kill_tree(bridge)
    kill_tree(stream)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                # show any crash instead of failing silently
        msg(f"Console3DS launcher error:\n{e}", MB_ICONERR)
        sys.exit(1)
