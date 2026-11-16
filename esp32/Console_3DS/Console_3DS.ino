// Console_3DS - Console3DS step 7d (main sketch for playing in Azahar)
//   - Sends all inputs over MQTT: buttons, stick, touch
//   - Shows Azahar's 3DS BOTTOM SCREEN, streamed by laptop\screen_stream.py
//     (JPEG frames over a plain TCP socket, port 5005)
//
// Board: Tools > Board > esp32 > "ESP32 Dev Module"
// Libraries: PubSubClient (Nick O'Leary), TJpg_Decoder (Bodmer), TFT_eSPI (working User_Setup.h)
// Serial Monitor: 115200 baud
// Wi-Fi name/password + laptop IP are in secrets.h (second tab)
//
// IMPORTANT: don't touch the joystick while the ESP32 boots (center is measured then).
//
// On the laptop (2 PowerShell windows, in Console3DS\laptop):
//   python azahar_bridge.py      <- controls -> Azahar
//   python screen_stream.py      <- Azahar bottom screen -> this screen
//
// Screen protocol (TCP 5005): ESP32 sends "N" = "give me the next update", laptop answers
//   "FRM2" + uint32 body length + body
//   body = uint16 tile count + per tile: uint16 x, uint16 y, uint32 jpeg length, jpeg
// Only the 64x48 tiles that changed are sent (or 1 full 320x240 JPEG if most changed).
// Drawing uses DMA: the next 16x16 block is decoded while the last one goes to the screen.
//
// Colors wrong (blue/red swapped or noisy)? -> set JPEG_SWAP_IN_DECODER to the other value.
#define JPEG_SWAP_IN_DECODER  true      // same as Bodmer's Flash_Jpg_DMA example

#include <TJpg_Decoder.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <Preferences.h>
#include "secrets.h"

// ---------- Settings ----------
#define MQTT_CLIENT_ID  "console3ds-esp32"
#define TOPIC_BUTTON    "console/input/button"
#define TOPIC_STICK     "console/input/stick"
#define TOPIC_TOUCH     "console/input/touch"
#define TOPIC_STATUS    "console/status"
#define SCREEN_PORT     5005

#define JOY_X_PIN   34
#define JOY_Y_PIN   35
#define JOY_SW_PIN  33
#define INVERT_X    false
#define INVERT_Y    true      // your stick reads up as down
#define DEADZONE    8         // percent
#define STICK_STEP  3         // only send if a value moved by at least this much

const unsigned long STREAM_MS   = 33;   // ~30 messages/s max for stick and touch
const unsigned long DEBOUNCE_MS = 20;

// Touch calibration:
//  - precise 9-point fit saved in flash by Touch_Calibrate9 (used if present)
//  - otherwise the old 4-corner calData below (TFT_eSPI's built-in method)
uint16_t calData[5] = { 489, 3353, 384, 3373, 7 };  // old calibration, rotation 1
float touchCal[6];            // screenX = c0*rx + c1*ry + c2 ; screenY = c3*rx + c4*ry + c5
bool haveTouchCal = false;
#define Z_THRESHOLD  500      // touch pressure (same as Touch_Calibrate9)

// ---------- Globals ----------
TFT_eSPI tft = TFT_eSPI();
WiFiClient wifiClient;        // MQTT
WiFiClient screenClient;      // bottom-screen frames
PubSubClient mqtt(wifiClient);

struct Btn {
  const char* name;
  int pin;
  bool pressed;
  bool lastRaw;
  unsigned long changedAt;
};
Btn buttons[] = {
  { "A",  25,         false, false, 0 },
  { "B",  26,         false, false, 0 },
  { "X",  27,         false, false, 0 },
  { "Y",  14,         false, false, 0 },
  { "SW", JOY_SW_PIN, false, false, 0 },   // stick click, sent like a button
};
const int NUM_BTNS = sizeof(buttons) / sizeof(buttons[0]);

int joyCenterX = 2048, joyCenterY = 2048;
int sentX = 0, sentY = 0;
unsigned long lastStickSend = 0;

bool touchDown = false;
uint16_t sentTouchX = 0, sentTouchY = 0;
unsigned long lastTouchSend = 0;

unsigned long lastWifiTry = 0, lastMqttTry = 0;

// Frame receiver
static uint8_t jpgBuf[60000];             // laptop keeps updates under 58000 bytes
uint16_t dmaBuf1[16 * 16], dmaBuf2[16 * 16];   // DMA ping-pong buffers (one JPEG block each)
bool dmaSel = false;
int statTiles = 0;
uint8_t header[8];
int headerGot = 0;
uint32_t frameLen = 0, frameGot = 0;
bool readingBody = false;
bool showingGame = false;                 // true once a frame is on screen
unsigned long lastScreenTry = 0, lastFrameAt = 0;
unsigned long statTime = 0;
int statFrames = 0;
unsigned long statDecodeMs = 0;

