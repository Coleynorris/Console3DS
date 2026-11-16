r"""
Console3DS - azahar_bridge.py (steps 7a + 7b)
Turns the ESP32's MQTT inputs into a virtual Xbox 360 controller (ViGEmBus + vgamepad).
Azahar sees it like a real gamepad plugged into the laptop.

  ESP32 stick          -> left stick      (3DS Circle Pad)
  ESP32 A B X Y        -> Xbox B A Y X    (same POSITIONS as a 3DS: A right, B bottom,
                                           X top, Y left - so the layout feels right)
  ESP32 SW (stick click) -> Xbox START     (3DS START, for now)
  ESP32 touch          -> click on Azahar's bottom screen at the same spot (step 7b)
                          (the ESP32 shows the bottom screen 1:1, both are 320x240)

ESP32 sketch to use: esp32\Console_3DS (shows the bottom screen, run screen_stream.py too)
Azahar: View > Screen Layout > Separate Windows (bottom window NOT minimized)

Setup (once):
  python -m pip install vgamepad      (also installs the ViGEmBus driver: accept the installer)
Run:
  python azahar_bridge.py
Then in Azahar: Emulation > Configure > Controls, pick the "Xbox 360 Controller"
and bind each 3DS button by pressing the matching ESP32 button.
"""

import ctypes
import json
import time

import paho.mqtt.client as mqtt
import vgamepad as vg
import win32api
import win32con
import win32gui

BROKER = "localhost"
PORT = 1883

B = vg.XUSB_BUTTON
BUTTON_MAP = {
    "A": B.XUSB_GAMEPAD_B,      # right
    "B": B.XUSB_GAMEPAD_A,      # bottom
    "X": B.XUSB_GAMEPAD_Y,      # top
    "Y": B.XUSB_GAMEPAD_X,      # left
    "SW": B.XUSB_GAMEPAD_START,
}

# Touch mode:
#   "post"  = send mouse messages straight to Azahar's window. Your real mouse doesn't move
#             and Azahar doesn't need to be in front. Try this first.
#   "mouse" = really move + click the mouse there, then put the cursor back.
#             Use it if "post" does nothing in Azahar.
TOUCH_MODE = "post"
TOUCH_W, TOUCH_H = 320, 240          # ESP32 touch coordinates
WINDOW_HINTS = ("Azahar", "Secondary Window")

ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixel positions with Windows scaling

pad = vg.VX360Gamepad()
print("Virtual Xbox 360 controller created")


def release_all():
    """Neutral controller: used at start and when the ESP32 goes offline."""
    pad.reset()
    pad.update()


# ---------- Touch -> Azahar bottom screen ----------
class Touch:
    def __init__(self):
        self.hwnd = None          # window that receives the clicks (Azahar's render area)
        self.down = False
        self.saved_cursor = None

    def find_window(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            return self.hwnd
        found = []

        def cb(h, _):
            if win32gui.IsWindowVisible(h) and all(t in win32gui.GetWindowText(h) for t in WINDOW_HINTS):
                found.append(h)

        win32gui.EnumWindows(cb, None)
        if not found:
            print("Touch: Azahar bottom window not found (Separate Windows layout?)")
            return None
        target = found[0]
        children = []
        win32gui.EnumChildWindows(target, lambda c, _: children.append(c), None)
        if children:
            target = children[0]   # Qt draws the game in a child window covering the client area
        self.hwnd = target
        return target

    def to_client(self, hwnd, x, y):
        """ESP32 320x240 -> pixel inside the 4:3 game area of Azahar's window."""
        _, _, w, h = win32gui.GetClientRect(hwnd)
        if w * 3 > h * 4:
            gh, gw = h, h * 4 // 3
        else:
            gw, gh = w, w * 3 // 4
        gx, gy = (w - gw) // 2, (h - gh) // 2
        cx = gx + int((min(max(x, 0), TOUCH_W - 1) + 0.5) * gw / TOUCH_W)
        cy = gy + int((min(max(y, 0), TOUCH_H - 1) + 0.5) * gh / TOUCH_H)
        return cx, cy

    def event(self, x, y, down):
        hwnd = self.find_window()
        if hwnd is None:
            return
        try:
            cx, cy = self.to_client(hwnd, x, y)
            if TOUCH_MODE == "post":
                lp = win32api.MAKELONG(cx, cy)
                if down and not self.down:
                    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lp)
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
                elif down:
                    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON, lp)
                else:
                    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lp)
            else:
                sx, sy = win32gui.ClientToScreen(hwnd, (cx, cy))
                if down and not self.down:
                    self.saved_cursor = win32api.GetCursorPos()
                    win32api.SetCursorPos((sx, sy))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
                elif down:
                    win32api.SetCursorPos((sx, sy))
                else:
                    win32api.SetCursorPos((sx, sy))
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
                    if self.saved_cursor:
                        win32api.SetCursorPos(self.saved_cursor)
            if down != self.down:
                print(f"touch {'down' if down else 'up  '} ESP32 {x:3d},{y:3d} -> Azahar {cx},{cy}")
            self.down = down
        except win32gui.error as e:   # window closed / changed
            print("Touch error:", e)
            self.hwnd = None

    def release(self):
        if self.down:
            self.event(0, 0, False)


touch = Touch()


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"MQTT connected ({reason_code}), waiting for ESP32 inputs...")
    client.subscribe("console/input/#", qos=1)
    client.subscribe("console/status", qos=1)


def on_message(client, userdata, msg):
    try:
        if msg.topic == "console/status":
            status = msg.payload.decode()
            print("ESP32 is", status)
            if status == "offline":
                release_all()       # don't leave a button stuck if the ESP32 drops
                touch.release()
            return

        data = json.loads(msg.payload)

        if msg.topic == "console/input/stick":
            # ESP32 sends -100..100 with up = positive, same as XInput
            pad.left_joystick_float(x_value_float=data["x"] / 100, y_value_float=data["y"] / 100)
            pad.update()

        elif msg.topic == "console/input/button":
            button = BUTTON_MAP.get(data["btn"])
            if button is None:
                return
            if data["pressed"]:
                pad.press_button(button=button)
            else:
                pad.release_button(button=button)
            pad.update()
            print(f"{data['btn']:>2} {'down' if data['pressed'] else 'up'}")

        elif msg.topic == "console/input/touch":
            touch.event(int(data["x"]), int(data["y"]), bool(data["down"]))

    except (ValueError, KeyError) as e:
        print("Bad message on", msg.topic, msg.payload, e)


def main():
    release_all()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="console3ds-bridge")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=10)
    print("Bridge running. Ctrl+C to stop.")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        release_all()
        touch.release()
        client.disconnect()
        print("Bridge stopped")


if __name__ == "__main__":
    main()
