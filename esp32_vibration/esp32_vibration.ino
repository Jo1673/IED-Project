/*
 * ESP32 + GY-521 (MPU-6050) vibration monitor for 3D print failure detection
 *
 * Wiring (ESP32 dev board -> GY-521):
 *   3V3  -> VCC
 *   GND  -> GND
 *   GPIO21 (SDA) -> SDA
 *   GPIO22 (SCL) -> SCL
 *   AD0 -> GND (or leave open; GY-521 pulls it low, I2C address 0x68)
 *   INT -> not used
 *
 * What it does:
 *   - Samples the accelerometer at ~1 kHz (+/-4 g range).
 *   - Removes gravity with a slow running average, so only vibration is left.
 *   - Every 500 ms it sends one line over USB serial (115200 baud):
 *
 *       V,<esp_ms>,<rms_g>,<peak_g>,<samples>,<i2c_errors>
 *
 *     rms_g   = RMS of the vibration magnitude over the window
 *     peak_g  = largest instantaneous vibration magnitude in the window
 *     samples = good samples in the window (should be about 500)
 *     i2c_errors = failed reads in the window
 *
 *   Lines starting with '#' are human-readable status messages.
 *
 * The laptop script (monitor/monitor.py) decides what counts as a failure.
 */

#include <Wire.h>
#include <math.h>

// ---------- settings you may want to change ----------
constexpr int      SDA_PIN        = 21;
constexpr int      SCL_PIN        = 22;
constexpr uint32_t I2C_HZ         = 400000;
constexpr uint32_t SAMPLE_US      = 1000;     // 1 kHz sampling
constexpr uint32_t WINDOW_MS      = 500;      // one output line per window
constexpr float    LSB_PER_G      = 8192.0f;  // +/-4 g range on the MPU-6050
constexpr float    GRAVITY_ALPHA  = 0.001f;   // gravity estimate time constant ~1 s
constexpr uint32_t WARMUP_SAMPLES = 2000;     // settle the gravity estimate first
// -----------------------------------------------------

constexpr uint8_t MPU_ADDR = 0x68;
constexpr uint8_t REG_SMPLRT_DIV   = 0x19;
constexpr uint8_t REG_CONFIG       = 0x1A;
constexpr uint8_t REG_ACCEL_CONFIG = 0x1C;
constexpr uint8_t REG_ACCEL_XOUT_H = 0x3B;
constexpr uint8_t REG_PWR_MGMT_1   = 0x6B;
constexpr uint8_t REG_WHO_AM_I     = 0x75;

static uint32_t nextSample = 0;
static uint32_t winStart = 0;
static uint32_t warm = 0;
static bool     seeded = false;
static float    gx = 0, gy = 0, gz = 0;     // running gravity estimate (g)
static double   sumsq = 0;
static float    peakMag = 0;
static uint32_t nGood = 0;
static uint32_t nErr = 0;

static bool writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool readReg(uint8_t reg, uint8_t &val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)1) != 1) return false;
  val = Wire.read();
  return true;
}

static bool readAccel(float &ax, float &ay, float &az) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT_H);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((uint8_t)MPU_ADDR, (uint8_t)6) != 6) return false;
  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = Wire.read();   // read in order, one by one
  int16_t rx = (int16_t)(((uint16_t)b[0] << 8) | b[1]);
  int16_t ry = (int16_t)(((uint16_t)b[2] << 8) | b[3]);
  int16_t rz = (int16_t)(((uint16_t)b[4] << 8) | b[5]);
  ax = rx / LSB_PER_G;
  ay = ry / LSB_PER_G;
  az = rz / LSB_PER_G;
  return true;
}

static bool initMpu() {
  uint8_t who = 0;
  if (!readReg(REG_WHO_AM_I, who)) return false;
  if (who != 0x68) {
    Serial.printf("# WARN: WHO_AM_I=0x%02X (expected 0x68, may be a clone; continuing)\n", who);
  }
  if (!writeReg(REG_PWR_MGMT_1, 0x80)) return false;   // reset
  delay(100);
  if (!writeReg(REG_PWR_MGMT_1, 0x01)) return false;   // wake, PLL clock
  delay(10);
  if (!writeReg(REG_CONFIG, 0x00)) return false;       // DLPF off: ~260 Hz accel bandwidth
  if (!writeReg(REG_SMPLRT_DIV, 7)) return false;      // 8 kHz / 8 = 1 kHz
  if (!writeReg(REG_ACCEL_CONFIG, 0x08)) return false; // +/-4 g
  return true;
}

void setup() {
  Serial.begin(115200);
#ifdef LED_BUILTIN
  pinMode(LED_BUILTIN, OUTPUT);
#endif
  delay(300);
  Serial.println("# esp32_vibration starting");

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(I2C_HZ);

  while (!initMpu()) {
    Serial.println("# ERROR: MPU-6050 not responding. Check wiring (3V3, GND, SDA=21, SCL=22).");
    delay(1000);
  }
  Serial.println("# MPU-6050 OK, +/-4 g, 1 kHz. Letting the gravity estimate settle...");

  nextSample = micros() + SAMPLE_US;
  winStart = millis();
}

void loop() {
  uint32_t now = micros();
  if ((int32_t)(now - nextSample) < 0) return;
  nextSample += SAMPLE_US;
  if ((int32_t)(now - nextSample) > 0) nextSample = now + SAMPLE_US;  // fell behind: resync

  float ax, ay, az;
  if (!readAccel(ax, ay, az)) {
    nErr++;
  } else {
    if (!seeded) {
      gx = ax; gy = ay; gz = az;
      seeded = true;
    }
    float a = (warm < WARMUP_SAMPLES) ? 0.01f : GRAVITY_ALPHA;
    gx += a * (ax - gx);
    gy += a * (ay - gy);
    gz += a * (az - gz);

    if (warm < WARMUP_SAMPLES) {
      warm++;
      if (warm == WARMUP_SAMPLES) {
        Serial.println("# ready, streaming");
        winStart = millis();
        nErr = 0;
      }
      return;
    }

    float dx = ax - gx, dy = ay - gy, dz = az - gz;
    float m2 = dx * dx + dy * dy + dz * dz;
    sumsq += m2;
    float mag = sqrtf(m2);
    if (mag > peakMag) peakMag = mag;
    nGood++;
  }

  if (warm >= WARMUP_SAMPLES && (millis() - winStart) >= WINDOW_MS) {
    float rms = nGood ? sqrtf((float)(sumsq / nGood)) : 0.0f;
    Serial.printf("V,%lu,%.4f,%.4f,%u,%u\n",
                  (unsigned long)millis(), rms, peakMag,
                  (unsigned)nGood, (unsigned)nErr);
    sumsq = 0; peakMag = 0; nGood = 0; nErr = 0;
    winStart = millis();
#ifdef LED_BUILTIN
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));  // heartbeat blink
#endif
  }
}
