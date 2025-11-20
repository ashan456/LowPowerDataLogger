# sht45_calibration_display_v3.py
# Fixed window, "Read Once", Periodic Readings (5s), CSV logging for periodic data.

import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, date
import threading, queue, time, re, csv, os
import serial, serial.tools.list_ports

BAUD = 115200

OK_SENSE_RE = re.compile(
    r"""^OK\s+T=([\-0-9.]+)C\s+RH=([\-0-9.]+)%\s+VBAT=([0-9]+)mV""",
    re.IGNORECASE,
)

PERIOD_MS = 5000  # 5 seconds

class CalibGUI:
    def __init__(self, root):
        self.root = root
        root.title("SHT45 Calibration Display")

        # Fixed size window
        root.geometry("820x360")
        root.resizable(False, False)

        # Fonts
        self.label_font = ("Segoe UI", 22, "bold")
        self.value_font = ("Consolas", 40, "bold")
        self.button_font = ("Segoe UI", 14, "bold")

        # Serial / state
        self.ser = None
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.q = queue.Queue()

        # Periodic state
        self.periodic_active = False
        self.periodic_after_id = None

        # CSV state
        self.current_csv_date = None
        self.csv_path = None

        # --- Top Bar (compact) ---
        top = ttk.Frame(root, padding=(8, 8, 8, 0))
        top.grid(row=0, column=0, sticky="ew")
        for c in range(6): top.columnconfigure(c, weight=0)
        top.columnconfigure(6, weight=1)

        ttk.Label(top, text="Port:", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=(2, 4))
        self.port_combo = ttk.Combobox(top, width=12, state="readonly", values=self.list_ports())
        self.port_combo.grid(row=0, column=1, sticky="w", padx=(0, 4))

        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=3, sticky="w", padx=(0, 4))
        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self.disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=4, sticky="w")

        # --- Read Buttons Row ---
        controls = ttk.Frame(root, padding=(8, 6, 8, 8))
        controls.grid(row=1, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(2, weight=1)

        self.read_once_btn = tk.Button(
            controls, text="Read Once",
            command=self.sense_once,
            font=self.button_font, relief="raised", height=1
        )
        self.read_once_btn.grid(row=0, column=0, sticky="ew")

        # spacer for padding between buttons
        ttk.Frame(controls, width=16).grid(row=0, column=1)

        self.periodic_btn = tk.Button(
            controls, text="Start Periodic Readings",
            command=self.toggle_periodic,
            font=self.button_font, relief="raised", height=1, bg="#f0f0f0", activebackground="#e0e0e0"
        )
        self.periodic_btn.grid(row=0, column=2, sticky="ew")

        # --- Large Readouts ---
        panel = ttk.Frame(root, padding=(8, 0, 8, 10))
        panel.grid(row=2, column=0, sticky="nsew")
        for c in range(6): panel.columnconfigure(c, weight=1)

        ttk.Label(panel, text="TIME", font=self.label_font, anchor="center").grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 20))
        self.val_time = ttk.Label(panel, text="—", font=self.value_font, anchor="center")
        self.val_time.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 20))

        ttk.Label(panel, text="TEMP (°C)", font=self.label_font, anchor="center").grid(row=0, column=2, columnspan=2, sticky="ew", padx=(20, 20))
        self.val_temp = ttk.Label(panel, text="—", font=self.value_font, anchor="center")
        self.val_temp.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(20, 20))

        ttk.Label(panel, text="RH (%)", font=self.label_font, anchor="center").grid(row=0, column=4, columnspan=2, sticky="ew", padx=(20, 0))
        self.val_rh = ttk.Label(panel, text="—", font=self.value_font, anchor="center")
        self.val_rh.grid(row=1, column=4, columnspan=2, sticky="ew", padx=(20, 0))

        # --- Status bar ---
        self.status = tk.StringVar(value="Disconnected")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(10, 6)).grid(row=3, column=0, sticky="ew")

        root.after(100, self.poll_queue)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ===== CSV =====
    def ensure_csv(self):
        today = date.today().isoformat()
        if self.current_csv_date != today or not self.csv_path:
            self.current_csv_date = today
            self.csv_path = f"sht45_log_{today}.csv"
            if not os.path.exists(self.csv_path):
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(["TIME", "TEMP(C)", "RH (%)"])
        return self.csv_path

    def append_csv_row(self, ts: str, temp_c: float, rh: float):
        path = self.ensure_csv()
        try:
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([ts, f"{temp_c:.2f}", f"{rh:.2f}"])
        except Exception as e:
            self.status.set(f"CSV write failed: {e}")

    # ===== Serial helpers =====
    def list_ports(self):
        return [p.device for p in serial.tools.list_ports.comports()]

    def refresh_ports(self):
        vals = self.list_ports()
        self.port_combo["values"] = vals
        if not self.port_combo.get() and vals:
            self.port_combo.current(0)

    def connect(self):
        port = self.port_combo.get()
        if not port:
            messagebox.showwarning("Select Port", "Please select a COM port.")
            return
        try:
            self.ser = serial.Serial(port, BAUD, timeout=0.2)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.status.set(f"Connected: {port} @ {BAUD}")
            self.connect_btn.config(state="disabled")
            self.disconnect_btn.config(state="normal")
            self.start_reader_thread()
            self.root.after(400, self.sense_once)
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            self.status.set("Disconnected")

    def disconnect(self):
        self.stop_periodic()
        self.stop_reader_thread()
        if self.ser and self.ser.is_open:
            try: self.ser.close()
            except: pass
        self.ser = None
        self.connect_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.status.set("Disconnected")

    def start_reader_thread(self):
        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self.reader_loop, daemon=True)
        self.reader_thread.start()

    def stop_reader_thread(self):
        self.reader_stop.set()
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=1)
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
                            try:
                                s = line.decode("utf-8", errors="ignore").strip("\r").strip()
                            except:
                                s = ""
                            if s:
                                self.q.put(("serial_line", s))
                else:
                    time.sleep(0.2)
            except Exception as e:
                self.q.put(("error", f"Serial error: {e}"))
                time.sleep(0.5)

    def send_line(self, text: str):
        if not (self.ser and self.ser.is_open):
            return
        try:
            self.ser.write((text.strip() + "\n").encode("utf-8"))
        except Exception as e:
            self.q.put(("error", f"Write failed: {e}"))

    # ===== Reads =====
    def sense_once(self):
        if not (self.ser and self.ser.is_open):
            messagebox.showwarning("Not connected", "Connect first.")
            return
        self.send_line("SENSE?")
        self.status.set("Requesting reading…")

    def toggle_periodic(self):
        if not self.periodic_active:
            if not (self.ser and self.ser.is_open):
                messagebox.showwarning("Not connected", "Connect first.")
                return
            self.periodic_active = True
            self.periodic_btn.config(text="Stop Periodic Readings", bg="#3CB371", activebackground="#2E8B57")  # green
            self.status.set("Periodic readings started (5 s).")
            self.schedule_next_periodic()
        else:
            self.stop_periodic()
            self.status.set("Periodic readings stopped.")

    def schedule_next_periodic(self):
        # Send a sense now, then schedule next
        if self.periodic_active and self.ser and self.ser.is_open:
            self.send_line("SENSE?")
            self.periodic_after_id = self.root.after(PERIOD_MS, self.schedule_next_periodic)
        else:
            self.stop_periodic()

    def stop_periodic(self):
        self.periodic_active = False
        if self.periodic_after_id is not None:
            try: self.root.after_cancel(self.periodic_after_id)
            except: pass
            self.periodic_after_id = None
        self.periodic_btn.config(text="Start Periodic Readings", bg="#f0f0f0", activebackground="#e0e0e0")

    # ===== UI / parsing =====
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
        m = OK_SENSE_RE.match(s)
        if m:
            t_c = float(m.group(1))
            rh = float(m.group(2))
            ts = datetime.now().strftime("%H:%M:%S")
            self.val_time.config(text=ts)
            self.val_temp.config(text=f"{t_c:.2f}")
            self.val_rh.config(text=f"{rh:.2f}")
            # Log only when periodic mode is on
            if self.periodic_active:
                self.append_csv_row(ts, t_c, rh)
            self.status.set("Reading updated.")
            return

        if "hello" in s.lower() or "ready" in s.lower():
            self.status.set(s)
        elif s:
            self.status.set(s)

    def on_close(self):
        self.disconnect()
        self.root.destroy()

def main():
    root = tk.Tk()
    try: root.tk.call("tk", "scaling", 1.3)
    except: pass
    CalibGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
