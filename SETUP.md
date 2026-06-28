# Orion BMS Jr. Logger — Setup Guide

A Raspberry Pi reads live data from an **Orion BMS Junior** over RS232 and
writes one CSV row per sample. Think of it as a flight recorder wired to the
battery: the Pi keeps asking the BMS "what are your numbers?" and writes every
answer to a log you can open later in Excel.

---

## Part A — Set up the BMS itself

The Orion Jr. speaks the **OBD2 diagnostic protocol over its serial (RS232)
port**, fixed at **9600 baud, 8 data bits, Even parity, 1 stop bit (8E1)**.
There is no separate "enable serial output" switch — polling is always
available on that port. What you do need to get right:

1. **Load and activate a battery profile.** Connect the Orion Jr. to a Windows
   PC with the supplied cable, open the **Orion BMS Utility**, and make sure a
   valid profile is written to the BMS (correct cell count, chemistry, current
   sensor). The BMS only reports real numbers once it is configured and powered
   (sensing the pack and any ignition/load input it expects).

2. **One OBD2 master at a time.** This is the single most common gotcha. The
   Orion *Utility itself talks OBD2 on the same serial port*, and only **one**
   device may poll at once. So **close the Orion Utility / unplug it from the
   serial port before the Pi starts logging** — otherwise the two masters
   collide and you get garbage or nothing.

3. **Confirm the serial port wiring.** The MAX3232's DB9 connects to the Orion
   Jr.'s serial connector. You only need three lines: **TX, RX, GND**. If you
   get no data after everything else checks out, the classic fix is to **swap TX
   and RX** — "straight vs. crossover" is the usual culprit on a fresh build.

4. **OBD2 is read-only telemetry, not safety control.** Orion explicitly says
   OBD2 polling has no checksums and can be corrupted by electrical noise, so
   never use it for charge/discharge control — only for logging and display.
   This logger is exactly that: passive logging.

> CANBUS note: if you have a **CAN-equipped Jr.**, the same PIDs are available
> over CANBUS instead, but this logger targets the RS232 serial port per your
> hardware (MAX3232).

---

## Part B — Raspberry Pi hardware wiring

Power the MAX3232 from **3.3 V, never 5 V.**

| MAX3232 module pin | Raspberry Pi 4 pin            |
| ------------------ | ----------------------------- |
| VCC                | Pin 1  — 3.3 V  ⚠️ not 5 V    |
| GND                | Pin 6  — GND                  |
| TX (module out)    | Pin 10 — GPIO15 / RXD         |
| RX (module in)     | Pin 8  — GPIO14 / TXD         |

The MAX3232's DB9 plugs into the Orion Jr. serial port (TX/RX/GND, see A‑3).

---

## Part C — Raspberry Pi software

**1. Flash Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the
gear/settings: set hostname `bmslogger`, enable SSH, create user `pi`, and add
your Wi‑Fi for first boot.

**2. SSH in:**
```bash
ssh pi@bmslogger.local
```

**3. Free up and enable the UART:**
```bash
sudo raspi-config
#  Interface Options -> Serial Port
#   - login shell over serial?  -> No
#   - serial port hardware?     -> Yes
#  Finish -> reboot
```
After reboot, confirm the port exists:
```bash
ls /dev/ttyS0     # if missing, try:  ls /dev/ttyAMA0
```
If your port is `/dev/ttyAMA0`, change `SERIAL_PORT` at the top of
`orion_bms_logger.py` accordingly.

**4. Install the dependency and grant serial access:**
```bash
pip3 install pyserial
sudo usermod -a -G dialout pi    # then log out/in (or reboot)
```

**5. Copy the script over** (from your computer):
```bash
scp orion_bms_logger.py pi@bmslogger.local:/home/pi/
```

**6. Test it:**
```bash
python3 /home/pi/orion_bms_logger.py
```
Expected output:
```
Orion BMS Jr. Logger
  Port    : /dev/ttyS0 @ 9600 8E1
  ...
BMS handshake OK.

#00000 | 2026-06-27 14:30:22 | SOC: 87.0% | V: 48.3 | A: -12.4 | T: 28/25C | Faults: NONE
```
If you see `WARNING: no valid response` or all values are blank, see
**Troubleshooting** below.

---

## Part D — Run automatically on boot (systemd)

```bash
sudo nano /etc/systemd/system/bmslogger.service
```
```ini
[Unit]
Description=Orion BMS Junior Logger
After=multi-user.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/orion_bms_logger.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bmslogger.service
sudo systemctl status bmslogger.service
```

---

## Part E — Get the CSV logs off the Pi

Logs are written to `/home/pi/bms_logs/LOG_YYYYMMDD_HHMMSS.csv`. Copy them:
```bash
scp pi@bmslogger.local:/home/pi/bms_logs/*.csv ./
```
Each row: timestamp, elapsed seconds, SOC %, pack V, pack A (signed), high/low/
avg temperature, low/high/avg cell voltage, pack health, cycles, fault codes,
then one column per cell voltage.

---

## Troubleshooting

| Symptom | Fix |
| ------- | --- |
| `Permission denied: /dev/ttyS0` | `sudo usermod -a -G dialout pi`, then reboot |
| All values blank / `WARNING: no valid response` | Check 3.3 V power, **swap TX/RX**, confirm `SERIAL_PORT`, ensure BMS is powered, and **close the Orion Utility** (only one OBD2 master allowed) |
| `No module named serial` | `pip3 install pyserial` |
| Port `/dev/ttyS0` missing | Use `/dev/ttyAMA0` and update `SERIAL_PORT` |
| Service won't start | `sudo systemctl status bmslogger.service` and read the error |

## Adapting the script

Edit the config block at the top of `orion_bms_logger.py`:
`SERIAL_PORT`, `BAUD_RATE`, `LOG_INTERVAL`, `NUM_CELLS`, `LOG_DIR`. To log more
parameters, add `(name, pid, scaling, signed)` rows to `SCALAR_PIDS` using
Orion's official PID list (`orionbms.com/downloads/misc/orionbms_obd2_pids.pdf`).
