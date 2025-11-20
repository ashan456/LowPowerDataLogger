#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <RV-3028-C7.h>
#include "esp_sleep.h"
#include <Preferences.h>   // <<< NEW (NVS)
#include "esp_partition.h" // <-- for partition table access
#include <SPI.h>
#include <SPIMemory.h>

// Added
//New RV3028-C7 RTC Added
// View Logged data  from Flash memeory. (Show information Size,Raw Count)
// Delete a file and folder from the storage.
// Restart Logger via USb Serial commands.
// Format Storage. (Password Protected)
// View Flash Details. (FREE AVILABLE , Partitoon table details)
// RTC_Fail_Safe Handled Corrctly (Removed)
// Hartbeat LEd function and config UART commands added
//Sensor Calibration added

// ===================== CONFIG =====================
#define SDA_PIN 4
#define SCL_PIN 5
#define LED_PIN 3
#define MODE_BTN_PIN 14          // Hold LOW at boot for Dump/Data Retrieval mode
#define PIN_SCK   11
#define PIN_MOSI  22   // DI
#define PIN_MISO  12   // DO
#define PIN_CS    10   // CS#


#define DEFAULT_SLEEP_SECONDS 30 // default 7.5min if no NVS value
static const char *ROOT = "/logs";

// ---- Battery sense (SET THESE) ----
#define ADC_SW_PIN       2       // MOSFET gate (Active HIGH)
#define USB_DETECT_PIN   13      // LOW = USB connected
#define I2C_ADDR_ADC     0x4D

// --- Reference voltages ---
#define VREF_USB         3.298f
#define VREF_BAT         3.294f

#define ADC_STABILIZE_MS 10     

// --- Divider & calibration ---
#define DIV_GAIN_INV     1.3599f          // your refined divider ratio
#define CAL_SLOPE        1.0164083f       // from DMM regression
#define CAL_OFFSET_V    -0.0650094f


// ==================================================

// --- Devices ---
Adafruit_SHT4x sht4;
RV3028 rtc;
bool rtc_ok = false;

// --- NVS (Preferences) ---
Preferences prefs;
static const char *NVS_NS = "logger";
static const char *NVS_KEY_INTERVAL = "int_s";

// --- NVS key for format code hash ---
static const char *NVS_KEY_FMT_HASH = "fmt_hash";

static const char *NVS_KEY_LAST_EPOCH = "last_ep"; // uint32_t (UNIX epoch seconds)

// Heartbeat (status LED) control
static const char *NVS_KEY_HB_ON = "hb_on";
static const char *NVS_KEY_HB_PER = "hb_per";
#define DEFAULT_HB_PERIOD 30 // seconds (blink twice each period)

// Storage mode selection
static const char *NVS_KEY_STORAGE_MODE = "stor_mode";
static const char *NVS_KEY_AUTO_THRESHOLD = "auto_thr";
static const char *NVS_KEY_AUTO_SWITCHED = "auto_sw";
enum StorageMode : uint8_t {
  STORAGE_INTERNAL = 0,
  STORAGE_EXTERNAL = 1,
  STORAGE_AUTO = 2
};
StorageMode storageMode = STORAGE_INTERNAL; // runtime variable
uint8_t autoThreshold = 90; // percentage (5-95), default 90%
bool autoSwitched = false; // true if auto-switched to external

// --- Compile-time default (change this!) ---
// If no code is set in NVS, this fallback is used.
#ifndef DEFAULT_FORMAT_CODE
#define DEFAULT_FORMAT_CODE "246810" // <<< CHANGE THIS PER UNIT
#endif

// =========================================================
// Lightweight DateTime struct for RV3028
// =========================================================
// struct RTCDateTime
// {
//   uint16_t year;
//   uint8_t month;
//   uint8_t day;
//   uint8_t hour;
//   uint8_t minute;
//   uint8_t second;
// };

// --- RTC-deep sleep boot counter (in RTC RAM) ---
RTC_DATA_ATTR uint32_t wakeCount = 0;

// --- Current targets for logger mode ---
String currentDate, currentFolder, currentCsv;

// --- Active logging interval (loaded from NVS) ---
uint32_t sleepSeconds = DEFAULT_SLEEP_SECONDS;

// Heartbeat runtime state
bool heartbeatOn = false;
uint32_t heartbeatPeriod = DEFAULT_HB_PERIOD;

// Track scheduling across deep sleep
RTC_DATA_ATTR uint32_t hbSecondsUntilNextLog = 0; // counts down to next *log* wake
RTC_DATA_ATTR uint32_t hbLastSleepPlanned = 0;    // how long we planned to sleep last time
RTC_DATA_ATTR uint8_t hbInitFlag = 0;             // first-run init guard

// ======== NEW BIN RECORD (7 bytes) ========
// Order (little-endian):
//  [0..2] uint24_le  : seconds since midnight (0..86399)
//  [3..4] int16_le   : temperature in deci-°C (T_C * 10, two's complement)
//  [5]    uint8_t    : relative humidity (0..100)
//  [6]    uint8_t    : vbat mapped 2.50–4.50 V -> 0..255
static const size_t RECORD_SIZE = 7;




// --- add this line so Arduino's auto prototypes see the type ---
struct SuperBlock;

SPIFlash flash(PIN_CS);

// ------------------ LAYOUT ---------------------
static const uint32_t SECTOR_SIZE  = 4096;
static const uint32_t EXT_RECORD_SIZE  = 8;
static const uint32_t DATA_BEGIN   = 2 * SECTOR_SIZE;  // reserve sectors 0-1 for superblock
static uint32_t FLASH_BYTES = 0;
static uint32_t DATA_END    = 0;

static uint32_t g_tail = 0;    // where next record will be written

// ------------------ SUPERBLOCK (persist tail) ------------------
static const uint32_t SUPER_A_ADDR = 0x0000;  // sector 0
static const uint32_t SUPER_B_ADDR = 0x1000;  // sector 1
static const uint16_t SUPER_VERSION = 0x0001;
static const uint32_t SAVE_EVERY = 128;  // save tail every N appends
static uint32_t g_seq = 0;
static uint32_t g_append_since_save = 0;


// ------------------ STRUCTS ------------------
struct SuperBlock {
  uint32_t magic;     // 'RLOG' = 0x474F4C52 (LE)
  uint16_t version;   // 0x0001
  uint16_t reserved;
  uint32_t seq;       // increments on every save
  uint32_t tail;      // persisted g_tail
  uint32_t crc32;     // CRC over magic..tail (exclude this field)
} __attribute__((packed));

// ------------------ FWD DECLS ------------------
static uint32_t parseU32(const String& s);
static void handleCommand(const String& line);
static bool formatDataRegion();
static bool appendOne(uint32_t unixTime, int16_t temp10, uint8_t rh, uint16_t vbat_mV);
static void dumpAll();
static void findTailLinear();
static uint32_t resyncTailFrom(uint32_t addr);
static bool saveSuperTail();
static void cmdDumpRaw(const String& rest);
static void cmdDumpSector(const String& rest);
static void cmdDumpRawSector(const String& rest);
static void cmdListAvailableSectors(const String& rest = "");



//-------------Parse Helper------------------------------
static uint32_t parseU32(const String& s) {
  const char* c = s.c_str();
  return (strncmp(c, "0x", 2) == 0 || strncmp(c, "0X", 2) == 0) ? strtoul(c + 2, nullptr, 16)
                                                                : strtoul(c, nullptr, 10);
}

// ------------------ CRC32 ------------------
static uint32_t crc32_update(uint32_t crc, uint8_t data) {
  crc ^= data;
  for (int i = 0; i < 8; ++i) {
    uint32_t mask = -(crc & 1u);
    crc = (crc >> 1) ^ (0xEDB88320u & mask);
  }
  return crc;
}
static uint32_t crc32_calc(const uint8_t* p, size_t n) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < n; ++i) crc = crc32_update(crc, p[i]);
  return ~crc;
}

// ------------------ SUPERBLOCK HELPERS ------------------
static bool readSuper(const uint32_t base, SuperBlock& sb, bool& valid) {
  valid = false;
  if (!flash.readByteArray(base, (uint8_t*)&sb, sizeof(SuperBlock))) return false;
  if (sb.magic != 0x474F4C52UL || sb.version != SUPER_VERSION) return true; // read ok, but not valid
  uint32_t calc = crc32_calc((uint8_t*)&sb, offsetof(SuperBlock, crc32));
  if (calc != sb.crc32) return true; // read ok, crc mismatch
  valid = true;
  return true;
}
static bool eraseSectorIfNeeded(uint32_t addr) {
  uint8_t b;
  if (flash.readByte(addr, &b) && b == 0xFF) return true;
  if (!flash.eraseSector(addr)) return false;
  delay(2);
  return true;
}
static bool writeSuperTo(uint32_t base, uint32_t seq, uint32_t tail) {
  SuperBlock sb;
  sb.magic = 0x474F4C52UL;
  sb.version = SUPER_VERSION;
  sb.reserved = 0;
  sb.seq = seq;
  sb.tail = tail;
  sb.crc32 = crc32_calc((uint8_t*)&sb, offsetof(SuperBlock, crc32));

  if (!eraseSectorIfNeeded(base)) return false;
  return flash.writeByteArray(base, (uint8_t*)&sb, sizeof(SuperBlock));
}


static bool loadSuperAndSetTail() {
  SuperBlock sa, sb;
  bool va = false, vb = false;
  if (!readSuper(SUPER_A_ADDR, sa, va)) return false;
  if (!readSuper(SUPER_B_ADDR, sb, vb)) return false;
  if (!va && !vb) return false;

  const SuperBlock& best = (!vb || (va && sa.seq >= sb.seq)) ? sa : sb;
  g_tail = best.tail;
  g_seq  = best.seq;
  return true;
}
static bool saveSuperTail() {
  uint32_t nextSeq = g_seq + 1;
  bool toB = (g_seq % 2 == 0);
  uint32_t dst = toB ? SUPER_B_ADDR : SUPER_A_ADDR;
  uint32_t old = toB ? SUPER_A_ADDR : SUPER_B_ADDR;

  if (!writeSuperTo(dst, nextSeq, g_tail)) return false;
  eraseSectorIfNeeded(old);
  g_seq = nextSeq;
  g_append_since_save = 0;
  return true;
}

// ------------------ VBAT QUANTIZE ------------------
static inline uint8_t quantizeVBAT7(uint16_t mv) {
  float v = mv / 1000.0f;
  if (v < 2.0f) v = 2.0f;
  if (v > 4.5f) v = 4.5f;
  float t = (v - 2.0f) / (4.5f - 2.0f);
  int q = (int)roundf(t * 127.0f);
  if (q < 0) q = 0;
  if (q > 127) q = 127;
  return (uint8_t)q;
}
static inline uint16_t dequantizeVBATmV(uint8_t v7) {
  if (v7 > 127) v7 = 127;
  float v = 2.0f + (v7 / 127.0f) * (4.5f - 2.0f);
  return (uint16_t)roundf(v * 1000.0f);
}
static inline bool isValidRecord(uint8_t vbatQ) {
  return ((vbatQ & 0x80) == 0);
}
static void buildFirst7(uint8_t out7[7], uint32_t unixTime, int16_t temp10, uint8_t rh, uint8_t vbat7) {
  out7[0] = (uint8_t)(unixTime & 0xFF);
  out7[1] = (uint8_t)((unixTime >> 8) & 0xFF);
  out7[2] = (uint8_t)((unixTime >> 16) & 0xFF);
  out7[3] = (uint8_t)((unixTime >> 24) & 0xFF);
  out7[4] = (uint8_t)(temp10 & 0xFF);
  out7[5] = (uint8_t)((temp10 >> 8) & 0xFF);
  out7[6] = rh;
  (void)vbat7;
}

// ------------------ SCANNERS ------------------
static bool slotAllFF(const uint8_t* b) {
  for (int i = 0; i < 8; ++i) if (b[i] != 0xFF) return false;
  return true;
}
static bool slotAll00(const uint8_t* b) {
  for (int i = 0; i < 8; ++i) if (b[i] != 0x00) return false;
  return true;
}
static uint32_t resyncTailFrom(uint32_t addr) {
  if (addr < DATA_BEGIN) addr = DATA_BEGIN;
  if (addr > DATA_END)   addr = DATA_END;

  uint32_t p = addr;
  uint8_t buf[8];

  while ((p + EXT_RECORD_SIZE) <= DATA_END) {
    if (!flash.readByteArray(p, buf, 8)) {
      Serial.println(F("[RESYNC] Read error, stopping."));
      break;
    }
    bool allFF = slotAllFF(buf);
    bool all00 = slotAll00(buf);
    if (allFF || all00) break;

    uint8_t vbatQ = buf[7];
    if ((vbatQ & 0x80) != 0) break; // first uncommitted
    p += EXT_RECORD_SIZE;
  }
  return p;
}
static void findTailLinear() {
  g_tail = DATA_BEGIN;
  uint8_t buf[8];
  while ((g_tail + EXT_RECORD_SIZE) <= DATA_END) {
    if (!flash.readByteArray(g_tail, buf, 8)) {
      Serial.println(F("[SCAN] Read error -> stopping."));
      break;
    }
    bool allFF = slotAllFF(buf);
    bool all00 = slotAll00(buf);
    if (allFF || all00) break;

    if ((buf[7] & 0x80) != 0) break; // uncommitted
    g_tail += EXT_RECORD_SIZE;
  }
  Serial.print(F("[SCAN] Tail at 0x")); Serial.println(g_tail, HEX);
}

// ------------------ CORE OPS ------------------
static bool formatDataRegion() {
  Serial.println(F("[FORMAT] Chip erase…"));
  if (!flash.eraseChip()) {
    Serial.println(F("[FORMAT] eraseChip failed."));
    return false;
  }
  delay(10);
  Serial.println(F("[FORMAT] Done."));
  g_tail = DATA_BEGIN;
  g_seq = 0;
  if (!saveSuperTail()) {
    Serial.println(F("[FORMAT] saveSuperTail failed (non-fatal)."));
  }
  return true;
}