// ---------- Screen (status, shown until the game picture arrives) ----------
void drawStatusScreen(const char* msg) {
  showingGame = false;
  bool w = (WiFi.status() == WL_CONNECTED);
  bool m = mqtt.connected();
  bool s = screenClient.connected();
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(TL_DATUM);
  tft.setTextSize(2);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("Console3DS", 10, 10);
  tft.setTextColor(w ? TFT_GREEN : TFT_RED, TFT_BLACK);
  tft.drawString(w ? "WiFi    OK" : "WiFi    ...", 10, 50);
  tft.setTextColor(m ? TFT_GREEN : TFT_RED, TFT_BLACK);
  tft.drawString(m ? "MQTT    OK" : "MQTT    ...", 10, 75);
  tft.setTextColor(s ? TFT_GREEN : TFT_RED, TFT_BLACK);
  tft.drawString(s ? "Screen  OK" : "Screen  ...", 10, 100);
  tft.setTextSize(1);
  tft.setTextColor(TFT_YELLOW, TFT_BLACK);
  tft.drawString(msg, 10, 140);
  if (w) {
    tft.setTextColor(TFT_DARKGREY, TFT_BLACK);
    tft.drawString(("ESP32 IP " + WiFi.localIP().toString()).c_str(), 10, 220);
  }
}

// TJpg_Decoder hands us 16x16 blocks. pushImageDMA copies the block into one of two
// buffers and sends it in the background, so the decoder can start on the next block.
bool tftOutput(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
  if (y >= tft.height()) return false;
  uint16_t* buf = dmaSel ? dmaBuf2 : dmaBuf1;
  dmaSel = !dmaSel;
  tft.pushImageDMA(x, y, w, h, bitmap, buf);
  return true;
}

// Draw every tile of a received update
void drawUpdate(uint32_t bodyLen) {
  if (bodyLen < 2) return;
  uint16_t count;
  memcpy(&count, jpgBuf, 2);
  uint32_t off = 2;

  tft.startWrite();                       // keep the SPI bus for the whole update
  for (uint16_t i = 0; i < count; i++) {
    if (off + 8 > bodyLen) break;
    uint16_t x, y;
    uint32_t len;
    memcpy(&x, jpgBuf + off, 2);
    memcpy(&y, jpgBuf + off + 2, 2);
    memcpy(&len, jpgBuf + off + 4, 4);
    off += 8;
    if (off + len > bodyLen) break;      // corrupted, stop here
    TJpgDec.drawJpg(x, y, jpgBuf + off, len);
    off += len;
  }
  tft.dmaWait();                          // last block finished before releasing the bus
  tft.endWrite();
  statTiles += count;
}

// ---------- MQTT ----------
void send(const char* topic, const char* payload) {
  bool ok = mqtt.connected() && mqtt.publish(topic, payload);
  Serial.printf("%-22s %s%s\n", topic, payload, ok ? "" : "   (NOT sent, no MQTT)");
}

bool handleWifi() {
  static bool wasUp = false;
  bool up = (WiFi.status() == WL_CONNECTED);
  if (up != wasUp) {
    if (up) Serial.printf("Wi-Fi: connected, IP %s, %d dBm\n",
                          WiFi.localIP().toString().c_str(), WiFi.RSSI());
    else    Serial.println("Wi-Fi: lost");
    wasUp = up;
    drawStatusScreen(up ? "Connecting to the laptop..." : "Wi-Fi lost, reconnecting...");
  }
  if (!up && millis() - lastWifiTry > 10000) {
    Serial.println("Wi-Fi: retrying (hotspot on? 2.4 GHz?)");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWifiTry = millis();
  }
  return up;
}

bool handleMqtt() {
  static bool wasUp = false;
  bool up = mqtt.connected();
  if (!up && millis() - lastMqttTry > 3000) {
    lastMqttTry = millis();
    Serial.printf("MQTT: connecting to %s:%d...\n", MQTT_HOST, MQTT_PORT);
    if (mqtt.connect(MQTT_CLIENT_ID, TOPIC_STATUS, 1, true, "offline")) {
      wifiClient.setNoDelay(true);
      mqtt.publish(TOPIC_STATUS, "online", true);
      up = true;
    } else {
      Serial.printf("MQTT: failed, state %d\n", mqtt.state());
    }
  }
  if (up != wasUp) {
    Serial.println(up ? "MQTT: connected" : "MQTT: lost");
    wasUp = up;
    if (!showingGame) drawStatusScreen(up ? "Waiting for screen_stream.py..." : "MQTT lost, reconnecting...");
  }
  return up;
}

// ---------- Bottom-screen stream ----------
void requestFrame() {
  screenClient.write((uint8_t)'N');
}

