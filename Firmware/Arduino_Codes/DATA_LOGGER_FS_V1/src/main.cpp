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
#define MODE_BTN_PIN  11        // Hold LOW at boot for Data Retrieval mode
#define SLEEP_SECONDS 60        // Deep-sleep interval
static const char *ROOT = "/logs";
// ==================================================

// --- Devices ---
Adafruit_SHT4x sht4;
RTC_PCF8523 rtc;
bool rtc_ok = false;

// --- RTC-deep sleep boot counter (in RTC RAM) ---
RTC_DATA_ATTR uint32_t wakeCount = 0;

// --- Current targets for logger mode ---
String currentDate, currentFolder, currentCsv;

// ---------- Utils ----------
static inline void blinkOnce(uint16_t ms = 200) {
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(ms);
  digitalWrite(LED_PIN, LOW);
}

static inline void zp2(Print& p, int v) { if (v < 10) p.print('0'); p.print(v); }

void printPrompt() {
  Serial.println("==================================");
  Serial.println("📂 CSV File Reader");
  Serial.println("Enter date in format YYYY-MM-DD:");
}

bool isValidDateFormat(const String &s) {
  return (s.length() == 10 && s[4] == '-' && s[7] == '-' &&
          isDigit(s[0]) && isDigit(s[1]) && isDigit(s[2]) && isDigit(s[3]) &&
          isDigit(s[5]) && isDigit(s[6]) && isDigit(s[8]) && isDigit(s[9]));
}

void listAvailableDates() {
  Serial.println("📅 Available dates under /logs:");
  File root = LittleFS.open(ROOT, "r");
  if (!root || !root.isDirectory()) {
    Serial.println("  (none or /logs missing)");
    return;
  }
  File entry = root.openNextFile();
  bool any = false;
  while (entry) {
    if (entry.isDirectory()) {
      Serial.println(String("  ") + entry.name()); // e.g. /logs/2025-08-27
      any = true;
    }
    entry = root.openNextFile();
  }
  if (!any) Serial.println("  (no date folders)");
}

// Print CSV contents with row numbers; skip header row if present
void printCsvWithRowNumbers(File &f, bool skipHeader) {
  size_t row = 0;
  bool headerSkipped = !skipHeader;
  String line;

  while (f.available()) {
    line = f.readStringUntil('\n');
    line.trim();
    if (line.isEmpty()) continue;

    if (!headerSkipped) { headerSkipped = true; continue; } // skip header

    Serial.print(++row);
    Serial.print(": ");
    Serial.println(line);
  }
  if (row == 0) Serial.println("(no data rows)");
}

// ---------- FS helpers for logger ----------
bool ensureTodayTargets(const String &dateStr) {
  LittleFS.mkdir("/logs");
  String dayFolder = "/logs/" + dateStr;
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
    f.println("time,temperature,humidity");   // CSV header
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
  Wire.setClock(100000); // 100 kHz is fine for SHT4x + PCF8523

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
  return s; // YYYY-MM-DD
}

String timeToStr(const DateTime& dt) {
  String s;
  s.reserve(8);
  if (dt.hour()   < 10) s += '0'; s += String(dt.hour());   s += ':';
  if (dt.minute() < 10) s += '0'; s += String(dt.minute()); s += ':';
  if (dt.second() < 10) s += '0'; s += String(dt.second());
  return s; // HH:MM:SS
}

bool readSHT(float &tC, float &rh) {
  sensors_event_t humidity, temp;
  if (!sht4.getEvent(&humidity, &temp)) return false;
  tC = temp.temperature;
  rh = humidity.relative_humidity;
  return true;
}

// ---------- Modes ----------
void runDataRetrievalMode() {
  Serial.println("📖 Data Retrieval Mode (button held LOW at boot)");
  Serial.println("LittleFS mounted ✅");
  printPrompt();

  while (true) {
    if (!Serial.available()) { delay(10); continue; }

    String inputDate = Serial.readStringUntil('\n');
    inputDate.trim();

    if (!isValidDateFormat(inputDate)) {
      Serial.println("⚠️ Invalid format! Please enter YYYY-MM-DD");
      Serial.println();
      printPrompt();
      continue;
    }

    String filePath = String(ROOT) + "/" + inputDate + "/" + inputDate + ".csv";
    Serial.println("🔍 Trying to open: " + filePath);

    File file = LittleFS.open(filePath, "r");
    if (!file) {
      Serial.println("❌ File not found.");
      listAvailableDates();
      Serial.println();
      printPrompt();
      continue;
    }

    Serial.println("✅ File opened, printing contents (row-numbered; header skipped):");
    Serial.println("----------------------");
    printCsvWithRowNumbers(file, /*skipHeader=*/true);
    Serial.println("----------------------");
    file.close();

    Serial.println();
    Serial.print("Enter another date (YYYY-MM-DD): ");
  }
}

void runLoggingMode() {
  // Wake reason
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.println("\n==============================");
  if (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.println("🔋 Woke up from deep sleep (timer).");
  } else {
    Serial.println("⚡ First boot or external reset.");
  }
  Serial.printf("Wake count: %lu\n", (unsigned long)++wakeCount);

  blinkOnce(150);

  // I2C + devices
  bool dev_ok = initI2CAndDevices();

  // Timestamp as close as possible to measurement
  DateTime now = readNow();
  String d = dateToStr(now);
  String t = timeToStr(now);

  // Ensure CSV for today
  if (!ensureTodayTargets(d)) {
    Serial.println("⚠️ Could not ensure today's CSV.");
  }

  // Read sensors
  float tC = NAN, rh = NAN;
  bool got = dev_ok && readSHT(tC, rh);
  if (!got) {
    Serial.println("❌ SHT4x read failed! Writing NaN placeholders.");
  }

  // Append CSV line: time,temperature,humidity
  String line = t + "," + String(tC, 2) + "," + String(rh, 2);
  if (!appendCsv(line)) {
    Serial.println("Append failed");
  } else {
    Serial.print("Logged -> ");
    Serial.println(line);
    Serial.println("File: " + currentCsv);
  }

  // Tidy up before sleep
  Wire.end();
  pinMode(SDA_PIN, INPUT);
  pinMode(SCL_PIN, INPUT);
  digitalWrite(LED_PIN, LOW);

  // Sleep
  esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_SECONDS * 1000000ULL);
  Serial.print("🌙 Going to deep sleep for ");
  Serial.print(SLEEP_SECONDS);
  Serial.println(" s...");
  Serial.flush();
  delay(20);
  esp_deep_sleep_start();
}

// ---------- Arduino entry points ----------
void setup() {
  pinMode(MODE_BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Serial.begin(115200);
  delay(100);

  // Mount FS (try no-format, then format if needed)
  if (!LittleFS.begin()) {
    Serial.println("⚠️ LittleFS mount failed. Attempting to format...");
    if (!LittleFS.begin(true)) {
      Serial.println("❌ LittleFS mount/format failed. Retrieval/logging to FS disabled.");
    }
  }

  // Decide mode at boot
  if (digitalRead(MODE_BTN_PIN) == LOW) {
    runDataRetrievalMode();   // never returns
  } else {
    runLoggingMode();         // deep sleeps at end
  }
}

void loop() {
  // Not used
}