static bool findPrevValidRecord(uint32_t &addr_out, uint32_t &unix_out) {
  if (g_tail <= DATA_BEGIN) return false;
  uint32_t addr = g_tail - EXT_RECORD_SIZE;
  uint8_t buf[8];

  while (addr >= DATA_BEGIN) {
    if (!flash.readByteArray(addr, buf, 8)) break;
    if (slotAllFF(buf) || slotAll00(buf)) {
      if (addr < EXT_RECORD_SIZE) break;
      addr -= EXT_RECORD_SIZE;
      continue;
    }
    if ((buf[7] & 0x80) == 0) {  // committed record
      addr_out = addr;
      unix_out = ((uint32_t)buf[0]) |
                 ((uint32_t)buf[1] << 8) |
                 ((uint32_t)buf[2] << 16) |
                 ((uint32_t)buf[3] << 24);
      return true;
    }
    if (addr < EXT_RECORD_SIZE) break;
    addr -= EXT_RECORD_SIZE;
  }
  return false;
}




static bool appendOne(uint32_t unixTime, int16_t temp10, uint8_t rh, uint16_t vbat_mV) {
  if ((g_tail + EXT_RECORD_SIZE) > DATA_END) {
    Serial.println(F("[APPEND] No space left."));
    return false;
  }

  // ---- RTC FAIL-SAFE ----
  // If unixTime == (uint32_t)-1, auto-generate using previous record + LOG_INTERVAL_MS
//   if (unixTime == (uint32_t)-1) {
//     uint32_t prevAddr = 0, prevUnix = 0;
//     if (!findPrevValidRecord(prevAddr, prevUnix)) {
//       Serial.println(F("[APPEND] Auto-time failed: no previous record found."));
//       return false; // No previous record → can't estimate
//     }
//     uint32_t stepSec = LOG_INTERVAL_MS / 1000UL; // convert ms → seconds
//     if (stepSec == 0) stepSec = 1;               // safety fallback
//     unixTime = prevUnix + stepSec;
// #ifdef VERBOSE_ON
//     Serial.print(F("[APPEND] RTC fail-safe time used: "));
//     Serial.println(unixTime);
// #endif
//   }

  // ---- Check if current slot is free ----
  uint8_t probe[8];
  if (!flash.readByteArray(g_tail, probe, 8)) {
    Serial.println(F("[APPEND] probe read failed."));
    return false;
  }
  bool allFF = slotAllFF(probe);
  bool all00 = slotAll00(probe);
  bool freeSlot = allFF || all00 || ((probe[7] & 0x80) != 0);
  if (!freeSlot) {
    uint32_t newTail = resyncTailFrom(g_tail);
    if (newTail == g_tail) {
      Serial.println(F("[APPEND] No free slot at/after tail."));
      return false;
    }
    g_tail = newTail;
  }

  // ---- Write record ----
  uint8_t vbat7 = quantizeVBAT7(vbat_mV);
  uint8_t first7[7];
  buildFirst7(first7, unixTime, temp10, rh, vbat7);

  if (!flash.writeByteArray(g_tail, first7, 7)) {
    Serial.println(F("[APPEND] write first7 failed."));
    return false;
  }

  delay(1); // small delay before commit
  uint8_t commit = (uint8_t)(vbat7 & 0x7F); // MSB cleared = committed
  if (!flash.writeByteArray(g_tail + 7, &commit, 1)) {
    Serial.println(F("[APPEND] commit write failed."));
    return false;
  }

  g_tail += EXT_RECORD_SIZE;

  // ---- Superblock periodic update ----
  g_append_since_save++;
  if (g_append_since_save >= SAVE_EVERY) {
    if (!saveSuperTail()) {
      Serial.println(F("[APPEND] saveSuperTail failed (non-fatal)."));
    } else {
      Serial.print(F("[APPEND] Superblock updated, seq="));
      Serial.println(g_seq);
    }
  }
  return true;
}






static void dumpAll() {
  Serial.println(F("unix,temp_c,rh,vbat_mV"));
  uint32_t addr = DATA_BEGIN;
  uint8_t buf[8];

  while ((addr + EXT_RECORD_SIZE) <= DATA_END) {
    if (!flash.readByteArray(addr, buf, 8)) {
      Serial.println(F("[DUMP] Read error, stopping."));
      break;
    }
    bool allFF = slotAllFF(buf);
    if (allFF) break;

    uint8_t vbatQ = buf[7];
    if ((vbatQ & 0x80) != 0) break; // uncommitted

    uint32_t unixTime = (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) |
                        ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
    int16_t  temp10   = (int16_t)((uint16_t)buf[4] | ((uint16_t)buf[5] << 8));
    uint8_t  rh       = buf[6];
    uint8_t  vbat7    = (uint8_t)(vbatQ & 0x7F);

    float tempC = temp10 / 10.0f;
    uint16_t vbat_mV = dequantizeVBATmV(vbat7);

    Serial.print(unixTime); Serial.print(',');
    Serial.print(tempC, 1); Serial.print(',');
    Serial.print((int)rh);  Serial.print(',');
    Serial.println(vbat_mV);

    addr += EXT_RECORD_SIZE;
  }
}

// ------------------ SECTOR TOOLS / CMDS ------------------
static int summarizeSector(uint32_t sectorStart,
                           uint32_t& committedCount,
                           uint32_t& firstRecAddr,
                           uint32_t& lastCommittedAddr,
                           uint32_t& firstUncommittedAddr) {
  committedCount = 0;
  firstRecAddr = 0;
  lastCommittedAddr = 0;
  firstUncommittedAddr = 0;

  const uint32_t slotsPerSector = SECTOR_SIZE / EXT_RECORD_SIZE;
  bool sawData = false, sawErasedAfterData = false, sawUncommitted = false, mixed = false;

  uint32_t addr = sectorStart;
  uint8_t buf[8];

  for (uint32_t i = 0; i < slotsPerSector; ++i, addr += EXT_RECORD_SIZE) {
    if (!flash.readByteArray(addr, buf, 8)) { mixed = true; break; }

    const bool isFF = slotAllFF(buf);
    const bool is00 = slotAll00(buf);
    const bool empty = isFF || is00;

    if (empty) {
      if (sawData) sawErasedAfterData = true;
      continue;
    }

    const uint8_t vbatQ = buf[7];
    const bool committed = ((vbatQ & 0x80) == 0);
    if (!sawData) {
      sawData = true; firstRecAddr = addr;
    } else if (sawErasedAfterData) mixed = true;

    if (!committed) {
      if (!firstUncommittedAddr) firstUncommittedAddr = addr;
      sawUncommitted = true;
    } else {
      committedCount++;
      lastCommittedAddr = addr;
      if (sawUncommitted) mixed = true;
    }
  }

  if (!sawData) return 0;  // EMPTY
  if (mixed)     return 3; // MIXED
  if (committedCount == (SECTOR_SIZE / EXT_RECORD_SIZE) && !sawUncommitted) return 2; // FULL
  return 1; // PARTIAL
}

static void cmdListAvailableSectors(const String& rest) {
  const uint32_t firstDataSector = DATA_BEGIN / SECTOR_SIZE;
  const uint32_t lastDataSector  = (DATA_END - 1) / SECTOR_SIZE;

  uint32_t startSector = firstDataSector;
  if (rest.length()) {
    String s = rest; s.trim();
    if (s.length()) {
      startSector = parseU32(s);
      if (startSector < firstDataSector) startSector = firstDataSector;
      if (startSector > lastDataSector)  startSector = lastDataSector;
    }
  }

  Serial.println(F("SECTOR\tSTART\t\tEND\t\tSTATE\t\tCOMMIT#\tFIRST\t\tLAST\t\tUNCOMMIT@\tUTIL"));
  for (uint32_t sec = startSector; sec <= lastDataSector; ++sec) {
    const uint32_t start = sec * SECTOR_SIZE;
    const uint32_t end   = start + SECTOR_SIZE - 1;
    uint32_t committed = 0, firstA = 0, lastC = 0, firstU = 0;
    const int state = summarizeSector(start, committed, firstA, lastC, firstU);

    const char* stateStr = (state == 0) ? "EMPTY" :
                           (state == 1) ? "PARTIAL" :
                           (state == 2) ? "FULLY_COMMITTED" : "MIXED";
    const uint32_t slotsPerSector = SECTOR_SIZE / EXT_RECORD_SIZE;
    const uint32_t utilPct = (committed * 100U) / slotsPerSector;

    Serial.print(sec); Serial.print('\t');
    Serial.print(F("0x")); Serial.print(start, HEX); Serial.print('\t');
    Serial.print(F("0x")); Serial.print(end,   HEX); Serial.print('\t');
    Serial.print(stateStr); Serial.print('\t'); Serial.print('\t');
    Serial.print(committed); Serial.print('\t');
    if (firstA) { Serial.print(F("0x")); Serial.print(firstA, HEX); } else Serial.print('-');
    Serial.print('\t');
    if (lastC)  { Serial.print(F("0x")); Serial.print(lastC, HEX); }  else Serial.print('-');
    Serial.print('\t');
    if (firstU) { Serial.print(F("0x")); Serial.print(firstU, HEX); } else Serial.print('-');
    Serial.print('\t');
    Serial.print(utilPct); Serial.println('%');

    if (state == 0) {
      Serial.println(F("[LIST_AVAILABLE_SECTORS] First EMPTY sector reached. Stopping."));
      break;
    }
  }
  Serial.println(F("OK,DONE"));
}

static void cmdDumpRawSector(const String& rest) {
  String s = rest; s.trim();
  if (!s.length()) { Serial.println(F("ERR,DUMPRAW_SECTOR,Usage: DUMPRAW_SECTOR <sectorIdx> [rows]")); return; }
  int sp = s.indexOf(' ');
  String sSector = (sp < 0) ? s : s.substring(0, sp);
  String sRows   = (sp < 0) ? "" : s.substring(sp + 1);

  uint32_t sectorIdx = parseU32(sSector);
  bool rowsProvided = sRows.length() > 0;
  uint32_t maxRows = 0xFFFFFFFFUL;
  if (rowsProvided) {
    long n = sRows.toInt();
    if (n > 0) maxRows = (uint32_t)n;
  }

  const uint32_t firstDataSector = DATA_BEGIN / SECTOR_SIZE;
  const uint32_t lastDataSector  = (DATA_END - 1) / SECTOR_SIZE;
  if (sectorIdx < firstDataSector || sectorIdx > lastDataSector) {
    Serial.println(F("ERR,DUMPRAW_SECTOR,OutOfRange")); return;
  }

  const uint32_t base = sectorIdx * SECTOR_SIZE;
  const uint32_t end  = base + SECTOR_SIZE;
  const uint32_t slotsPerSector = SECTOR_SIZE / EXT_RECORD_SIZE;

  Serial.print(F("[DUMPRAW_SECTOR] Sector ")); Serial.print(sectorIdx);
  Serial.print(F("  range 0x")); Serial.print(base, HEX); Serial.print(F("..0x"));
  Serial.println(end - 1, HEX);

  uint32_t committed = 0, uncommitted = 0, erased = 0, printed = 0;
  uint8_t buf[8];
  uint32_t addr = base;

  for (uint32_t i = 0; i < slotsPerSector; ++i, addr += EXT_RECORD_SIZE) {
    if (printed >= maxRows) { Serial.println(F("[DUMPRAW_SECTOR] Row limit reached.")); break; }

    if (!flash.readByteArray(addr, buf, 8)) {
      Serial.print('#'); Serial.print(i); Serial.print(' ');
      Serial.print(F("0x")); Serial.print(addr, HEX);
      Serial.println(F(": ?? ?? ?? ?? ?? ?? ?? ?? | READ_ERR"));
      printed++;
      if (!rowsProvided) break;
      continue;
    }

    bool isFF = slotAllFF(buf);
    bool is00 = slotAll00(buf);
    bool empty = isFF || is00;

    Serial.print('#'); Serial.print(i); Serial.print(' ');
    Serial.print(F("0x")); Serial.print(addr, HEX); Serial.print(F(": "));
    for (int j=0;j<8;++j){ if (buf[j] < 16) Serial.print('0'); Serial.print(buf[j], HEX); Serial.print(' '); }

    if (empty) {
      Serial.println(F("| ERASED"));
      erased++; printed++;
      if (!rowsProvided) break;
      continue;
    }

    bool committedSlot = ((buf[7] & 0x80) == 0);
    if (committedSlot) { Serial.println(F("| COMMITTED")); committed++; }
    else { Serial.println(F("| UNCOMMITTED")); uncommitted++; }
    printed++;
  }

  Serial.print(F("[SUMMARY] committed="));   Serial.print(committed);
  Serial.print(F(" uncommitted="));          Serial.print(uncommitted);
  Serial.print(F(" erased="));               Serial.print(erased);
  Serial.print(F(" of "));                   Serial.println(slotsPerSector);
  Serial.println(F("OK,DONE"));
}

static void cmdDumpSector(const String& rest) {
  String s = rest; s.trim();
  if (!s.length()) { Serial.println(F("ERR,DUMP_SECTOR,Usage: DUMP_SECTOR <sectorIdx> [count]")); return; }
  int sp = s.indexOf(' ');
  String sSector = (sp < 0) ? s : s.substring(0, sp);
  String sCount  = (sp < 0) ? "" : s.substring(sp + 1);

  uint32_t sectorIdx = parseU32(sSector);
  bool wantCount = sCount.length() > 0;
  uint32_t maxRows = 0xFFFFFFFFUL;
  if (wantCount) {
    long n = sCount.toInt();
    if (n > 0) maxRows = (uint32_t)n;
  }

  const uint32_t firstDataSector = DATA_BEGIN / SECTOR_SIZE;
  const uint32_t lastDataSector  = (DATA_END - 1) / SECTOR_SIZE;
  if (sectorIdx < firstDataSector || sectorIdx > lastDataSector) {
    Serial.println(F("ERR,DUMP_SECTOR,OutOfRange")); return;
  }

  const uint32_t base = sectorIdx * SECTOR_SIZE;
  const uint32_t end  = base + SECTOR_SIZE;
  const uint32_t slotsPerSector = SECTOR_SIZE / EXT_RECORD_SIZE;

  Serial.print(F("[DUMP_SECTOR] Sector ")); Serial.print(sectorIdx);
  Serial.print(F("  range 0x")); Serial.print(base, HEX);
  Serial.print(F("..0x")); Serial.println(end - 1, HEX);
  Serial.println(F("unix,temp_c,rh,vbat_mV"));

  uint8_t buf[8];
  uint32_t addr = base, printed = 0;

  for (uint32_t i = 0; i < slotsPerSector; ++i, addr += EXT_RECORD_SIZE) {
    if (printed >= maxRows) break;
    if (!flash.readByteArray(addr, buf, 8)) { Serial.println(F("[DUMP_SECTOR] Read error → stopping.")); break; }

    bool allFF = slotAllFF(buf), all00 = slotAll00(buf);
    bool empty = allFF || all00;
    bool committed = ((buf[7] & 0x80) == 0);

    if (!wantCount && (empty || !committed)) break;
    if (empty || !committed) continue;

    uint32_t unixTime = (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) |
                        ((uint32_t)buf[2] << 16) | ((uint32_t)buf[3] << 24);
    int16_t  temp10   = (int16_t)((uint16_t)buf[4] | ((uint16_t)buf[5] << 8));
    uint8_t  rh       = buf[6];
    uint8_t  vbat7    = (uint8_t)(buf[7] & 0x7F);

    float tempC = temp10 / 10.0f;
    uint16_t vbat_mV = dequantizeVBATmV(vbat7);

    Serial.print(unixTime); Serial.print(',');
    Serial.print(tempC, 1); Serial.print(',');
    Serial.print((int)rh);  Serial.print(',');
    Serial.println(vbat_mV);

    printed++;
  }

  Serial.print(F("[DUMP_SECTOR] rows=")); Serial.println(printed);
  Serial.println(F("OK,DONE"));
}



