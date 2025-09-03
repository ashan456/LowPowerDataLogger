#include <Arduino.h>
#include <LittleFS.h>

void listDir(const char *dirname = "/")
{
    File root = LittleFS.open(dirname);
    if (!root || !root.isDirectory())
    {
        Serial.println("open dir failed");
        return;
    }
    File file = root.openNextFile();
    while (file)
    {
        Serial.printf("%-4s %s  %u bytes\n",
                      file.isDirectory() ? "DIR" : "FILE",
                      file.name(), (unsigned)file.size());
        file = root.openNextFile();
    }
}

void appendLog(const char *path, const String &line)
{
    File f = LittleFS.open(path, "a"); // create if missing
    if (!f)
        f = LittleFS.open(path, "w");
    if (!f)
    {
        Serial.println("open log failed");
        return;
    }
    f.println(line);
    f.close();
}

// --- Read whole file and print raw lines (works for any text file)
void printFile(const char *path)
{
    File f = LittleFS.open(path, "r");
    if (!f)
    {
        Serial.printf("open read failed: %s\n", path);
        return;
    }
    Serial.printf("---- %s (size=%u bytes) ----\n", path, (unsigned)f.size());
    while (f.available())
    {
        String line = f.readStringUntil('\n');
        line.trim(); // remove \r and trailing spaces
        if (line.length())
            Serial.println(line);
    }
    Serial.println("------------------------------");
    f.close();
}

// --- Print only a specific range of rows from a CSV (1-based inclusive)
//     Useful if your log grows large; pass start=1, count=10 for first 10 lines.
void printCsvRows(const char *path, uint32_t start, uint32_t count, char delimiter = ',')
{
    if (count == 0)
        return;
    File f = LittleFS.open(path, "r");
    if (!f)
    {
        Serial.printf("open read failed: %s\n", path);
        return;
    }
    uint32_t lineNo = 0, printed = 0;
    while (f.available() && printed < count)
    {
        String line = f.readStringUntil('\n');
        line.trim();
        if (!line.length())
            continue;
        ++lineNo;
        if (lineNo < start)
            continue;

        // Raw print; swap to pretty split if you like:
        Serial.printf("%lu: %s\n", (unsigned long)lineNo, line.c_str());
        ++printed;
    }
    f.close();
}

// --- Print the last N lines (tail) without loading the whole file into RAM
//     Uses a small ring buffer of String objects.
void tailFile(const char *path, uint16_t lastN = 20)
{
    File f = LittleFS.open(path, "r");
    if (!f)
    {
        Serial.printf("open read failed: %s\n", path);
        return;
    }
    if (lastN == 0)
    {
        f.close();
        return;
    }
    String *buf = new String[lastN];
    uint16_t idx = 0, filled = 0;
    while (f.available())
    {
        String line = f.readStringUntil('\n');
        line.trim();
        if (!line.length())
            continue;
        buf[idx] = line;
        idx = (idx + 1) % lastN;
        if (filled < lastN)
            filled++;
    }
    f.close();

    Serial.printf("---- Last %u lines of %s ----\n", filled, path);
    for (uint16_t i = 0; i < filled; ++i)
    {
        uint16_t j = (idx + i) % lastN;
        Serial.println(buf[j]);
    }
    Serial.println("--------------------------------");
    delete[] buf;
}

// Split a CSV line into up to maxCols fields without STL/Vector.
// Returns number of columns parsed.
int splitCsvLine(const String &line, String cols[], int maxCols, char delimiter = ',')
{
    int n = 0;
    int start = 0;
    int len = line.length();
    for (int i = 0; i <= len; ++i)
    {
        if (i == len || line.charAt(i) == delimiter)
        {
            if (n < maxCols)
                cols[n++] = line.substring(start, i);
            start = i + 1;
        }
    }
    // Trim whitespace around each field
    for (int i = 0; i < n; ++i)
        cols[i].trim();
    return n;
}

void printCsvPretty(const char *path, char delimiter = ',')
{
    File f = LittleFS.open(path, "r");
    if (!f)
    {
        Serial.printf("open read failed: %s\n", path);
        return;
    }
    Serial.printf("---- CSV: %s ----\n", path);

    const int MAXC = 12; // adjust if your CSV has more columns
    String cols[MAXC];
    size_t row = 0;

    while (f.available())
    {
        String line = f.readStringUntil('\n');
        line.trim();
        if (!line.length())
            continue;

        int n = splitCsvLine(line, cols, MAXC, delimiter);

        Serial.printf("Row %u: ", (unsigned)row++);
        for (int i = 0; i < n; ++i)
        {
            Serial.printf("[%d]=%s  ", i, cols[i].c_str());
        }
        Serial.println();
    }
    Serial.println("------------------");
    f.close();
}

void setup()
{
    Serial.begin(115200);
    while (!Serial)
    {
    }

    // Mount + format on failure; uses default FS partition (label is typically "spiffs")
    if (!LittleFS.begin(true))
    {
        Serial.println("LittleFS mount failed even after format");
        while (1)
            delay(1000);
    }
    Serial.println("Time LOG Enabled");
    appendLog("/log.csv", String(millis()) + ",23.7,55.2");
    delay(100);
    // Optional: show size
    Serial.printf("FS total=%u, used=%u\n", (unsigned)LittleFS.totalBytes(), (unsigned)LittleFS.usedBytes());
    delay(200);
    listDir("/");
    /* // Use paths RELATIVE to the base (do NOT prefix with /littlefs again)
      File f = LittleFS.open("/test.txt", "w");
      if (!f) { Serial.println("open write failed"); return; }
      f.println("Hello from H2 via LittleFS");
      f.close();

      f = LittleFS.open("/test.txt", "r");
      if (!f) { Serial.println("open read failed"); return; }
      Serial.println(f.readString());
      f.close();*/
    // Read & print the whole CSV as raw lines:
   // printFile("/log.csv");
    // Or, pretty-print columns:
    //printCsvPretty("/log.csv");
    //printCsvRows("/log.csv", /*start=*/5, /*count=*/8);

    // Print the last 10 lines:
    //tailFile("/log.csv", 4);
}

void loop() {}
