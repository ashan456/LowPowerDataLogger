#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <RTClib.h>
#include "esp_sleep.h"

// ===================== CONFIG =====================
#define SDA_PIN        4
#define SCL_PIN        5
#define LED_PIN       10
#define MODE_BTN_PIN  11        // Hold LOW at boot for Dump/Data Retrieval mode
#define SLEEP_SECONDS 450       // Deep-sleep interval 7.5min
static const char *ROOT = "/logs";

// ---- Battery sense (SET THESE) ----
#define VBAT_PIN      3
static const float VBAT_RTOP = 390000.0f;  // ohms
static const float VBAT_RBOT = 100000.0f;  // ohms
static const float VBAT_CAL  = 1.0225f;    // tweak after DMM comparison
// ==================================================

// --- Devices ---
Adafruit_SHT4x sht4;
RTC_PCF8523 rtc;
bool rtc_ok = false;

// --- RTC-deep sleep boot counter (in RTC RAM) ---
RTC_DATA_ATTR uint32_t wakeCount = 0;

// --- Current targets for logger mode ---
String currentDate, currentFolder, currentCsv;

// ---------- Small utils ----------
static inline void blinkOnce(uint16_t ms = 200) {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(ms);
  digitalWrite(LED_PIN, LOW);
}

static inline void zp2(Print& p, int v) { if (v < 10) p.print('0'); p.print(v); }

// ---------- File-system helpers for logger ----------
bool ensureTodayTargets(const String &dateStr) {
  LittleFS.mkdir("/logs");
  String dayFolder = String(ROOT) + "/" + dateStr;
  LittleFS.mkdir(dayFolder);

  String csvPath = dayFolder + "/" + dateStr + ".csv";

  bool needHeader = !LittleFS.exists(csvPath);
  if (!needHeader) {
    File f = LittleFS.open(csvPath, FILE_READ);
    if (!f || f.size() == 0) needHeader = true;
    if (f) f.close();
  }
  if (needHeader) {
    File f = LittleFS.open(csvPath, FILE_WRITE);
    if (!f) {
      Serial.println("Failed to create CSV for header");
      return false;
    }
    f.println("time,temperature,humidity,vbat_mV");
    f.close();
  }

  currentDate   = dateStr;
  currentFolder = dayFolder;
  currentCsv    = csvPath;
  return true;
}

bool appendCsv(const String &csvLine) {
  File f = LittleFS.open(currentCsv, FILE_APPEND);
  if (!f) {
    f = LittleFS.open(currentCsv, FILE_WRITE);
    if (!f) {
      Serial.println("Failed to open CSV (append/create)");
      return false;
    }
  }
  f.println(csvLine);
  f.flush();
  f.close();
  return true;
}

// ---------- Sensor/RTC helpers ----------
bool initI2CAndDevices() {
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000); // 100 kHz

  // RTC
  rtc_ok = rtc.begin();
  if (!rtc_ok) {
    Serial.println("❌ Couldn't find PCF8523 RTC on I2C bus!");
  } else {
    if (!rtc.initialized() || rtc.lostPower()) {
      Serial.println("⏱️ RTC not initialized or lost power. Setting to compile time.");
      rtc.adjust(DateTime(F(__DATE__), F(__TIME__)));
    }
    rtc.start();
  }

  // SHT4x
  if (!sht4.begin(&Wire)) {
    Serial.println("❌ Failed to find SHT4x. Check wiring.");
    return false;
  }
  sht4.setPrecision(SHT4X_HIGH_PRECISION);
  sht4.setHeater(SHT4X_NO_HEATER);

  // VBAT ADC pin prepare
  pinMode(VBAT_PIN, INPUT);
  #if defined(ARDUINO_ARCH_ESP32)
    analogSetPinAttenuation(VBAT_PIN, ADC_11db);
  #endif

  return true;
}

DateTime readNow() {
  return rtc_ok ? rtc.now() : DateTime(F(__DATE__), F(__TIME__));
}

String dateToStr(const DateTime& dt) {
  String s;
  s.reserve(10);
  s += String(dt.year()); s += '-';
  if (dt.month() < 10) s += '0'; s += String(dt.month()); s += '-';
  if (dt.day()   < 10) s += '0'; s += String(dt.day());
  return s;
}

String timeToStr(const DateTime& dt) {
  String s;
  s.reserve(8);
  if (dt.hour()   < 10) s += '0'; s += String(dt.hour());   s += ':';
  if (dt.minute() < 10) s += '0'; s += String(dt.minute()); s += ':';
  if (dt.second() < 10) s += '0'; s += String(dt.second());
  return s;
}