static void cmdDumpRaw(const String& rest) {
  String s = rest; s.trim();
  uint32_t startAddr = DATA_BEGIN;
  uint32_t maxRows = 0xFFFFFFFFUL;

  if (s.length()) {
    int sp = s.indexOf(' ');
    if (sp < 0) {
      startAddr = parseU32(s);
    } else {
      String a0 = s.substring(0, sp); a0.trim();
      String a1 = s.substring(sp + 1); a1.trim();
      if (a0.length()) startAddr = parseU32(a0);
      if (a1.length()) {
        long n = a1.toInt();
        if (n > 0) maxRows = (uint32_t)n;
      }
    }
  }

  if (startAddr < DATA_BEGIN) startAddr = DATA_BEGIN;
  if (startAddr > DATA_END)   startAddr = DATA_END;
  if (startAddr % EXT_RECORD_SIZE) startAddr -= (startAddr % EXT_RECORD_SIZE);

  uint32_t addr = startAddr, recIdx = 0, rowsOut = 0;
  uint8_t buf[8];

  Serial.print(F("[DUMP_RAW] From 0x")); Serial.println(addr, HEX);

  while ((addr + EXT_RECORD_SIZE) <= DATA_END && rowsOut < maxRows) {
    if (!flash.readByteArray(addr, buf, 8)) { 
      Serial.println(F("[DUMP_RAW] Read error, stopping.")); 
      break; 
    }

    bool allFF = slotAllFF(buf);
    if (allFF) { 
      Serial.println(F("[DUMP_RAW] Hit erased region (all 0xFF).")); 
      break; 
    }

    // Print only hex bytes, no decoding
    Serial.print('#'); Serial.print(recIdx++); Serial.print(' ');
    Serial.print(F("0x")); Serial.print(addr, HEX); Serial.print(F(": "));
    for (int i=0;i<8;++i){ 
      if (buf[i] < 16) Serial.print('0'); 
      Serial.print(buf[i], HEX); 
      Serial.print(' '); 
    }
    Serial.println();

    rowsOut++;
    addr += EXT_RECORD_SIZE;
  }
  
  if (rowsOut >= maxRows) {
    Serial.println(F("[DUMP_RAW] Row limit reached."));
  }

  Serial.print(F("[DUMP_RAW] Lines: ")); Serial.println(rowsOut);
  Serial.println(F("OK,DONE"));
}



//---------------------Read Sensors -Helpers------------------------------------------

 // -------------------Read Temp Sensor------------------------

/* bool readSHT(float &tC, float &rh)
{
  sensors_event_t humidity, temp;
  if (!sht4.getEvent(&humidity, &temp))
    return false;
  tC = temp.temperature;
  rh = humidity.relative_humidity;
  return true;
} */
 
// -------------------Read Temp Sensor (Calibrated)------------------------
 bool readSHT(float &tC, float &rh)
{
  sensors_event_t humidity, temp;
  if (!sht4.getEvent(&humidity, &temp))
    return false;

  // --- Raw readings ---
  float T_raw  = temp.temperature;          // °C
  float RH_raw = humidity.relative_humidity; // %RH

  // --- Temperature calibration (linear best-fit from IMC certificate) ---
  // T_corr = -0.5073 + 1.003024 * T_raw
  float T_corr = -0.5073f + 1.003024f * T_raw;

  // --- Humidity calibration (linear fit from IMC certificate, clamp 0–100%) ---
  // RH_corr = 9.6354 + 0.85256 * RH_raw
  float RH_corr = 9.6354f + 0.85256f * RH_raw;
  if (RH_corr < 0) RH_corr = 0;
  else if (RH_corr > 100) RH_corr = 100;

  // --- Output calibrated values ---
  tC = T_corr;
  rh = RH_corr;

  return true;
}
 


// ------------------------------------------------------------------
//  Initialize ADC interface
// ------------------------------------------------------------------
void ADC_Init() {
  pinMode(ADC_SW_PIN, OUTPUT);
  digitalWrite(ADC_SW_PIN, LOW);
  pinMode(USB_DETECT_PIN, INPUT_PULLUP);
  //Wire.begin(SDA_PIN, SCL_PIN);
  //Wire.setClock(I2C_HZ);
}

// ---------------VBAT-Measurement Helpers----------------------
//  Read ADC once and return calibrated Vin in millivolts (mV)
// ------------------------------------------------------------------
uint16_t ADC_Read_mV() {
  // Detect USB or battery → choose VREF
  bool usb = (digitalRead(USB_DETECT_PIN) == LOW);
  float vref = usb ? VREF_USB : VREF_BAT;

  // Enable ADC path and wait to settle
  digitalWrite(ADC_SW_PIN, HIGH);
  delay(ADC_STABILIZE_MS);

  // Request ADC bytes
  uint16_t raw12 = 0;
  if (Wire.requestFrom(I2C_ADDR_ADC, (uint8_t)2) == 2) {
    uint8_t msb = Wire.read();
    uint8_t lsb = Wire.read();
    raw12 = ((uint16_t)(msb & 0x0F) << 8) | lsb;   // 12-bit right-aligned
  } else {
    digitalWrite(ADC_SW_PIN, LOW);
    return 0;  // error
  }

  // Disable ADC path
  digitalWrite(ADC_SW_PIN, LOW);

  // Convert to voltage
  float v_adc = (raw12 / 4095.0f) * vref;
  float vin_raw = v_adc * DIV_GAIN_INV;
  float vin_cal = (vin_raw * CAL_SLOPE) + CAL_OFFSET_V;

  // Convert to mV (rounded)
  uint16_t vin_mV = (uint16_t)(vin_cal * 1000.0f + 0.5f);
  return vin_mV;
}



// ---------- Sensor/RTC Init ----------
bool initI2CAndDevices()
{
  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000); // 100 kHz

  // RTC
  rtc_ok = rtc.begin();
  if (!rtc_ok)
  {
    Serial.println("❌ Couldn't find PCF8523 RTC on I2C bus!");
    Serial.println("⏱️ RTC not initialized or lost power. Time not set (use SETTIME).");
  }

  // SHT4x
  if (!sht4.begin(&Wire))
  {
    Serial.println("❌ Failed to find SHT4x. Check wiring.");
    return false;
  }
  sht4.setPrecision(SHT4X_HIGH_PRECISION);
  sht4.setHeater(SHT4X_NO_HEATER);

  ADC_Init(); //Init ADC chip MCP3021


  return true;
}



//---------------------Data Encoding to bin record------------------------


// Pack uint24 little-endian
static inline void pack_u24le(uint32_t v, uint8_t out[3])
{
  if (v > 0xFFFFFFu)
    v = 0xFFFFFFu;
  out[0] = (uint8_t)(v & 0xFF);
  out[1] = (uint8_t)((v >> 8) & 0xFF);
  out[2] = (uint8_t)((v >> 16) & 0xFF);
}

// Pack int16 little-endian
static inline void pack_i16le(int16_t v, uint8_t out[2])
{
  out[0] = (uint8_t)(v & 0xFF);
  out[1] = (uint8_t)((v >> 8) & 0xFF);
}

// Bin file path for /logs/YYYY-MM-DD/YYYY-MM-DD.bin
static String buildBinPath(const String &date)
{
  if (date.length() != 10 || date.charAt(4) != '-' || date.charAt(7) != '-')
    return "";
  for (uint8_t i : {0, 1, 2, 3, 5, 6, 8, 9})
    if (!isDigit(date[i]))
      return "";
  return String(ROOT) + "/" + date + "/" + date + ".bin";
}

// Create today's .bin file if missing (no header needed)
static bool ensureTodayTargetsBin(const String &dateStr, String &outBinPath)
{
  LittleFS.mkdir("/logs");
  String dayFolder = String(ROOT) + "/" + dateStr;
  LittleFS.mkdir(dayFolder);

  String binPath = buildBinPath(dateStr);
  if (binPath == "")
    return false;

  if (!LittleFS.exists(binPath))
  {
    File f = LittleFS.open(binPath, FILE_WRITE);
    if (!f)
      return false;
    f.close();
  }
  outBinPath = binPath;
  return true;
}




// ---- Helpers to quantize fields ----

// Temperature: store (T - 5.0) * 10 in 0..500
static inline uint16_t enc_temp01(float tC)
{
  int v = (int)lroundf((tC - 5.0f) * 10.0f);
  if (v < 0)
    v = 0;
  if (v > 500)
    v = 500;
  return (uint16_t)v;
}

// Humidity: clamp 0..100 into 0..100 (fits in uint8)
static inline uint8_t enc_rh(float rh)
{
  int v = (int)lroundf(rh);
  if (v < 0)
    v = 0;
  if (v > 100)
    v = 100;
  return (uint8_t)v;
}

// ---- VBAT (1 byte) ----
// Map 2.50 V .. 4.50 V -> 0 .. 255 (≈7.84 mV/LSB). Values outside clamp to ends.
static const uint32_t VBAT_MIN_MV = 2500;
static const uint32_t VBAT_MAX_MV = 4500;
static inline uint8_t enc_vbat(uint32_t mv)
{
  if ((int32_t)mv <= (int32_t)VBAT_MIN_MV)
    return 0;
  if ((int32_t)mv >= (int32_t)VBAT_MAX_MV)
    return 255;
  // Linear map
  float frac = (float)(mv - VBAT_MIN_MV) / (float)(VBAT_MAX_MV - VBAT_MIN_MV);
  int b = (int)lroundf(frac * 255.0f);
  if (b < 0)
    b = 0;
  if (b > 255)
    b = 255;
  return (uint8_t)b;
}

// Append raw 7-byte record to today's .bin
static bool appendBinRecord(const String &binPath, const uint8_t rec[RECORD_SIZE])
{
  File f = LittleFS.open(binPath, FILE_APPEND);
  if (!f)
  {
    f = LittleFS.open(binPath, FILE_WRITE);
    if (!f)
      return false;
  }
  size_t n = f.write(rec, RECORD_SIZE);
  f.flush();
  f.close();
  return (n == RECORD_SIZE);
}

// ---------- Small utils ----------
static inline void blinkOnce(uint16_t ms = 200)
{
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(ms);
  digitalWrite(LED_PIN, LOW);
}

static void emergencyBlink(int count)
{
  pinMode(LED_PIN, OUTPUT);
  for (int i = 0; i < count; i++)
  {
    digitalWrite(LED_PIN, HIGH);
    delay(100);
    digitalWrite(LED_PIN, LOW);
    delay(100);
  }
}

static inline void zp2(Print &p, int v)
{
  if (v < 10)
    p.print('0');
  p.print(v);
}

// Load heartbeat settings from NVS
static void loadHeartbeatFromNVS()
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/true))
  {
    heartbeatOn = false;
    heartbeatPeriod = DEFAULT_HB_PERIOD;
    return;
  }
  heartbeatOn = prefs.getUChar(NVS_KEY_HB_ON, 0) != 0;
  heartbeatPeriod = prefs.getUInt(NVS_KEY_HB_PER, DEFAULT_HB_PERIOD);
  prefs.end();

  // sanity clamp
  if (heartbeatPeriod < 5)
    heartbeatPeriod = 5; // avoid too chatty
  if (heartbeatPeriod > 3600)
    heartbeatPeriod = 3600; // ≤ 1h
}

static bool storeHeartbeatOnToNVS(bool on)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return false;
  prefs.putUChar(NVS_KEY_HB_ON, on ? 1 : 0);
  prefs.end();
  heartbeatOn = on;
  return true;
}

static bool storeHeartbeatPeriodToNVS(uint32_t sec)
{
  if (sec < 5)
    sec = 5;
  if (sec > 3600)
    sec = 3600;
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return false;
  prefs.putUInt(NVS_KEY_HB_PER, sec);
  prefs.end();
  heartbeatPeriod = sec;
  return true;
}

// Storage mode helpers
static void loadStorageModeFromNVS()
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/true))
  {
    storageMode = STORAGE_INTERNAL; // default
    autoThreshold = 90;
    autoSwitched = false;
    return;
  }
  uint8_t mode = prefs.getUChar(NVS_KEY_STORAGE_MODE, STORAGE_INTERNAL);
  autoThreshold = prefs.getUChar(NVS_KEY_AUTO_THRESHOLD, 90);
  autoSwitched = (prefs.getUChar(NVS_KEY_AUTO_SWITCHED, 0) != 0);
  prefs.end();
  
  if (mode == STORAGE_EXTERNAL)
    storageMode = STORAGE_EXTERNAL;
  else if (mode == STORAGE_AUTO)
    storageMode = STORAGE_AUTO;
  else
    storageMode = STORAGE_INTERNAL;
}

static bool storeStorageModeToNVS(StorageMode mode)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return false;
  prefs.putUChar(NVS_KEY_STORAGE_MODE, (uint8_t)mode);
  // Clear auto-switched flag when user manually changes mode (reset mechanism A)
  prefs.putUChar(NVS_KEY_AUTO_SWITCHED, 0);
  prefs.end();
  storageMode = mode;
  autoSwitched = false;
  return true;
}

static bool storeAutoThresholdToNVS(uint8_t pct)
{
  if (pct < 5) pct = 5;
  if (pct > 95) pct = 95;
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return false;
  prefs.putUChar(NVS_KEY_AUTO_THRESHOLD, pct);
  prefs.end();
  autoThreshold = pct;
  return true;
}

static bool storeAutoSwitchedToNVS(bool switched)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return false;
  prefs.putUChar(NVS_KEY_AUTO_SWITCHED, switched ? 1 : 0);
  prefs.end();
  autoSwitched = switched;
  return true;
}

