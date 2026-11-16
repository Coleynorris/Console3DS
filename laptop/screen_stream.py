r"""
Console3DS - screen_stream.py (step 7d)
Streams Azahar's bottom screen to the ESP32 screen, sending ONLY THE PARTS THAT CHANGED.

  Azahar "Secondary Window" --(PrintWindow, works even if covered)--> crop the 4:3 game area
  --> resize to 320x240 (= ESP32 screen, 1:1) --> cut into 5x5 tiles of 64x48
  --> only tiles that changed since the ESP32's last picture are JPEG'd and sent (TCP 5005)

Why: drawing a full 320x240 JPEG takes the ESP32 ~124 ms. Pokemon's bottom screen is
mostly static (menus, battle layout), so usually only a few tiles change -> much faster.
If most of the screen changed, one full-screen JPEG is sent instead (cheaper than 25 tiles).

Capture runs in its own thread (always has a fresh picture ready), and the ESP32 ASKS for
each update (1 byte "N" when it finished drawing), so updates never pile up.

Packet (laptop -> ESP32):
  "FRM2" + uint32 body length + body
  body = uint16 tile count + for each tile: uint16 x, uint16 y, uint32 jpeg length, jpeg bytes
  (count 0 = "nothing changed", sent once per second as a keep-alive)
All numbers little endian.

Needs:  python -m pip install pillow pywin32     (already done)
        Azahar: View > Screen Layout > Separate Windows (bottom window NOT minimized)
Run:    python screen_stream.py         (with azahar_bridge.py in another window)
ESP32:  esp32\Console_3DS
"""

import ctypes
import io
import socket
import struct
import threading
import time

import win32gui
import win32ui
from PIL import Image

PORT = 5005
OUT_W, OUT_H = 320, 240           # ESP32 screen = 3DS bottom screen
TILE_W, TILE_H = 64, 48           # 5 x 5 tiles, multiples of 16 (JPEG blocks)
FULL_IF_CHANGED = 16              # this many tiles changed (of 25) -> send one full frame
JPEG_QUALITY = 90                 # lower = smaller/faster, uglier (was 70)
JPEG_SUBSAMPLING = 0              # 0 = 4:4:4 full color detail (sharp colored text/edges)
MAX_BODY = 58000                  # must fit in the ESP32 buffer (60000)
# Resize Azahar's bottom window so the game is drawn at exactly 2x (640x480).
# An exact multiple shrinks back to 320x240 without blur; 400x300 (1.25x) can't.
# Set to 0 to leave the window alone.
BOTTOM_WINDOW_SCALE = 2
CAPTURE_FPS = 30
KEEPALIVE_S = 1.0
WINDOW_HINTS = ("Azahar", "Secondary Window")

ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixel sizes with Windows scaling
PW_CLIENTONLY_RENDERFULL = 3                     # PrintWindow: client area + GPU content

TILES = [(x, y) for y in range(0, OUT_H, TILE_H) for x in range(0, OUT_W, TILE_W)]


# ---------- Capture ----------
def find_bottom_window():
    found = []

    def cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if win32gui.IsWindowVisible(hwnd) and all(h in title for h in WINDOW_HINTS):
            found.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return found[0] if found else None


def grab_window(hwnd):
    """Capture the window's client area, even if other windows cover it."""
    _, _, w, h = win32gui.GetClientRect(hwnd)
    if w < 4 or h < 3:
        return None                                     # minimized
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_CLIENTONLY_RENDERFULL)
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (w, h), data, "raw", "BGRX", 0, 1)
    finally:
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
        win32gui.DeleteObject(bmp.GetHandle())


def game_area(img):
    """Azahar draws the 4:3 screen centered with black bars: keep only that part."""
    w, h = img.size
    if w * 3 > h * 4:            # window too wide -> bars left/right
        gh, gw = h, h * 4 // 3
    else:                        # window too tall -> bars top/bottom
        gw, gh = w, w * 3 // 4
    x, y = (w - gw) // 2, (h - gh) // 2
    return img.crop((x, y, x + gw, y + gh))


