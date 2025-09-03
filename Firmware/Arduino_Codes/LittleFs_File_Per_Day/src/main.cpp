#include <Arduino.h>
#include "FS.h"
#include <LittleFS.h>

// --------- User-provided date/time (replace with your RTC/NTP) ----------
String getDateStr()
{
  // Example: "YYYY-MM-DD"
  return "2025-08-26";
}

String getTimeStr()
{
  // Example: "HH:MM:SS"
  // Ideally replace with RTC/NTP
  return "06:25:42";
}
// -----------------------------------------------------------------------

// Paths updated when the date changes
String currentDate = "";
String currentFolder = "";
String currentCsv = "";

// Ensure base folder exists and today folder + file are ready
bool ensureTodayTargets(const String &dateStr)
{
  LittleFS.mkdir("/logs");

  String dayFolder = "/logs/" + dateStr;
  LittleFS.mkdir(dayFolder);

  String csvPath = dayFolder + "/" + dateStr + ".csv";

  bool needHeader = false;
  if (!LittleFS.exists(csvPath))
  {
    needHeader = true;
  }
  else
  {
    File f = LittleFS.open(csvPath, FILE_READ);
    if (!f || f.size() == 0)
      needHeader = true;
    if (f)
      f.close();
  }

  if (needHeader)
  {
    File f = LittleFS.open(csvPath, FILE_WRITE);
    if (!f)
    {
      Serial.println("Failed to create CSV for header");
      return false;
    }
    f.println("time,temperature,humidity");
    f.close();
  }

  currentDate = dateStr;
  currentFolder = dayFolder;
  currentCsv = csvPath;
  return true;
}

// Append one CSV line
bool appendCsv(const String &csvLine)
{
  File f = LittleFS.open(currentCsv, FILE_APPEND);
  if (!f)
  {
    f = LittleFS.open(currentCsv, FILE_WRITE);
    if (!f)
    {
      Serial.println("Failed to open CSV (append/create)");
      return false;
    }
  }
  f.println(csvLine);
  f.flush();
  f.close();
  return true;
}

// Public logging API
void logReading(float temperature, float humidity)
{
  String today = getDateStr();

  if (today != currentDate)
  {
    if (!ensureTodayTargets(today))
      return;
  }

  String line = getTimeStr() + "," + String(temperature, 2) + "," + String(humidity, 2);
  if (!appendCsv(line))
  {
    Serial.println("Append failed");
  }
}

void setup()
{
  Serial.begin(115200);
  delay(100);

  if (!LittleFS.begin(true))
  {
    Serial.println("LittleFS mount failed");
    return;
  }

  Serial.println("Random Time LOG Enabled");
  ensureTodayTargets(getDateStr());

  randomSeed(analogRead(A0)); // seed RNG
}

void loop()
{
  static uint32_t lastMs = 0;
  if (millis() - lastMs >= 60000)   // every 60s
  {
    lastMs = millis();

    // Generate random dummy values
    float t = random(200, 350) / 10.0;  // 20.0°C – 35.0°C
    float h = random(300, 800) / 10.0;  // 30.0% – 80.0%

    logReading(t, h);

    Serial.print("Logged: ");
    Serial.print(t, 1);
    Serial.print(" °C , ");
    Serial.print(h, 1);
    Serial.println(" %");
  }
}