bool readSHT(float &tC, float &rh) {
  sensors_event_t humidity, temp;
  if (!sht4.getEvent(&humidity, &temp)) return false;
  tC = temp.temperature;
  rh = humidity.relative_humidity;
  return true;
}

// ----------- VBAT measurement helpers -----------
uint32_t readVBAT_mV() {
  const uint8_t N = 12;
  uint32_t acc_mV = 0;

  for (uint8_t i = 0; i < N; i++) {
    #if defined(ARDUINO_ARCH_ESP32)
      uint32_t mv = analogReadMilliVolts(VBAT_PIN);
    #else
      uint32_t raw = analogRead(VBAT_PIN);
      uint32_t mv = (raw * 3300UL) / 4095UL;
    #endif
    acc_mV += mv;
    delay(2);
  }
  float pin_mV = acc_mV / float(N);

  float gain = (VBAT_RTOP + VBAT_RBOT) / VBAT_RBOT;
  float vbat_mV = pin_mV * gain * VBAT_CAL;

  if (vbat_mV < 0) vbat_mV = 0;
  if (vbat_mV > 100000.0f) vbat_mV = 100000.0f;

  return (uint32_t)(vbat_mV + 0.5f);
}

// ===================================================
// ===============  DUMP MODE (Protocol) =============
// Commands: HELLO | LIST | GET YYYY-MM-DD | BYE
// ==== PLUS: SETTIME YYYY-MM-DD HH:MM:SS | TIME? | SENSE?
// ===================================================

static String readLineWithTimeout(uint32_t timeout_ms = 2000) {
  String line;
  uint32_t start = millis();
  while (millis() - start < timeout_ms) {
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\r') continue;
      if (c == '\n') return line;
      line += c;
    }
    delay(1);
  }
  return "";
}

static uint32_t crc32_update(uint32_t crc, const uint8_t* data, size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t k = 0; k < 8; k++) {
      uint32_t mask = -(crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320U & mask);
    }
  }
  return ~crc;
}

static String buildLogPath(const String& date) {
  if (date.length() != 10 || date.charAt(4) != '-' || date.charAt(7) != '-') return "";
  for (uint8_t i : {0,1,2,3,5,6,8,9}) if (!isDigit(date[i])) return "";
  return String(ROOT) + "/" + date + "/" + date + ".csv";
}

static uint32_t countRowsQuick(File& f) {
  const size_t BUFSZ = 1024;
  static uint8_t buf[BUFSZ];
  uint32_t rows = 0;
  f.seek(0, SeekSet);
  while (true) {
    int n = f.read(buf, BUFSZ);
    if (n <= 0) break;
    for (int i = 0; i < n; i++) if (buf[i] == '\n') rows++;
  }
  f.seek(0, SeekSet);
  return rows;
}

static void listDatesProtocol() {
  File root = LittleFS.open(ROOT, "r");
  if (!root || !root.isDirectory()) {
    Serial.println("DATES 0");
    Serial.println("END");
    return;
  }

  uint32_t count = 0;
  for (File e = root.openNextFile(); e; e = root.openNextFile()) {
    if (e.isDirectory()) count++;
  }

  Serial.print("DATES "); Serial.println(count);

  root = LittleFS.open(ROOT, "r");
  for (File e = root.openNextFile(); e; e = root.openNextFile()) {
    if (!e.isDirectory()) continue;
    String full = e.name();
    String date = full;
    if (full.startsWith(String(ROOT) + "/")) date = full.substring(strlen(ROOT) + 1);
    if (date.endsWith("/")) date.remove(date.length() - 1);
    Serial.println(date);
  }
  Serial.println("END");
}

static void handleGetDate(const String& date) {
  String path = buildLogPath(date);
  if (path == "") {
    Serial.println("ERR code=BAD_DATE msg=\"format YYYY-MM-DD\"");
    return;
  }
  if (!LittleFS.exists(path)) {
    Serial.print("ERR code=NOT_FOUND msg=\"no file for ");
    Serial.print(date);
    Serial.println("\"");
    return;
  }

  File f = LittleFS.open(path, "r");
  if (!f) {
    Serial.println("ERR code=IO msg=\"open failed\"");
    return;
  }

  size_t size = f.size();
  uint32_t rows = countRowsQuick(f);

  Serial.print("OK SIZE=");  Serial.print(size);
  Serial.print(" ROWS=");    Serial.print(rows);
  Serial.print(" PATH=");    Serial.println(path);

  Serial.println("DATA");

  const size_t BUFSZ = 1024;
  static uint8_t buf[BUFSZ];
  uint32_t crc = 0x00000000;
  size_t remaining = size;

  while (remaining > 0) {
    size_t chunk = remaining > BUFSZ ? BUFSZ : remaining;
    int n = f.read(buf, chunk);
    if (n <= 0) break;
    Serial.write(buf, n);
    crc = crc32_update(crc, buf, n);
    remaining -= n;
  }
  f.close();

  Serial.println();
  Serial.println("END");

  char hex[9];
  snprintf(hex, sizeof(hex), "%08lX", (unsigned long)crc);
  Serial.print("CRC32="); Serial.println(hex);
}

