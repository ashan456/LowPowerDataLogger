#include <Arduino.h>

#include "FS.h"
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


void setup() {
  Serial.begin(115200);
  if(!LittleFS.begin(true)){
    Serial.println("LittleFS mount failed");
    return;
  }

  // Create a directory
  if(!LittleFS.mkdir("/logs")){
    Serial.println("mkdir failed");
  }

  // Write a file inside that directory
  File f = LittleFS.open("/logs/data.txt", FILE_WRITE);
  if(f){
    f.println("Hello from LittleFS");
    f.close();
  } else {
    Serial.println("File open failed");
  }

  // Read the file
  File fr = LittleFS.open("/logs/data.txt", FILE_READ);
  if(fr){
    while(fr.available()){
      Serial.write(fr.read());
    }
    fr.close();
  }

  // List directory contents
   listDir("/");
  File root = LittleFS.open("/logs");
  File file = root.openNextFile();
  while(file){
    Serial.printf("Found file: %s, size: %d\n", file.name(), file.size());
    file = root.openNextFile();
  }
}

void loop() {}
