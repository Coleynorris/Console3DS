// Touch_Calibrate9 - precise 9-point touch calibration for Console3DS
//
// Board: ESP32 Dev Module. Serial Monitor: 115200 baud. Use a stylus or a fingernail.
//
// 1. Tap and HOLD each crosshair (~half a second) until it turns green, then lift.
// 2. The sketch computes the best fit for all 9 points (corrects offset, scale, rotation
//    and skew) and SAVES it in the ESP32 flash (Preferences "touchcal").
//    Console_3DS loads it automatically at boot - nothing to copy.
// 3. Test mode: tap anywhere, a dot is drawn where the ESP32 thinks you touched.
//    The 9 crosshairs stay visible so you can check accuracy on them.
//    Not happy? Press the RESET button on the ESP32 to calibrate again.
//
// Saved values survive uploading other sketches (unless "Erase All Flash" is enabled).

#include <SPI.h>
#include <TFT_eSPI.h>
#include <Preferences.h>

#define Z_THRESHOLD  500     // pressure needed to count as a touch
#define MARGIN       20      // crosshair distance from the screen edges

TFT_eSPI tft = TFT_eSPI();
Preferences prefs;

const int NPTS = 9;
int targetX[NPTS], targetY[NPTS];
float rawX[NPTS], rawY[NPTS];
float cal[6];                 // screenX = c0*rx + c1*ry + c2 ; screenY = c3*rx + c4*ry + c5

// ---- Raw touch reading: 9 samples, keep the middle 5 (removes spikes) ----
bool readRawFiltered(float& rx, float& ry) {
  uint16_t xs[9], ys[9];
  for (int i = 0; i < 9; i++) {
    if (tft.getTouchRawZ() < Z_THRESHOLD) return false;
    tft.getTouchRaw(&xs[i], &ys[i]);
  }
  if (tft.getTouchRawZ() < Z_THRESHOLD) return false;   // lifted while sampling
  // sort (tiny insertion sort)
  for (int i = 1; i < 9; i++) {
    uint16_t kx = xs[i], ky = ys[i];
    int j = i - 1, k = i - 1;
    while (j >= 0 && xs[j] > kx) { xs[j + 1] = xs[j]; j--; }
    xs[j + 1] = kx;
    while (k >= 0 && ys[k] > ky) { ys[k + 1] = ys[k]; k--; }
    ys[k + 1] = ky;
  }
  rx = (xs[2] + xs[3] + xs[4] + xs[5] + xs[6]) / 5.0;
  ry = (ys[2] + ys[3] + ys[4] + ys[5] + ys[6]) / 5.0;
  return true;
}

void crosshair(int x, int y, uint16_t color) {
  tft.drawFastHLine(x - 10, y, 21, color);
  tft.drawFastVLine(x, y - 10, 21, color);
  tft.drawCircle(x, y, 4, color);
}

// Hold on a point: average ~0.4 s of filtered readings
void measurePoint(int i) {
  crosshair(targetX[i], targetY[i], TFT_YELLOW);
  float sx = 0, sy = 0;
  int n = 0;
  while (n < 25) {
    float rx, ry;
    if (readRawFiltered(rx, ry)) {
      sx += rx; sy += ry; n++;
    } else if (n > 0) {
      n = 0; sx = sy = 0;          // lifted too early: start this point again
    }
    delay(15);
  }
  rawX[i] = sx / n;
  rawY[i] = sy / n;
  crosshair(targetX[i], targetY[i], TFT_GREEN);
  Serial.printf("Point %d: screen %3d,%3d  raw %.0f,%.0f\n", i + 1, targetX[i], targetY[i], rawX[i], rawY[i]);
  while (tft.getTouchRawZ() >= Z_THRESHOLD / 2) delay(10);   // wait for release
  delay(250);
}

// Solve the 3x3 system M * p = v (Cramer's rule)
bool solve3(double M[3][3], double v[3], double p[3]) {
  auto det = [](double a[3][3]) {
    return a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
         - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
         + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]);
  };
  double d = det(M);
  if (fabs(d) < 1e-9) return false;
  for (int c = 0; c < 3; c++) {
    double T[3][3];
    for (int r = 0; r < 3; r++)
      for (int k = 0; k < 3; k++) T[r][k] = (k == c) ? v[r] : M[r][k];
    p[c] = det(T) / d;
  }
  return true;
}

