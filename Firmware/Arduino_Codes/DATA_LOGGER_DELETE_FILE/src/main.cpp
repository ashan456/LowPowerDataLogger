#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>

static const char *ROOT = "/logs";

void deleteLogDir(const String &dateStr) {
  // File path inside the folder
  String filePath = String(ROOT) + "/" + dateStr + "/" + dateStr + ".csv";
  String dirPath  = String(ROOT) + "/" + dateStr;

  // Step 1: remove file if exists
  if (LittleFS.exists(filePath)) {
    if (LittleFS.remove(filePath)) {
      Serial.println("🗑️ Removed log file: " + filePath);
    } else {
      Serial.println("⚠️ Failed to remove log file: " + filePath);
    }
  } else {
    Serial.println("No log file found for " + dateStr);
  }

  // Step 2: try removing directory
  if (LittleFS.rmdir(dirPath)) {
    Serial.println("🗑️ Removed directory: " + dirPath);
  } else {
    Serial.println("⚠️ Failed to remove directory: " + dirPath + " (not empty?)");
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);

  if (!LittleFS.begin()) {
    Serial.println("❌ LittleFS mount failed!");
    return;
  }

  String date1 = "2025-08-26";   // <-- target date
  deleteLogDir(date1);
  String date2 = "2025-08-27";   // <-- target date
  deleteLogDir(date2);
  String date3 = "2025-08-30";   // <-- target date
  deleteLogDir(date3);
}

void loop() {
  // nothing
}