static const char* storageModeToString(StorageMode mode)
{
  if (mode == STORAGE_EXTERNAL) return "EXTERNAL";
  if (mode == STORAGE_AUTO) return "AUTO";
  return "INTERNAL";
}

// Two-blink heartbeat (short and cheap)
static inline void blinkTwice(uint16_t on_ms = 100, uint16_t gap_ms = 150)
{
  blinkOnce(on_ms);
  delay(gap_ms);
  blinkOnce(on_ms);
}

// ---------- NVS helpers ----------
void loadIntervalFromNVS()
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/true))
  {
    Serial.println("⚠️ NVS open(ro) failed, using default interval.");
    sleepSeconds = DEFAULT_SLEEP_SECONDS;
    return;
  }
  uint32_t val = prefs.getUInt(NVS_KEY_INTERVAL, DEFAULT_SLEEP_SECONDS);
  prefs.end();
  // sanity clamp (30s .. 24h)
  if (val < 10)
    val = 10;
  if (val > 86400UL)
    val = 86400UL;
  sleepSeconds = val;
}

bool storeIntervalToNVS(uint32_t val)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
  {
    return false;
  }
  prefs.putUInt(NVS_KEY_INTERVAL, val);
  prefs.end();
  sleepSeconds = val; // update RAM copy as well (for immediate reflection if needed)
  return true;
}

// ---------- File-system helpers for logger ----------
bool ensureTodayTargets(const String &dateStr)
{
  LittleFS.mkdir("/logs");
  String dayFolder = String(ROOT) + "/" + dateStr;
  LittleFS.mkdir(dayFolder);

  String csvPath = dayFolder + "/" + dateStr + ".csv";

  bool needHeader = !LittleFS.exists(csvPath);
  if (!needHeader)
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
    f.println("time,temperature,humidity,vbat_mV");
    f.close();
  }

  currentDate = dateStr;
  currentFolder = dayFolder;
  currentCsv = csvPath;
  return true;
}

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








// ===================================================
// ===============  DUMP MODE (Protocol) =============
// Commands: HELLO | LIST | GET YYYY-MM-DD | BYE
// PLUS: SETTIME YYYY-MM-DD HH:MM:SS | TIME? | SENSE? | VBAT?
//       INTERVAL? | SETINTERVAL <sec>
// ===================================================

static String readLineWithTimeout(uint32_t timeout_ms = 2000)
{
  String line;
  uint32_t start = millis();
  while (millis() - start < timeout_ms)
  {
    while (Serial.available())
    {
      char c = (char)Serial.read();
      if (c == '\r')
        continue;
      if (c == '\n')
        return line;
      line += c;
    }
    delay(1);
  }
  return "";
}

static uint32_t crc32_update(uint32_t crc, const uint8_t *data, size_t len)
{
  crc = ~crc;
  for (size_t i = 0; i < len; i++)
  {
    crc ^= data[i];
    for (uint8_t k = 0; k < 8; k++)
    {
      uint32_t mask = -(crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320U & mask);
    }
  }
  return ~crc;
}

static String buildLogPath(const String &date)
{
  if (date.length() != 10 || date.charAt(4) != '-' || date.charAt(7) != '-')
    return "";
  for (uint8_t i : {0, 1, 2, 3, 5, 6, 8, 9})
    if (!isDigit(date[i]))
      return "";
  return String(ROOT) + "/" + date + "/" + date + ".csv";
}

// --- Recursively delete a directory tree under LittleFS (counts files/bytes) ---
static bool fsRecursiveDelete(const String &absPath, uint32_t &filesDeleted, uint64_t &bytesFreed)
{
  fs::File f = LittleFS.open(absPath, "r");
  if (!f)
    return false;

  if (!f.isDirectory())
  {
    // File case
    size_t sz = f.size();
    f.close();
    if (LittleFS.remove(absPath))
    {
      filesDeleted += 1;
      bytesFreed += sz;
      return true;
    }
    return false;
  }

  // Directory case: iterate children first
  fs::File child = f.openNextFile();
  while (child)
  {
    String childName = child.name(); // basename under this dir
    String childAbs = absPath + "/" + childName;
    child.close(); // close handle before recursive call
    fsRecursiveDelete(childAbs, filesDeleted, bytesFreed);
    // re-open dir to continue enumerate (ESP FS iterators are stateful)
    fs::File reopen = LittleFS.open(absPath, "r");
    if (!reopen)
      break;
    child = reopen.openNextFile();
  }
  f.close();

  // finally remove the now-empty directory
  return LittleFS.rmdir(absPath);
}

// --- Delete a single date's CSV and its folder (/logs/YYYY-MM-DD/YYY-MM-DD.csv) ---
static bool deleteDateLogAndFolder(const String &date, uint32_t &filesDeleted, uint64_t &bytesFreed)
{
  filesDeleted = 0;
  bytesFreed = 0;

  // Build absolute paths safely
  String csvPath = buildLogPath(date); // /logs/2025-10-05/2025-10-05.csv
  if (csvPath == "")
    return false;

  // Derive the day folder: /logs/YYYY-MM-DD
  String dayFolder = String(ROOT) + "/" + date;

  // If the folder doesn’t exist, nothing to do
  if (!LittleFS.exists(dayFolder))
    return false;

  // If you only want to delete the CSV and then its folder IF empty, you could:
  //   1) delete CSV, 2) remove dir. But users may drop extra files in the folder.
  // Safer: delete the entire folder tree (counts all files & bytes).
  return fsRecursiveDelete(dayFolder, filesDeleted, bytesFreed);
}

static uint32_t countRowsQuick(File &f)
{
  const size_t BUFSZ = 1024;
  static uint8_t buf[BUFSZ];
  uint32_t rows = 0;
  f.seek(0, SeekSet);
  while (true)
  {
    int n = f.read(buf, BUFSZ);
    if (n <= 0)
      break;
    for (int i = 0; i < n; i++)
      if (buf[i] == '\n')
        rows++;
  }
  f.seek(0, SeekSet);
  return rows;
}

// --- quick info for a date's BIN (size bytes + data rows) ---
static bool getDateBinInfo(const String &date, size_t &outSize, uint32_t &outRows)
{
  String path = buildBinPath(date); // e.g. /logs/2025-10-05/2025-10-05.bin
  if (path == "" || !LittleFS.exists(path))
  {
    outSize = 0;
    outRows = 0;
    return false;
  }

  File f = LittleFS.open(path, "r");
  if (!f)
  {
    outSize = 0;
    outRows = 0;
    return false;
  }

  outSize = f.size();
  f.close();

  // Each record is exactly 7 bytes
  // Each record is exactly 7 bytes
  outRows = (uint32_t)(outSize / RECORD_SIZE);

  return true;
}

static void listDatesProtocol()
{
  // Ensure /logs exists and is a directory
  File root = LittleFS.open(ROOT, "r");
  if (!root || !root.isDirectory())
  {
    Serial.println("DATES 0");
    Serial.println("END");
    return;
  }

  // First pass: count date folders that actually have a BIN
  uint32_t count = 0;
  for (File e = root.openNextFile(); e; e = root.openNextFile())
  {
    if (!e.isDirectory())
      continue;

    String full = e.name();
    String date = full;
    if (full.startsWith(String(ROOT) + "/"))
      date = full.substring(strlen(ROOT) + 1);
    if (date.endsWith("/"))
      date.remove(date.length() - 1);

    size_t sz = 0;
    uint32_t rows = 0;
    if (getDateBinInfo(date, sz, rows) && rows > 0)
      count++;
  }

  Serial.print("DATES ");
  Serial.println(count);

  // Second pass: print date + size(KB) + rows
  root = LittleFS.open(ROOT, "r"); // reopen to reset iterator
  for (File e = root.openNextFile(); e; e = root.openNextFile())
  {
    if (!e.isDirectory())
      continue;

    String full = e.name();
    String date = full;
    if (full.startsWith(String(ROOT) + "/"))
      date = full.substring(strlen(ROOT) + 1);
    if (date.endsWith("/"))
      date.remove(date.length() - 1);

    size_t sz = 0;
    uint32_t rows = 0;
    if (!getDateBinInfo(date, sz, rows) || rows == 0)
      continue;

    uint32_t kb = (uint32_t)((sz + 1023) / 1024);
    // Machine-parseable:
    // DATE=YYYY-MM-DD SIZE_KB=<integer> ROWS=<integer>
    Serial.print("DATE=");
    Serial.print(date);
    Serial.print(" SIZE_KB=");
    Serial.print(kb);
    Serial.print(" ROWS=");
    Serial.println(rows);
  }

  Serial.println("END");
}

static void handleGetDate(const String &date)
{
  String path = buildBinPath(date);
  if (path == "")
  {
    Serial.println("ERR code=BAD_DATE msg=\"format YYYY-MM-DD\"");
    return;
  }
  if (!LittleFS.exists(path))
  {
    Serial.print("ERR code=NOT_FOUND msg=\"no file for ");
    Serial.print(date);
    Serial.println("\"");
    return;
  }

  File f = LittleFS.open(path, "r");
  if (!f)
  {
    Serial.println("ERR code=IO msg=\"open failed\"");
    return;
  }

  size_t size = f.size();
  uint32_t rows = (uint32_t)(size / RECORD_SIZE);

  Serial.print("OK SIZE=");
  Serial.print(size);
  Serial.print(" ROWS=");
  Serial.print(rows);
  Serial.print(" PATH=");
  Serial.println(path);

  Serial.println("DATA");

  const size_t BUFSZ = 1024;
  static uint8_t buf[BUFSZ];
  uint32_t crc = 0x00000000;
  size_t remaining = size;

  while (remaining > 0)
  {
    size_t chunk = remaining > BUFSZ ? BUFSZ : remaining;
    int n = f.read(buf, chunk);
    if (n <= 0)
      break;
    Serial.write(buf, n);
    crc = crc32_update(crc, buf, n);
    remaining -= n;
  }
  f.close();

  Serial.println();
  Serial.println("END");

  char hex[9];
  snprintf(hex, sizeof(hex), "%08lX", (unsigned long)crc);
  Serial.print("CRC32=");
  Serial.println(hex);
}

// ======================================RTC Helpers----------------------------------------=========
// Parse "YYYY-MM-DD HH:MM:SS" into RTCDateTime
/* // Return true if valid format
// =========================================================
static bool parseDateTime(const String &s)
{
  if (s.length() != 19)
    return false;

  // Expected format: 0123456789012345678
  //                  YYYY-MM-DD HH:MM:SS
  if (s.charAt(4) != '-' || s.charAt(7) != '-' || s.charAt(10) != ' ' ||
      s.charAt(13) != ':' || s.charAt(16) != ':')
    return false;

  for (uint8_t i : {0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18})
    if (!isDigit(s[i]))
      return false;

  out.year = s.substring(0, 4).toInt();
  out.month = s.substring(5, 7).toInt();
  out.day = s.substring(8, 10).toInt();
  out.hour = s.substring(11, 13).toInt();
  out.minute = s.substring(14, 16).toInt();
  out.second = s.substring(17, 19).toInt();

  if (out.year < 2000 || out.year > 2099)
    return false;
  if (out.month < 1 || out.month > 12)
    return false;
  if (out.day < 1 || out.day > 31)
    return false;
  if (out.hour > 23 || out.minute > 59 || out.second > 59)
    return false;

  return true;
}
 */