// Parse "YYYY-MM-DD HH:MM:SS" into DateTime; return true if ok
static bool parseDateTime(const String& s, DateTime& out) {
  if (s.length() != 19) return false;
  // 0123456789012345678
  // YYYY-MM-DD HH:MM:SS
  if (s.charAt(4) != '-' || s.charAt(7) != '-' || s.charAt(10) != ' ' ||
      s.charAt(13) != ':' || s.charAt(16) != ':') return false;
  for (uint8_t i : {0,1,2,3,5,6,8,9,11,12,14,15,17,18})
    if (!isDigit(s[i])) return false;

  int yr = s.substring(0,4).toInt();
  int mo = s.substring(5,7).toInt();
  int dy = s.substring(8,10).toInt();
  int hh = s.substring(11,13).toInt();
  int mm = s.substring(14,16).toInt();
  int ss = s.substring(17,19).toInt();

  if (yr < 2000 || yr > 2099) return false;
  if (mo < 1 || mo > 12) return false;
  if (dy < 1 || dy > 31) return false;
  if (hh < 0 || hh > 23) return false;
  if (mm < 0 || mm > 59) return false;
  if (ss < 0 || ss > 59) return false;

  out = DateTime(yr, mo, dy, hh, mm, ss);
  return true;
}

// Dump/Data Retrieval server (push-button entry only)
void runDataRetrievalMode() {
  // Bring up I2C + devices so commands work in dump mode
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  // RTC init (okay if missing)
  rtc_ok = rtc.begin();
  if (rtc_ok) rtc.start();

  // Try SHT4x init (okay if missing; we'll retry on demand)
  bool sht_ok = sht4.begin(&Wire);
  if (sht_ok) {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
  }

  bool led = false;
  uint32_t lastBlink = 0;

  Serial.println("OK v=1 caps=GET,LIST,CRC,SETTIME,TIME?,SENSE?"); // capability banner

  while (true) {
    // non-blocking blink (250 ms on / 750 ms off)
    uint32_t now = millis();
    if (now - lastBlink >= (led ? 250u : 750u)) {
      led = !led;
      digitalWrite(LED_PIN, led ? HIGH : LOW);
      lastBlink = now;
    }

    String line = readLineWithTimeout(100);
    if (line.length() == 0) continue;
    line.trim();

    if (line.startsWith("HELLO")) {
      Serial.println("OK v=1 caps=GET,LIST,CRC,SETTIME,TIME?,SENSE?");
    } else if (line == "LIST") {
      listDatesProtocol();
    } else if (line.startsWith("GET ")) {
      String date = line.substring(4);
      date.trim();
      handleGetDate(date);
    } else if (line == "BYE") {
      Serial.println("OK bye");
      // stay in server
    }
    else if (line == "TIME?") {
      if (!rtc_ok) {
        Serial.println("ERR code=RTC msg=\"not found\"");
      } else {
        DateTime nowdt = rtc.now();
        Serial.print("OK ");
        Serial.print(nowdt.year()); Serial.print('-');
        if (nowdt.month()<10) Serial.print('0'); Serial.print(nowdt.month()); Serial.print('-');
        if (nowdt.day()<10)   Serial.print('0'); Serial.print(nowdt.day());   Serial.print(' ');
        if (nowdt.hour()<10)  Serial.print('0'); Serial.print(nowdt.hour());  Serial.print(':');
        if (nowdt.minute()<10)Serial.print('0'); Serial.print(nowdt.minute());Serial.print(':');
        if (nowdt.second()<10)Serial.print('0'); Serial.println(nowdt.second());
      }
    }
    else if (line.startsWith("SETTIME ")) {
      String payload = line.substring(8);
      payload.trim();

      if (!rtc_ok) {
        Serial.println("ERR code=RTC msg=\"not found\"");
        continue;
      }

      DateTime dt;
      if (!parseDateTime(payload, dt)) {
        Serial.println("ERR code=BAD_DT msg=\"use SETTIME YYYY-MM-DD HH:MM:SS\"");
        continue;
      }

      rtc.adjust(dt);
      rtc.start();
      Serial.print("OK set ");
      Serial.print(dt.year()); Serial.print('-');
      if (dt.month()<10) Serial.print('0'); Serial.print(dt.month()); Serial.print('-');
      if (dt.day()<10)   Serial.print('0'); Serial.print(dt.day());   Serial.print(' ');
      if (dt.hour()<10)  Serial.print('0'); Serial.print(dt.hour());  Serial.print(':');
      if (dt.minute()<10)Serial.print('0'); Serial.print(dt.minute());Serial.print(':');
      if (dt.second()<10)Serial.print('0'); Serial.println(dt.second());
    }
    // ===== NEW: SENSE? -> take one SHT4x measurement =====
    else if (line == "SENSE?") {
      // (Re)try init if needed
      if (!sht_ok) {
        sht_ok = sht4.begin(&Wire);
        if (sht_ok) {
          sht4.setPrecision(SHT4X_HIGH_PRECISION);
          sht4.setHeater(SHT4X_NO_HEATER);
        }
      }

      if (!sht_ok) {
        Serial.println("ERR code=SHT4X msg=\"not found\"");
        continue;
      }

      sensors_event_t humidity, temp;
      if (!sht4.getEvent(&humidity, &temp)) {
        Serial.println("ERR code=SHT4X msg=\"read failed\"");
        continue;
      }

      Serial.print("OK T=");
      Serial.print(temp.temperature, 2);
      Serial.print("C RH=");
      Serial.print(humidity.relative_humidity, 2);
      Serial.println("%");
    }
    else {
      Serial.println("ERR code=UNKNOWN msg=\"use HELLO|LIST|GET YYYY-MM-DD|SETTIME YYYY-MM-DD HH:MM:SS|TIME?|SENSE?|BYE\"");
    }
  }
}

