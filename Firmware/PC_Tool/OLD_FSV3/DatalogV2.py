# -*- coding: utf-8 -*-
"""
Created on Sun Aug 24 12:16:34 2025

@author: ashan
"""

#!/usr/bin/env python3
import argparse, sys, time, os, re, binascii
from typing import Optional, Tuple, List
import serial
from serial.tools import list_ports

OK_RE = re.compile(r'^OK(?:\s+(.+))?$', re.IGNORECASE)
ERR_RE = re.compile(r'^ERR\s+(.+)$', re.IGNORECASE)

def open_port_auto(preferred: Optional[str], baud: int, timeout: float, discover: bool, reset: bool) -> serial.Serial:
    if preferred:
        ser = serial.Serial(preferred, baudrate=baud, timeout=timeout)
        if reset:
            ser.dtr = False; ser.rts = False
            time.sleep(0.1)
            ser.dtr = True; ser.rts = True
            time.sleep(0.1)
            ser.dtr = False; ser.rts = False
            time.sleep(1.0)
        ser.reset_input_buffer(); ser.reset_output_buffer()
        return ser

    if not discover:
        raise RuntimeError("No --port provided and --discover is disabled.")

    for p in list_ports.comports():
        try:
            ser = serial.Serial(p.device, baudrate=baud, timeout=timeout)
            ser.dtr = False; ser.rts = False
            ser.reset_input_buffer(); ser.reset_output_buffer()
            # Try HELLO handshake
            send_line(ser, "HELLO v=1")
            line = read_line(ser, 2.0)
            if line and line.upper().startswith("OK"):
                return ser
            ser.close()
        except Exception:
            continue
    raise RuntimeError("Could not auto-discover device. Use --port.")

def send_line(ser: serial.Serial, s: str):
    ser.write((s + "\n").encode("utf-8"))
    ser.flush()

def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
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
    # e.g., "SIZE=8421 ROWS=318 PATH=/logs/..."
    out = {}
    if not s: return out
    parts = s.split()
    for p in parts:
        if '=' in p:
            k, v = p.split('=', 1)
            out[k.upper()] = v
    return out

def request_list(ser: serial.Serial) -> List[str]:
    send_line(ser, "LIST")
    dates = []
    header = read_line(ser, 2.0)
    if not header or not header.upper().startswith("DATES"):
        raise RuntimeError("LIST: Bad header")
    # read until END
    while True:
        line = read_line(ser, 5.0)
        if line is None:
            raise RuntimeError("LIST: Timeout")
        if line == "END":
            break
        dates.append(line.strip())
    return dates

def recv_exact_bytes(ser: serial.Serial, n: int, progress: bool) -> bytes:
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
                print(f"\rDownloading... {pct:3d}% ({len(data)}/{n} bytes)", end='', flush=True)
                last_pct = pct
    if progress:
        print()
    return bytes(data)

def get_file(ser: serial.Serial, date: str, outpath: str, progress: bool):
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
        raise RuntimeError("Missing SIZE")

    # Expect DATA
    line = read_line(ser, 3.0)
    if not line or line != "DATA":
        raise RuntimeError(f"Expected DATA, got: {line}")

    # Receive exactly SIZE bytes
    tmp = outpath + ".part"
    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    with open(tmp, "wb") as f:
        blob = recv_exact_bytes(ser, size, progress)
        f.write(blob)

    # Read END (skip optional blank lines)
    line = read_line(ser, 3.0)
    while line is not None and line.strip() == "":
        line = read_line(ser, 2.0)
        if line != "END":
            raise RuntimeError(f"Expected END, got: {line}")


    # Read CRC32 (optional)
    line = read_line(ser, 1.0)
    if line and line.upper().startswith("CRC32="):
        remote_crc = int(line.split("=", 1)[1], 16)
        local_crc = binascii.crc32(blob) & 0xFFFFFFFF
        if local_crc != remote_crc:
            raise RuntimeError(f"CRC mismatch: got {local_crc:08X}, expected {remote_crc:08X}")

    # Success
    os.replace(tmp, outpath)

def main():
    ap = argparse.ArgumentParser(description="Fetch ESP32 CSV logs by date over serial.")
    ap.add_argument("--port", help="Serial port (e.g., COM7 or /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=115200, help="Baud rate (default 115200)")
    ap.add_argument("--date", help="Date YYYY-MM-DD to download")
    ap.add_argument("--out", help="Output CSV path (default ./logs/<date>.csv)")
    ap.add_argument("--list", action="store_true", help="List available dates")
    ap.add_argument("--discover", action="store_true", default=True, help="Auto-discover port if --port not given")
    ap.add_argument("--no-discover", dest="discover", action="store_false")
    ap.add_argument("--reset", action="store_true", help="Toggle DTR/RTS to reset device before HELLO")
    ap.add_argument("--no-progress", action="store_true", help="Disable progress display")
    args = ap.parse_args()

    if not args.list and not args.date:
        ap.error("Provide --list or --date YYYY-MM-DD")

    try:
        ser = open_port_auto(args.port, args.baud, timeout=0.2, discover=args.discover, reset=args.reset)
    except Exception as e:
        print(f"Port open/discover failed: {e}", file=sys.stderr)
        sys.exit(2)

    # Always do HELLO; tolerate that firmware may already print an OK banner
    try:
        ser.reset_input_buffer()
        send_line(ser, "HELLO v=1")
        hello = read_line(ser, 2.0)
        # If banner already printed, we might get OK without sending HELLO; accept any OK
        if not hello or not hello.upper().startswith("OK"):
            # Try one more read; some boards echo or delay
            hello = read_line(ser, 1.0)
        if not hello or not hello.upper().startswith("OK"):
            print("Warning: No 'OK' to HELLO; continuing anyway.", file=sys.stderr)
    except Exception as e:
        print(f"HELLO failed: {e}", file=sys.stderr)

    try:
        if args.list:
            dates = request_list(ser)
            if not dates:
                print("(no dates found)")
            else:
                for d in dates:
                    print(d)
        if args.date:
            outpath = args.out or os.path.join(".", "logs", f"{args.date}.csv")
            get_file(ser, args.date, outpath, progress=not args.no_progress)
            print(f"Saved: {outpath}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        ser.close()

if __name__ == "__main__":
    main()
