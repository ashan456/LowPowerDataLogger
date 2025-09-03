#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>

static String joinPath(const char* base, const char* name) {
  // If child already starts with '/', return as-is
  if (name && name[0] == '/') return String(name);
  String b = base ? String(base) : String("/");
  if (!b.startsWith("/")) b = "/" + b;
  if (!b.endsWith("/")) b += "/";
  return b + String(name ? name : "");
}

void listDir(fs::FS &fs, const char * dirname, uint8_t levels) {
  Serial.printf("Listing directory: %s\n", dirname);

  File root = fs.open(dirname);
  if (!root) { Serial.println("Failed to open directory"); return; }
  if (!root.isDirectory()) { Serial.println("Not a directory"); return; }

  for (File f = root.openNextFile(); f; f = root.openNextFile()) {
    if (f.isDirectory()) {
      Serial.printf("  DIR : %s\n", f.name());
      if (levels > 0) {
        String child = joinPath(dirname, f.name());  // <-- build absolute path
        listDir(fs, child.c_str(), levels - 1);
      }
    } else {
      Serial.printf("  FILE: %s  SIZE: %d\n", f.name(), f.size());
    }
  }
}

void setup() {
  Serial.begin(115200);
  if (!LittleFS.begin()) { Serial.println("LittleFS Mount Failed"); return; }

  listDir(LittleFS, "/logs", 3);  // start from absolute path
}

void loop() {}