void resetReceiver() {
  headerGot = 0;
  frameGot = 0;
  readingBody = false;
}

void closeStream(const char* why) {
  Serial.printf("Screen: %s\n", why);
  screenClient.stop();
  resetReceiver();
  drawStatusScreen("Screen stream lost - is screen_stream.py running?");
}

void handleScreen(bool wifiUp) {
  if (!wifiUp) return;

  // Not connected: try every 2 s
  if (!screenClient.connected()) {
    if (showingGame) closeStream("connection lost");
    if (millis() - lastScreenTry < 2000) return;
    lastScreenTry = millis();
    if (screenClient.connect(MQTT_HOST, SCREEN_PORT, 1000)) {
      Serial.println("Screen: connected to screen_stream.py");
      screenClient.setNoDelay(true);
      resetReceiver();
      lastFrameAt = millis();
      requestFrame();
    }
    return;
  }

  // Laptop sends at least 1 frame/s; nothing for 5 s = dead connection
  if (millis() - lastFrameAt > 5000) {
    closeStream("no frame for 5 s");
    return;
  }

  // Read what arrived (without blocking, so inputs keep working)
  int avail = screenClient.available();
  if (avail <= 0) return;

  if (!readingBody) {
    int n = screenClient.read(header + headerGot, min(avail, 8 - headerGot));
    if (n > 0) headerGot += n;
    if (headerGot < 8) return;
    if (memcmp(header, "FRM2", 4) != 0) { closeStream("bad header (old screen_stream.py?)"); return; }
    memcpy(&frameLen, header + 4, 4);                 // little endian, same as ESP32
    if (frameLen == 0 || frameLen > sizeof(jpgBuf)) { closeStream("frame too big"); return; }
    frameGot = 0;
    readingBody = true;
    avail = screenClient.available();
    if (avail <= 0) return;
  }

  int n = screenClient.read(jpgBuf + frameGot, min((uint32_t)avail, frameLen - frameGot));
  if (n > 0) frameGot += n;
  if (frameGot < frameLen) return;

  // Full update received: draw the changed tiles, then ask for the next one
  unsigned long t0 = millis();
  uint16_t count;
  memcpy(&count, jpgBuf, 2);
  if (count > 0) {
    drawUpdate(frameLen);
    statDecodeMs += millis() - t0;
    statFrames++;
    showingGame = true;
  }
  lastFrameAt = millis();                 // keep-alives (count 0) also count as "alive"
  resetReceiver();
  requestFrame();

  if (millis() - statTime >= 5000) {
    float secs = (millis() - statTime) / 1000.0;
    Serial.printf("Screen: %.1f updates/s, draw %lu ms/update, %.1f tiles/update\n",
                  statFrames / secs, statDecodeMs / max(statFrames, 1),
                  statTiles / (float)max(statFrames, 1));
    statTime = millis();
    statFrames = 0;
    statDecodeMs = 0;
    statTiles = 0;
  }
}

// ---------- Inputs ----------
int readAvg(int pin) {
  long sum = 0;
  for (int i = 0; i < 8; i++) sum += analogRead(pin);
  return sum / 8;
}

int normalize(int raw, int center, bool invert) {
  int v = (raw >= center) ? map(raw, center, 4095, 0, 100) : map(raw, 0, center, -100, 0);
  v = constrain(v, -100, 100);
  if (abs(v) < DEADZONE) v = 0;
  return invert ? -v : v;
}

void readButtons() {
  unsigned long now = millis();
  for (int i = 0; i < NUM_BTNS; i++) {
    Btn& b = buttons[i];
    bool raw = (digitalRead(b.pin) == LOW);
    if (raw != b.lastRaw) { b.lastRaw = raw; b.changedAt = now; }
    if (raw != b.pressed && now - b.changedAt >= DEBOUNCE_MS) {
      b.pressed = raw;
      char p[40];
      snprintf(p, sizeof(p), "{\"btn\":\"%s\",\"pressed\":%s}", b.name, raw ? "true" : "false");
      send(TOPIC_BUTTON, p);
    }
  }
}

void readStick() {
  unsigned long now = millis();
  if (now - lastStickSend < STREAM_MS) return;

  int x = normalize(readAvg(JOY_X_PIN), joyCenterX, INVERT_X);
  int y = normalize(readAvg(JOY_Y_PIN), joyCenterY, INVERT_Y);

  bool moved    = abs(x - sentX) >= STICK_STEP || abs(y - sentY) >= STICK_STEP;
  bool released = (x == 0 && y == 0) && (sentX != 0 || sentY != 0);
  if (moved || released) {
    char p[32];
    snprintf(p, sizeof(p), "{\"x\":%d,\"y\":%d}", x, y);
    send(TOPIC_STICK, p);
    sentX = x;
    sentY = y;
    lastStickSend = now;
  }
}

