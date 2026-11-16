# Console3DS

A homemade 3DS-style handheld: an **ESP32** with a touchscreen, 4 buttons and a joystick acts
as the controller and the **bottom screen**, while a **Windows laptop** runs the
[Azahar](https://azahar-emu.org) 3DS emulator and shows the top screen.

```
                    MQTT (port 1883): buttons, stick, touch
   ESP32  ─────────────────────────────────────────────────►  Laptop
 (controller +                                              Mosquitto broker
  bottom screen) ◄─────────────────────────────────────────  azahar_bridge.py ──► virtual Xbox pad + clicks ──► Azahar
                    TCP (port 5005): bottom-screen JPEG tiles  screen_stream.py ◄── captures Azahar's bottom window
                      over the laptop's Mobile Hotspot
```

- Inputs go ESP32 → laptop over **MQTT**. `azahar_bridge.py` turns them into a virtual Xbox 360
  controller (buttons, Circle Pad) and mouse clicks on Azahar's bottom screen (touch).
- `screen_stream.py` captures Azahar's bottom screen and streams it to the ESP32, which draws it
  1:1 (both are 320×240). Only the parts that changed are sent (~20+ updates/s).

---

## 1. Hardware

| Part | Notes |
|---|---|
| ESP32 dev board (ESP32-WROOM, "ESP32 Dev Module") | |
| 3.2" ILI9341 SPI display with XPT2046 touch (320×240) | the common red-PCB module |
| 4 tactile push buttons | A, B, X, Y (12×12 mm used here, 6×6 mm also works) |
| Analog joystick module (VRx, VRy, SW) | 2-axis thumbstick with click |
| Breadboard(s) + jumper wires | |
| Windows 10/11 laptop with Wi-Fi | must be able to run a **Mobile Hotspot on 2.4 GHz** |

## 2. Wiring

### Display + touch

| Display pin | ESP32 |
|---|---|
| VCC, LED | 3.3V |
| GND | GND |
| SCK **and** T_CLK | GPIO 18 |
| SDI/MOSI **and** T_DIN | GPIO 23 |
| SDO/MISO **and** T_DO | GPIO 19 |
| CS | GPIO 5 |
| DC | GPIO 2 |
| RESET | GPIO 4 |
| T_CS | GPIO 21 |
| T_IRQ | GPIO 22 (not required by the main sketch) |

The display and the touch chip share the same SPI bus (18/23/19).

### Buttons (3DS layout: X top, Y left, A right, B bottom)

| Button | ESP32 | Other side of the button |
|---|---|---|
| A | GPIO 25 | GND |
| B | GPIO 26 | GND |
| X | GPIO 27 | GND |
| Y | GPIO 14 | GND |

No resistors and **no 3.3V** on the buttons: the sketch uses the ESP32's internal pull-ups
(pressed = LOW).

### Joystick

| Joystick pin | ESP32 |
|---|---|
| +5V / VCC | **3.3V** (not 5V, the ESP32's analog inputs max out at 3.3V) |
| GND | GND |
| VRx | GPIO 34 |
| VRy | GPIO 35 |
| SW | GPIO 33 |

34/35 are on ADC1, which still works while Wi-Fi is on (ADC2 pins don't).

### Breadboard tips (learned the hard way)

- **Wire diagonal legs of each button.** A 4-leg button is really a 2-terminal switch; legs are
  joined in pairs inside. Diagonally opposite legs are always on opposite sides of the switch.
- **Big 12×12 buttons placed on one side of the center gap:** the breadboard's 5-hole row strips
  can short the switch (button reads "always pressed"). Fix: bend up two diagonal legs so each
  row strip holds only one leg, and put the GPIO wire and GND wire in those two rows.
- **Each wire goes in the same row strip as its leg** (another hole of that row), not the row
  above/below.
- **Connect both GND rails** to ESP32 GND. Long breadboards often split the rails in the middle.
- Avoid GPIO 12 (boot pin), 0, 15, 1/3 (USB serial), 6–11 (flash). Free pins for more buttons:
  **13, 32** (16/17 on boards without PSRAM).

---

## 3. Software setup (laptop)

### 3.1 Arduino IDE + ESP32

1. Install **Arduino IDE 2** and the **esp32 by Espressif** board package
   (Boards Manager). Board: **Tools → Board → esp32 → ESP32 Dev Module**.
2. Library Manager, install:
   - **TFT_eSPI** (Bodmer)
   - **TJpg_Decoder** (Bodmer)
   - **PubSubClient** (Nick O'Leary)
3. **Configure TFT_eSPI** (important, the display stays black otherwise): copy
   `esp32/tft_espi_config/User_Setup.h` over `Documents/Arduino/libraries/TFT_eSPI/User_Setup.h`.
   It sets the pins above and **SPI at 27 MHz** (40 MHz gave a black screen with this module).
   Updating the TFT_eSPI library overwrites this file, so copy it back after an update.

### 3.2 Python

Install **Python 3.12** (tick "Add python.exe to PATH"), then:

```powershell
python -m pip install paho-mqtt pygame vgamepad pillow pywin32 mss pyinstaller
```

`vgamepad` installs the **ViGEmBus** driver (accept the installer, then reboot).
If the installer didn't run, start it by hand:
`<Python>\Lib\site-packages\vgamepad\win\vigem\install\x64\ViGEmBusSetup_x64.msi`.

### 3.3 MQTT broker (Mosquitto)

1. Install [Mosquitto](https://mosquitto.org/download/) for Windows (installs a Windows service).
2. Copy `broker/mosquitto.conf` over `C:\Program Files\mosquitto\mosquitto.conf`
   (admin), then restart the service: `Restart-Service mosquitto`.
   Mosquitto 2.x only accepts connections from the laptop itself without this config
   (`listener 1883` + `allow_anonymous true`). Logs go to `C:\Program Files\mosquitto\mosquitto.log`.
3. Open the firewall (admin PowerShell):
   ```powershell
   New-NetFirewallRule -DisplayName "Mosquitto MQTT 1883" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow -Profile Any
   New-NetFirewallRule -DisplayName "Console3DS screen 5005" -Direction Inbound -Protocol TCP -LocalPort 5005 -Action Allow -Profile Any
   ```

### 3.4 Mobile Hotspot

Settings → Network & internet → **Mobile hotspot**: turn it on, band **2.4 GHz** (the ESP32
can't use 5 GHz; "Any available" can end up on 5 GHz). The laptop is always
**192.168.137.1** on its own hotspot, which is the address the ESP32 uses.

### 3.5 Azahar + your game

1. Install Azahar from the official site **azahar-emu.org** (or GitHub `azahar-emu/azahar`).
2. Use a game **you dumped from your own 3DS/cartridge** (e.g. with GodMode9). It must be
   **decrypted**: Azahar refuses encrypted games.
   - `.cci` / `.3ds` (cartridge image) → **File → Load File**
   - `.cia` → **File → Install CIA** (updates/DLC too)
3. **View → Screen Layout → Separate Windows**: top and bottom screens in 2 windows.
   The bottom window is the one streamed to the ESP32. Don't minimize it (it can be behind
   other windows).
4. **Emulation → Configure → Controls**: with `azahar_bridge.py` running, pick the Xbox 360
   controller and bind each 3DS input by pressing the matching button on the ESP32.

---

## 4. ESP32 setup

1. Copy `esp32/Console_3DS/secrets.example.h` to **`secrets.h`** (same folder) and put
   **your** hotspot name and password in it (`secrets.h` is git-ignored, so it never gets pushed):
   ```cpp
   #define WIFI_SSID     "your-hotspot-name"
   #define WIFI_PASSWORD "your-hotspot-password"
   #define MQTT_HOST     "192.168.137.1"
   ```
2. **Touch calibration (once):** upload `esp32/Touch_Calibrate9/Touch_Calibrate9.ino`.
   Tap and hold each yellow cross until it turns green (stylus or fingernail).
   The result (aim for under ~3 px average error) is saved in the ESP32's flash and used
   automatically by the main sketch. Press RESET to redo it.
3. Upload `esp32/Console_3DS/Console_3DS.ino`.
   **Don't touch the joystick while the ESP32 boots**: it measures the stick's center then.

Until it's connected, the ESP32 shows a status screen: **WiFi / MQTT / Screen** (green = OK).

---

## 5. Running it

### One click

Double-click **`Console3DS.exe`**. It:
1. checks the Mobile Hotspot (opens its settings if it's off) and Mosquitto,
2. opens 2 windows: **Bridge** (controls + touch) and **Screen stream**,
3. starts Azahar directly in the game,
4. closes the 2 windows when you close Azahar.

The .exe isn't in the repo: build it yourself. First set **`GAMES_DIR`** (your games folder)
and **`GAME_HINT`** (part of the game's file name) at the top of `laptop/launcher.py`
(Azahar's default install path is already set; the scripts are found automatically). Then:

```powershell
cd laptop
python -m PyInstaller --onefile --noconsole --name Console3DS --distpath .. launcher.py
```

Windows SmartScreen may warn about a self-made .exe the first time: More info → Run anyway.

### By hand

Hotspot on, ESP32 running `Console_3DS`, then two PowerShell windows in `laptop/`:

```powershell
python azahar_bridge.py      # controls + touch -> Azahar
python screen_stream.py      # Azahar bottom screen -> ESP32
```

and start Azahar with the game.

### Controls

| ESP32 | 3DS |
|---|---|
| Joystick | Circle Pad |
| A / B / X / Y | A / B / X / Y (same positions as a real 3DS) |
| Joystick click | Start |
| Touchscreen | Bottom touchscreen (1:1) |

Not mapped yet: L, R, Select, D-pad, Home.

---

## 6. Project files

```
Console3DS/
├── Console3DS.exe              one-click launcher (built locally, not in the repo)
├── README.md
├── TODO.md                     progress log + notes
├── broker/
│   └── mosquitto.conf          Mosquitto config (copy into Program Files)
├── esp32/
│   ├── Console_3DS/            MAIN sketch: inputs over MQTT + bottom screen
│   │   └── secrets.example.h   copy to secrets.h: Wi-Fi name/password + laptop IP
│   ├── Touch_Calibrate9/       9-point touch calibration (saves to ESP32 flash)
│   └── tft_espi_config/
│       └── User_Setup.h        working TFT_eSPI config (copy into the library)
└── laptop/
    ├── azahar_bridge.py        MQTT -> virtual Xbox controller + touch clicks
    ├── screen_stream.py        Azahar bottom window -> ESP32 (TCP 5005)
    ├── launcher.py             source of Console3DS.exe
    └── test_game.py            small pygame test game (controls only now)
```

### MQTT topics (all JSON)

| Topic | Direction | Example | QoS / retained |
|---|---|---|---|
| `console/input/button` | ESP32 → laptop | `{"btn":"A","pressed":true}` (`SW` = stick click) | 0 |
| `console/input/stick` | ESP32 → laptop | `{"x":-42,"y":100}` (-100..100, up/right = +) | 0 |
| `console/input/touch` | ESP32 → laptop | `{"x":160,"y":120,"down":true}` (pixels) | 0 |
| `console/status` | ESP32 → laptop | `online` / `offline` (Last Will) | 1, retained |

Watch everything live:
```powershell
& "C:\Program Files\mosquitto\mosquitto_sub.exe" -h localhost -t "console/#" -v
```

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Display stays black | `User_Setup.h` not copied into TFT_eSPI, or SPI too fast (keep 27 MHz) |
| A button always pressed | Its GPIO and GND legs are joined (same row strip or same internal pair): use diagonal legs / bend legs, see wiring tips |
| A button never pressed | Its GND rail isn't connected to ESP32 GND (split rail?) |
| Stick moves the wrong way | Flip `INVERT_X` / `INVERT_Y` at the top of `Console_3DS.ino` |
| Character keeps moving after releasing the stick | Already fixed in the sketch (`WiFi.setSleep(false)` + `setNoDelay`) |
| Serial: `network not found` | Hotspot off or on 5 GHz → set band to 2.4 GHz |
| Serial: `MQTT: failed, state -2` | Broker unreachable: Mosquitto service stopped, firewall rule missing, wrong IP |
| Mosquitto service stops right after starting | A `log_dest stdout` line in the config (a service has no console) → log to a file |
| `VIGEM_ERROR_BUS_NOT_FOUND` | ViGEmBus driver not installed → run the .msi (see 3.2), reboot |
| `VIGEM_ERROR_TARGET_NOT_PLUGGED_IN` | First run while Windows installs the Xbox driver → run again. If it stays, install ViGEmBus 1.22 from github.com/nefarius/ViGEmBus/releases |
| Azahar: "encrypted" | Decrypt your dump with GodMode9 on your own 3DS |
| ESP32 stuck on "Screen ..." | `screen_stream.py` not running, Azahar not in Separate Windows, or bottom window minimized |
| Bottom screen colors wrong | Flip `JPEG_SWAP_IN_DECODER` in `Console_3DS.ino` |
| Touch lands next to the right spot | Run `Touch_Calibrate9` again |
| Touch does nothing in Azahar | In `azahar_bridge.py` set `TOUCH_MODE = "mouse"` |