// Accepts "YYYY-MM-DD HH:MM:SS" exactly.
// Returns true on success, false on validation error.
static bool setRTC_fromString(const String &ts)
{
  // --- quick shape checks ---
  if (ts.length() < 19)
    return false;
  // fixed separators
  if (ts.charAt(4) != '-' || ts.charAt(7) != '-' || ts.charAt(10) != ' ' ||
      ts.charAt(13) != ':' || ts.charAt(16) != ':')
    return false;

  auto is2digits = [&](int i)
  { return isDigit(ts.charAt(i)) && isDigit(ts.charAt(i + 1)); };
  // digits at all numeric slots
  for (int i : {0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18})
  {
    if (!isDigit(ts.charAt(i)))
      return false;
  }

  // --- parse ---
  uint16_t year = (uint16_t)ts.substring(0, 4).toInt();
  uint8_t month = (uint8_t)ts.substring(5, 7).toInt();
  uint8_t date = (uint8_t)ts.substring(8, 10).toInt();
  uint8_t hour = (uint8_t)ts.substring(11, 13).toInt();
  uint8_t min = (uint8_t)ts.substring(14, 16).toInt();
  uint8_t sec = (uint8_t)ts.substring(17, 19).toInt();

  // --- validate ranges ---
  if (year < 2000 || year > 2099)
    return false;
  if (month < 1 || month > 12)
    return false;

  auto isLeap = [&](uint16_t y)
  { return (y % 4 == 0 && (y % 100 != 0 || y % 400 == 0)); };
  auto daysInMonth = [&](uint16_t y, uint8_t m) -> uint8_t
  {
    static const uint8_t d[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    if (m == 2)
      return d[m - 1] + (isLeap(y) ? 1 : 0);
    return d[m - 1];
  };
  uint8_t dim = daysInMonth(year, month);
  if (date < 1 || date > dim)
    return false;
  if (hour > 23)
    return false;
  if (min > 59)
    return false;
  if (sec > 59)
    return false;

  // --- compute ISO weekday (1=Mon .. 7=Sun) ---
  // Tomohiko Sakamoto’s algorithm (works for Gregorian dates):
  auto isoWeekday = [&](int y, int m, int d) -> uint8_t
  {
    static int t[] = {0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4};
    y -= (m < 3);
    int w = (y + y / 4 - y / 100 + y / 400 + t[m - 1] + d) % 7; // 0=Sun..6=Sat
    // convert to ISO 1..7 (Mon..Sun)
    return (w == 0) ? 7 : w;
  };
  uint8_t weekday_iso = isoWeekday(year, month, date);

  // NOTE: If your RV3028::setTime expects 0..6 (Mon=1..Sat=6, Sun=0), map like:
  // uint8_t weekday = (weekday_iso % 7); // Mon=1..Sun=0
  // Many drivers prefer 1..7; we’ll pass ISO 1..7 by default:
  uint8_t weekday = weekday_iso;

  // --- set RTC ---
  return rtc.setTime(sec, min, hour, weekday, date, month, year);
}

static void printRTC_YMD_HMS()
{
  // Make sure the internal shadow registers are fresh
  rtc.updateTime();

  int y = rtc.getYear();
  int mo = rtc.getMonth();
  int d = rtc.getDate();
  int h = rtc.getHours();
  int mi = rtc.getMinutes();
  int s = rtc.getSeconds();

  // Some libs return 0..99, others 2000..2099. Normalize:
  if (y < 100)
    y += 2000;

  char buf[20];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d", y, mo, d, h, mi, s);
  Serial.println(buf);
}

// // Set RTC from struct
// static void setRTCfromStruct(const RTCDateTime &t)
// {
//   rtc.setTime(t.second, t.minute, t.hour, 0 /*weekday*/,
//               t.day, t.month, t.year - 2000);
// }

// Parse days <-> calendar (UTC) — H. Hinnant algorithms
static int64_t days_from_civil(int y, unsigned m, unsigned d)
{
  y -= m <= 2;
  const int era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return (int64_t)era * 146097 + (int64_t)doe - 719468; // 1970-01-01
}
static void civil_from_days(int64_t z, int &y, unsigned &m, unsigned &d)
{
  z += 719468;
  const int era = (z >= 0 ? z : z - 146096) / 146097;
  const unsigned doe = (unsigned)(z - era * 146097); // [0, 146096]
  const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  y = (int)yoe + era * 400;
  const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const unsigned mp = (5 * doy + 2) / 153;
  d = doy - (153 * mp + 2) / 5 + 1;
  m = mp + (mp < 10 ? 3 : -9);
  y += (m <= 2);
}

// UNIX -> struct
// static void unixToRTC(uint32_t epoch, RTCDateTime &out)
// {
//   int64_t days = epoch / 86400;
//   uint32_t rem = epoch % 86400;
//   int y;
//   unsigned m, d;
//   civil_from_days(days, y, m, d);
//   out.year = (uint16_t)y;
//   out.month = (uint8_t)m;
//   out.day = (uint8_t)d;
//   out.hour = (uint8_t)(rem / 3600);
//   out.minute = (uint8_t)((rem % 3600) / 60);
//   out.second = (uint8_t)(rem % 60);
// }

// formatters for struct
// static String fmtDate(const RTCDateTime &t)
// {
//   char buf[11];
//   snprintf(buf, sizeof(buf), "%04u-%02u-%02u", t.year, t.month, t.day);
//   return String(buf);
// }
// static String fmtTime(const RTCDateTime &t)
// {
//   char buf[9];
//   snprintf(buf, sizeof(buf), "%02u:%02u:%02u", t.hour, t.minute, t.second);
//   return String(buf);
// }

//-------------------------------------END RTC HELPERS-------------------------------

// --- Safe reboot helper: prints OK, flushes serial, closes FS, then restarts ---
static void safeRebootNow(const char *reason = "cmd")
{
  // Machine-parseable ack so host tools know the restart is intentional
  Serial.print("OK REBOOT REASON=");
  Serial.println(reason);
  Serial.flush();

  // Best effort: close FS and give peripherals a moment to settle
  LittleFS.end();
  delay(100); // allow UART TX buffer to drain fully

  esp_restart(); // soft reset SoC
  // no return
}

// Compute a 32-bit hash of the security code (reuses your CRC32)
static uint32_t codeHash(const String &code)
{
  const uint8_t *p = (const uint8_t *)code.c_str();
  size_t n = code.length();
  uint32_t crc = 0x00000000;
  crc = crc32_update(crc, p, n);
  return crc;
}

// Load expected hash from NVS; if missing, fall back to compile-time default
static bool loadFormatCodeHash(uint32_t &outHash)
{
  Preferences p;
  if (p.begin(NVS_NS, /*readOnly=*/true))
  {
    uint32_t hv = p.getUInt(NVS_KEY_FMT_HASH, 0);
    p.end();
    if (hv != 0)
    {
      outHash = hv;
      return true;
    }
  }
  // Fallback to compile-time code
  outHash = codeHash(String(DEFAULT_FORMAT_CODE));
  return true;
}

// Store/update the security code hash into NVS
static bool storeFormatCodeHash(uint32_t hv)
{
  Preferences p;
  if (!p.begin(NVS_NS, /*readOnly=*/false))
    return false;
  p.putUInt(NVS_KEY_FMT_HASH, hv);
  p.end();
  return true;
}

// Attempt-limiter to avoid brute-force over UART in one session
static uint8_t &formatTries()
{
  static uint8_t tries = 0;
  return tries;
}

// Perform the actual format (returns bytes-total after remount or 0 on failure)
static size_t doLittleFSFormat()
{
  // Close before a destructive format
  LittleFS.end();
  delay(50);

  bool ok = LittleFS.format(); // erase/format partition
  if (!ok)
    return 0;

  // Remount
  if (!LittleFS.begin(false))
  {
    // As a last resort, try with auto-format flag (should be already formatted though)
    if (!LittleFS.begin(true))
      return 0;
  }

  // Recreate /logs root
  LittleFS.mkdir("/logs");

  return LittleFS.totalBytes(); // report new total capacity
}

// ---------- Pretty printers / headers ----------
static String humanBytes(uint64_t b)
{
  const char *u[] = {"B", "KB", "MB", "GB"};
  double v = (double)b;
  int i = 0;
  while (v >= 1024.0 && i < 3)
  {
    v /= 1024.0;
    ++i;
  }
  char buf[48];
  snprintf(buf, sizeof(buf), "%.2f %s", v, u[i]);
  return String(buf);
}

static void header(const char *t)
{
  Serial.println();
  Serial.println(F("======================================"));
  Serial.println(t);
  Serial.println(F("======================================"));
}

static const char *partTypeName(esp_partition_type_t t)
{
  return (t == ESP_PARTITION_TYPE_DATA) ? "DATA" : (t == ESP_PARTITION_TYPE_APP) ? "APP"
                                                                                 : "UNKNOWN";
}

// --- List ALL internal flash partitions ---
// ---- helpers: % and aligned sizes ----

static const char *partSubtypeName(esp_partition_type_t t, esp_partition_subtype_t s)
{
  if (t == ESP_PARTITION_TYPE_DATA)
  {
#ifdef ESP_PARTITION_SUBTYPE_DATA_LITTLEFS
    if (s == ESP_PARTITION_SUBTYPE_DATA_LITTLEFS)
      return "littlefs";
#endif
    if (s == ESP_PARTITION_SUBTYPE_DATA_SPIFFS)
      return "spiffs";
    if (s == ESP_PARTITION_SUBTYPE_DATA_NVS)
      return "nvs";
    if (s == ESP_PARTITION_SUBTYPE_DATA_PHY)
      return "phy";
    if (s == ESP_PARTITION_SUBTYPE_DATA_COREDUMP)
      return "coredump";
    if (s == ESP_PARTITION_SUBTYPE_DATA_OTA)
      return "ota_data";
    return "data(other)";
  }
  if (t == ESP_PARTITION_TYPE_APP)
  {
    if (s == ESP_PARTITION_SUBTYPE_APP_FACTORY)
      return "factory";
    if (s >= ESP_PARTITION_SUBTYPE_APP_OTA_0 && s <= ESP_PARTITION_SUBTYPE_APP_OTA_15)
      return "ota_x";
    return "app(other)";
  }
  return "unknown";
}

static String humanPct(uint64_t part, uint64_t total)
{
  double p = (total > 0) ? (100.0 * (double)part / (double)total) : 0.0;
  char buf[24];
  snprintf(buf, sizeof(buf), "%.2f%%", p);
  return String(buf);
}

static const char *encYesNo(bool enc) { return enc ? "yes" : "no"; }

static const char *subtypeStr(esp_partition_type_t t, esp_partition_subtype_t s)
{
  // Keep your existing partSubtypeName() for specific names; fall back here for generic numeric
  const char *name = partSubtypeName(t, s);
  return (name && name[0]) ? name : "unknown";
}

static void reportAllFlashPartitions()
{
  header("Internal Flash (all partitions)");

  esp_partition_iterator_t it =
      esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, nullptr);
  if (!it)
  {
    Serial.println(F("No partitions found."));
    return;
  }

  Serial.println(F("Type/Sub        Label         Address    Size            Encrypted"));
  Serial.println(F("-------------------------------------------------------------------"));

  while (it != nullptr)
  {
    const esp_partition_t *part = esp_partition_get(it);

    // Columns (bounded by design; stream printed to avoid large snprintf buffers)
    const char *typeStr = partTypeName(part->type);
    const char *subStr = subtypeStr(part->type, part->subtype);
    const char *labelStr = (part->label && part->label[0]) ? part->label : "(none)";
    const char *encStr = part->encrypted ? "yes" : "no";

    // Human-readable size
    String sizeStr = humanBytes(part->size);

    // Streamed (no giant format buffers; widths approximate previous layout)
    // Type/Sub column
    Serial.print(typeStr);
    Serial.print("  /");
    Serial.print(subStr);
    if (strlen(subStr) < 9)
      for (int i = 0; i < (9 - (int)strlen(subStr)); ++i)
        Serial.print(' ');
    Serial.print(' ');

    // Label column (pad to ~12)
    Serial.print(labelStr);
    if (strlen(labelStr) < 12)
      for (int i = 0; i < (12 - (int)strlen(labelStr)); ++i)
        Serial.print(' ');

    // Address / Size / Encrypted
    Serial.print("  0x");
    char addrBuf[7];
    snprintf(addrBuf, sizeof(addrBuf), "%06X", (unsigned)part->address);
    Serial.print(addrBuf);
    Serial.print("  ");
    // pad size to ~14 chars
    if (sizeStr.length() < 14)
    {
      for (int i = 0; i < 14 - (int)sizeStr.length(); ++i)
        Serial.print(' ');
      Serial.print(sizeStr);
    }
    else
    {
      Serial.print(sizeStr.substring(0, 14));
    }
    Serial.print(' ');
    Serial.println(encStr);

    // Move to next; NOTE: esp_partition_next() frees the current iterator
    it = esp_partition_next(it);
  }

  // After the while loop, 'it' is NULL (all freed by esp_partition_next).
  // It's SAFE to call release(NULL); it becomes a no-op.
  esp_partition_iterator_release(it);
}

// --- Quick LittleFS usage (with % free) ---
static void reportLittleFSQuick()
{
  header("LittleFS (quick usage)");
  if (!LittleFS.begin(false))
  {
    Serial.println(F("Mount: FAILED"));
    return;
  }
  size_t total = LittleFS.totalBytes();
  size_t used = LittleFS.usedBytes();
  size_t freeB = (total >= used) ? (total - used) : 0;

  Serial.printf("Mount    : OK\n");
  Serial.printf("Total    : %u (%s)\n", (unsigned)total, humanBytes(total).c_str());
  Serial.printf("Used     : %u (%s)\n", (unsigned)used, humanBytes(used).c_str());
  Serial.printf("Free     : %u (%s)\n", (unsigned)freeB, humanBytes(freeB).c_str());
  Serial.printf("Free %%   : %s\n", humanPct(freeB, total).c_str());
}

static void reportLittleFSUsage()
{
  header("LittleFS (usage)");
  if (!LittleFS.begin(false))
  {
    Serial.println(F("Mount: FAILED"));
    return;
  }
  size_t total = LittleFS.totalBytes();
  size_t used = LittleFS.usedBytes();
  float usedPct = (total > 0) ? (100.0f * used / total) : 0.0f;

  Serial.printf("Mount    : OK\n");
  Serial.printf("Total    : %u (%s)\n", (unsigned)total, humanBytes(total).c_str());
  Serial.printf("Used     : %u (%s)\n", (unsigned)used, humanBytes(used).c_str());
  Serial.printf("Used %%   : %.2f%%\n", usedPct);
  Serial.printf("Status   : %s\n", (usedPct >= 90.0f) ? "WARNING - NEAR FULL" : "OK");
}

// --- Detailed: partition line + mount/usage (matches your sample block) ---
static void reportLittleFSVerbose()
{
  header("Partition (LittleFS on internal flash)");
  const esp_partition_t *p = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA,
#ifdef ESP_PARTITION_SUBTYPE_DATA_LITTLEFS
      ESP_PARTITION_SUBTYPE_DATA_LITTLEFS,
#else
      ESP_PARTITION_SUBTYPE_DATA_SPIFFS,
#endif
      nullptr);
  if (!p)
  {
    Serial.println(F("Could not find a DATA/LittleFS partition."));
    return;
  }

  Serial.printf("Label     : %s\n", p->label);
  Serial.printf("Type/Sub  : %s / %s\n", partTypeName(p->type), partSubtypeName(p->type, p->subtype));
  Serial.printf("Address   : 0x%06X\n", (unsigned)p->address);
  Serial.printf("Size      : %u (%s)\n", (unsigned)p->size, humanBytes(p->size).c_str());
  Serial.printf("Encrypted : %s\n", p->encrypted ? "yes" : "no");
  // ^ note: ESP-IDF sets part->encrypted but LittleFS isn't per-file encrypted by default.
  // If you truly use flash encryption, this will print "yes".

  header("LittleFS (usage)");
  if (!LittleFS.begin(false))
  {
    Serial.println(F("Mount    : FAILED"));
    return;
  }

  size_t total = LittleFS.totalBytes();
  size_t used = LittleFS.usedBytes();
  size_t freeB = (total >= used) ? (total - used) : 0;

  Serial.printf("Mount    : OK\n");
  Serial.printf("Total    : %u (%s)\n", (unsigned)total, humanBytes(total).c_str());
  Serial.printf("Used     : %u (%s)\n", (unsigned)used, humanBytes(used).c_str());
  Serial.printf("Free     : %u (%s)\n", (unsigned)freeB, humanBytes(freeB).c_str());
  Serial.printf("Free %%   : %s\n", humanPct(freeB, total).c_str());
}

