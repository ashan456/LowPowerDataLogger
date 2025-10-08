#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ESP32 DataLogger Command Line Interface
Complete serial control for ESP32 H2 Temperature & Humidity Logger
Supports all firmware features including RTC, sensors, NVS config, storage management

USAGE:
  Auto-discovery (easiest):
    python datalogger_cli.py --list
    python datalogger_cli.py --sense
  
  With specific port:
    python datalogger_cli.py --port COM19 --list
    python datalogger_cli.py --port /dev/ttyUSB0 --sense
  
  Environment variables (avoid repeating port/baud):
    set LOGGER_PORT=COM19       (Windows)
    export LOGGER_PORT=/dev/ttyUSB0  (Linux/Mac)
    
    Then just use:
    python datalogger_cli.py --list
"""

import argparse
import sys
import time
import os
import re
import binascii
import datetime
from typing import Optional, Tuple, List
import serial
from serial.tools import list_ports

# ============================================================================
# REGEX PATTERNS
# ============================================================================
OK_RE = re.compile(r'^OK(?:\s+(.+))?$')

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()

ERR_RE = re.compile(r'^ERR\s+(.+)')


# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()

STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}')


# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()

STAMP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}')

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()


VBAT_RE = re.compile(r'^OK\s+VBAT=(\d+)mV')

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()

INTERVAL_RE = re.compile(r'^OK\s+INTERVAL=(\d+)')

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)
SETINTERVAL_RE = re.compile(r'^OK\s+set\s+INTERVAL=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)
DEL_RE = re.compile(
    r'^OK\s+DEL\s+DATE=([^\s]+)\s+FILES=(\d+)\s+BYTES=(\d+)\s+SIZE_KB=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
,
    re.IGNORECASE
)
REBOOT_RE = re.compile(r'^OK\s+REBOOT\s+REASON=(.+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)
FORMAT_RE = re.compile(r'^OK\s+FORMAT\s+DONE\s+TOTAL=(\d+)\s+USED=(\d+)\s+FREE=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)
DATE_INFO_RE = re.compile(r'^DATE=([^\s]+)\s+SIZE_KB=(\d+)\s+ROWS=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
)
HEARTBEAT_RE = re.compile(r'^OK\s+HEARTBEAT\s+ON=([01])\s+PERIOD=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)
HEARTBEAT_PERIOD_RE = re.compile(r'^OK\s+HEARTBEAT\s+PERIOD=(\d+)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
, re.IGNORECASE)

# ============================================================================
# SERIAL PORT UTILITIES
# ============================================================================
def open_port_auto(preferred: Optional[str], baud: int, timeout: float, 
                   discover: bool, reset: bool) -> serial.Serial:
    """Open serial port with auto-discovery support"""
    if preferred:
        try:
            ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
            if reset:
                ser.dtr = False
                ser.rts = False
                time.sleep(0.1)
                ser.dtr = True
                ser.rts = True
                time.sleep(0.1)
                ser.dtr = False
                ser.rts = False
                time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except Exception as e:
            raise RuntimeError(f"Failed to open {preferred}: {e}")

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    print("Auto-discovering device...", file=sys.stderr)
    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False
            ser.rts = False
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                print(f"Found device on {p.device}", file=sys.stderr)
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    """Send line with newline terminator"""
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    """Read line with timeout, returns None on timeout"""
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < timeout:
        b = ser.read(1)
        if not b:
            continue
        if b == b'\n':
            return buf.decode(errors='ignore').rstrip('\r')
        if b != b'\r':
            buf += b
    return None

def parse_ok_kv(s: str) -> dict:
    """Parse key=value pairs from OK response"""
    out = {}
    if not s:
        return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

# ============================================================================
# FILE DOWNLOAD/UPLOAD UTILITIES
# ============================================================================
def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
    """Receive exact number of bytes with optional progress display"""
    data = bytearray()
    last_pct = -1
    while len(data) < n:
        chunk = ser.read(min(4096, n - len(data)))
        if not chunk:
            continue
        data += chunk
        if progress:
            pct = int(100 * len(data) / n)
            if pct != last_pct:
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", 
                      end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

# ============================================================================
# LISTING & INFORMATION COMMANDS
# ============================================================================
def request_list(ser: serial.Serial, verbose: bool = False) -> List[dict]:
    """List available date logs with size and row count"""
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        
        # Parse: DATE=YYYY-MM-DD SIZE_KB=<int> ROWS=<int>
        m = DATE_INFO_RE.match(line.strip())
        if m:
            dates.append({
                'date': m.group(1),
                'size_kb': int(m.group(2)),
                'rows': int(m.group(3))
            })
        else:
            # Fallback for older format
            dates.append({'date': line.strip(), 'size_kb': 0, 'rows': 0})
    
    return dates

def query_flash_info(ser: serial.Serial):
    """Display all flash partition information"""
    send_line(ser, "FLASH-INFO")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        # Look for section dividers to know when done
        if "======" in line:
            # Read a few more lines after last divider
            for _ in range(5):
                line = read_line(ser, 1.0)
                if line and line.strip():
                    print(line)

def query_flash_free(ser: serial.Serial):
    """Display quick LittleFS usage summary"""
    send_line(ser, "FLASH-FREE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

def query_flash_verbose(ser: serial.Serial):
    """Display detailed LittleFS partition and usage"""
    send_line(ser, "FLASH-VERBOSE")
    print("\n" + "="*70)
    while True:
        line = read_line(ser, 3.0)
        if line is None:
            break
        if line.strip() == "":
            continue
        print(line)
        if "Free %" in line:
            # Read one more line if available
            line = read_line(ser, 0.5)
            if line and line.strip():
                print(line)
            break

# ============================================================================
# FILE DOWNLOAD
# ============================================================================
def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
    """Download a specific date's CSV file"""
    send_line(ser, f"GET {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("No response to GET")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"Unexpected header: {line}")
    
    kv = parse_ok_kv(m.group(1))
    size = int(kv.get("SIZE", "-1"))
    if size < 0:
        raise RuntimeError("Missing SIZE in response")

    # Expect DATA marker
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Download file content
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END marker
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
    if line != "END":
        raise RuntimeError(f"Expected END, got: {line}")

    # Verify CRC32 if provided
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(
                f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}"
            )

    # Success - rename temp file
    os.replace(tmp, outpath)