// Touch coordinates = 3DS bottom-screen coordinates (both 320x240), used in step 7b
void loadTouchCal() {
  Preferences prefs;
  prefs.begin("touchcal", true);
  haveTouchCal = (prefs.getBytesLength("c") == sizeof(touchCal)) &&
                 (prefs.getBytes("c", touchCal, sizeof(touchCal)) == sizeof(touchCal));
  prefs.end();
  Serial.println(haveTouchCal ? "Touch: using 9-point calibration from flash"
                              : "Touch: no 9-point calibration, using old calData (run Touch_Calibrate9)");
}

// 9 raw samples, keep the middle 5 (removes the XPT2046's random spikes), then calibrate
bool readTouchXY(uint16_t* x, uint16_t* y) {
  if (!haveTouchCal) return tft.getTouch(x, y);

  uint16_t xs[9], ys[9];
  for (int i = 0; i < 9; i++) {
    if (tft.getTouchRawZ() < Z_THRESHOLD) return false;
    tft.getTouchRaw(&xs[i], &ys[i]);
  }
  if (tft.getTouchRawZ() < Z_THRESHOLD) return false;     // lifted while sampling
  for (int i = 1; i < 9; i++) {
    uint16_t kx = xs[i], ky = ys[i];
    int j = i - 1, k = i - 1;
    while (j >= 0 && xs[j] > kx) { xs[j + 1] = xs[j]; j--; }
    xs[j + 1] = kx;
    while (k >= 0 && ys[k] > ky) { ys[k + 1] = ys[k]; k--; }
    ys[k + 1] = ky;
  }
  float rx = (xs[2] + xs[3] + xs[4] + xs[5] + xs[6]) / 5.0;
  float ry = (ys[2] + ys[3] + ys[4] + ys[5] + ys[6]) / 5.0;
  float fx = touchCal[0] * rx + touchCal[1] * ry + touchCal[2];
  float fy = touchCal[3] * rx + touchCal[4] * ry + touchCal[5];
  *x = (uint16_t)constrain((int)(fx + 0.5f), 0, 319);
  *y = (uint16_t)constrain((int)(fy + 0.5f), 0, 239);
  return true;
}

void readTouch() {
  unsigned long now = millis();
  uint16_t x, y;
  bool down = readTouchXY(&x, &y);

  // Ignore a single missed reading while the finger is down (no fake "release" + re-tap)
  static int misses = 0;
  if (!down && touchDown && ++misses < 3) return;
  misses = 0;
  char p[48];

  if (down && !touchDown) {
    touchDown = true;
    sentTouchX = x; sentTouchY = y;
    lastTouchSend = now;
    snprintf(p, sizeof(p), "{\"x\":%u,\"y\":%u,\"down\":true}", x, y);
    send(TOPIC_TOUCH, p);
  } else if (down && touchDown) {
    if (now - lastTouchSend >= STREAM_MS &&
        (abs((int)x - sentTouchX) > 2 || abs((int)y - sentTouchY) > 2)) {
      sentTouchX = x; sentTouchY = y;
      lastTouchSend = now;
      snprintf(p, sizeof(p), "{\"x\":%u,\"y\":%u,\"down\":true}", x, y);
      send(TOPIC_TOUCH, p);
    }
  } else if (!down && touchDown) {
    touchDown = false;
    snprintf(p, sizeof(p), "{\"x\":%u,\"y\":%u,\"down\":false}", sentTouchX, sentTouchY);
    send(TOPIC_TOUCH, p);
  }
}

// ---------- Main ----------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=== Console_3DS boot ===");

  for (int i = 0; i < NUM_BTNS; i++) pinMode(buttons[i].pin, INPUT_PULLUP);
  analogReadResolution(12);
  joyCenterX = readAvg(JOY_X_PIN);
  joyCenterY = readAvg(JOY_Y_PIN);
  Serial.printf("Joystick center: x %d  y %d\n", joyCenterX, joyCenterY);

  tft.init();
  tft.setRotation(1);
  tft.setTouch(calData);
  loadTouchCal();
  tft.initDMA();                     // background transfers to the screen
  tft.setSwapBytes(!JPEG_SWAP_IN_DECODER);   // JPEG colors -> TFT byte order (one of the two)

  TJpgDec.setJpgScale(1);
  TJpgDec.setSwapBytes(JPEG_SWAP_IN_DECODER);
  TJpgDec.setCallback(tftOutput);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(5);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);              // no Wi-Fi power saving: lower latency
  Serial.printf("Wi-Fi: joining \"%s\"...\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiTry = millis();
  statTime = millis();

  drawStatusScreen("Joining the laptop's hotspot...");
}

void loop() {
  bool wifiUp = handleWifi();
  if (wifiUp && handleMqtt()) mqtt.loop();
  handleScreen(wifiUp);
  readButtons();
  readStick();
  readTouch();
  delay(1);
}