// Dump/Data Retrieval server (push-button entry only)
void runDataRetrievalMode()
{
  initI2CAndDevices();

  // Try SHT4x init (okay if missing; we'll retry on demand)
  bool sht_ok = sht4.begin(&Wire);
  if (sht_ok)
  {
    sht4.setPrecision(SHT4X_HIGH_PRECISION);
    sht4.setHeater(SHT4X_NO_HEATER);
  }



  bool led = false;
  uint32_t lastBlink = 0;

  loadHeartbeatFromNVS();
  loadStorageModeFromNVS();

  // Initialize external SPI flash
  if (flash.begin()) {
    Serial.println("✅ External SPI flash initialized.");
    FLASH_BYTES = flash.getCapacity();
    DATA_END = FLASH_BYTES;
    Serial.print("   Capacity: ");
    Serial.print(FLASH_BYTES);
    Serial.println(" bytes");
    
    // Try to load superblock and set tail
    if (loadSuperAndSetTail()) {
      Serial.print("   Tail loaded from superblock: 0x");
      Serial.println(g_tail, HEX);
    } else {
      Serial.println("   No valid superblock found, scanning...");
      findTailLinear();
    }
  } else {
    Serial.println("⚠️ External SPI flash not found or init failed.");
  }

  Serial.print("Current storage mode: ");
  Serial.println(storageModeToString(storageMode));

  // capability banner
  Serial.println("OK v=1 caps=GET(.bin),LIST(.bin),CRC,SETTIME,TIME?,SENSE?,VBAT?,INTERVAL?,SETINTERVAL,DEL,REBOOT,FORMAT,SETFMTCODE,FLASH-INFO,FLASH-FREE,FLASH-USAGE,FS-VERBOSE,HEARTBEAT?,HEARTBEAT,SETHEARTBEAT,STORAGE?,SETSTORAGE,AUTO-THRESHOLD-SET,AUTO-THRESHOLD?,AUTO-STATUS?,AUTO-RESET,EXT-INFO,EXT-DUMP,EXT-DUMP-RAW,EXT-DUMP-SECTOR,EXT-DUMP-SECTOR-RAW,EXT-FORMAT,EXT-LIST-SECTORS");

  while (true)
  {
    // non-blocking blink (250 ms on / 750 ms off)
    uint32_t now = millis();
    if (now - lastBlink >= (led ? 250u : 750u))
    {
      led = !led;
      digitalWrite(LED_PIN, led ? HIGH : LOW);
      lastBlink = now;
    }

    String line = readLineWithTimeout(100);
    if (line.length() == 0)
      continue;
    line.trim();

    if (line.startsWith("HELP"))
    {
      Serial.println("OK v=1 caps=GET(.bin),LIST(.bin),CRC,SETTIME,TIME?,SENSE?,VBAT?,INTERVAL?,SETINTERVAL,DEL,REBOOT,FORMAT,SETFMTCODE,FLASH-INFO,FLASH-FREE,FS-VERBOSE,HEARTBEAT?,HEARTBEAT,SETHEARTBEAT,STORAGE?,SETSTORAGE,EXT-INFO,EXT-DUMP,EXT-DUMP-RAW,EXT-DUMP-SECTOR,EXT-DUMP-SECTOR-RAW,EXT-FORMAT,EXT-LIST-SECTORS");
    }
    else if (line == "LIST")
    {
      listDatesProtocol();
    }
    else if (line.startsWith("GET "))
    {
      String date = line.substring(4);
      date.trim();
      handleGetDate(date);
    }

    else if (line.startsWith("DEL "))
    {
      String date = line.substring(4);
      date.trim();

      // Validate and build path
      String path = buildLogPath(date);
      if (path == "")
      {
        Serial.println("ERR code=BAD_DATE msg=\"format YYYY-MM-DD\"");
        continue;
      }

      // Require the day folder to exist
      String dayFolder = String(ROOT) + "/" + date;
      if (!LittleFS.exists(dayFolder))
      {
        Serial.print("ERR code=NOT_FOUND msg=\"no folder for ");
        Serial.print(date);
        Serial.println("\"");
        continue;
      }

      uint32_t filesDeleted = 0;
      uint64_t bytesFreed = 0;
      bool ok = deleteDateLogAndFolder(date, filesDeleted, bytesFreed);

      if (!ok)
      {
        Serial.println("ERR code=IO msg=\"delete failed\"");
      }
      else
      {
        // Machine-parseable success line
        // Example: OK DEL DATE=2025-10-05 FILES=3 BYTES=123456 SIZE_KB=121
        uint32_t kb = (uint32_t)((bytesFreed + 1023ULL) / 1024ULL);
        Serial.print("OK DEL DATE=");
        Serial.print(date);
        Serial.print(" FILES=");
        Serial.print(filesDeleted);
        Serial.print(" BYTES=");
        Serial.print((unsigned long)bytesFreed);
        Serial.print(" SIZE_KB=");
        Serial.println(kb);
      }
    }

    else if (line == "REBOOT" || line == "RESET")
    {
      safeRebootNow("user");
      // esp_restart() does not return; this line is never reached
    }

    else if (line == "BYE")
    {
      Serial.println("OK bye");
      // stay in server
    }

    // ===== SETFMTCODE <oldcode> <newcode>
    // Authenticated replacement: user must know current code.
    // If no code has been set in NVS yet, the expected old code is DEFAULT_FORMAT_CODE.
    else if (line.startsWith("SETFMTCODE "))
    {
      String payload = line.substring(strlen("SETFMTCODE "));
      payload.trim();

      // Split into two tokens: old and new
      int sp = payload.indexOf(' ');
      if (sp <= 0)
      {
        Serial.println("ERR code=BAD_ARG msg=\"use: SETFMTCODE <oldcode> <newcode>\"");
        continue;
      }
      String oldCode = payload.substring(0, sp);
      String newCode = payload.substring(sp + 1);
      oldCode.trim();
      newCode.trim();

      // Basic validation
      if (newCode.length() < 4 || newCode.length() > 32)
      {
        Serial.println("ERR code=BAD_ARG msg=\"newcode length 4..32\"");
        continue;
      }

      // Load expected (current) code hash; falls back to DEFAULT_FORMAT_CODE if unset
      uint32_t expectedHash = 0;
      loadFormatCodeHash(expectedHash);

      // Verify old code
      if (codeHash(oldCode) != expectedHash)
      {
        Serial.println("ERR code=AUTH msg=\"bad_old_code\"");
        continue;
      }

      // Prevent no-op updates
      if (oldCode == newCode)
      {
        Serial.println("ERR code=BAD_ARG msg=\"newcode equals oldcode\"");
        continue;
      }

      // Store new code hash
      uint32_t newHash = codeHash(newCode);
      if (!storeFormatCodeHash(newHash))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.println("OK SETFMTCODE");
      }
    }

    // ===== FORMAT <code>  -> securely format LittleFS (requires correct code) =====
    else if (line.startsWith("FORMAT "))
    {
      // Throttle attempts per session
      if (formatTries() >= 3)
      {
        Serial.println("ERR code=LOCKED msg=\"too many attempts\"");
        continue;
      }

      String code = line.substring(strlen("FORMAT "));
      code.trim();

      if (code.isEmpty())
      {
        Serial.println("ERR code=BAD_ARG msg=\"FORMAT <code>\"");
        continue;
      }

      // Load expected hash (NVS or default)
      uint32_t expected = 0;
      loadFormatCodeHash(expected);

      uint32_t provided = codeHash(code);
      if (provided != expected)
      {
        formatTries()++;
        Serial.println("ERR code=AUTH msg=\"bad_code\"");
        continue;
      }

      // Auth OK -> perform destructive format
      Serial.println("OK FORMAT START");
      Serial.flush();

      size_t totalAfter = doLittleFSFormat();
      if (totalAfter == 0)
      {
        Serial.println("ERR code=FORMAT_FAIL");
      }
      else
      {
        size_t used = LittleFS.usedBytes();
        size_t freeB = LittleFS.totalBytes() - used;
        // Machine-parseable completion line
        Serial.print("OK FORMAT DONE TOTAL=");
        Serial.print((unsigned)LittleFS.totalBytes());
        Serial.print(" USED=");
        Serial.print((unsigned)used);
        Serial.print(" FREE=");
        Serial.println((unsigned)freeB);
      }

      // Optional: reset tries after success
      formatTries() = 0;
    }

    else if (line == "TIME?")
    {
      if (!rtc_ok)
      {
        Serial.println("ERR code=RTC msg=\"not found\"");
      }
      else
      {
        rtc.updateTime(); // refresh internal cache
        Serial.print("OK ");
        printRTC_YMD_HMS(); // prints "YYYY-MM-DD HH:MM:SS"
      }
    }
    else if (line.startsWith("SETTIME "))
    {
      String payload = line.substring(8);
      payload.trim();

      if (!rtc_ok)
      {
        Serial.println("ERR code=RTC msg=\"not found\"");
        continue;
      }

      if (!setRTC_fromString(payload))
      {
        Serial.println("ERR code=BAD_DT msg=\"use SETTIME YYYY-MM-DD HH:MM:SS\"");
        continue;
      }

      // setRTCfromStruct(t);

      // Read back to confirm what the RTC actually holds
      rtc.updateTime();
      Serial.print("OK set ");
      printRTC_YMD_HMS();
    }

    // ===== SENSE? -> one SHT4x reading + battery voltage =====
    else if (line == "SENSE?")
    {
      // (Re)try init if needed
      if (!sht_ok)
      {
        sht_ok = sht4.begin(&Wire);
        if (sht_ok)
        {
          sht4.setPrecision(SHT4X_HIGH_PRECISION);
          sht4.setHeater(SHT4X_NO_HEATER);
        }
      }
      if (!sht_ok)
      {
        Serial.println("ERR code=SHT4X msg=\"not found\"");
        continue;
      }
      
      float tC = NAN, rh = NAN;
      bool got = readSHT(tC, rh);

      if (!got)
      {
        Serial.println("ERR code=SHT4X msg=\"read failed\"");
        continue;
      }
      uint32_t mv = ADC_Read_mV();
      Serial.print("OK T=");
      Serial.print(tC, 2);
      Serial.print("C RH=");
      Serial.print(rh, 2);
      Serial.print("% VBAT=");
      Serial.print(mv);
      Serial.println("mV");
    }
    // ===== VBAT? -> battery voltage only =====
    else if (line == "VBAT?")
    {
      uint32_t mv = ADC_Read_mV();
      Serial.print("OK VBAT=");
      Serial.print(mv);
      Serial.println("mV");
    }
    // ===== INTERVAL? -> current logging interval from NVS =====
    else if (line == "INTERVAL?")
    {
      // Ensure we report the latest value (reload)
      loadIntervalFromNVS();
      Serial.print("OK INTERVAL=");
      Serial.println(sleepSeconds);
    }
    // ===== SETINTERVAL <seconds> -> store in NVS =====
    else if (line.startsWith("SETINTERVAL "))
    {
      String payload = line.substring(12);
      payload.trim();
      if (payload.length() == 0)
      {
        Serial.println("ERR code=BAD_ARG msg=\"use SETINTERVAL <seconds>\"");
        continue;
      }
      uint32_t val = (uint32_t)payload.toInt();
      if (val < 10)
        val = 10;
      if (val > 86400UL)
        val = 86400UL; // clamp to 24h
      if (!storeIntervalToNVS(val))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.print("OK set INTERVAL=");
        Serial.println(val);
      }
    }

    // ===== FLASH-INFO -> full internal flash partition table =====
    else if (line == "FLASH-INFO")
    {
      reportAllFlashPartitions();
    }
    // ===== FLASH-FREE -> quick LittleFS usage with % =====
    else if (line == "FLASH-FREE")
    {
      reportLittleFSQuick();
    }
    // ===== FLASH-USAGE -> show used percentage and status =====
    else if (line == "FLASH-USAGE")
    {
      reportLittleFSUsage();
    }
    // ===== FS-VERBOSE -> exact block (Label/Type/Sub/Address/Size/Encrypted + Mount/Total/Used/Free + %) =====
    else if (line == "FLASH-VERBOSE")
    {
      reportLittleFSVerbose();
    }

    else if (line == "HEARTBEAT?")
    {
      loadHeartbeatFromNVS(); // ensure latest
      Serial.print("OK HEARTBEAT ON=");
      Serial.print(heartbeatOn ? 1 : 0);
      Serial.print(" PERIOD=");
      Serial.println(heartbeatPeriod);
    }
    else if (line == "HEARTBEAT ON")
    {
      if (!storeHeartbeatOnToNVS(true))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.println("OK HEARTBEAT ON");
      }
    }
    else if (line == "HEARTBEAT OFF")
    {
      if (!storeHeartbeatOnToNVS(false))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.println("OK HEARTBEAT OFF");
      }
    }
    else if (line.startsWith("SETHEARTBEAT "))
    {
      String payload = line.substring(strlen("SETHEARTBEAT "));
      payload.trim();
      if (payload.length() == 0)
      {
        Serial.println("ERR code=BAD_ARG msg=\"SETHEARTBEAT <seconds>\"");
      }
      else
      {
        uint32_t sec = (uint32_t)payload.toInt();
        if (!storeHeartbeatPeriodToNVS(sec))
        {
          Serial.println("ERR code=NVS msg=\"write failed\"");
        }
        else
        {
          Serial.print("OK HEARTBEAT PERIOD=");
          Serial.println(heartbeatPeriod);
        }
      }
    }

    // ===== STORAGE? -> current storage mode =====
    else if (line == "STORAGE?")
    {
      loadStorageModeFromNVS(); // ensure latest
      Serial.print("OK STORAGE=");
      Serial.print(storageModeToString(storageMode));
      if (storageMode == STORAGE_AUTO)
      {
        Serial.print(" THRESHOLD=");
        Serial.print(autoThreshold);
        Serial.print("% SWITCHED=");
        Serial.print(autoSwitched ? "YES" : "NO");
      }
      Serial.println();
    }
    // ===== SETSTORAGE INTERNAL|EXTERNAL|AUTO =====
    else if (line.startsWith("SETSTORAGE "))
    {
      String payload = line.substring(strlen("SETSTORAGE "));
      payload.trim();
      payload.toUpperCase();
      
      StorageMode newMode;
      if (payload == "INTERNAL")
      {
        newMode = STORAGE_INTERNAL;
      }
      else if (payload == "EXTERNAL")
      {
        newMode = STORAGE_EXTERNAL;
      }
      else if (payload == "AUTO")
      {
        newMode = STORAGE_AUTO;
      }
      else
      {
        Serial.println("ERR code=BAD_ARG msg=\"use INTERNAL, EXTERNAL, or AUTO\"");
        continue;
      }
      
      if (!storeStorageModeToNVS(newMode))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.print("OK STORAGE=");
        Serial.println(storageModeToString(storageMode));
      }
    }

    // ===== AUTO-THRESHOLD-SET <pct> -> set auto-failover threshold (5-95%) =====
    else if (line.startsWith("AUTO-THRESHOLD-SET "))
    {
      String arg = line.substring(19);
      arg.trim();
      int val = arg.toInt();
      if (val < 5 || val > 95)
      {
        Serial.println("ERR code=BAD_ARG msg=\"threshold must be 5-95%\"");
      }
      else if (!storeAutoThresholdToNVS((uint8_t)val))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.print("OK AUTO-THRESHOLD=");
        Serial.print(val);
        Serial.println("%");
      }
    }

    // ===== AUTO-THRESHOLD? -> show current auto threshold =====
    else if (line == "AUTO-THRESHOLD?" || line == "AUTO-THRESHOLD")
    {
      Serial.print("OK AUTO-THRESHOLD=");
      Serial.print(autoThreshold);
      Serial.println("%");
    }

    // ===== AUTO-STATUS? -> show auto-failover status =====
    else if (line == "AUTO-STATUS?" || line == "AUTO-STATUS")
    {
      Serial.print("OK MODE=");
      Serial.print(storageModeToString(storageMode));
      Serial.print(" THRESHOLD=");
      Serial.print(autoThreshold);
      Serial.print("% SWITCHED=");
      Serial.println(autoSwitched ? "YES" : "NO");
    }

    // ===== AUTO-RESET -> manually reset auto-switched flag =====
    else if (line == "AUTO-RESET")
    {
      if (!storeAutoSwitchedToNVS(false))
      {
        Serial.println("ERR code=NVS msg=\"write failed\"");
      }
      else
      {
        Serial.println("OK AUTO-SWITCHED=NO");
      }
    }

    // ===== EXT-INFO -> external flash chip info + superblock status =====
    else if (line == "EXT-INFO")
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      Serial.println("[EXT-INFO] External SPI Flash");
      Serial.print("Chip ID      : 0x");
      Serial.println(flash.getJEDECID(), HEX);
      Serial.print("Capacity     : ");
      Serial.print(FLASH_BYTES);
      Serial.print(" bytes (");
      Serial.print(FLASH_BYTES / 1024);
      Serial.println(" KB)");
      Serial.print("Sector size  : ");
      Serial.println(SECTOR_SIZE);
      Serial.print("Record size  : ");
      Serial.println(EXT_RECORD_SIZE);
      Serial.print("Data region  : 0x");
      Serial.print(DATA_BEGIN, HEX);
      Serial.print(" - 0x");
      Serial.println(DATA_END - 1, HEX);
      Serial.print("Current tail : 0x");
      Serial.println(g_tail, HEX);
      Serial.print("Superblock seq: ");
      Serial.println(g_seq);
      
      uint32_t capacity = (DATA_END - DATA_BEGIN) / EXT_RECORD_SIZE;
      uint32_t used = (g_tail - DATA_BEGIN) / EXT_RECORD_SIZE;
      Serial.print("Records used : ");
      Serial.print(used);
      Serial.print(" / ");
      Serial.print(capacity);
      Serial.print(" (");
      Serial.print((used * 100) / capacity);
      Serial.println("%)" );
      Serial.println("OK");
    }

    // ===== EXT-DUMP-RAW [addr] [count] -> raw hex dump =====
    else if (line.startsWith("EXT-DUMP-RAW"))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      String rest = "";
      if (line.length() > 12) {
        rest = line.substring(13);
        rest.trim();
      }
      cmdDumpRaw(rest);
    }

    // ===== EXT-DUMP-SECTOR-RAW <N> [count] -> raw hex dump of sector =====
    else if (line.startsWith("EXT-DUMP-SECTOR-RAW "))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      String rest = line.substring(20);
      rest.trim();
      cmdDumpRawSector(rest);
    }

    // ===== EXT-DUMP-SECTOR <N> [count] -> dump decoded records from sector =====
    else if (line.startsWith("EXT-DUMP-SECTOR "))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      String rest = line.substring(16);
      rest.trim();
      cmdDumpSector(rest);
    }

    // ===== EXT-DUMP -> dump all committed records as CSV =====
    else if (line.startsWith("EXT-DUMP"))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      String rest = "";
      if (line.length() > 8) {
        rest = line.substring(9);
        rest.trim();
      }
      
      // Optional count limit
      uint32_t maxRows = 0xFFFFFFFF;
      if (rest.length() > 0) {
        long n = rest.toInt();
        if (n > 0) maxRows = (uint32_t)n;
      }
      
      Serial.println("[EXT-DUMP] Dumping committed records");
      dumpAll();
      Serial.println("OK");
    }

    // ===== EXT-FORMAT <code> -> securely erase and reinitialize external flash =====
    else if (line.startsWith("EXT-FORMAT "))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }

      // Throttle attempts per session (share same counter as internal FORMAT)
      if (formatTries() >= 3)
      {
        Serial.println("ERR code=LOCKED msg=\"too many attempts\"");
        continue;
      }

      String code = line.substring(strlen("EXT-FORMAT "));
      code.trim();

      if (code.isEmpty())
      {
        Serial.println("ERR code=BAD_ARG msg=\"EXT-FORMAT <code>\"");
        continue;
      }

      // Load expected hash (NVS or default)
      uint32_t expected = 0;
      loadFormatCodeHash(expected);

      uint32_t provided = codeHash(code);
      if (provided != expected)
      {
        formatTries()++;
        Serial.println("ERR code=AUTH msg=\"bad_code\"");
        continue;
      }

      // Auth OK -> perform destructive format
      Serial.println("OK EXT-FORMAT START");
      Serial.flush();
      
      if (formatDataRegion()) {
        Serial.println("OK EXT-FORMAT DONE");
      } else {
        Serial.println("ERR code=FORMAT_FAIL msg=\"erase failed\"");
      }
    }

    // ===== EXT-LIST-SECTORS [start] -> list sector usage =====
    else if (line.startsWith("EXT-LIST-SECTORS"))
    {
      if (FLASH_BYTES == 0) {
        Serial.println("ERR code=EXT_FLASH msg=\"not initialized\"");
        continue;
      }
      
      String rest = "";
      if (line.length() > 16) {
        rest = line.substring(17);
        rest.trim();
      }
      cmdListAvailableSectors(rest);
    }

    else
    {
      Serial.println("OK v=1 caps=GET,LIST,CRC,SETTIME,TIME?,SENSE?,VBAT?,INTERVAL?,SETINTERVAL,DEL,REBOOT,FORMAT,SETFMTCODE,FLASH-INFO,FLASH-FREE,FS-VERBOSE,HEARTBEAT?,HEARTBEAT,SETHEARTBEAT,STORAGE?,SETSTORAGE,EXT-INFO,EXT-DUMP,EXT-DUMP-RAW,EXT-DUMP-SECTOR,EXT-DUMP-SECTOR-RAW,EXT-FORMAT,EXT-LIST-SECTORS");
    }
  }
}

