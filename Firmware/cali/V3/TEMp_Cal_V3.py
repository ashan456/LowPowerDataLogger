# sht45_logger_gui.py
# Connects to COM, checks greeting, sends SENSE?, parses "OK T=...C RH=...% VBAT=...mV"
# CSV columns: TIME,SHT45,Thermometer

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, date
import csv, threading, queue, time, os, re
import serial, serial.tools.list_ports

BAUD = 115200

OK_SENSE_RE = re.compile(
    r"""^OK\s+T=([\-0-9.]+)C\s+RH=([\-0-9.]+)%\s+VBAT=([0-9]+)mV""",
    re.IGNORECASE,
)

class SHT45GUI:
    def __init__(self, root):
        self.root = root
        root.title("SHT45 Serial Logger")

        # State
        self.ser = None
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.q = queue.Queue()
        self.streaming = False
        self.current_csv_date = None
        self.csv_path = None

        # --- Top frame: connection ---
        frm_top = ttk.Frame(root, padding=8)
        frm_top.grid(row=0, column=0, sticky="ew")
        frm_top.columnconfigure(4, weight=1)

        ttk.Label(frm_top, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(frm_top, width=18, state="readonly", values=self.list_ports())
        self.port_combo.grid(row=0, column=1, sticky="w", padx=(4, 8))
        ttk.Button(frm_top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, sticky="w")
        self.connect_btn = ttk.Button(frm_top, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=3, sticky="e")

        # --- Middle frame: controls ---
        frm_mid = ttk.Frame(root, padding=(8, 0, 8, 8))
        frm_mid.grid(row=1, column=0, sticky="ew")
        frm_mid.columnconfigure(10, weight=1)

        ttk.Label(frm_mid, text="Interval (ms):").grid(row=0, column=0, sticky="w")
        self.interval_var = tk.StringVar(value="5000")
        ttk.Entry(frm_mid, width=10, textvariable=self.interval_var).grid(row=0, column=1, sticky="w", padx=(4, 16))

        self.start_btn = ttk.Button(frm_mid, text="Start", command=self.start_stream, state="disabled")
        self.start_btn.grid(row=0, column=2, padx=4)
        self.stop_btn = ttk.Button(frm_mid, text="Stop", command=self.stop_stream, state="disabled")
        self.stop_btn.grid(row=0, column=3, padx=4)

        ttk.Label(frm_mid, text="Thermometer (°C):").grid(row=0, column=4, sticky="e", padx=(12, 4))
        self.thermo_var = tk.StringVar(value="")
        ttk.Entry(frm_mid, width=10, textvariable=self.thermo_var).grid(row=0, column=5, sticky="w")
        self.single_btn = ttk.Button(frm_mid, text="Sense Now", command=self.single_read, state="disabled")
        self.single_btn.grid(row=0, column=6, padx=8)

        # Live readouts
        box = ttk.LabelFrame(frm_mid, text="Latest Reading", padding=8)
        box.grid(row=1, column=0, columnspan=11, sticky="ew", pady=(8, 0))
        for i in range(8): box.columnconfigure(i, weight=1)
        ttk.Label(box, text="Time:").grid(row=0, column=0, sticky="e")
        self.lbl_time = ttk.Label(box, text="—"); self.lbl_time.grid(row=0, column=1, sticky="w")
        ttk.Label(box, text="SHT45 Temp (°C):").grid(row=0, column=2, sticky="e")
        self.lbl_temp = ttk.Label(box, text="—"); self.lbl_temp.grid(row=0, column=3, sticky="w")
        ttk.Label(box, text="Humidity (%RH):").grid(row=0, column=4, sticky="e")
        self.lbl_rh = ttk.Label(box, text="—"); self.lbl_rh.grid(row=0, column=5, sticky="w")
        ttk.Label(box, text="VBAT (mV):").grid(row=0, column=6, sticky="e")
        self.lbl_vbat = ttk.Label(box, text="—"); self.lbl_vbat.grid(row=0, column=7, sticky="w")

        # Status bar
        self.status = tk.StringVar(value="Disconnected")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(8, 4)).grid(row=2, column=0, sticky="ew")

        # UI poll
        self.root.after(100, self.poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- Serial / ports ----
    def list_ports(self): 
        return [p.device for p in serial.tools.list_ports.comports()]

    def refresh_ports(self):
        self.port_combo["values"] = self.list_ports()
        if not self.port_combo.get() and self.port_combo["values"]:
            self.port_combo.current(0)

    def connect(self):
        if self.ser and self.ser.is_open:
            self.stop_reader_thread()
            try: self.ser.close()
            except: pass
            self.ser = None
            self.connect_btn.config(text="Connect")
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")
            self.single_btn.config(state="disabled")
            self.status.set("Disconnected")
            return

        port = self.port_combo.get()
        if not port:
            messagebox.showwarning("Select Port", "Please select a COM port.")
            return

        try:
            self.ser = serial.Serial(port, BAUD, timeout=0.1)
            # Clear any buffered junk and try to catch hello
            try:
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
            except: 
                pass

            self.status.set(f"Connected: {port} @ {BAUD}")
            self.connect_btn.config(text="Disconnect")
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.single_btn.config(state="normal")
            self.start_reader_thread()

            # Give the device a moment to print its hello/ready line
            self.status.set("Connected. Waiting for hello/ready…")
            self.root.after(500, self.auto_sense_once)
        except Exception as e:
            self.ser = None
            messagebox.showerror("Connection failed", str(e))
            self.status.set("Disconnected")

    def auto_sense_once(self):
        # After connect, attempt one SENSE? to populate fields
        if self.ser and self.ser.is_open:
            self.send_line("SENSE?")
            self.status.set("Sent SENSE? …")

    # ---- Reader thread ----
    def start_reader_thread(self):
        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self.reader_loop, daemon=True)
        self.reader_thread.start()

    def stop_reader_thread(self):
        self.reader_stop.set()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1.0)
        self.reader_thread = None
        self.reader_stop.clear()

    def reader_loop(self):
        buf = bytearray()
        while not self.reader_stop.is_set():
            try:
                if self.ser and self.ser.is_open:
                    chunk = self.ser.read(256)
                    if chunk:
                        buf.extend(chunk)
                        while b"\n" in buf:
                            line, _, buf = buf.partition(b"\n")
                            try: s = line.decode("utf-8", errors="ignore").strip("\r")
                            except: s = ""
                            s = s.strip()
                            if s:
                                self.q.put(("serial_line", s))
                else:
                    time.sleep(0.2)
            except Exception as e:
                self.q.put(("error", f"Serial error: {e}"))
                time.sleep(0.5)

    # ---- Commands ----
    def send_line(self, text):
        if not (self.ser and self.ser.is_open):
            return
        try:
            self.ser.write((text.strip() + "\n").encode("utf-8"))
        except Exception as e:
            self.q.put(("error", f"Write failed: {e}"))

    def start_stream(self):
        try:
            ival = int(self.interval_var.get().strip())
        except ValueError:
            messagebox.showwarning("Interval", "Enter a valid integer interval in ms.")
            return
        if ival < 1000:
            ival = 1000
            self.interval_var.set("1000")
        self.send_line(f"START {ival}")
        self.streaming = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.set("Streaming…")

    def stop_stream(self):
        self.send_line("STOP")
        self.streaming = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set("Stopped")

    def single_read(self):
        if not (self.ser and self.ser.is_open):
            messagebox.showwarning("Not connected", "Connect to a COM port first.")
            return
        self.send_line("SENSE?")
        self.status.set("Single SENSE? requested…")

    # ---- CSV ----
    def ensure_csv(self):
        today = date.today().isoformat()
        if self.current_csv_date != today or not self.csv_path:
            self.current_csv_date = today
            self.csv_path = f"sht45_log_{today}.csv"
            if not os.path.exists(self.csv_path):
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["TIME", "SHT45", "Thermometer"])
        return self.csv_path

    def append_row(self, time_str, sht45_temp_str, thermometer_val_str=""):
        path = self.ensure_csv()
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([time_str, sht45_temp_str, thermometer_val_str])
        except Exception as e:
            self.q.put(("error", f"CSV write failed: {e}"))

    # ---- UI / parsing ----
    def poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "serial_line":
                    self.handle_serial_line(payload)
                elif kind == "error":
                    self.status.set(payload)
                self.q.task_done()
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def handle_serial_line(self, s: str):
        # 1) Handle SENSE? reply: "OK T=28.08C RH=58.06% VBAT=4168mV"
        m = OK_SENSE_RE.match(s)
        if m:
            try:
                t_c = float(m.group(1))
                rh = float(m.group(2))
                vbat_mV = int(m.group(3))
            except ValueError:
                self.status.set("Parse error on SENSE? reply.")
                return

            ts = datetime.now().strftime("%H:%M:%S")  # HH:MM:SS
            self.lbl_time.config(text=ts)
            self.lbl_temp.config(text=f"{t_c:.2f}")
            self.lbl_rh.config(text=f"{rh:.2f}")
            self.lbl_vbat.config(text=str(vbat_mV))

            # On SENSE? we save SHT45 temp + user-provided thermometer value
            th_txt = self.thermo_var.get().strip()
            try:
                th_val = f"{float(th_txt):.2f}" if th_txt != "" else ""
            except ValueError:
                messagebox.showwarning("Thermometer value", "Invalid thermometer temperature; leaving blank.")
                th_val = ""

            self.append_row(ts, f"{t_c:.2f}", th_val)
            self.status.set("Logged one SENSE sample.")
            return

        # 2) Optional: streaming firmware line "DATA,<millis>,<temp>,<rh>"
        if s.startswith("DATA,"):
            parts = s.split(",")
            if len(parts) >= 4:
                try:
                    t_c = float(parts[2])
                    rh = float(parts[3])
                except ValueError:
                    self.status.set("Bad DATA line.")
                    return
                ts = datetime.now().strftime("%H:%M:%S")
                self.lbl_time.config(text=ts)
                self.lbl_temp.config(text=f"{t_c:.2f}")
                self.lbl_rh.config(text=f"{rh:.2f}")
                # Streaming → save SHT45 only (Thermometer blank)
                if self.streaming:
                    self.append_row(ts, f"{t_c:.2f}", "")
                    self.status.set("Streaming…")
                else:
                    # if not streaming and DATA arrives, don’t auto-log (avoid duplicates)
                    self.status.set("DATA received.")
            return

        # 3) Greeting / hello / ready lines: show them in status
        if ("hello" in s.lower()) or ("ready" in s.lower()) or s.upper().startswith("OK"):
            # Show but don't try to parse unless it matches SENSE reply above
            self.status.set(s[:120])
            return

        # Fallback: show any other text in the status bar
        self.status.set(s[:120])

    def on_close(self):
        try:
            if self.streaming:
                self.send_line("STOP")
                time.sleep(0.05)
        except: 
            pass
        self.stop_reader_thread()
        if self.ser and self.ser.is_open:
            try: self.ser.close()
            except: pass
        self.root.destroy()

def main():
    root = tk.Tk()
    try: root.tk.call("tk", "scaling", 1.2)
    except: pass
    app = SHT45GUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
