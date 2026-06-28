#!/usr/bin/env python3
"""
Orion BMS Junior - RS232 OBD2 Data Logger -> CSV
================================================
For: Raspberry Pi 4 + MAX3232 (RS232<->TTL) connected to an Orion Jr. BMS.

Protocol (Orion App-Note AN2586, "Retrieving Data With 3rd Party Devices"):
  * Serial: 9600 baud, 8 data bits, EVEN parity, 1 stop bit (8E1)
  * Every request/response is ASCII-hex, starts with ':' and ends with '\n'
  * Request frame : :<LEN><MODE><PID>          e.g. SOC = :0322F00F\n
  * Response frame: :<LEN><MODE+0x40><PID><DATA>  e.g. :0562F00F0064\n
        LEN counts MODE + PID + DATA bytes (NOT the leading ':' nor the LEN byte)
  * RS232 has a single BMS, so NO ECU ID is sent (unlike CANBUS).

Wiring (MAX3232 module -> Raspberry Pi 4 header):
  MAX3232 VCC -> Pi 3.3V  (Pin 1)   << 3.3V, NOT 5V
  MAX3232 GND -> Pi GND   (Pin 6)
  MAX3232 TX  -> Pi RXD / GPIO15 (Pin 10)
  MAX3232 RX  -> Pi TXD / GPIO14 (Pin 8)
  MAX3232 DB9 -> Orion Jr. serial port

Install:  pip3 install pyserial
Run:      python3 orion_bms_logger.py
"""

import csv
import math
import os
import sys
import time
from datetime import datetime

try:
    import serial
except ImportError:
    sys.exit("Missing dependency. Install it with:  pip3 install pyserial")

# ── Configuration ─────────────────────────────────────────────
SERIAL_PORT = "/dev/ttyS0"  # Pi 4 built-in UART. If no data, try "/dev/ttyAMA0"
BAUD_RATE = 9600  # Orion Jr. RS232 is fixed at 9600 8E1
LOG_INTERVAL = 1.0  # seconds between samples
NUM_CELLS = 16  # number of cells in your pack
LOG_DIR = "/home/pi/bms_logs"  # where CSV files are written
READ_TIMEOUT = 0.3  # serial read timeout (s) per request
RECONNECT_AFTER = 10  # consecutive dead samples before reopening the port
# ──────────────────────────────────────────────────────────────

# OBD2 Mode $22 PIDs (verified against orionbms_obd2_pids.pdf, rev 8/27/2018).
# (name, pid, scaling, signed)  -> value = raw * scaling
SCALAR_PIDS = [
    ("soc_pct", 0xF00F, 0.5, False),  # State of Charge, %
    ("pack_v", 0xF00D, 0.1, False),  # Pack Voltage, V
    (
        "pack_a",
        0xF00C,
        0.1,
        True,
    ),  # Signed Pack Current, A (+charge / -discharge per BMS config)
    ("temp_high_c", 0xF028, 1.0, True),  # Highest pack temperature, C
    ("temp_low_c", 0xF029, 1.0, True),  # Lowest pack temperature, C
    ("temp_avg_c", 0xF02A, 1.0, True),  # Average pack temperature, C
    ("low_cell_v", 0xF032, 0.0001, False),  # Low cell voltage, V
    ("high_cell_v", 0xF033, 0.0001, False),  # High cell voltage, V
    ("avg_cell_v", 0xF034, 0.0001, False),  # Average cell voltage, V
    ("pack_health_pct", 0xF013, 1.0, False),  # Pack health, %
    ("pack_cycles", 0xF018, 1.0, False),  # Total pack cycles
]

# Cell-voltage block PIDs: each returns up to 12 cells (2 bytes each), 0.0001 V/bit.
CELL_BLOCK_PIDS = [
    0xF100,
    0xF101,
    0xF102,
    0xF103,
    0xF104,
    0xF105,
    0xF106,
    0xF107,
    0xF108,
    0xF109,
    0xF10A,
    0xF10B,
    0xF10C,
    0xF10D,
    0xF10E,
]


# ── Serial framing helpers ────────────────────────────────────
def open_serial():
    """Open the RS232 port as 9600 8E1 (the Orion Jr. fixed settings)."""
    return serial.Serial(
        port=SERIAL_PORT,
        baudrate=BAUD_RATE,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_EVEN,
        stopbits=serial.STOPBITS_ONE,
        timeout=READ_TIMEOUT,
    )


def build_request(mode: int, pid: int | None = None, pid_bytes: int = 2) -> bytes:
    """
    Build an Orion RS232 OBD2 request frame.
    LEN = number of bytes in (MODE + PID).  No ECU ID on RS232.
    """
    body = f"{mode:02X}"
    if pid is not None:
        body += f"{pid:0{pid_bytes * 2}X}"
    length = len(body) // 2
    return f":{length:02X}{body}\n".encode("ascii")


def transact(ser, request: bytes) -> bytes | None:
    """Send a request, return the decoded response body (MODE..DATA) or None."""
    ser.reset_input_buffer()
    ser.write(request)
    line = ser.readline().decode("ascii", errors="ignore").strip()
    if not line.startswith(":"):
        return None
    try:
        raw = bytes.fromhex(line[1:])
    except ValueError:
        return None
    if not raw:
        return None
    length = raw[0]
    if len(raw) < 1 + length or length < 1:
        return None
    return raw[1 : 1 + length]  # body = MODE (+ PID) (+ DATA)