// Least squares fit: screen = a*rawX + b*rawY + c (for X and for Y)
bool computeCalibration() {
  double M[3][3] = { {0} }, vx[3] = {0}, vy[3] = {0};
  for (int i = 0; i < NPTS; i++) {
    double row[3] = { rawX[i], rawY[i], 1.0 };
    for (int r = 0; r < 3; r++) {
      for (int c = 0; c < 3; c++) M[r][c] += row[r] * row[c];
      vx[r] += row[r] * targetX[i];
      vy[r] += row[r] * targetY[i];
    }
  }
  double px[3], py[3];
  if (!solve3(M, vx, px) || !solve3(M, vy, py)) return false;
  for (int k = 0; k < 3; k++) { cal[k] = px[k]; cal[3 + k] = py[k]; }
  return true;
}

void applyCal(float rx, float ry, float& x, float& y) {
  x = cal[0] * rx + cal[1] * ry + cal[2];
  y = cal[3] * rx + cal[4] * ry + cal[5];
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== Touch_Calibrate9 ===");

  tft.init();
  tft.setRotation(1);                 // same rotation as Console_3DS
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("Tap and HOLD each yellow cross", 160, 100, 2);
  tft.drawString("until it turns green (stylus or nail)", 160, 120, 2);
  delay(2500);
  tft.fillScreen(TFT_BLACK);

  int xs[3] = { MARGIN, 160, 319 - MARGIN };
  int ys[3] = { MARGIN, 120, 239 - MARGIN };
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 3; c++) {
      targetX[r * 3 + c] = xs[c];
      targetY[r * 3 + c] = ys[r];
    }

  for (int i = 0; i < NPTS; i++) measurePoint(i);

  if (!computeCalibration()) {
    tft.fillScreen(TFT_RED);
    tft.setTextColor(TFT_WHITE, TFT_RED);
    tft.drawString("Calibration failed - press RESET", 160, 120, 2);
    Serial.println("Calibration failed (points too close?). Press RESET and retry.");
    while (true) delay(1000);
  }

  // Error on each point after the fit
  float worst = 0, sum = 0;
  for (int i = 0; i < NPTS; i++) {
    float x, y;
    applyCal(rawX[i], rawY[i], x, y);
    float e = sqrtf((x - targetX[i]) * (x - targetX[i]) + (y - targetY[i]) * (y - targetY[i]));
    sum += e;
    if (e > worst) worst = e;
  }
  Serial.printf("Fit error: average %.1f px, worst %.1f px\n", sum / NPTS, worst);
  Serial.printf("cal = { %.6f, %.6f, %.2f, %.6f, %.6f, %.2f }\n",
                cal[0], cal[1], cal[2], cal[3], cal[4], cal[5]);

  prefs.begin("touchcal", false);
  prefs.putBytes("c", cal, sizeof(cal));
  prefs.end();
  Serial.println("Saved to flash (Preferences \"touchcal\"). Console_3DS will use it.");

  // Test screen
  tft.fillScreen(TFT_BLACK);
  for (int i = 0; i < NPTS; i++) crosshair(targetX[i], targetY[i], TFT_DARKGREY);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  char buf[48];
  snprintf(buf, sizeof(buf), "Saved! avg error %.1f px, worst %.1f px", sum / NPTS, worst);
  tft.drawString(buf, 160, 60, 2);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("Test: tap the crosses. RESET = redo", 160, 180, 2);
}

void loop() {
  float rx, ry;
  if (readRawFiltered(rx, ry)) {
    float x, y;
    applyCal(rx, ry, x, y);
    tft.fillCircle((int)constrain(x, 0, 319), (int)constrain(y, 0, 239), 2, TFT_RED);
    static unsigned long lastPrint = 0;
    if (millis() - lastPrint > 200) {
      Serial.printf("touch %.0f,%.0f\n", x, y);
      lastPrint = millis();
    }
  }
  delay(10);
}
