#!/usr/bin/env python3
"""
ESP32 DataLogger GUI - Complete interface for configuration and data analysis
Supports new 7-byte BIN format with automatic CSV decoding
"""

import sys
import os
import json
import threading
import struct
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import serial
from serial.tools import list_ports
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
import matplotlib.dates as mdates

# ============================================================================
# BIN DECODING CONSTANTS (must match firmware)
# ============================================================================
_RECORD_SIZE = 7        # 7 bytes per record
_VBAT_MIN_MV = 2500     # Battery voltage minimum (mV)
_VBAT_MAX_MV = 4500     # Battery voltage maximum (mV)

# ============================================================================
# BIN DECODING FUNCTIONS
# ============================================================================
def _sec_to_hms(sec: int) -> str:
    """Map seconds-since-midnight (0..86399) to HH:MM:SS."""
    if sec < 0: sec = 0
    if sec > 86399: sec = 86399
    hh = sec // 3600
    sec %= 3600
    mm = sec // 60
    ss = sec % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"

def _vbat_raw_to_mv(vb: int) -> int:
    """Inverse map of enc_vbat: 2.50–4.50 V -> 0..255 (≈7.84 mV/LSB)."""
    return _VBAT_MIN_MV + round((vb / 255.0) * (_VBAT_MAX_MV - _VBAT_MIN_MV))

def decode_bin_to_csv(bin_path: Path, csv_path: Path) -> dict:
    """
    Decode firmware 7-byte records into CSV with columns:
      time,temperature,humidity,vbat_mV

    7-byte layout (little-endian):
      [0..2] uint24  : seconds since midnight (0..86399)
      [3..4] int16   : temperature in deci-°C (T_C*10), two's complement
      [5]    uint8   : relative humidity (0..100)
      [6]    uint8   : vbat mapped 2.50–4.50 V -> 0..255
    """
    data = bin_path.read_bytes()
    nbytes = len(data)
    nrec = nbytes // _RECORD_SIZE

    if nrec == 0:
        raise ValueError(f"No records: {bin_path} is {nbytes} bytes.")

    tail = nbytes % _RECORD_SIZE
    if tail:
        print(f"[warn] {bin_path.name}: {nbytes} bytes not multiple of 7; decoding first {nrec} records.")

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        f.write("time,temperature,humidity,vbat_mV\n")
        off = 0
        for _ in range(nrec):
            # seconds since midnight (uint24 LE)
            s0 = data[off + 0]
            s1 = data[off + 1]
            s2 = data[off + 2]
            sec = (s0 | (s1 << 8) | (s2 << 16))
            if sec > 86399:
                sec = 86399

            # temperature deci-°C (int16 LE)
            t_i16 = int.from_bytes(data[off + 3:off + 5], byteorder="little", signed=True)
            t_c = t_i16 / 10.0

            # humidity (uint8, clamp)
            rh = data[off + 5]
            if rh > 100:
                rh = 100

            # vbat (uint8 -> mV)
            vb = data[off + 6]
            vbat_mv = _vbat_raw_to_mv(vb)

            # time string
            time_str = _sec_to_hms(sec)

            f.write(f"{time_str},{t_c:.1f},{int(rh)},{vbat_mv}\n")
            off += _RECORD_SIZE

    return {
        "records": nrec,
        "bytes": nbytes,
        "tail_bytes": tail,
    }


class LoggerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("ESP32 DataLogger - Configuration & Analysis (BIN Format)")
        self.root.geometry("1200x800")
        
        # State
        self.serial_port: Optional[serial.Serial] = None
        self.current_port: str = ""
        self.downloaded_data: Optional[pd.DataFrame] = None
        
        # Create main notebook (tabs)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True, padx=5, pady=5)
        
        # Create tabs
        self.create_connection_tab()
        self.create_config_tab()
        self.create_data_tab()
        self.create_plot_tab()
        self.create_storage_tab()
        self.create_diagnostic_tab()
        
        # Status bar
        self.status_bar = ttk.Label(root, text="Ready", relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        
    def create_connection_tab(self):
        """Tab for device connection and basic info"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📡 Connection")
        
        # Connection frame
        conn_frame = ttk.LabelFrame(frame, text="Device Connection", padding=10)
        conn_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(conn_frame, text="Serial Port:").grid(row=0, column=0, sticky='w', pady=5)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, width=30)
        self.port_combo.grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Button(conn_frame, text="🔄 Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=5)
        ttk.Button(conn_frame, text="🔌 Connect", command=self.connect_device).grid(row=0, column=3, padx=5)
        ttk.Button(conn_frame, text="❌ Disconnect", command=self.disconnect_device).grid(row=0, column=4, padx=5)
        
        self.conn_status = ttk.Label(conn_frame, text="⚫ Disconnected", foreground="red")
        self.conn_status.grid(row=0, column=5, padx=10)
        
        # Device info frame
        info_frame = ttk.LabelFrame(frame, text="Device Information", padding=10)
        info_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Quick actions
        actions = ttk.Frame(info_frame)
        actions.pack(fill='x', pady=5)
        
        ttk.Button(actions, text="⏰ Get Time", command=self.get_time).pack(side='left', padx=5)
        ttk.Button(actions, text="🔧 Set Time (Now)", command=self.set_time_now).pack(side='left', padx=5)
        ttk.Button(actions, text="🌡️ Read Sensors", command=self.read_sensors).pack(side='left', padx=5)
        ttk.Button(actions, text="🔋 Battery", command=self.read_battery).pack(side='left', padx=5)
        ttk.Button(actions, text="🔄 Reboot", command=self.reboot_device).pack(side='left', padx=5)
        
        # Output text
        self.info_text = scrolledtext.ScrolledText(info_frame, height=20, wrap=tk.WORD)
        self.info_text.pack(fill='both', expand=True, pady=5)
        
        # Initialize
        self.refresh_ports()
        
    def create_config_tab(self):
        """Tab for device configuration"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="⚙️ Configuration")
        
        # Storage Mode Configuration
        storage_frame = ttk.LabelFrame(frame, text="Storage Mode", padding=10)
        storage_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(storage_frame, text="Current Mode:").grid(row=0, column=0, sticky='w', pady=5)
        self.storage_mode_status = ttk.Label(storage_frame, text="Unknown", font=('TkDefaultFont', 10, 'bold'))
        self.storage_mode_status.grid(row=0, column=1, sticky='w', padx=10)
        ttk.Button(storage_frame, text="🔄 Refresh", command=self.get_storage_mode).grid(row=0, column=2, padx=5)
        
        ttk.Label(storage_frame, text="Select Mode:").grid(row=1, column=0, sticky='w', pady=5)
        self.storage_mode_var = tk.StringVar(value="INTERNAL")
        mode_combo = ttk.Combobox(storage_frame, textvariable=self.storage_mode_var, 
                                  values=["INTERNAL", "EXTERNAL", "AUTO"], width=15, state='readonly')
        mode_combo.grid(row=1, column=1, sticky='w', padx=10)
        ttk.Button(storage_frame, text="✅ Set Mode", command=self.set_storage_mode).grid(row=1, column=2, padx=5)
        
        # Auto-failover threshold
        ttk.Label(storage_frame, text="Auto Threshold (%):").grid(row=2, column=0, sticky='w', pady=5)
        self.auto_threshold_var = tk.StringVar(value="90")
        ttk.Entry(storage_frame, textvariable=self.auto_threshold_var, width=10).grid(row=2, column=1, sticky='w', padx=10)
        ttk.Button(storage_frame, text="✅ Set Threshold", command=self.set_auto_threshold).grid(row=2, column=2, padx=5)
        
        ttk.Label(storage_frame, text="Auto Status:").grid(row=3, column=0, sticky='w', pady=5)
        self.auto_status_label = ttk.Label(storage_frame, text="Not switched", foreground="blue")
        self.auto_status_label.grid(row=3, column=1, sticky='w', padx=10)
        ttk.Button(storage_frame, text="🔄 Reset Auto", command=self.reset_auto_switch).grid(row=3, column=2, padx=5)
        
        # Logging interval
        interval_frame = ttk.LabelFrame(frame, text="Logging Interval", padding=10)
        interval_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(interval_frame, text="Current Interval:").grid(row=0, column=0, sticky='w', pady=5)
        self.current_interval = ttk.Label(interval_frame, text="Unknown", font=('TkDefaultFont', 10, 'bold'))
        self.current_interval.grid(row=0, column=1, sticky='w', padx=10)
        ttk.Button(interval_frame, text="🔄 Refresh", command=self.get_interval).grid(row=0, column=2, padx=5)
        
        ttk.Label(interval_frame, text="New Interval (seconds):").grid(row=1, column=0, sticky='w', pady=5)
        self.interval_var = tk.StringVar(value="300")
        ttk.Entry(interval_frame, textvariable=self.interval_var, width=15).grid(row=1, column=1, sticky='w', padx=10)
        ttk.Button(interval_frame, text="✅ Set Interval", command=self.set_interval).grid(row=1, column=2, padx=5)
        
        # Presets
        presets = ttk.Frame(interval_frame)
        presets.grid(row=2, column=0, columnspan=3, pady=10)
        ttk.Label(presets, text="Presets:").pack(side='left', padx=5)
        for label, seconds in [("30s", 30), ("1min", 60), ("5min", 300), ("15min", 900), ("1hr", 3600)]:
            ttk.Button(presets, text=label, command=lambda s=seconds: self.interval_var.set(str(s))).pack(side='left', padx=2)
        
        # Heartbeat LED
        hb_frame = ttk.LabelFrame(frame, text="Heartbeat LED", padding=10)
        hb_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(hb_frame, text="Status:").grid(row=0, column=0, sticky='w', pady=5)
        self.hb_status = ttk.Label(hb_frame, text="Unknown", font=('TkDefaultFont', 10, 'bold'))
        self.hb_status.grid(row=0, column=1, sticky='w', padx=10)
        ttk.Button(hb_frame, text="🔄 Refresh", command=self.get_heartbeat).grid(row=0, column=2, padx=5)
        
        ttk.Button(hb_frame, text="✅ Enable", command=self.heartbeat_on).grid(row=1, column=0, pady=5, padx=5)
        ttk.Button(hb_frame, text="❌ Disable", command=self.heartbeat_off).grid(row=1, column=1, pady=5, padx=5)
        
        ttk.Label(hb_frame, text="Period (seconds):").grid(row=2, column=0, sticky='w', pady=5)
        self.hb_period_var = tk.StringVar(value="10")
        ttk.Entry(hb_frame, textvariable=self.hb_period_var, width=15).grid(row=2, column=1, sticky='w', padx=10)
        ttk.Button(hb_frame, text="✅ Set Period", command=self.set_heartbeat_period).grid(row=2, column=2, padx=5)
        
        # RTC Time
        rtc_frame = ttk.LabelFrame(frame, text="RTC Time", padding=10)
        rtc_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(rtc_frame, text="Custom Time (YYYY-MM-DD HH:MM:SS):").grid(row=0, column=0, sticky='w', pady=5)
        self.rtc_time_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        ttk.Entry(rtc_frame, textvariable=self.rtc_time_var, width=30).grid(row=0, column=1, sticky='w', padx=10)
        ttk.Button(rtc_frame, text="⏰ Set Custom Time", command=self.set_custom_time).grid(row=0, column=2, padx=5)
        
    def create_data_tab(self):
        """Tab for data download and management"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="💾 Data")
        
        # Available logs
        list_frame = ttk.LabelFrame(frame, text="Available Logs (BIN Format)", padding=10)
        list_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        ttk.Button(list_frame, text="🔄 Refresh List", command=self.list_dates).pack(pady=5)
        
        # Treeview for dates
        columns = ('Date', 'Size (KB)', 'Rows')
        self.dates_tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=10)
        for col in columns:
            self.dates_tree.heading(col, text=col , anchor='center')
            self.dates_tree.column(col, width=150 , anchor='center')
        
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.dates_tree.yview)
        self.dates_tree.configure(yscrollcommand=scrollbar.set)
        
        self.dates_tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        # Actions
        actions = ttk.Frame(frame)
        actions.pack(fill='x', padx=10, pady=5)
        
        ttk.Button(actions, text="⬇️ Download Selected", command=self.download_selected).pack(side='left', padx=5)
        ttk.Button(actions, text="🗑️ Delete Selected", command=self.delete_selected).pack(side='left', padx=5)
        ttk.Button(actions, text="📊 Plot Selected", command=self.plot_selected).pack(side='left', padx=5)
        
    def create_plot_tab(self):
        """Tab for data visualization"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="📊 Plot")
        
        # Controls
        controls = ttk.Frame(frame)
        controls.pack(fill='x', padx=10, pady=5)
        
        ttk.Button(controls, text="📂 Load CSV", command=self.load_csv_for_plot).pack(side='left', padx=5)
        ttk.Button(controls, text="🔄 Refresh Plot", command=self.refresh_plot).pack(side='left', padx=5)
        
        # Plot options
        opts = ttk.LabelFrame(controls, text="Options", padding=5)
        opts.pack(side='left', padx=10, fill='x', expand=True)
        
        self.plot_temp_var = tk.BooleanVar(value=True)
        self.plot_rh_var = tk.BooleanVar(value=True)
        self.plot_vbat_var = tk.BooleanVar(value=True)
        
        ttk.Checkbutton(opts, text="🌡️ Temperature", variable=self.plot_temp_var).pack(side='left', padx=5)
        ttk.Checkbutton(opts, text="💧 Humidity", variable=self.plot_rh_var).pack(side='left', padx=5)
        ttk.Checkbutton(opts, text="🔋 Battery", variable=self.plot_vbat_var).pack(side='left', padx=5)
        
        # Matplotlib figure
        self.fig = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill='both', expand=True, padx=10, pady=5)
        
        # Toolbar
        toolbar = NavigationToolbar2Tk(self.canvas, frame)
        toolbar.update()
        
        # Stats frame
        stats_frame = ttk.LabelFrame(frame, text="Statistics", padding=10)
        stats_frame.pack(fill='x', padx=10, pady=5)
        self.stats_text = tk.Text(stats_frame, height=4, wrap=tk.WORD)
        self.stats_text.pack(fill='x')
        
    def create_storage_tab(self):
        """Tab for storage management"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="💽 Storage")
        
        # Create notebook for internal and external flash tabs
        storage_notebook = ttk.Notebook(frame)
        storage_notebook.pack(fill='both', expand=True, padx=5, pady=5)
        
        # Internal Flash Tab
        internal_frame = ttk.Frame(storage_notebook)
        storage_notebook.add(internal_frame, text="Internal Flash")
        
        # Storage info
        info_frame = ttk.LabelFrame(internal_frame, text="Internal Flash Information", padding=10)
        info_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Button(info_frame, text="🔄 Refresh Storage Info", command=self.get_storage_info).pack(pady=5)
        
        self.storage_text = scrolledtext.ScrolledText(info_frame, height=12, wrap=tk.WORD)
        self.storage_text.pack(fill='both', expand=True, pady=5)
        
        # Dangerous operations for internal
        danger_frame = ttk.LabelFrame(internal_frame, text="⚠️ Dangerous Operations - Internal Flash", padding=10)
        danger_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(danger_frame, text="Format Code:", foreground="red").grid(row=0, column=0, sticky='w', pady=5)
        self.format_code_var = tk.StringVar()
        ttk.Entry(danger_frame, textvariable=self.format_code_var, show="*", width=20).grid(row=0, column=1, padx=5)
        ttk.Button(danger_frame, text="🗑️ FORMAT INTERNAL STORAGE", command=self.format_storage, 
                  style='Danger.TButton').grid(row=0, column=2, padx=5)
        
        # External Flash Tab
        external_frame = ttk.Frame(storage_notebook)
        storage_notebook.add(external_frame, text="External Flash")
        
        # External flash diagnostics
        ext_diag_frame = ttk.LabelFrame(external_frame, text="External Flash Diagnostics", padding=10)
        ext_diag_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Quick status bar
        status_bar = ttk.Frame(ext_diag_frame)
        status_bar.pack(fill='x', pady=5)
        
        ttk.Button(status_bar, text="🔄 Refresh Diagnostics", command=self.refresh_ext_diagnostics).pack(side='left', padx=5)
        ttk.Button(status_bar, text="📊 List Sectors", command=self.list_ext_sectors).pack(side='left', padx=5)
        
        # Diagnostics display
        diag_display = ttk.Frame(ext_diag_frame)
        diag_display.pack(fill='both', expand=True, pady=5)
        
        # Left column - Key metrics
        left_col = ttk.Frame(diag_display)
        left_col.pack(side='left', fill='both', expand=True, padx=5)
        
        metrics = [
            ("Chip ID:", "ext_chip_id"),
            ("Capacity:", "ext_capacity"),
            ("Sector Size:", "ext_sector_size"),
            ("Record Size:", "ext_record_size"),
            ("Data Region:", "ext_data_region"),
            ("Current Tail:", "ext_tail"),
            ("Superblock Seq:", "ext_seq"),
        ]
        
        for i, (label, attr) in enumerate(metrics):
            ttk.Label(left_col, text=label, font=('TkDefaultFont', 9, 'bold')).grid(row=i, column=0, sticky='w', pady=2)
            setattr(self, attr, ttk.Label(left_col, text="--", font=('TkDefaultFont', 9)))
            getattr(self, attr).grid(row=i, column=1, sticky='w', padx=10, pady=2)
        
        # Right column - Usage metrics
        right_col = ttk.Frame(diag_display)
        right_col.pack(side='right', fill='both', expand=True, padx=5)
        
        usage_metrics = [
            ("Records Used:", "ext_records_used"),
            ("Total Records:", "ext_records_total"),
            ("Usage %:", "ext_usage_pct"),
            ("Free Space:", "ext_free_space"),
            ("Used Space:", "ext_used_space"),
            ("Written Sectors:", "ext_sectors_written"),
            ("Current Sector:", "ext_current_sector"),
        ]
        
        for i, (label, attr) in enumerate(usage_metrics):
            ttk.Label(right_col, text=label, font=('TkDefaultFont', 9, 'bold')).grid(row=i, column=0, sticky='w', pady=2)
            setattr(self, attr, ttk.Label(right_col, text="--", font=('TkDefaultFont', 9)))
            getattr(self, attr).grid(row=i, column=1, sticky='w', padx=10, pady=2)
        
        # Sector list display (scrollable)
        sector_frame = ttk.LabelFrame(external_frame, text="Sector Details", padding=10)
        sector_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        self.ext_sector_text = scrolledtext.ScrolledText(sector_frame, height=8, wrap=tk.NONE, font=('Courier', 8))
        self.ext_sector_text.pack(fill='both', expand=True)
        
        # External flash dangerous operations
        ext_danger_frame = ttk.LabelFrame(external_frame, text="⚠️ Dangerous Operations - External Flash", padding=10)
        ext_danger_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(ext_danger_frame, text="Format Code:", foreground="red").grid(row=0, column=0, sticky='w', pady=5)
        self.ext_format_code_var = tk.StringVar()
        ttk.Entry(ext_danger_frame, textvariable=self.ext_format_code_var, show="*", width=20).grid(row=0, column=1, padx=5)
        ttk.Button(ext_danger_frame, text="🗑️ FORMAT EXTERNAL FLASH", command=self.format_ext_storage, 
                  style='Danger.TButton').grid(row=0, column=2, padx=5)
    
    def create_diagnostic_tab(self):
        """Tab for diagnostic terminal (serial monitor)"""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="🔧 Diagnostic")
        
        # Command input frame
        cmd_frame = ttk.LabelFrame(frame, text="Command Input", padding=10)
        cmd_frame.pack(fill='x', padx=10, pady=5)
        
        # Command entry
        ttk.Label(cmd_frame, text="Command:").grid(row=0, column=0, sticky='w', pady=5)
        self.diag_cmd_var = tk.StringVar()
        self.diag_cmd_entry = ttk.Entry(cmd_frame, textvariable=self.diag_cmd_var, width=60)
        self.diag_cmd_entry.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
        
        # Send button
        ttk.Button(cmd_frame, text="📤 Send", command=self.send_diagnostic_command).grid(row=0, column=2, padx=5)
        
        # Quick commands
        quick_frame = ttk.Frame(cmd_frame)
        quick_frame.grid(row=1, column=0, columnspan=3, pady=5, sticky='w')
        ttk.Label(quick_frame, text="Quick:").pack(side='left', padx=5)
        
        quick_cmds = [
            ("TIME?", "TIME?"),
            ("SENSE?", "SENSE?"),
            ("VBAT?", "VBAT?"),
            ("INTERVAL?", "INTERVAL?"),
            ("STORAGE?", "STORAGE?"),
            ("EXT-INFO", "EXT-INFO"),
            ("LIST", "LIST"),
            ("HELP", "HELP")
        ]
        
        for label, cmd in quick_cmds:
            ttk.Button(quick_frame, text=label, 
                      command=lambda c=cmd: self.send_quick_command(c)).pack(side='left', padx=2)
        
        # Configure grid weights
        cmd_frame.columnconfigure(1, weight=1)
        
        # Output text area
        output_frame = ttk.LabelFrame(frame, text="Serial Monitor Output", padding=10)
        output_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Scrolled text for output
        self.diag_output = scrolledtext.ScrolledText(output_frame, height=25, wrap=tk.WORD, 
                                                      font=('Consolas', 9))
        self.diag_output.pack(fill='both', expand=True)
        
        # Control buttons
        control_frame = ttk.Frame(output_frame)
        control_frame.pack(fill='x', pady=5)
        
        ttk.Button(control_frame, text="🗑️ Clear Output", 
                  command=self.clear_diagnostic_output).pack(side='left', padx=5)
        ttk.Button(control_frame, text="💾 Save Log", 
                  command=self.save_diagnostic_log).pack(side='left', padx=5)
        
        self.diag_autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(control_frame, text="Auto-scroll", 
                       variable=self.diag_autoscroll_var).pack(side='left', padx=20)
        
        # Bind Enter key to send command
        self.diag_cmd_entry.bind('<Return>', lambda e: self.send_diagnostic_command())
    
    # ==================== Diagnostic Terminal Methods ====================
    
    def send_diagnostic_command(self):
        """Send custom command from diagnostic terminal"""
        if not self.check_connection():
            return
        
        cmd = self.diag_cmd_var.get().strip()
        if not cmd:
            return
        
        try:
            # Log the command
            self.log_diagnostic(f">>> {cmd}", "blue")
            
            # Send command
            self.send_command(cmd)
            
            # Read response (with timeout)
            response_lines = []
            timeout_count = 0
            max_timeouts = 3
            
            while timeout_count < max_timeouts:
                line = self.read_line(timeout=1.0)
                if line:
                    response_lines.append(line)
                    timeout_count = 0  # Reset on successful read
                    
                    # Check for end markers
                    if line in ["END", "OK", "OK,DONE"] or line.startswith("ERR"):
                        break
                else:
                    timeout_count += 1
            
            # Log the response
            if response_lines:
                for line in response_lines:
                    color = "red" if line.startswith("ERR") else "green" if line.startswith("OK") else "black"
                    self.log_diagnostic(f"<<< {line}", color)
            else:
                self.log_diagnostic("<<< (no response)", "gray")
            
            # Clear command entry
            self.diag_cmd_var.set("")
            
        except Exception as e:
            self.log_diagnostic(f"ERROR: {str(e)}", "red")
            messagebox.showerror("Error", str(e))
    
    def send_quick_command(self, cmd: str):
        """Send a quick command (button click)"""
        self.diag_cmd_var.set(cmd)
        self.send_diagnostic_command()
    
    def log_diagnostic(self, message: str, color: str = "black"):
        """Log message to diagnostic output with color"""
        self.diag_output.insert(tk.END, f"{message}\n")
        
        # Apply color to the last line
        last_line_start = self.diag_output.index("end-2c linestart")
        last_line_end = self.diag_output.index("end-1c")
        
        # Create tag for color if it doesn't exist
        tag_name = f"color_{color}"
        if tag_name not in self.diag_output.tag_names():
            self.diag_output.tag_configure(tag_name, foreground=color)
        
        self.diag_output.tag_add(tag_name, last_line_start, last_line_end)
        
        # Auto-scroll if enabled
        if self.diag_autoscroll_var.get():
            self.diag_output.see(tk.END)
    
    def clear_diagnostic_output(self):
        """Clear diagnostic output"""
        self.diag_output.delete(1.0, tk.END)
        self.log_diagnostic("=== Output cleared ===", "gray")
    
    def save_diagnostic_log(self):
        """Save diagnostic log to file"""
        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"diagnostic_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        
        if filename:
            try:
                content = self.diag_output.get(1.0, tk.END)
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(content)
                messagebox.showinfo("Success", f"Log saved to:\n{filename}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save log:\n{str(e)}")
        
    # ==================== Serial Communication ====================
    
    def refresh_ports(self):
        """Refresh available serial ports"""
        ports = [p.device for p in list_ports.comports()]
        self.port_combo['values'] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])
    
    def connect_device(self):
        """Connect to selected serial port"""
        port = self.port_var.get()
        if not port:
            messagebox.showerror("Error", "Please select a port")
            return
        
        try:
            self.serial_port = serial.Serial(port, baudrate=115200, timeout=2.0)
            self.current_port = port
            self.send_command("HELLO v=1")
            response = self.read_line()
            
            if response and response.startswith("OK"):
                self.conn_status.config(text="🟢 Connected", foreground="green")
                self.log_info(f"Connected to {port}\n{response}")
                self.status_bar.config(text=f"Connected to {port}")
            else:
                raise Exception("No valid response from device")
                
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))
            self.disconnect_device()
    
    def disconnect_device(self):
        """Disconnect from device"""
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None
        self.conn_status.config(text="⚫ Disconnected", foreground="red")
        self.status_bar.config(text="Disconnected")
        self.current_port = ""
    
    def send_command(self, cmd: str):
        """Send command to device"""
        if not self.serial_port:
            raise Exception("Not connected")
        self.serial_port.write((cmd + "\n").encode('utf-8'))
        self.serial_port.flush()
    
    def read_line(self, timeout: float = 2.0) -> str:
        """Read line from device"""
        if not self.serial_port:
            return ""
        self.serial_port.timeout = timeout
        line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
        return line
    
    def check_connection(self) -> bool:
        """Check if connected"""
        if not self.serial_port:
            messagebox.showerror("Error", "Not connected to device")
            return False
        return True
    
    # ==================== Device Commands ====================
    
    def get_time(self):
        """Get RTC time"""
        if not self.check_connection():
            return
        try:
            self.send_command("TIME?")
            response = self.read_line()
            self.log_info(f"RTC Time: {response}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_time_now(self):
        """Set RTC to current PC time"""
        if not self.check_connection():
            return
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.send_command(f"SETTIME {now}")
            response = self.read_line()
            self.log_info(f"Set time to {now}\n{response}")
            messagebox.showinfo("Success", f"Time set to: {now}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_custom_time(self):
        """Set RTC to custom time"""
        if not self.check_connection():
            return
        try:
            time_str = self.rtc_time_var.get()
            self.send_command(f"SETTIME {time_str}")
            response = self.read_line()
            self.log_info(f"Set time to {time_str}\n{response}")
            messagebox.showinfo("Success", f"Time set to: {time_str}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def read_sensors(self):
        """Read temperature and humidity"""
        if not self.check_connection():
            return
        try:
            self.send_command("SENSE?")
            response = self.read_line()
            self.log_info(f"Sensor Reading:\n{response}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def read_battery(self):
        """Read battery voltage"""
        if not self.check_connection():
            return
        try:
            self.send_command("VBAT?")
            response = self.read_line()
            self.log_info(f"Battery: {response}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def get_interval(self):
        """Get logging interval"""
        if not self.check_connection():
            return
        try:
            self.send_command("INTERVAL?")
            response = self.read_line()
            if "INTERVAL=" in response:
                seconds = response.split("=")[1]
                self.current_interval.config(text=f"{seconds}s ({int(seconds)//60}min)")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_interval(self):
        """Set logging interval"""
        if not self.check_connection():
            return
        try:
            seconds = int(self.interval_var.get())
            self.send_command(f"SETINTERVAL {seconds}")
            response = self.read_line()
            self.log_info(f"Set interval: {response}")
            self.get_interval()
            messagebox.showinfo("Success", f"Interval set to {seconds} seconds")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def get_heartbeat(self):
        """Get heartbeat status"""
        if not self.check_connection():
            return
        try:
            self.send_command("HEARTBEAT?")
            response = self.read_line()
            self.log_info(f"Heartbeat: {response}")
            if "ON=" in response:
                status = "ENABLED" if "ON=1" in response else "DISABLED"
                self.hb_status.config(text=status)
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def heartbeat_on(self):
        """Enable heartbeat"""
        if not self.check_connection():
            return
        try:
            self.send_command("HEARTBEAT ON")
            response = self.read_line()
            self.log_info(f"Heartbeat enabled: {response}")
            self.get_heartbeat()
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def heartbeat_off(self):
        """Disable heartbeat"""
        if not self.check_connection():
            return
        try:
            self.send_command("HEARTBEAT OFF")
            response = self.read_line()
            self.log_info(f"Heartbeat disabled: {response}")
            self.get_heartbeat()
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_heartbeat_period(self):
        """Set heartbeat period"""
        if not self.check_connection():
            return
        try:
            period = int(self.hb_period_var.get())
            self.send_command(f"SETHEARTBEAT {period}")
            response = self.read_line()
            self.log_info(f"Set heartbeat period: {response}")
            messagebox.showinfo("Success", f"Heartbeat period set to {period} seconds")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def reboot_device(self):
        """Reboot the device"""
        if not self.check_connection():
            return
        if messagebox.askyesno("Confirm", "Reboot the device?"):
            try:
                self.send_command("REBOOT")
                response = self.read_line()
                self.log_info(f"Rebooting: {response}")
                self.disconnect_device()
            except Exception as e:
                messagebox.showerror("Error", str(e))
    
    # ==================== Data Management (BIN FORMAT) ====================
    
    def list_dates(self):
        """List available dates with BIN file info"""
        if not self.check_connection():
            return
        try:
            self.send_command("LIST")
            header = self.read_line()
            
            # Clear tree
            for item in self.dates_tree.get_children():
                self.dates_tree.delete(item)
            
            while True:
                line = self.read_line()
                if line == "END":
                    break
                if line.startswith("DATE="):
                    # Parse: DATE=YYYY-MM-DD SIZE_KB=123 ROWS=456
                    parts = line.split()
                    date = parts[0].split("=")[1]
                    size_kb = parts[1].split("=")[1]
                    rows = parts[2].split("=")[1]
                    self.dates_tree.insert('', 'end', values=(date, size_kb, rows))
            
            self.status_bar.config(text=f"Found {len(self.dates_tree.get_children())} log dates")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def download_selected(self):
        """Download selected date as BIN and auto-decode to CSV"""
        selection = self.dates_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a date to download")
            return

        date = self.dates_tree.item(selection[0])['values'][0]

        # Ask for save location (BIN file)
        bin_filename = filedialog.asksaveasfilename(
            defaultextension=".bin",
            initialfile=f"{date}.bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")]
        )
        if not bin_filename:
            return

        # Guard re-entrancy and keep UI responsive
        if getattr(self, "_is_downloading", False):
            return
        self._is_downloading = True
        if hasattr(self, "download_btn"):
            self.download_btn.config(state="disabled")

        def download():
            prev_timeout = getattr(self.serial_port, "timeout", None)
            try:
                self.root.after(0, lambda d=date: self.status_bar.config(text=f"Downloading {d}..."))

                # Request file
                self.send_command(f"GET {date}")

                response = self.read_line()
                if not response or not response.startswith("OK"):
                    raise Exception(f"Bad response: {response!r}")

                # Parse size
                try:
                    size = int(response.split("SIZE=")[1].split()[0])
                except Exception:
                    raise Exception(f"Couldn't parse size from: {response!r}")

                # Expect DATA marker
                marker = self.read_line()
                if marker != "DATA":
                    raise Exception(f"Expected 'DATA', got {marker!r}")

                # Read file content with progress and a simple stall watchdog
                self.serial_port.timeout = 10.0  # temporary bump
                data = bytearray()
                last_pct = -1
                stalled_reads = 0
                STALL_LIMIT = 30  # ~30 empty reads with 10s timeout each is extreme; adjust to your protocol

                while len(data) < size:
                    chunk = self.serial_port.read(min(4096, size - len(data)))
                    if chunk:
                        data += chunk
                        stalled_reads = 0
                        pct = int(100 * len(data) / size)
                        if pct != last_pct:
                            self.root.after(0, lambda p=pct, d=date: self.status_bar.config(text=f"Downloading {d}... {p}%"))
                            last_pct = pct
                    else:
                        stalled_reads += 1
                        if stalled_reads >= STALL_LIMIT:
                            raise Exception("Serial read stalled (no data)")

                # Save BIN
                from pathlib import Path
                bin_path = Path(bin_filename)
                bin_path.parent.mkdir(parents=True, exist_ok=True)
                with open(bin_path, 'wb') as f:
                    f.write(data)

                # Read END marker (and validate if your firmware sends 'END')
                end_marker = self.read_line()
                if end_marker and end_marker.strip() not in ("END", "OK", ""):
                    # Not fatal, but useful to surface
                    self.root.after(0, lambda m=end_marker: self.status_bar.config(text=f"Note: trailer {m!r}"))

                # Auto-decode to CSV
                csv_path = bin_path.parent / "decoded" / f"{date}.csv"
                try:
                    csv_path.parent.mkdir(parents=True, exist_ok=True)
                    stats = decode_bin_to_csv(bin_path, csv_path)

                    msg = (
                        f"Downloaded to:\n{bin_path}\n\n"
                        f"Decoded to:\n{csv_path}\n\n"
                        f"Records: {stats.get('records', '?')}\n"
                        f"Bytes: {stats.get('bytes', len(data))}\n"
                    )
                    if stats.get('tail_bytes', 0) > 0:
                        msg += f"Warning: {stats['tail_bytes']} trailing bytes ignored"

                    from functools import partial
                    self.root.after(0, partial(messagebox.showinfo, "Success", msg))
                    self.root.after(0, lambda d=date: self.status_bar.config(text=f"Downloaded & decoded {d}"))
                except Exception as de:
                    # Bind the message NOW so we don't hit the scoping bug
                    self.root.after(0, lambda m=str(de), b=str(bin_path): messagebox.showwarning(
                        "Partial Success",
                        f"Downloaded BIN but decode failed:\n{m}\n\nBIN saved to:\n{b}"
                    ))

            except Exception as e:
                # ✅ FIX: capture e at schedule-time
                self.root.after(0, lambda m=str(e): messagebox.showerror("Error", m))

            finally:
                # Always restore serial timeouts and UI state so a second download works
                try:
                    if prev_timeout is not None:
                        self.serial_port.timeout = prev_timeout
                    # Optional hygiene (won't hurt): clear buffers
                    if self.serial_port and self.serial_port.is_open:
                        try:
                            self.serial_port.reset_input_buffer()
                            self.serial_port.reset_output_buffer()
                        except Exception:
                            pass
                finally:
                    self._is_downloading = False
                    self.root.after(0, lambda: hasattr(self, "download_btn") and self.download_btn.config(state="normal"))
                    self.root.after(0, lambda: self.status_bar.config(text="Ready"))

        import threading
        threading.Thread(target=download, daemon=True).start()

    
    def delete_selected(self):
        """Delete selected date"""
        selection = self.dates_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a date to delete")
            return
        
        date = self.dates_tree.item(selection[0])['values'][0]
        
        if not messagebox.askyesno("Confirm Delete", f"Delete all data for {date}?\nThis cannot be undone!"):
            return
        
        if not self.check_connection():
            return
        
        try:
            self.send_command(f"DEL {date}")
            response = self.read_line()
            self.log_info(f"Deleted {date}: {response}")
            self.list_dates()
            messagebox.showinfo("Success", f"Deleted {date}")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def plot_selected(self):
        """Plot selected date (download, decode, and plot)"""
        selection = self.dates_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "Please select a date to plot")
            return
        
        date = self.dates_tree.item(selection[0])['values'][0]
        
        # Download to temp file, decode, and plot
        import tempfile
        temp_dir = tempfile.mkdtemp()
        temp_bin = os.path.join(temp_dir, f"{date}.bin")
        temp_csv = os.path.join(temp_dir, f"{date}.csv")
        
        def download_and_plot():
            try:
                self.status_bar.config(text=f"Downloading {date} for plotting...")
                self.send_command(f"GET {date}")
                
                response = self.read_line()
                if not response.startswith("OK"):
                    raise Exception(f"Error: {response}")
                
                size = int(response.split("SIZE=")[1].split()[0])
                marker = self.read_line()
                if marker != "DATA":
                    raise Exception(f"Expected DATA, got {marker}")
                
                self.serial_port.timeout = 10.0
                data = self.serial_port.read(size)
                
                # Save BIN
                with open(temp_bin, 'wb') as f:
                    f.write(data)
                
                self.read_line()  # END marker
                
                # Decode to CSV
                stats = decode_bin_to_csv(Path(temp_bin), Path(temp_csv))
                
                # Load and plot
                self.root.after(0, lambda: self.load_and_plot_file(temp_csv))
                self.root.after(0, lambda: self.notebook.select(3))  # Switch to plot tab
                self.root.after(0, lambda: self.status_bar.config(text=f"Plotted {date} ({stats['records']} records)"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                try:
                    os.unlink(temp_bin)
                    os.unlink(temp_csv)
                    os.rmdir(temp_dir)
                except:
                    pass
        
        threading.Thread(target=download_and_plot, daemon=True).start()
    
    def get_storage_info(self):
        """Get storage information"""
        if not self.check_connection():
            return
        try:
            self.storage_text.delete(1.0, tk.END)
            
            # Get quick info
            self.send_command("FLASH-FREE")
            while True:
                line = self.read_line(timeout=1.0)
                if not line or "Free %" in line:
                    self.storage_text.insert(tk.END, line + "\n")
                    if "Free %" in line:
                        break
                else:
                    self.storage_text.insert(tk.END, line + "\n")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def format_storage(self):
        """Format internal storage (dangerous!)"""
        code = self.format_code_var.get()
        if not code:
            messagebox.showerror("Error", "Please enter format code")
            return
        
        if not messagebox.askyesno("⚠️ FINAL WARNING - INTERNAL FLASH", 
                                   "ALL INTERNAL FLASH DATA WILL BE PERMANENTLY DELETED!\n\n"
                                   "This action CANNOT be undone.\n\n"
                                   "Are you ABSOLUTELY sure?"):
            return
        
        if not self.check_connection():
            return
        
        try:
            self.send_command(f"FORMAT {code}")
            response = self.read_line()
            if response != "OK FORMAT START":
                raise Exception(response)
            
            self.status_bar.config(text="Formatting internal storage... (this may take a few seconds)")
            response = self.read_line(timeout=10.0)
            
            self.log_info(f"Format result: {response}")
            self.storage_text.delete(1.0, tk.END)
            self.storage_text.insert(tk.END, f"Internal storage formatted successfully\n{response}")
            messagebox.showinfo("Success", "Internal storage formatted")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def format_ext_storage(self):
        """Format external flash storage (dangerous!)"""
        code = self.ext_format_code_var.get()
        if not code:
            messagebox.showerror("Error", "Please enter format code")
            return
        
        if not messagebox.askyesno("⚠️ FINAL WARNING - EXTERNAL FLASH", 
                                   "ALL EXTERNAL FLASH DATA WILL BE PERMANENTLY DELETED!\n\n"
                                   "This includes ALL logged records in external memory.\n\n"
                                   "This action CANNOT be undone.\n\n"
                                   "Are you ABSOLUTELY sure?"):
            return
        
        if not self.check_connection():
            return
        
        try:
            self.send_command(f"EXT-FORMAT {code}")
            response = self.read_line()
            if response != "OK EXT-FORMAT START":
                raise Exception(response)
            
            self.status_bar.config(text="Formatting external flash... (this may take several seconds)")
            response = self.read_line(timeout=15.0)
            
            self.log_info(f"External format result: {response}")
            messagebox.showinfo("Success", "External flash formatted successfully")
            # Refresh diagnostics after format
            self.refresh_ext_diagnostics()
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    # ==================== Plotting ====================
    
    def load_csv_for_plot(self):
        """Load CSV file for plotting"""
        filename = filedialog.askopenfilename(
            title="Select CSV file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if filename:
            self.load_and_plot_file(filename)
    
    def load_and_plot_file(self, filepath: str):
        """Load and plot a CSV file"""
        try:
            # Read CSV
            df = pd.read_csv(filepath)
            df.columns = [c.strip() for c in df.columns]
            
            # Find columns
            time_col = self.find_column(df, ['time', 'timestamp', 'Time'])
            temp_col = self.find_column(df, ['temperature', 'temp', 'temp_c'], required=False)
            rh_col = self.find_column(df, ['humidity', 'rh', 'RH'], required=False)
            vbat_col = self.find_column(df, ['vbat_mv', 'vbat_mV', 'vbat', 'VBAT_mV'], required=False)
            
            if not time_col:
                raise Exception("Could not find time column")
            
            # Parse time
            df['_time'] = pd.to_datetime(df[time_col], errors='coerce', format='%H:%M:%S')
            df = df.dropna(subset=['_time'])
            
            # Convert numeric columns
            if temp_col:
                df['temp'] = pd.to_numeric(df[temp_col], errors='coerce')
            if rh_col:
                df['rh'] = pd.to_numeric(df[rh_col], errors='coerce')
            if vbat_col:
                df['vbat'] = pd.to_numeric(df[vbat_col], errors='coerce')
            
            self.downloaded_data = df
            self.refresh_plot()
            self.update_stats()
            
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load CSV: {str(e)}")
    
    def find_column(self, df: pd.DataFrame, candidates: List[str], required: bool = True) -> Optional[str]:
        """Find column by candidates"""
        cols = {c.lower(): c for c in df.columns}
        for cand in candidates:
            if cand.lower() in cols:
                return cols[cand.lower()]
        if required:
            raise KeyError(f"None of {candidates} found in columns: {list(df.columns)}")
        return None
    
    def refresh_plot(self):
        """Refresh the plot with current data"""
        if self.downloaded_data is None:
            return
        
        df = self.downloaded_data
        self.fig.clear()
        
        # Count subplots needed
        n_plots = sum([
            self.plot_temp_var.get() and 'temp' in df.columns,
            self.plot_rh_var.get() and 'rh' in df.columns,
            self.plot_vbat_var.get() and 'vbat' in df.columns
        ])
        
        if n_plots == 0:
            return
        
        axes = self.fig.subplots(n_plots, 1, sharex=True)
        if n_plots == 1:
            axes = [axes]
        
        idx = 0
        
        # Temperature
        if self.plot_temp_var.get() and 'temp' in df.columns:
            ax = axes[idx]
            ax.plot(df['_time'], df['temp'], 'r-', linewidth=1.5, label='Temperature')
            ax.set_ylabel('Temperature (°C)', color='r')
            ax.tick_params(axis='y', labelcolor='r')
            ax.grid(True, alpha=0.3)
            idx += 1
        
        # Humidity
        if self.plot_rh_var.get() and 'rh' in df.columns:
            ax = axes[idx]
            ax.plot(df['_time'], df['rh'], 'b-', linewidth=1.5, label='Humidity')
            ax.set_ylabel('Humidity (%)', color='b')
            ax.tick_params(axis='y', labelcolor='b')
            ax.grid(True, alpha=0.3)
            idx += 1
        
        # Battery
        if self.plot_vbat_var.get() and 'vbat' in df.columns:
            ax = axes[idx]
            ax.plot(df['_time'], df['vbat'], 'g-', linewidth=1.5, label='Battery')
            ax.set_ylabel('Battery (mV)', color='g')
            ax.tick_params(axis='y', labelcolor='g')
            ax.grid(True, alpha=0.3)
            
            # Add low battery threshold
            ax.axhline(3300, color='orange', linestyle='--', alpha=0.5, label='Low (3.3V)')
            ax.axhline(3000, color='red', linestyle='--', alpha=0.5, label='Critical (3.0V)')
            ax.legend(loc='upper right', fontsize=8)
            idx += 1
        
        # Format x-axis
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        axes[-1].set_xlabel('Time')
        
        self.fig.tight_layout()
        self.canvas.draw()
    
    def update_stats(self):
        """Update statistics display"""
        if self.downloaded_data is None:
            return
        
        df = self.downloaded_data
        self.stats_text.delete(1.0, tk.END)
        
        stats = []
        
        if 'temp' in df.columns:
            temp_data = df['temp'].dropna()
            if not temp_data.empty:
                stats.append(f"🌡️  Temperature: min={temp_data.min():.2f}°C  "
                           f"max={temp_data.max():.2f}°C  avg={temp_data.mean():.2f}°C")
        
        if 'rh' in df.columns:
            rh_data = df['rh'].dropna()
            if not rh_data.empty:
                stats.append(f"💧 Humidity: min={rh_data.min():.2f}%  "
                           f"max={rh_data.max():.2f}%  avg={rh_data.mean():.2f}%")
        
        if 'vbat' in df.columns:
            vbat_data = df['vbat'].dropna()
            if not vbat_data.empty:
                stats.append(f"🔋 Battery: min={vbat_data.min():.0f}mV  "
                           f"max={vbat_data.max():.0f}mV  avg={vbat_data.mean():.0f}mV")
        
        stats.append(f"\n📊 Total data points: {len(df)}")
        
        self.stats_text.insert(1.0, '\n'.join(stats))
    
    # ==================== Storage Mode Commands ====================
    
    def get_storage_mode(self):
        """Get current storage mode"""
        if not self.check_connection():
            return
        try:
            self.send_command("STORAGE?")
            response = self.read_line()
            if response.startswith("OK STORAGE="):
                # Parse: OK STORAGE=INTERNAL or OK STORAGE=AUTO THRESHOLD=90% SWITCHED=NO
                parts = response.split()
                mode = parts[1].split("=")[1]
                self.storage_mode_var.set(mode)
                self.storage_mode_status.config(text=mode)
                
                # Parse additional info for AUTO mode
                if "THRESHOLD=" in response:
                    for part in parts:
                        if part.startswith("THRESHOLD="):
                            thresh = part.split("=")[1].rstrip("%")
                            self.auto_threshold_var.set(thresh)
                        elif part.startswith("SWITCHED="):
                            switched = part.split("=")[1]
                            if switched == "YES":
                                self.auto_status_label.config(text="Switched to EXTERNAL", foreground="orange")
                            else:
                                self.auto_status_label.config(text="Not switched", foreground="blue")
                
                self.log_info(f"Storage mode: {response}")
            else:
                raise Exception(response)
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_storage_mode(self):
        """Set storage mode"""
        if not self.check_connection():
            return
        try:
            mode = self.storage_mode_var.get()
            self.send_command(f"SETSTORAGE {mode}")
            response = self.read_line()
            self.log_info(f"Set storage mode: {response}")
            if response.startswith("OK STORAGE="):
                messagebox.showinfo("Success", f"Storage mode set to {mode}")
                self.get_storage_mode()  # Refresh status
            else:
                raise Exception(response)
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def set_auto_threshold(self):
        """Set auto-failover threshold"""
        if not self.check_connection():
            return
        try:
            threshold = int(self.auto_threshold_var.get())
            if threshold < 5 or threshold > 95:
                messagebox.showerror("Error", "Threshold must be between 5-95%")
                return
            
            self.send_command(f"AUTO-THRESHOLD-SET {threshold}")
            response = self.read_line()
            self.log_info(f"Set auto threshold: {response}")
            if response.startswith("OK AUTO-THRESHOLD="):
                messagebox.showinfo("Success", f"Auto threshold set to {threshold}%")
            else:
                raise Exception(response)
        except ValueError:
            messagebox.showerror("Error", "Invalid threshold value")
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    def reset_auto_switch(self):
        """Reset auto-switched flag"""
        if not self.check_connection():
            return
        if not messagebox.askyesno("Confirm", "Reset auto-switch flag? This will allow auto-mode to use internal flash again."):
            return
        try:
            self.send_command("AUTO-RESET")
            response = self.read_line()
            self.log_info(f"Reset auto-switch: {response}")
            if "OK" in response:
                messagebox.showinfo("Success", "Auto-switch flag reset")
                self.get_storage_mode()  # Refresh status
            else:
                raise Exception(response)
        except Exception as e:
            messagebox.showerror("Error", str(e))
    
    # ==================== External Flash Diagnostics ====================
    
    def refresh_ext_diagnostics(self):
        """Refresh external flash diagnostics"""
        if not self.check_connection():
            return
        try:
            self.send_command("EXT-INFO")
            
            # Read until OK
            lines = []
            while True:
                line = self.read_line(timeout=2.0)
                if not line:
                    break
                lines.append(line)
                if line.strip() == "OK":
                    break
            
            # Parse the response
            info = {}
            for line in lines:
                if ":" in line:
                    key, value = line.split(":", 1)
                    info[key.strip()] = value.strip()
            
            # Update diagnostics display
            self.ext_chip_id.config(text=info.get("Chip ID", "--"))
            
            # Parse capacity
            capacity_str = info.get("Capacity", "--")
            if "bytes" in capacity_str:
                capacity_bytes = int(capacity_str.split()[0])
                capacity_kb = capacity_bytes / 1024
                self.ext_capacity.config(text=f"{capacity_kb:.0f} KB ({capacity_bytes} bytes)")
            else:
                self.ext_capacity.config(text=capacity_str)
            
            self.ext_sector_size.config(text=info.get("Sector size", "--"))
            self.ext_record_size.config(text=info.get("Record size", "--"))
            self.ext_data_region.config(text=info.get("Data region", "--"))
            self.ext_tail.config(text=info.get("Current tail", "--"))
            self.ext_seq.config(text=info.get("Superblock seq", "--"))
            
            # Parse records used line: "Records used : 123 / 456 (27%)"
            records_line = info.get("Records used", "--")
            if "/" in records_line:
                parts = records_line.split("/")
                used = parts[0].strip()
                rest = parts[1].split("(")
                total = rest[0].strip()
                pct = rest[1].rstrip(")")
                
                self.ext_records_used.config(text=used)
                self.ext_records_total.config(text=total)
                self.ext_usage_pct.config(text=pct)
                
                # Calculate space (assuming 8 bytes per record)
                try:
                    used_int = int(used)
                    total_int = int(total)
                    used_bytes = used_int * 8
                    total_bytes = total_int * 8
                    free_bytes = total_bytes - used_bytes
                    
                    self.ext_used_space.config(text=f"{used_bytes / 1024:.2f} KB")
                    self.ext_free_space.config(text=f"{free_bytes / 1024:.2f} KB")
                    
                    # Calculate sectors (4096 bytes per sector)
                    written_sectors = (used_bytes + 4095) // 4096
                    current_sector = (used_bytes // 4096) + 2  # +2 for superblock sectors
                    
                    self.ext_sectors_written.config(text=str(written_sectors))
                    self.ext_current_sector.config(text=str(current_sector))
                except:
                    pass
            else:
                self.ext_records_used.config(text="--")
                self.ext_records_total.config(text="--")
                self.ext_usage_pct.config(text="--")
                self.ext_free_space.config(text="--")
                self.ext_used_space.config(text="--")
                self.ext_sectors_written.config(text="--")
                self.ext_current_sector.config(text="--")
            
            self.log_info("External flash diagnostics refreshed")
            self.status_bar.config(text="External flash diagnostics updated")
            
        except Exception as e:
            # Clear all fields on error
            for attr in ['ext_chip_id', 'ext_capacity', 'ext_sector_size', 'ext_record_size',
                        'ext_data_region', 'ext_tail', 'ext_seq', 'ext_records_used',
                        'ext_records_total', 'ext_usage_pct', 'ext_free_space', 'ext_used_space',
                        'ext_sectors_written', 'ext_current_sector']:
                getattr(self, attr).config(text="N/A")
            messagebox.showerror("Error", f"Failed to get external flash info:\n{str(e)}")
    
    def list_ext_sectors(self):
        """List external flash sectors"""
        if not self.check_connection():
            return
        try:
            self.ext_sector_text.delete(1.0, tk.END)
            self.ext_sector_text.insert(tk.END, "Fetching sector information...\n")
            self.status_bar.config(text="Listing external flash sectors...")
            
            self.send_command("EXT-LIST-SECTORS")
            
            # Read all lines until OK,DONE
            while True:
                line = self.read_line(timeout=2.0)
                if not line:
                    break
                self.ext_sector_text.insert(tk.END, line + "\n")
                self.ext_sector_text.see(tk.END)
                if "OK,DONE" in line:
                    break
            
            self.status_bar.config(text="Sector list updated")
            
        except Exception as e:
            self.ext_sector_text.insert(tk.END, f"\nError: {str(e)}\n")
            messagebox.showerror("Error", str(e))
    
    # ==================== Utilities ====================
    
    def log_info(self, message: str):
        """Log message to info text"""
        self.info_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.info_text.see(tk.END)


def main():
    root = tk.Tk()
    
    # Configure style
    style = ttk.Style()
    style.theme_use('clam')
    
    # Create danger button style
    style.configure('Danger.TButton', foreground='red')
    
    app = LoggerGUI(root)
    
    # Handle window close
    def on_closing():
        if app.serial_port:
            app.disconnect_device()
        root.destroy()
    
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()