def read_pid(ser, pid: int, signed: bool = False) -> int | None:
    """Mode $22 read of one PID. Returns the raw integer (pre-scaling) or None."""
    body = transact(ser, build_request(0x22, pid))
    if body is None or body[0] != 0x62 or len(body) < 4:
        return (
            None  # response mode for $22 is $62; body = [62][pid_hi][pid_lo][data...]
        )
    data = body[3:]
    if not data:
        return None
    return int.from_bytes(data, "big", signed=signed)


def read_scaled(ser, pid: int, scaling: float, signed: bool):
    raw = read_pid(ser, pid, signed=signed)
    if raw is None:
        return None
    val = raw * scaling
    return round(val, 4) if scaling < 1 else round(val, 1)


def read_cell_voltages(ser, num_cells: int) -> list:
    """Read individual cell voltages across as many 12-cell blocks as needed."""
    voltages = [None] * num_cells
    blocks = math.ceil(num_cells / 12)
    for b in range(blocks):
        body = transact(ser, build_request(0x22, CELL_BLOCK_PIDS[b]))
        if body is None or body[0] != 0x62 or len(body) < 4:
            continue
        data = body[3:]
        for i in range(12):
            cell_idx = b * 12 + i
            if cell_idx >= num_cells:
                break
            off = i * 2
            if off + 1 < len(data):
                raw = (data[off] << 8) | data[off + 1]
                voltages[cell_idx] = round(raw * 0.0001, 4)
    return voltages


def read_faults(ser) -> str:
    """Mode $3 active DTC read. Returns pipe-joined codes or 'NONE'."""
    body = transact(ser, build_request(0x03))  # no PID for mode $3
    if body is None or body[0] != 0x43 or len(body) < 2:
        return "NONE"
    count = body[1]
    if count == 0:
        return "NONE"
    codes = []
    for i in range(count):
        idx = 2 + i * 2
        if idx + 1 < len(body):
            codes.append(f"P{body[idx]:02X}{body[idx + 1]:02X}")
    return "|".join(codes) if codes else "NONE"


def connection_ok(ser) -> bool:
    """
    Self-test: ask Mode $9 PID $0B (returns ASCII 'ORIONBMS'); if that yields
    nothing, fall back to a plain SOC poll. Either success means we are talking.
    """
    ser.reset_input_buffer()
    ser.write(b":02090B\n")  # Mode $9, PID $0B (1-byte PID)
    line = ser.readline().decode("ascii", errors="ignore")
    if "ORIONBMS" in line:
        return True
    try:
        raw = bytes.fromhex(line.strip()[1:]) if line.strip().startswith(":") else b""
        if b"ORIONBMS" in raw:
            return True
    except ValueError:
        pass
    return read_pid(ser, 0xF00F) is not None  # fallback: can we read SOC?


# ── CSV ───────────────────────────────────────────────────────
def build_header(num_cells: int) -> list:
    header = ["timestamp", "elapsed_s"]
    header += [name for (name, *_rest) in SCALAR_PIDS]
    header += ["faults"]
    header += [f"cell{i + 1}_v" for i in range(num_cells)]
    return header


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    filename = os.path.join(LOG_DIR, datetime.now().strftime("LOG_%Y%m%d_%H%M%S.csv"))

    print("Orion BMS Jr. Logger")
    print(f"  Port    : {SERIAL_PORT} @ {BAUD_RATE} 8E1")
    print(f"  Interval: {LOG_INTERVAL}s   Cells: {NUM_CELLS}")
    print(f"  Logfile : {filename}")
    print("  Stop    : Ctrl+C\n")

    try:
        ser = open_serial()
    except serial.SerialException as e:
        print(f"ERROR: cannot open serial port: {e}")
        print("Check: sudo raspi-config -> Interface Options -> Serial Port")
        print("       and that your user is in the 'dialout' group.")
        return

    if connection_ok(ser):
        print("BMS handshake OK.\n")
    else:
        print("WARNING: no valid response from BMS yet (logging anyway).")
        print("         Check wiring, SERIAL_PORT, and that the BMS is powered.\n")

    start = time.time()
    sample = 0
    dead_streak = 0

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(build_header(NUM_CELLS))
        f.flush()
        try:
            while True:
                loop_start = time.time()

                values = {}
                for name, pid, scaling, signed in SCALAR_PIDS:
                    values[name] = read_scaled(ser, pid, scaling, signed)
                faults = read_faults(ser)
                cells = read_cell_voltages(ser, NUM_CELLS)

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                elapsed = round(time.time() - start, 1)
                row = [ts, elapsed]
                row += [values[name] for (name, *_rest) in SCALAR_PIDS]
                row += [faults]
                row += cells
                writer.writerow(row)
                f.flush()  # write to disk now so a power cut loses at most one row

                print(
                    f"#{sample:05d} | {ts} | SOC: {values['soc_pct']}% | "
                    f"V: {values['pack_v']} | A: {values['pack_a']} | "
                    f"T: {values['temp_high_c']}/{values['temp_low_c']}C | "
                    f"Faults: {faults}"
                )
                sample += 1

                # Reconnect logic: if the BMS goes quiet, reopen the port.
                if values["pack_v"] is None and values["soc_pct"] is None:
                    dead_streak += 1
                    if dead_streak >= RECONNECT_AFTER:
                        print("No data for a while - reopening serial port...")
                        try:
                            ser.close()
                            time.sleep(1)
                            ser = open_serial()
                        except serial.SerialException as e:
                            print(f"  reopen failed: {e}")
                        dead_streak = 0
                else:
                    dead_streak = 0

                time.sleep(max(0, LOG_INTERVAL - (time.time() - loop_start)))
        except KeyboardInterrupt:
            print(f"\nStopped. {sample} samples written to:\n  {filename}")
        finally:
            ser.close()


if __name__ == "__main__":
    main()
