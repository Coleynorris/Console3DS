r"""
Console3DS launcher -> built into Console3DS.exe (works on any Windows PC, no Python needed)

Double-click Console3DS.exe and it:
  1. first time only: asks for your game file (.cci/.3ds/.cxi) and, if it isn't in the
     default place, azahar.exe. Saved in console3ds_config.json next to the .exe.
     Hold SHIFT while starting it to pick another game.
  2. installs the ViGEmBus driver if it's missing (needed for the virtual controller)
  3. checks the Mobile Hotspot (192.168.137.1) and the Mosquitto broker (port 1883)
  4. opens 2 windows: the bridge (controls + touch) and the screen stream (bottom screen)
     -> these are this same .exe started with --bridge / --stream
  5. starts Azahar directly in the game; closing Azahar closes the 2 windows

Build (from the laptop folder, needs: pip install pyinstaller paho-mqtt vgamepad pillow pywin32):
  python -m PyInstaller --onefile --noconsole --name Console3DS --collect-all vgamepad --distpath .. launcher.py
Run without building (for development):
  python launcher.py            (or: python launcher.py --bridge / --stream)
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import traceback

TITLE = "Console3DS"
HOTSPOT_IP = "192.168.137.1"
DEFAULT_AZAHAR = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Azahar\azahar.exe")
GAME_TYPES = [("3DS games", "*.cci *.3ds *.cxi"), ("All files", "*.*")]

FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
CONFIG = os.path.join(APP_DIR, "console3ds_config.json")

MB_OK, MB_OKCANCEL, MB_YESNO = 0x0, 0x1, 0x4
MB_ICONWARN, MB_ICONERR, MB_ICONINFO = 0x30, 0x10, 0x40
IDOK, IDYES = 1, 6
CREATE_NEW_CONSOLE = 0x10
CREATE_NO_WINDOW = 0x08000000


def msg(text, flags=MB_OK):
    return ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)


# ---------- Worker modes (--bridge / --stream) ----------
def run_worker(mode):
    """Runs the bridge or the stream in its own console window."""
    # The .exe has no console (--noconsole): create one and send print() there
    if ctypes.windll.kernel32.AllocConsole():
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
        sys.stdin = open("CONIN$", "r")
    ctypes.windll.kernel32.SetConsoleTitleW(
        "Console3DS - Bridge (controls + touch)" if mode == "--bridge" else "Console3DS - Screen stream")
    try:
        if mode == "--bridge":
            import azahar_bridge
            azahar_bridge.main()
        else:
            import screen_stream
            screen_stream.main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        input("\nError above. Press Enter to close this window...")


# ---------- Launcher helpers ----------
def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def pick_file(title, filetypes):
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(title=title, filetypes=filetypes)
    root.destroy()
    return os.path.normpath(path) if path else None


def shift_held():
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)


def hotspot_on():
    # The hotspot IP only exists on this PC while the Mobile Hotspot is on
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind((HOTSPOT_IP, 0))
        s.close()
        return True
    except OSError:
        return False


def mosquitto_running():
    try:
        socket.create_connection(("127.0.0.1", 1883), timeout=1).close()
        return True
    except OSError:
        return False


def process_running(exe_name):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
                         capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout
    return exe_name.lower() in out.lower()


def vigem_ok():
    """True if the ViGEmBus driver answers (needed for the virtual Xbox controller)."""
    try:
        import vgamepad  # noqa: F401  (connects to the driver on import)
        return True
    except Exception:
        return False


def install_vigem():
    import vgamepad
    msi = os.path.join(os.path.dirname(vgamepad.__file__),
                       "win", "vigem", "install", "x64", "ViGEmBusSetup_x64.msi")
    if not os.path.exists(msi):
        msg("The ViGEmBus driver is missing and its installer wasn't found.\n"
            "Install it from github.com/nefarius/ViGEmBus/releases, reboot, and try again.", MB_ICONERR)
        return
    subprocess.run(["msiexec", "/i", msi])
    msg("ViGEmBus installed. Restart the PC, then start Console3DS again.", MB_ICONINFO)


def start_worker(mode):
    if FROZEN:
        cmd = [sys.executable, mode]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), mode]
    return subprocess.Popen(cmd, cwd=APP_DIR, creationflags=CREATE_NEW_CONSOLE)


def kill_tree(proc):
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)


# ---------- Launcher ----------
def launcher():
    cfg = load_config()

    # Azahar
    azahar = cfg.get("azahar") or DEFAULT_AZAHAR
    if not os.path.exists(azahar):
        msg("Azahar wasn't found in its default place.\n"
            "Select azahar.exe in the next window.\n\n"
            "(Don't have it? Get it from azahar-emu.org)", MB_ICONINFO)
        azahar = pick_file("Select azahar.exe", [("azahar.exe", "azahar.exe"), ("Programs", "*.exe")])
        if not azahar:
            return
        cfg["azahar"] = azahar
        save_config(cfg)

    # Game (first time, file moved, or SHIFT held)
    game = cfg.get("game")
    if not game or not os.path.exists(game) or shift_held():
        msg("Select your game file (.cci / .3ds / .cxi) in the next window.\n"
            "It must be a decrypted dump of a game you own.\n\n"
            "Tip: hold SHIFT while starting Console3DS to change the game later.", MB_ICONINFO)
        game = pick_file("Select your 3DS game", GAME_TYPES)
        if not game:
            return
        cfg["game"] = game
        save_config(cfg)

    if process_running(os.path.basename(azahar)):
        msg("Azahar is already running. Close it first, then start Console3DS again.", MB_ICONWARN)
        return

    # Virtual controller driver
    if not vigem_ok():
        if msg("The ViGEmBus driver (virtual game controller) isn't installed.\n"
               "Install it now? (a restart is needed after)", MB_YESNO | MB_ICONWARN) == IDYES:
            install_vigem()
        return

    # Network
    while not hotspot_on():
        os.startfile("ms-settings:network-mobilehotspot")
        if msg("The Mobile Hotspot is OFF, so the ESP32 can't connect.\n\n"
               "Turn it on (band 2.4 GHz) in the Settings window that just opened, "
               "then press OK.\n(Cancel = quit)", MB_OKCANCEL | MB_ICONWARN) != IDOK:
            return

    if not mosquitto_running():
        if msg("The MQTT broker (Mosquitto) isn't running on port 1883,\n"
               "so the ESP32's buttons won't reach the PC.\n\n"
               "Install it from mosquitto.org and use broker/mosquitto.conf from the repo\n"
               "(see README, section 3.3), or start it in services.msc.\n\n"
               "OK = continue anyway, Cancel = quit", MB_OKCANCEL | MB_ICONWARN) != IDOK:
            return

    # Start everything
    bridge = start_worker("--bridge")
    stream = start_worker("--stream")
    time.sleep(2)                          # virtual controller exists before Azahar starts
    emu = subprocess.Popen([azahar, game])

    emu.wait()                             # Azahar closed -> close the 2 windows
    kill_tree(bridge)
    kill_tree(stream)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--bridge", "--stream"):
        run_worker(sys.argv[1])
    else:
        try:
            launcher()
        except Exception as e:             # show any crash instead of failing silently
            msg(f"Console3DS launcher error:\n{e}", MB_ICONERR)
            sys.exit(1)
