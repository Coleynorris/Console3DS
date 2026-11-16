"""
Console3DS - test_game.py (step 5)
Simple test game: the laptop window is the "top screen", the ESP32 is the controller
and the "bottom screen". Tests both directions of MQTT:
  ESP32 -> laptop : console/input/button, console/input/stick, console/input/touch
  laptop -> ESP32 : game/state/ui  (retained, QoS 1)  -> ESP32 draws score + minimap

Goal: move the green player onto the yellow coin to score.
  Stick     move the player
  A         hold = go faster
  B         reset score
  X         change the player's color
  Y / SW    teleport the coin somewhere else
  Touch     tap the ESP32 screen = teleport the player there (whole screen = whole map)

Keyboard (to test without the ESP32): arrows = stick, Z=A, X=B, C=X, V=Y, mouse click = touch

Run:  python test_game.py
Needs: python -m pip install paho-mqtt pygame   (already done)
"""

import json
import random
import threading
import time

import paho.mqtt.client as mqtt
import pygame

BROKER = "localhost"          # Mosquitto runs on this laptop
PORT = 1883

W, H = 640, 480               # window size (map size)
TOUCH_W, TOUCH_H = 320, 240   # ESP32 screen size (landscape)
PLAYER_R, COIN_R = 16, 10
SPEED = 250                   # pixels per second at full stick
COLORS = [(60, 220, 90), (80, 160, 255), (255, 90, 90), (230, 120, 255)]


# ---------- Inputs shared between the MQTT thread and the game ----------
class Inputs:
    def __init__(self):
        self.lock = threading.Lock()
        self.stick = (0, 0)       # -100..100
        self.held = set()         # buttons currently held
        self.presses = []         # button presses since last frame
        self.taps = []            # touch presses since last frame (ESP32 pixels)
        self.esp_status = "unknown"
        self.msg_count = 0


inputs = Inputs()


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"MQTT connected ({reason_code})")
    client.subscribe("console/input/#", qos=1)
    client.subscribe("console/status", qos=1)


def on_message(client, userdata, msg):
    try:
        if msg.topic == "console/status":
            with inputs.lock:
                inputs.esp_status = msg.payload.decode()
            print("ESP32 is", inputs.esp_status)
            return

        data = json.loads(msg.payload)
        with inputs.lock:
            inputs.msg_count += 1
            if msg.topic == "console/input/stick":
                inputs.stick = (data["x"], data["y"])
            elif msg.topic == "console/input/button":
                if data["pressed"]:
                    inputs.held.add(data["btn"])
                    inputs.presses.append(data["btn"])
                else:
                    inputs.held.discard(data["btn"])
            elif msg.topic == "console/input/touch":
                if data["down"]:
                    inputs.taps.append((data["x"], data["y"]))
    except (ValueError, KeyError) as e:
        print("Bad message on", msg.topic, msg.payload, e)


def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="console3ds-laptop")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, keepalive=10)
    client.loop_start()           # MQTT runs in its own thread
    return client


# ---------- Game ----------
def random_coin():
    return [random.randint(COIN_R + 10, W - COIN_R - 10),
            random.randint(COIN_R + 40, H - COIN_R - 10)]


def main():
    client = start_mqtt()

    pygame.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Console3DS - test game (top screen)")
    font = pygame.font.SysFont("consolas", 18)
    clock = pygame.time.Clock()

    player = [W / 2, H / 2]
    coin = random_coin()
    score = 0
    color_i = 0
    last_ui = None
    last_ui_time = 0.0

    key_buttons = {pygame.K_z: "A", pygame.K_x: "B", pygame.K_c: "X", pygame.K_v: "Y"}

    running = True
    while running:
        dt = clock.tick(60) / 1000

        # --- keyboard / mouse fallback ---
        key_presses, key_taps = [], []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in key_buttons:
                key_presses.append(key_buttons[event.key])
            elif event.type == pygame.MOUSEBUTTONDOWN:
                mx, my = event.pos
                key_taps.append((mx * TOUCH_W / W, my * TOUCH_H / H))
        keys = pygame.key.get_pressed()
        kx = (keys[pygame.K_RIGHT] - keys[pygame.K_LEFT]) * 100
        ky = (keys[pygame.K_UP] - keys[pygame.K_DOWN]) * 100

        # --- read ESP32 inputs ---
        with inputs.lock:
            sx, sy = inputs.stick
            held = set(inputs.held)
            presses = inputs.presses + key_presses
            taps = inputs.taps + key_taps
            inputs.presses, inputs.taps = [], []
            esp_status, msg_count = inputs.esp_status, inputs.msg_count
        if kx or ky:
            sx, sy = kx, ky
        held |= {key_buttons[k] for k in key_buttons if keys[k]}

        # --- update ---
        for b in presses:
            if b == "B":
                score = 0
            elif b == "X":
                color_i = (color_i + 1) % len(COLORS)
            elif b in ("Y", "SW"):
                coin = random_coin()
        for tx, ty in taps:
            player = [tx * W / TOUCH_W, ty * H / TOUCH_H]

        speed = SPEED * (2 if "A" in held else 1)
        player[0] += sx / 100 * speed * dt
        player[1] -= sy / 100 * speed * dt          # stick up = positive y = screen up
        player[0] = max(PLAYER_R, min(W - PLAYER_R, player[0]))
        player[1] = max(PLAYER_R, min(H - PLAYER_R, player[1]))

        if (player[0] - coin[0]) ** 2 + (player[1] - coin[1]) ** 2 < (PLAYER_R + COIN_R) ** 2:
            score += 1
            coin = random_coin()

        # --- send bottom-screen state to the ESP32 (max 10/s, only if changed) ---
        ui = {
            "score": score,
            "px": round(player[0] / W, 3), "py": round(player[1] / H, 3),
            "cx": round(coin[0] / W, 3), "cy": round(coin[1] / H, 3),
            "col": color_i,
        }
        now = time.time()
        if ui != last_ui and now - last_ui_time > 0.1:
            client.publish("game/state/ui", json.dumps(ui, separators=(",", ":")), qos=1, retain=True)
            last_ui, last_ui_time = ui, now

        # --- draw ---
        screen.fill((20, 20, 30))
        pygame.draw.circle(screen, (255, 210, 40), coin, COIN_R)
        pygame.draw.circle(screen, COLORS[color_i], (int(player[0]), int(player[1])), PLAYER_R)
        held_txt = " ".join(sorted(held)) or "-"
        hud = f"Score {score}   stick {sx:4d},{sy:4d}   held {held_txt}   ESP32 {esp_status}   msgs {msg_count}"
        screen.blit(font.render(hud, True, (230, 230, 230)), (10, 10))
        pygame.display.flip()

    client.loop_stop()
    client.disconnect()
    pygame.quit()


if __name__ == "__main__":
    main()
