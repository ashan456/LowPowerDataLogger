#include <Arduino.h>
#include "FS.h"
#include <LittleFS.h>
#include "esp_sleep.h"

// ---------------- Date/Time stubs (replace with RTC/NTP later) ----------------
String getDateStr() {            // e.g., "YYYY-MM-DD"
  return "2025-08-27";           // <- plug in RTC/NTP date
}
String getTimeStr() {            // e.g., "HH:MM:SS"
  return "06:25:42";             // <- plug in RTC/NTP time
}
// -----------------------------------------------------------------------------

// Track boots across deep sleep (lives in RTC memory)
RTC_DATA_ATTR uint32_t wakeCount = 0;

// Current targets
String currentDate = "";
String currentFolder = "";
String currentCsv = "";

// Ensure /logs/YYYY-MM-DD/YYYY-MM-DD.csv with header
bool ensureTodayTargets(const String &dateStr) {
  LittleFS.mkdir("/logs");
  String dayFolder = "/logs/" + dateStr;
  LittleFS.mkdir(dayFolder);

  String csvPath = dayFolder + "/" + dateStr + ".csv";

  bool needHeader = false;
  if (!LittleFS.exists(csvPath)) {
    needHeader = true;
  } else {
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
    f.println("time,temperature,humidity");  // edit columns as needed
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

void logReading(float temperature, float humidity) {
  String today = getDateStr();
  if (today != currentDate) {
    if (!ensureTodayTargets(today)) return;
  }
  String line = getTimeStr() + "," + String(temperature, 2) + "," + String(humidity, 2);
  if (!appendCsv(line)) {
    Serial.println("Append failed");
  }
}

void setup() {
  Serial.begin(115200);
  delay(50);

  // Why we woke up
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.println("\n==============================");
  if (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.println("🔋 Woke up from deep sleep (timer).");
  } else {
    Serial.println("⚡ First boot or external reset.");
  }
  Serial.printf("Wake count: %lu\n", (unsigned long)++wakeCount);

  // Mount FS
  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS mount failed");
    // Even if FS fails, still go to sleep to save power
  } else {
    // Prepare today's CSV
    ensureTodayTargets(getDateStr());

    // --- Simulated sensor values ---
    // Use ESP RNG for good randomness across sleeps
    // Temp: 20.0–35.0°C, Humidity: 30.0–80.0 %
    uint32_t r1 = esp_random();
    uint32_t r2 = esp_random();
    float temperature = 20.0f + (float)(r1 % 150) / 10.0f;  // 20.0–34.9
    float humidity    = 30.0f + (float)(r2 % 500) / 10.0f;  // 30.0–79.9

    // Log once
    logReading(temperature, humidity);

    Serial.print("Logged -> T: ");
    Serial.print(temperature, 1);
    Serial.print(" °C, H: ");
    Serial.print(humidity, 1);
    Serial.println(" %");
    Serial.println("File: " + currentCsv);
  }

  // Short settle to ensure FS write completes
  delay(50);

  // Sleep for 60 seconds
  const uint64_t SLEEP_US = 60ULL * 1000000ULL;
  esp_sleep_enable_timer_wakeup(SLEEP_US);
  Serial.println("🌙 Going to deep sleep for 60 seconds...");
  Serial.flush();
  esp_deep_sleep_start();
}

void loop() {
  // Not used — device never returns here after deep sleep
}