// ---------- RTC fail-safe helpers (NVS/FS anchors) ----------

static bool nvsLoadLastEpoch(uint32_t &outEpoch)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/true))
    return false;
  uint32_t v = prefs.getUInt(NVS_KEY_LAST_EPOCH, 0);
  prefs.end();
  if (v == 0)
    return false;
  outEpoch = v;
  return true;
}

static void nvsStoreLastEpoch(uint32_t epoch)
{
  if (!prefs.begin(NVS_NS, /*readOnly=*/false))
    return;
  prefs.putUInt(NVS_KEY_LAST_EPOCH, epoch);
  prefs.end();
}

// Find latest /logs/YYYY-MM-DD folder (ISO order => lexicographic max)
static bool findLatestDateFolder(String &dateOut)
{
  File root = LittleFS.open(ROOT, "r");
  if (!root || !root.isDirectory())
    return false;

  String best = "";
  for (File e = root.openNextFile(); e; e = root.openNextFile())
  {
    if (!e.isDirectory())
      continue;
    String full = e.name();
    String d = full;
    if (full.startsWith(String(ROOT) + "/"))
      d = full.substring(strlen(ROOT) + 1);
    if (d.endsWith("/"))
      d.remove(d.length() - 1);
    if (d.length() == 10 && d.charAt(4) == '-' && d.charAt(7) == '-')
    {
      if (best.isEmpty() || d > best)
        best = d;
    }
  }
  if (best.isEmpty())
    return false;
  dateOut = best;
  return true;
}

// Read last data line's time "HH:MM:SS" from a CSV (tail scan)
static bool readLastTimeFromCsv(const String &csvPath, String &hhmmssOut)
{
  File f = LittleFS.open(csvPath, "r");
  if (!f)
    return false;
  size_t sz = f.size();
  if (sz == 0)
  {
    f.close();
    return false;
  }

  const size_t TAIL = sz > 2048 ? 2048 : sz; // 2KB tail for safety
  size_t offset = sz - TAIL;
  f.seek(offset, SeekSet);

  static char buf[2049];
  int n = f.read((uint8_t *)buf, TAIL);
  f.close();
  if (n <= 0)
    return false;
  buf[n] = '\0';

  // Find last non-empty line
  int end = n - 1;
  while (end >= 0 && (buf[end] == '\n' || buf[end] == '\r'))
    end--;
  if (end < 0)
    return false;

  int start = end;
  while (start >= 0 && buf[start] != '\n' && buf[start] != '\r')
    start--;
  start++;

  String lastLine = String(buf + start, (end - start + 1));
  lastLine.trim();
  if (lastLine.length() == 0)
    return false;

  // If it’s the header, step to previous line
  if (lastLine.startsWith("time,"))
  {
    int prevEnd = start - 2;
    while (prevEnd >= 0 && (buf[prevEnd] == '\n' || buf[prevEnd] == '\r'))
      prevEnd--;
    if (prevEnd < 0)
      return false;
    int prevStart = prevEnd;
    while (prevStart >= 0 && buf[prevStart] != '\n' && buf[prevStart] != '\r')
      prevStart--;
    prevStart++;
    lastLine = String(buf + prevStart, (prevEnd - prevStart + 1));
    lastLine.trim();
    if (lastLine.length() == 0)
      return false;
  }

  int comma = lastLine.indexOf(',');
  if (comma < 0)
    return false;
  String t = lastLine.substring(0, comma);
  t.trim();

  if (t.length() != 8 || t.charAt(2) != ':' || t.charAt(5) != ':')
    return false;
  for (uint8_t i : {0, 1, 3, 4, 6, 7})
    if (!isDigit(t[i]))
      return false;

  hhmmssOut = t;
  return true;
}

// static bool buildDateTimeFromStrings(const String &date, const String &time, RTCDateTime &out)
// {
//   if (date.length() != 10 || date.charAt(4) != '-' || date.charAt(7) != '-')
//     return false;
//   if (time.length() != 8 || time.charAt(2) != ':' || time.charAt(5) != ':')
//     return false;

//   int yr = date.substring(0, 4).toInt();
//   int mo = date.substring(5, 7).toInt();
//   int dy = date.substring(8, 10).toInt();
//   int hh = time.substring(0, 2).toInt();
//   int mm = time.substring(3, 5).toInt();
//   int ss = time.substring(6, 8).toInt();

//   if (yr < 2000 || yr > 2099)
//     return false;
//   if (mo < 1 || mo > 12)
//     return false;
//   if (dy < 1 || dy > 31)
//     return false;
//   if (hh < 0 || hh > 23)
//     return false;
//   if (mm < 0 || mm > 59)
//     return false;
//   if (ss < 0 || ss > 59)
//     return false;

//   out = RTCDateTime(yr, mo, dy, hh, mm, ss);
//   return true;
// }

// Read last 7-byte record's seconds-since-midnight
static bool readLastSecondsFromBin(const String &binPath, uint32_t &secOut)
{
  File f = LittleFS.open(binPath, "r");
  if (!f)
    return false;
  size_t sz = f.size();
  if (sz < RECORD_SIZE)
  {
    f.close();
    return false;
  }
  size_t off = sz - RECORD_SIZE;
  f.seek(off, SeekSet);
  uint8_t buf[RECORD_SIZE];
  int n = f.read((uint8_t *)buf, RECORD_SIZE);
  f.close();
  if (n != (int)RECORD_SIZE)
    return false;

  // parse uint24_le at buf[0..2]
  uint32_t s = (uint32_t)buf[0] | ((uint32_t)buf[1] << 8) | ((uint32_t)buf[2] << 16);
  if (s > 86399UL)
    s = 86399UL;
  secOut = s;
  return true;
}

// Convert TOD index (0..255) to HH:MM:SS string (≈5.625 min steps)
static String todToHHMMSS(uint8_t tod)
{
  // seconds ≈ round( tod * 86400 / 255 )
  uint32_t s = (uint32_t)lround((double)tod * 86400.0 / 255.0);
  if (s > 86399)
    s = 86399;
  uint32_t hh = s / 3600;
  s %= 3600;
  uint32_t mm = s / 60;
  s %= 60;
  uint32_t ss = s;
  char buf[9];
  snprintf(buf, sizeof(buf), "%02lu:%02lu:%02lu", (unsigned long)hh, (unsigned long)mm, (unsigned long)ss);
  return String(buf);
}

static uint32_t ymdhms_to_unix(int yr, int mo, int dy, int hh, int mm, int ss)
{
  int64_t days = days_from_civil(yr, (unsigned)mo, (unsigned)dy);
  int64_t sec = days * 86400 + (int64_t)hh * 3600 + (int64_t)mm * 60 + ss;
  if (sec < 0)
    sec = 0;
  return (uint32_t)sec;
}

