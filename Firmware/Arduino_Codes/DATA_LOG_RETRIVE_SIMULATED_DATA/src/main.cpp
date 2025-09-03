#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include "esp_sleep.h"

// --------- CONFIG ---------
#define MODE_BTN_PIN 11        // Button to enter Data Retrieval mode (hold LOW at boot)
static const char *ROOT = "/logs";
// --------------------------

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

// Current targets for logger mode
String currentDate = "";
String currentFolder = "";
String currentCsv = "";

// ---------- Shared utilities ----------
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
    line.trim();                // remove \r and spaces
    if (line.length() == 0) {   // skip empty lines
      continue;
    }

    if (!headerSkipped) {
      // Skip the first non-empty line as header
      headerSkipped = true;
      continue;
    }

    row++;
    Serial.print(row);
    Serial.print(": ");
    Serial.println(line);
  }

  if (row == 0) {
    Serial.println("(no data rows)");
  }
}

// ---------- Logger-side file helpers ----------
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

// ---------- Mode handlers ----------
void runDataRetrievalMode() {
  Serial.println("📖 Data Retrieval Mode (button held LOW at boot)");
  Serial.println("LittleFS mounted ✅");
  printPrompt();

  while (true) {
    static String inputDate;

    if (Serial.available()) {
      inputDate = Serial.readStringUntil('\n');
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
        listAvailableDates();                // show available folders
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

    // Small idle delay to keep loop responsive but not busy-waiting
    delay(10);
  }
}

void runLoggingMode() {
  // Why we woke up
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.println("\n==============================");
  if (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER) {
    Serial.println("🔋 Woke up from deep sleep (timer).");
  } else {
    Serial.println("⚡ First boot or external reset.");
  }
  Serial.printf("Wake count: %lu\n", (unsigned long)++wakeCount);

  // Prepare today's CSV
  ensureTodayTargets(getDateStr());

  // --- Simulated sensor values ---
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

  // Short settle to ensure FS write completes
  delay(50);

  // Sleep for 60 seconds
  const uint64_t SLEEP_US = 60ULL * 1000000ULL;
  esp_sleep_enable_timer_wakeup(SLEEP_US);
  Serial.println("🌙 Going to deep sleep for 60 seconds...");
  Serial.flush();
  esp_deep_sleep_start();
}

// ---------- Arduino entry points ----------
void setup() {
  pinMode(MODE_BTN_PIN, INPUT_PULLUP);

  Serial.begin(115200);
  delay(100);

  // Mount LittleFS (try without format first; if it fails, attempt format=true)
  if (!LittleFS.begin()) {
    Serial.println("⚠️ LittleFS mount failed. Attempting to format...");
    if (!LittleFS.begin(true)) {
      Serial.println("❌ LittleFS mount/format failed. Limited functionality.");
      // If FS is unavailable, still proceed: retrieval won't work; logging will skip file ops
    }
  }

  // Check mode at boot
  bool retrievalMode = (digitalRead(MODE_BTN_PIN) == LOW);

  if (retrievalMode) {
    runDataRetrievalMode();   // never returns
  } else {
    runLoggingMode();         // deep sleeps at end
  }
}

void loop() {
  // Not used. In retrieval mode, we stay inside runDataRetrievalMode() loop.
  // In logging mode, device never returns here after deep sleep.
}