// ===================================================
// ==================  LOGGING MODE  =================
// ===================================================
void runLoggingMode() {
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.println("\n==============================");
  if (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.println("🔋 Woke up from deep sleep (timer).");
  } else {
    Serial.println("⚡ First boot or external reset.");
  }
  Serial.printf("Wake count: %lu\n", (unsigned long)++wakeCount);

  blinkOnce(150);

  bool dev_ok = initI2CAndDevices();

  DateTime now = readNow();
  String d = dateToStr(now);
  String t = timeToStr(now);

  if (!ensureTodayTargets(d)) {
    Serial.println("⚠️ Could not ensure today's CSV.");
  }

  float tC = NAN, rh = NAN;
  bool got = dev_ok && readSHT(tC, rh);
  if (!got) {
    Serial.println("❌ SHT4x read failed! Writing NaN placeholders.");
  }

  uint32_t vbat_mV = readVBAT_mV();

  String line = t + "," + String(tC, 2) + "," + String(rh, 2) + "," + String(vbat_mV);
  if (!appendCsv(line)) {
    Serial.println("Append failed");
  } else {
    Serial.print("Logged -> ");
    Serial.println(line);
    Serial.println("File: " + currentCsv);
  }

  Wire.end();
  pinMode(SDA_PIN, INPUT);
  pinMode(SCL_PIN, INPUT);
  digitalWrite(LED_PIN, LOW);

  esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_SECONDS * 1000000ULL);
  Serial.print("🌙 Going to deep sleep for ");
  Serial.print(SLEEP_SECONDS);
  Serial.println(" s...");
  Serial.flush();
  delay(20);
  esp_deep_sleep_start();
}

// ===================================================
// ================= Arduino entry ===================
// ===================================================
void setup() {
  pinMode(MODE_BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(115200);
  delay(100);

  if (!LittleFS.begin()) {
    Serial.println("⚠️ LittleFS mount failed. Attempting to format...");
    if (!LittleFS.begin(true)) {
      Serial.println("❌ LittleFS mount/format failed. Retrieval/logging to FS disabled.");
    }
  }

  // Boot-time mode select (hold MODE button low to enter server)
  if (digitalRead(MODE_BTN_PIN) == LOW) {
    uint32_t t0 = millis();
    while (digitalRead(MODE_BTN_PIN) == LOW && millis() - t0 < 1000) {
      delay(5);
    }
    if (millis() - t0 >= 400) {
      runDataRetrievalMode(); // never returns
    }
  }

  runLoggingMode(); // deep-sleeps at end
}

void loop() { }
