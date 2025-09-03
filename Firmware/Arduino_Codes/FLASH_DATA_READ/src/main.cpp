#include <Arduino.h>
#include <LittleFS.h>

static const char *ROOT = "/logs";

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
      // Detect header: if it contains non-digit at start or common header tokens
      // but since user requested to skip header, just skip the first non-empty line
      headerSkipped = true;
      continue; // skip the first non-empty line
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

void setup() {
  Serial.begin(115200);
  delay(500);

  if (!LittleFS.begin()) {
    Serial.println("❌ Failed to mount LittleFS");
    return;
  }

  Serial.println("LittleFS mounted ✅");
  printPrompt();
}

void loop() {
  static String inputDate;

  // Wait for user to type a date then press Enter (set Serial Monitor line ending to NL or NL&CR)
  if (Serial.available()) {
    inputDate = Serial.readStringUntil('\n');
    inputDate.trim();

    if (!isValidDateFormat(inputDate)) {
      Serial.println("⚠️ Invalid format! Please enter YYYY-MM-DD");
      Serial.println();
      printPrompt();
      return;
    }

    String filePath = String(ROOT) + "/" + inputDate + "/" + inputDate + ".csv";
    Serial.println("🔍 Trying to open: " + filePath);

    File file = LittleFS.open(filePath, "r");
    if (!file) {
      Serial.println("❌ File not found.");
      listAvailableDates();                // (1) show available folders
      Serial.println();
      printPrompt();
      return;
    }

    Serial.println("✅ File opened, printing contents (row-numbered; header skipped):");
    Serial.println("----------------------");
    printCsvWithRowNumbers(file, /*skipHeader=*/true);   // (2) skip header, (3) row numbers
    Serial.println("----------------------");
    file.close();

    Serial.println();
    Serial.print("Enter another date (YYYY-MM-DD): ");
  }
}