# ============================================================================
# RTC COMMANDS
# ============================================================================
def query_time(ser: serial.Serial) -> str:
    """Query current RTC time"""
    send_line(ser, "TIME?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("TIME?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"TIME?: Unexpected: {line}")
    
    val = (m.group(1) or "").strip()
    if not STAMP_RE.match(val) and line.upper().startswith("OK "):
        val = line[3:].strip()
    if not STAMP_RE.match(val):
        raise RuntimeError(f"TIME?: Bad format: {line}")
    return val

def set_time(ser: serial.Serial, stamp: str) -> str:
    """Set RTC time"""
    send_line(ser, f"SETTIME {stamp}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETTIME: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = OK_RE.match(line)
    if not m:
        raise RuntimeError(f"SETTIME: Unexpected: {line}")
    return (m.group(1) or "").strip()

def normalize_set_stamp(arg: Optional[str]) -> str:
    """Normalize timestamp argument (now or YYYY-MM-DD HH:MM:SS)"""
    if arg is None or arg.strip().lower() == "now":
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")
    s = arg.strip()
    if not STAMP_RE.match(s):
        raise ValueError("Use 'YYYY-MM-DD HH:MM:SS' or 'now'")
    return s

# ============================================================================
# SENSOR COMMANDS
# ============================================================================
def query_sense(ser: serial.Serial) -> Tuple[float, float, Optional[int]]:
    """Query temperature, humidity, and optionally battery voltage"""
    send_line(ser, "SENSE?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SENSE?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SENSE_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"SENSE?: Unexpected: {line}")
    
    t = float(m.group(1))
    rh = float(m.group(2))
    vbat = int(m.group(3)) if m.group(3) is not None else None
    return t, rh, vbat

def query_vbat(ser: serial.Serial) -> int:
    """Query battery voltage in millivolts"""
    send_line(ser, "VBAT?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("VBAT?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = VBAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"VBAT?: Unexpected: {line}")
    return int(m.group(1))

# ============================================================================
# NVS/INTERVAL COMMANDS
# ============================================================================
def query_interval(ser: serial.Serial) -> int:
    """Query logging interval in seconds"""
    send_line(ser, "INTERVAL?")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("INTERVAL?: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = INTERVAL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"INTERVAL?: Unexpected: {line}")
    return int(m.group(1))

def set_interval(ser: serial.Serial, seconds: int) -> int:
    """Set logging interval in seconds"""
    send_line(ser, f"SETINTERVAL {int(seconds)}")
    line = read_line(ser, 2.5)
    if not line:
        raise RuntimeError("SETINTERVAL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = SETINTERVAL_RE.match(line.strip())
    if not m:
        m2 = INTERVAL_RE.match(line.strip())
        if not m2:
            raise RuntimeError(f"SETINTERVAL: Unexpected: {line}")
        return int(m2.group(1))
    return int(m.group(1))

# ============================================================================
# STORAGE MANAGEMENT
# ============================================================================
def delete_date(ser: serial.Serial, date: str) -> dict:
    """Delete a date's log folder and files"""
    send_line(ser, f"DEL {date}")
    line = read_line(ser, 3.0)
    if not line:
        raise RuntimeError("DEL: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = DEL_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"DEL: Unexpected: {line}")
    
    return {
        'date': m.group(1),
        'files': int(m.group(2)),
        'bytes': int(m.group(3)),
        'size_kb': int(m.group(4))
    }

def format_storage(ser: serial.Serial, code: str) -> dict:
    """Format LittleFS storage (requires security code)"""
    send_line(ser, f"FORMAT {code}")
    
    # Read start acknowledgment
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("FORMAT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK FORMAT START":
        raise RuntimeError(f"FORMAT: Unexpected start: {line}")
    
    print("Formatting storage...", file=sys.stderr)
    
    # Read completion
    line = read_line(ser, 10.0)  # Format can take time
    if not line:
        raise RuntimeError("FORMAT: Timeout waiting for completion")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = FORMAT_RE.match(line.strip())
    if not m:
        raise RuntimeError(f"FORMAT: Unexpected completion: {line}")
    
    return {
        'total': int(m.group(1)),
        'used': int(m.group(2)),
        'free': int(m.group(3))
    }

def set_format_code(ser: serial.Serial, old_code: str, new_code: str):
    """Change the storage format security code"""
    send_line(ser, f"SETFMTCODE {old_code} {new_code}")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("SETFMTCODE: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    if line != "OK SETFMTCODE":
        raise RuntimeError(f"SETFMTCODE: Unexpected: {line}")

# ============================================================================
# SYSTEM COMMANDS
# ============================================================================
def reboot_device(ser: serial.Serial):
    """Reboot the device"""
    send_line(ser, "REBOOT")
    line = read_line(ser, 2.0)
    if not line:
        raise RuntimeError("REBOOT: No response")
    if line.upper().startswith("ERR"):
        raise RuntimeError(line)
    
    m = REBOOT_RE.match(line.strip())
    if m:
        reason = m.group(1)
        return reason
    return None

# ============================================================================
# MAIN CLI
# ============================================================================
def main():
    # Check for environment variables (convenience feature)
    env_port = os.environ.get('LOGGER_PORT')
    env_baud = os.environ.get('LOGGER_BAUD', '115200')
    
    ap = argparse.ArgumentParser(
        description="ESP32 DataLogger CLI - Complete serial control interface",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Auto-discovery (no port needed, finds device automatically)
  %(prog)s --list
  %(prog)s --sense
  %(prog)s --date 2025-10-07
  
  # With specific port
  %(prog)s --port COM19 --list
  %(prog)s --port /dev/ttyUSB0 --time
  
  # Use environment variables (set once, use everywhere)
  Windows:
    set LOGGER_PORT=COM19
    set LOGGER_BAUD=115200
  Linux/Mac:
    export LOGGER_PORT=/dev/ttyUSB0
    export LOGGER_BAUD=115200
  Then:
    %(prog)s --list
    %(prog)s --sense
  
  # Download specific date
  %(prog)s --date 2025-10-07 --out ./data/log.csv
  
  # Set time to now
  %(prog)s --settime now
  
  # Query sensor readings
  %(prog)s --sense
  
  # Set logging interval to 5 minutes
  %(prog)s --setinterval 300
  
  # Delete old logs
  %(prog)s --delete 2025-10-01
  
  # Format storage (requires code)
  %(prog)s --format 246810
  
  # View storage information
  %(prog)s --flash-info
  
Environment Variables:
  LOGGER_PORT   - Default serial port (e.g., COM19 or /dev/ttyUSB0)
  LOGGER_BAUD   - Default baud rate (default: 115200)
        """
    )
    
    # Connection options
    conn = ap.add_argument_group('Connection Options')
    conn.add_argument("--port", default=env_port,
                      help=f"Serial port (default: {env_port or 'auto-discover'})")
    conn.add_argument("--baud", type=int, default=int(env_baud),
                      help=f"Baud rate (default: {env_baud})")
    conn.add_argument("--discover", action="store_true", default=True,
                      help="Auto-discover port if --port not given (default: enabled)")
    conn.add_argument("--no-discover", dest="discover", action="store_false",
                      help="Disable auto-discovery (requires --port)")
    conn.add_argument("--reset", action="store_true",
                      help="Toggle DTR/RTS to reset device before connecting")
    conn.add_argument("--no-progress", action="store_true",
                      help="Disable progress display during downloads")

    # Data retrieval
    data = ap.add_argument_group('Data Retrieval')
    data.add_argument("--list", action="store_true",
                      help="List available date logs with sizes and row counts")
    data.add_argument("--date", metavar="YYYY-MM-DD",
                      help="Download CSV for specific date")
    data.add_argument("--out", metavar="PATH",
                      help="Output CSV path (default: ./logs/<date>.csv)")

    # RTC management
    rtc = ap.add_argument_group('RTC (Real-Time Clock)')
    rtc.add_argument("--time", action="store_true",
                     help="Query current RTC time")
    rtc.add_argument("--settime", nargs='?', const="now", metavar="TIMESTAMP",
                     help="Set RTC time: 'now' or 'YYYY-MM-DD HH:MM:SS'")

    # Sensor readings
    sensor = ap.add_argument_group('Sensor Readings')
    sensor.add_argument("--sense", action="store_true",
                        help="Query temperature, humidity (and battery if supported)")
    sensor.add_argument("--vbat", action="store_true",
                        help="Query battery voltage in millivolts")

    # Configuration
    config = ap.add_argument_group('Configuration (NVS)')
    config.add_argument("--interval", action="store_true",
                        help="Query current logging interval (seconds)")
    config.add_argument("--setinterval", type=int, metavar="SECONDS",
                        help="Set logging interval (30-86400 seconds)")

    # Storage management
    storage = ap.add_argument_group('Storage Management')
    storage.add_argument("--delete", metavar="YYYY-MM-DD",
                         help="Delete date log folder and all files")
    storage.add_argument("--format", metavar="CODE",
                         help="Format storage (REQUIRES SECURITY CODE)")
    storage.add_argument("--setfmtcode", nargs=2, metavar=("OLD", "NEW"),
                         help="Change format security code: <old_code> <new_code>")
    storage.add_argument("--flash-info", action="store_true",
                         help="Display all flash partition information")
    storage.add_argument("--flash-free", action="store_true",
                         help="Display quick LittleFS usage summary")
    storage.add_argument("--flash-verbose", action="store_true",
                         help="Display detailed LittleFS partition and usage")

    # System
    system = ap.add_argument_group('System Control')
    system.add_argument("--reboot", action="store_true",
                        help="Reboot the device")

    args = ap.parse_args()

    # Require at least one action
    if not any([
        args.list, args.date, args.time, args.settime is not None,
        args.sense, args.vbat, args.interval, args.setinterval is not None,
        args.delete, args.format, args.setfmtcode, args.reboot,
        args.flash_info, args.flash_free, args.flash_verbose
    ]):
        ap.error("No action specified. Use -h for help.")

    # Open serial connection
    try:
        ser = open_port_auto(
            args.port, args.baud, timeout=0.2,
            discover=args.discover, reset=args.reset
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    # Send HELLO handshake
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        if not hello or not hello.upper().startswith("OK"):
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: HELLO failed: {e}", file=sys.stderr)

    # Execute requested commands
    try:
        # RTC commands
        if args.settime is not None:
            stamp = normalize_set_stamp(args.settime)
            resp = set_time(ser, stamp)
            print(f"✓ RTC set to: {stamp}" + (f" ({resp})" if resp else ""))

        if args.time:
            now = query_time(ser)
            print(f"Current time: {now}")

        # Sensor commands
        if args.sense:
            t, rh, vbat = query_sense(ser)
            print(f"Temperature: {t:.2f}°C")
            print(f"Humidity: {rh:.2f}%")
            if vbat is not None:
                print(f"Battery: {vbat} mV ({vbat/1000:.3f} V)")

        if args.vbat:
            mv = query_vbat(ser)
            print(f"Battery: {mv} mV ({mv/1000:.3f} V)")

        # Configuration commands
        if args.interval:
            val = query_interval(ser)
            print(f"Logging interval: {val} seconds ({val/60:.1f} minutes)")

        if args.setinterval is not None:
            val = set_interval(ser, args.setinterval)
            print(f"✓ Interval set to: {val} seconds ({val/60:.1f} minutes)")

        # Storage info commands
        if args.flash_info:
            query_flash_info(ser)

        if args.flash_free:
            query_flash_free(ser)

        if args.flash_verbose:
            query_flash_verbose(ser)

        # List and download
        if args.list:
            dates = request_list(ser, verbose=True)
            if not dates:
                print("No logs found")
            else:
                print(f"\nAvailable logs ({len(dates)} dates):")
                print("-" * 50)
                for d in dates:
                    date = d['date']
                    size_kb = d.get('size_kb', 0)
                    rows = d.get('rows', 0)
                    print(f"{date:12s}  {size_kb:6d} KB  {rows:6d} rows")

        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            print(f"Downloading {args.date}...", file=sys.stderr)
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            size = os.path.getsize(outpath)
            print(f"✓ Saved: {outpath} ({size:,} bytes)")

        # Storage management
        if args.delete:
            result = delete_date(ser, args.delete)
            print(f"✓ Deleted {result['date']}")
            print(f"  Files removed: {result['files']}")
            print(f"  Space freed: {result['size_kb']} KB ({result['bytes']:,} bytes)")

        if args.format:
            result = format_storage(ser, args.format)
            print(f"✓ Storage formatted")
            print(f"  Total: {result['total']:,} bytes")
            print(f"  Used: {result['used']:,} bytes")
            print(f"  Free: {result['free']:,} bytes")

        if args.setfmtcode:
            old_code, new_code = args.setfmtcode
            set_format_code(ser, old_code, new_code)
            print(f"✓ Format security code changed")

        # System commands
        if args.reboot:
            reason = reboot_device(ser)
            print(f"✓ Device rebooting..." + (f" (reason: {reason})" if reason else ""))
            time.sleep(0.5)  # Give time for reboot message

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()