def fit_window(hwnd):
    """Make the window's client area exactly 320x240 * BOTTOM_WINDOW_SCALE (no black bars)."""
    if not BOTTOM_WINDOW_SCALE:
        return
    want_w, want_h = OUT_W * BOTTOM_WINDOW_SCALE, OUT_H * BOTTOM_WINDOW_SCALE
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    if (cw, ch) == (want_w, want_h):
        return
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    border_w, border_h = (right - left) - cw, (bottom - top) - ch
    SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x2, 0x4, 0x10
    win32gui.SetWindowPos(hwnd, 0, 0, 0, want_w + border_w, want_h + border_h,
                          SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
    _, _, cw, ch = win32gui.GetClientRect(hwnd)
    print(f"Bottom window resized to {cw}x{ch} (game drawn at {BOTTOM_WINDOW_SCALE}x)")


def shrink(img):
    """Game area -> 320x240. Exact multiples average each block of pixels (sharp);
    other sizes use Lanczos (sharpest general filter)."""
    w, h = img.size
    if (w, h) == (OUT_W, OUT_H):
        return img
    exact = w % OUT_W == 0 and h % OUT_H == 0 and w // OUT_W == h // OUT_H
    return img.resize((OUT_W, OUT_H), Image.BOX if exact else Image.LANCZOS)


class Capturer(threading.Thread):
    """Keeps grabbing the bottom screen so a fresh 320x240 picture is always ready."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.latest = None
        self.seq = 0

    def run(self):
        hwnd = None
        warned = False
        while True:
            t0 = time.time()
            try:
                if hwnd is None or not win32gui.IsWindow(hwnd):
                    hwnd = find_bottom_window()
                    if hwnd is None:
                        if not warned:
                            print("Azahar bottom window not found (Separate Windows? game running?)")
                            warned = True
                        time.sleep(1)
                        continue
                    warned = False
                    print("Capturing:", win32gui.GetWindowText(hwnd))
                    fit_window(hwnd)
                img = grab_window(hwnd)
                if img is not None:
                    small = shrink(game_area(img))
                    with self.lock:
                        self.latest = small
                        self.seq += 1
            except Exception as e:                      # window closed mid-capture, etc.
                print("Capture error:", e)
                hwnd = None
                time.sleep(0.5)
            time.sleep(max(0, 1 / CAPTURE_FPS - (time.time() - t0)))

    def get(self):
        with self.lock:
            return self.latest, self.seq


# ---------- Encoding ----------
def jpeg(img, quality=JPEG_QUALITY):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality,             # baseline JPEG (ESP32 decoder needs it)
             subsampling=JPEG_SUBSAMPLING)
    return buf.getvalue()


def build_body(img, changed):
    """List of (x, y, jpeg) for the changed tiles, or one full frame if that's cheaper."""
    if len(changed) < FULL_IF_CHANGED:
        parts = []
        for i in changed:
            x, y = TILES[i]
            parts.append((x, y, jpeg(img.crop((x, y, x + TILE_W, y + TILE_H)))))
        if sum(len(p[2]) + 8 for p in parts) + 2 <= MAX_BODY:
            return parts
    quality = JPEG_QUALITY
    while True:                                         # full frame, shrink quality if needed
        data = jpeg(img, quality)
        if len(data) + 10 <= MAX_BODY or quality <= 30:
            return [(0, 0, data)]
        quality -= 10


def pack(parts):
    body = struct.pack("<H", len(parts))
    for x, y, data in parts:
        body += struct.pack("<HHI", x, y, len(data)) + data
    return b"FRM2" + struct.pack("<I", len(body)) + body


# ---------- Server ----------
def serve(conn, cap):
    esp_tiles = None                  # what the ESP32 currently shows (raw pixels per tile)
    last_seq = -1
    last_sent = 0.0
    updates, tiles_sent, bytes_sent, stat_time = 0, 0, 0, time.time()

    while True:
        if not conn.recv(1):          # ESP32 asks for the next update
            return

        # Wait for a picture that differs from what the ESP32 shows (or keep-alive time)
        while True:
            img, seq = cap.get()
            if img is not None and seq != last_seq:
                last_seq = seq
                raw = [img.crop((x, y, x + TILE_W, y + TILE_H)).tobytes() for x, y in TILES]
                if esp_tiles is None:
                    changed = list(range(len(TILES)))
                else:
                    changed = [i for i in range(len(TILES)) if raw[i] != esp_tiles[i]]
                if changed:
                    break
            if time.time() - last_sent > KEEPALIVE_S:
                changed, raw = [], esp_tiles
                break
            time.sleep(0.005)

        parts = build_body(img, changed) if changed else []
        conn.sendall(pack(parts))
        if changed:
            esp_tiles = raw
        last_sent = time.time()

        if changed:
            updates += 1
            tiles_sent += len(changed)
            bytes_sent += sum(len(p[2]) for p in parts)
        if time.time() - stat_time >= 5:
            dt = time.time() - stat_time
            avg_tiles = tiles_sent / max(updates, 1)
            print(f"{updates / dt:4.1f} updates/s, avg {avg_tiles:4.1f}/25 tiles changed, "
                  f"{bytes_sent / max(updates, 1) / 1024:4.1f} KB/update")
            updates, tiles_sent, bytes_sent, stat_time = 0, 0, 0, time.time()


def main():
    cap = Capturer()
    cap.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(1)
    server.settimeout(1)                   # so Ctrl+C works while waiting
    print(f"Screen stream server on port {PORT}, waiting for the ESP32... (Ctrl+C to stop)")

    while True:
        try:
            conn, addr = server.accept()
        except socket.timeout:
            continue
        print("ESP32 connected from", addr[0])
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(10)                # ESP32 silent for 10 s = gone
        try:
            serve(conn, cap)               # starts with a full frame (ESP32 state unknown)
        except (ConnectionError, socket.timeout, OSError) as e:
            print("Connection ended:", e)
        finally:
            conn.close()
        print("ESP32 disconnected, waiting again...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped")