static uint32_t compileTimeUnix()
{
  // __DATE__ = "Mmm dd yyyy", __TIME__ = "HH:MM:SS"
  static const char *months[] = {
      "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
  char mmm[4];
  mmm[3] = 0;
  int dd, yyyy, hh, mm, ss;
  sscanf(__DATE__, "%3s %d %d", mmm, &dd, &yyyy);
  sscanf(__TIME__, "%d:%d:%d", &hh, &mm, &ss);
  int mo = 1;
  for (int i = 0; i < 12; ++i)
    if (strcmp(mmm, months[i]) == 0)
    {
      mo = i + 1;
      break;
    }
  return ymdhms_to_unix(yyyy, mo, dd, hh, mm, ss);
}

// ---------- New version (returns UNIX epoch) ----------
// Decide "now" when RTC is bad using NVS anchor, else FS, else compile time.
// src returns one of: "RTC","NVS","FS","COMP"
static uint32_t computeEstimatedNow(uint32_t sleepSeconds, String &src)
{
  if (rtc_ok)
  {
    rtc.updateTime();
    src = "RTC";
    return rtc.getUNIX();
  }

  uint32_t lastEp = 0;
  if (nvsLoadLastEpoch(lastEp))
  {
    src = "NVS";
    return lastEp + sleepSeconds;
  }

  // FS fallback (prefer BIN)
  String latestDate;
  if (findLatestDateFolder(latestDate))
  {
    String binPath = buildBinPath(latestDate);
    uint32_t lastS = 0;
    if (binPath.length() && readLastSecondsFromBin(binPath, lastS))
    {
      // latestDate = "YYYY-MM-DD"
      int yr = latestDate.substring(0, 4).toInt();
      int mo = latestDate.substring(5, 7).toInt();
      int dy = latestDate.substring(8, 10).toInt();
      uint32_t base = ymdhms_to_unix(yr, mo, dy, 0, 0, 0);
      src = "FS";
      return base + lastS + sleepSeconds;
    }
  }

  src = "COMP";
  return compileTimeUnix() + sleepSeconds;
}

// ===================================================
// ==================  LOGGING MODE  =================
// ===================================================
void runLoggingMode()
{
  esp_sleep_wakeup_cause_t wakeup_reason = esp_sleep_get_wakeup_cause();
  Serial.println("\n==============================");
  if (wakeup_reason == ESP_SLEEP_WAKEUP_TIMER)
  {
    Serial.println("🔋 Woke up from deep sleep (timer).");
  }
  else
  {
    Serial.println("⚡ First boot or external reset.");
  }
  Serial.printf("Wake count: %lu\n", (unsigned long)++wakeCount);

  blinkOnce(150);

  // Load schedules
  loadIntervalFromNVS();
  loadHeartbeatFromNVS();
  loadStorageModeFromNVS();

  Serial.print("⏲️ Logging interval (s): ");
  Serial.println(sleepSeconds);
  Serial.print("💡 Heartbeat: ");
  Serial.print(heartbeatOn ? "ON" : "OFF");
  Serial.print(" / period(s)=");
  Serial.println(heartbeatPeriod);
  Serial.print("💾 Storage mode: ");
  Serial.print(storageModeToString(storageMode));
  if (storageMode == STORAGE_AUTO)
  {
    Serial.print(" (threshold=");
    Serial.print(autoThreshold);
    Serial.print("%, switched=");
    Serial.print(autoSwitched ? "YES" : "NO");
    Serial.print(")");
  }
  Serial.println();

  // Initialize countdown on first ever wake
  if (!hbInitFlag)
  {
    hbSecondsUntilNextLog = 0; // log immediately on first run
    hbLastSleepPlanned = 0;
    hbInitFlag = 1;
  }
  else
  {
    // Decrease the countdown by how long we intentionally slept last time
    if (heartbeatOn && hbLastSleepPlanned > 0)
    {
      if (hbSecondsUntilNextLog > hbLastSleepPlanned)
        hbSecondsUntilNextLog -= hbLastSleepPlanned;
      else
        hbSecondsUntilNextLog = 0;
    }
  }

  // ===== AUTO MODE: Check internal flash usage and auto-switch if needed =====
  StorageMode actualStorageMode = storageMode;
  if (storageMode == STORAGE_AUTO)
  {
    if (!autoSwitched)
    {
      // Check internal flash usage
      size_t totalBytes = LittleFS.totalBytes();
      size_t usedBytes = LittleFS.usedBytes();
      float usedPct = (totalBytes > 0) ? (100.0f * usedBytes / totalBytes) : 0.0f;
      Serial.printf("📊 Internal flash: %.1f%% used (%u/%u bytes)\n", usedPct, usedBytes, totalBytes);
      
      if (usedPct >= autoThreshold)
      {
        Serial.printf("⚠️ Internal flash ≥%d%% threshold → switching to EXTERNAL\n", autoThreshold);
        storeAutoSwitchedToNVS(true);
        actualStorageMode = STORAGE_EXTERNAL;
      }
      else
      {
        Serial.printf("✓ Internal flash below %d%% threshold → using INTERNAL\n", autoThreshold);
        actualStorageMode = STORAGE_INTERNAL;
      }
    }
    else
    {
      Serial.println("🔄 Already auto-switched → using EXTERNAL");
      actualStorageMode = STORAGE_EXTERNAL;
    }
  }
  else if (storageMode == STORAGE_EXTERNAL)
  {
    actualStorageMode = STORAGE_EXTERNAL;
  }
  else
  {
    actualStorageMode = STORAGE_INTERNAL;
  }

  // Initialize external flash if needed (EXTERNAL or AUTO-switched)
  bool extFlashReady = false;
  if (actualStorageMode == STORAGE_EXTERNAL)
  {
    if (flash.begin())
    {
      FLASH_BYTES = flash.getCapacity();
      DATA_END = FLASH_BYTES;
      if (!loadSuperAndSetTail())
      {
        findTailLinear();
      }
      Serial.print("External flash ready, tail=0x");
      Serial.println(g_tail, HEX);
      extFlashReady = true;
    }
    else
    {
      Serial.println("⚠️ External flash init failed, cannot log!");
      extFlashReady = false;
    }
  }

  // Decide whether this wake is for LOGGING or just HEARTBEAT
  bool doLog = (!heartbeatOn) || (hbSecondsUntilNextLog == 0);

  if (doLog)
  {
    // ===== Full logging wake =====
    bool dev_ok = initI2CAndDevices();

    // Decide "now": RTC if available, else NVS/FS/COMP fallback
    String timeSource;
    uint32_t nowEpoch = computeEstimatedNow(sleepSeconds, timeSource);

    String d, t;

    if (rtc_ok)
    {
      rtc.updateTime();
      int y = rtc.getYear(); // some libs return 0..99
      if (y < 100)
        y += 2000; // normalize

      int mo = rtc.getMonth();
      int dy = rtc.getDate();
      char dbuf[11], tbuf[9];

      // Build ISO date/time strings
      snprintf(dbuf, sizeof(dbuf), "%04d-%02d-%02d", y, mo, dy); // <-- YYYY-MM-DD
      snprintf(tbuf, sizeof(tbuf), "%02d:%02d:%02d",
               rtc.getHours(), rtc.getMinutes(), rtc.getSeconds());

      d = dbuf;
      t = tbuf;
    }
    else
    {
      // RTCDateTime tmp;
      // unixToRTC(nowEpoch, tmp);
      // d = fmtDate(tmp); // already YYYY-MM-DD
      // t = fmtTime(tmp);
    }

    // (optional) keep the epoch around for logs too
    Serial.print("⏱️ Time source: ");
    Serial.println(timeSource);
    Serial.print("DATE=");
    Serial.print(d);
    Serial.print(" TIME=");
    Serial.print(t);
    Serial.print(" UNIX=");
    Serial.println(nowEpoch);

    // BIN-only logging (no CSV)
    float tC = NAN, rh = NAN;
    bool got = dev_ok && readSHT(tC, rh);
    if (!got)
    {
      Serial.println("❌ SHT4x read failed! Using placeholders.");
    }

    uint32_t vbat_mV = ADC_Read_mV();  //Read Bt Voltage

    // Branch based on actual storage mode (resolved from AUTO if needed)
    if (actualStorageMode == STORAGE_INTERNAL)
    {
      // ===== INTERNAL FLASH (LittleFS) =====
      String binPath;
      if (!ensureTodayTargetsBin(d, binPath))
      {
        Serial.println("⚠️ ensureTodayTargetsBin failed");
      }
      else
      {
        // Build new 7-byte record (S[0..2], t_dC[3..4], RH[5], VBAT[6])
        uint8_t rec[RECORD_SIZE];

        // --- seconds since midnight (0..86399) ---
        uint32_t t_sec;
        if (rtc_ok)
        {
          rtc.updateTime();
          t_sec = (uint32_t)rtc.getHours() * 3600UL + (uint32_t)rtc.getMinutes() * 60UL + (uint32_t)rtc.getSeconds();
        }
        else
        {
          // from epoch we computed earlier
          t_sec = nowEpoch % 86400UL;
        }
        if (t_sec > 86399UL)
          t_sec = 86399UL;
        pack_u24le(t_sec, &rec[0]);

        // --- temperature in deci-°C (signed) ---
        int16_t t_dC = (int16_t)lroundf((isnan(tC) ? 0.0f : tC) * 10.0f);
        pack_i16le(t_dC, &rec[3]);

        // --- humidity (encoded) ---
        rec[5] = enc_rh(isnan(rh) ? 0.0f : rh);

        // --- VBAT (encoded 2.50–4.50 V) ---
        rec[6] = enc_vbat(vbat_mV);

        // --- write record ---
        if (!appendBinRecord(binPath, rec))
        {
          Serial.println("❌ BIN append failed");
        }
        else
        {
          Serial.print("BIN+ Logged 7B → ");
          Serial.println(binPath);
          nvsStoreLastEpoch(nowEpoch);
        }
      }
    }
    else // STORAGE_EXTERNAL or STORAGE_AUTO (actualStorageMode == STORAGE_EXTERNAL)
    {
      // ===== EXTERNAL FLASH (SPIMemory) =====
      // Check external flash capacity before writing
      bool canWrite = false;
      if (extFlashReady)
      {
        // Calculate used sectors (data region starts at sector 2)
        uint32_t usedSectors = (g_tail >= (2 * SECTOR_SIZE)) ? ((g_tail / SECTOR_SIZE) - 1) : 0;
        uint32_t totalSectors = FLASH_BYTES / SECTOR_SIZE;
        uint32_t dataSectors = (totalSectors > 2) ? (totalSectors - 2) : 0; // subtract superblock sectors 0-1
        float usedPct = (dataSectors > 0) ? (100.0f * usedSectors / dataSectors) : 100.0f;
        Serial.printf("📊 External flash: %.1f%% used (%lu/%lu data sectors)\n", usedPct, usedSectors, dataSectors);
        
        if (usedSectors < dataSectors)
        {
          canWrite = true;
        }
        else
        {
          Serial.println("🚨 EXTERNAL FLASH FULL! CANNOT LOG!");
          emergencyBlink(10);
          canWrite = false;
        }
      }
      else
      {
        Serial.println("🚨 EXTERNAL FLASH NOT READY! CANNOT LOG!");
        emergencyBlink(10);
        canWrite = false;
      }

      if (canWrite)
      {
        // Convert to 10x temperature for external flash format
        int16_t temp10 = (int16_t)lroundf((isnan(tC) ? 0.0f : tC) * 10.0f);
        uint8_t rh_byte = enc_rh(isnan(rh) ? 0.0f : rh);

        if (appendOne(nowEpoch, temp10, rh_byte, vbat_mV))
        {
          Serial.print("EXT+ Logged 8B → UNIX=");
          Serial.print(nowEpoch);
          Serial.print(" T=");
          Serial.print(tC, 1);
          Serial.print("C RH=");
          Serial.print(rh, 1);
          Serial.print("% VBAT=");
          Serial.print(vbat_mV);
          Serial.println("mV");
          nvsStoreLastEpoch(nowEpoch);
        }
        else
        {
          Serial.println("❌ External flash append failed");
        }
      }
      else
      {
        Serial.println("⚠️ Skipping log due to external flash capacity issue");
      }
    }

    // Tidy I2C & GPIOs before sleep
    Wire.end();
    pinMode(SDA_PIN, INPUT);
    pinMode(SCL_PIN, INPUT);
    digitalWrite(LED_PIN, LOW);

    // Reset countdown to next full log
    hbSecondsUntilNextLog = sleepSeconds;

    // Tiny visual ack we logged
    blinkOnce(120);
  }
  else
  {
    // ===== Heartbeat-only wake =====
    blinkTwice(100, 150);
  }

  // Plan next sleep
  uint32_t nextSleepSec;
  if (heartbeatOn)
  {
    // Wake either for the next heartbeat or next log, whichever is sooner
    uint32_t toLog = hbSecondsUntilNextLog;
    uint32_t toBeat = heartbeatPeriod;
    if (toLog == 0)
      toLog = sleepSeconds; // just in case
    nextSleepSec = (toLog < toBeat) ? toLog : toBeat;
  }
  else
  {
    // No heartbeat → sleep exactly the logging interval
    nextSleepSec = sleepSeconds;
  }

  if (nextSleepSec == 0)
    nextSleepSec = 1; // avoid zero-sleep corner case

  hbLastSleepPlanned = nextSleepSec;

  // Go to deep sleep
  esp_sleep_enable_timer_wakeup((uint64_t)nextSleepSec * 1000000ULL);
  Serial.print("🌙 Going to deep sleep for ");
  Serial.print(nextSleepSec);
  Serial.println(" s...");
  Serial.flush();
  delay(20);
  esp_deep_sleep_start();
}

static void ensureNVSNamespaceSeeded()
{
  // Try RO first; if it fails, create + seed in RW mode.
  if (!prefs.begin(NVS_NS, /*readOnly=*/true))
  {
    // Create namespace and seed defaults
    if (prefs.begin(NVS_NS, /*readOnly=*/false))
    {
      // Put your defaults
      prefs.putUInt(NVS_KEY_INTERVAL, DEFAULT_SLEEP_SECONDS);
      prefs.putUChar(NVS_KEY_HB_ON, 0);
      prefs.putUInt(NVS_KEY_HB_PER, DEFAULT_HB_PERIOD);

      // If you use a format code hash, seed it too:
      uint32_t hv = codeHash(String(DEFAULT_FORMAT_CODE));
      prefs.putUInt(NVS_KEY_FMT_HASH, hv);

      prefs.end();
    }
  }
  else
  {
    prefs.end(); // close RO
  }
}

// ===================================================
// ================= Arduino entry ===================
// ===================================================
void setup()
{
  pinMode(MODE_BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  // SPI flash
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);

  Serial.begin(115200);
  delay(100);

  // Mount FS (your improved block)
  if (!LittleFS.begin(false))
  {
    Serial.println("⚠️ LittleFS mount failed. Attempting to format...");
    if (!LittleFS.begin(true))
    {
      Serial.println("❌ LittleFS mount/format failed. Retrieval/logging to FS disabled.");
    }
    else
    {
      Serial.println("✅ LittleFS formatted and mounted successfully.");
      LittleFS.mkdir("/logs");
    }
  }
  else if (!LittleFS.exists("/logs"))
  {
    LittleFS.mkdir("/logs");
  }

  // <<< NEW: make sure the NVS namespace exists and has defaults
  ensureNVSNamespaceSeeded();

  // Boot-time mode select (hold MODE button low to enter server)
  if (digitalRead(MODE_BTN_PIN) == LOW)
  {
    uint32_t t0 = millis();
    while (digitalRead(MODE_BTN_PIN) == LOW && millis() - t0 < 1000)
    {
      delay(5);
    }
    if (millis() - t0 >= 400)
    {
      runDataRetrievalMode(); // never returns
    }
  }

  runLoggingMode(); // deep-sleeps at end
}

void loop() {}
