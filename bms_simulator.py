#!/usr/bin/env python3
"""
Orion BMS Junior - SIMULATOR / Stress Tester
============================================
Pretends to be an Orion BMS Jr. so you can exercise `orion_bms_logger.py` on a
bench - laptop + Pi + MAX3232 - instead of on the boat with a live battery.

Mental model: the real logger is a tape recorder that keeps asking the battery
"what are your numbers?". This script is a *stunt double* for the battery: it
answers every OBD2 question with believable - or deliberately nasty - numbers,
so you can watch how the logger behaves without a real pack.

----------------------------------------------------------------------------
TWO WAYS TO RUN IT
----------------------------------------------------------------------------
A) WITH THE PI (recommended - tests the whole real path)
   Replace the BMS with the laptop. Plug a USB<->RS-232 (DB9) adapter into the
   laptop, and plug that adapter into the MAX3232's DB9 connector - the exact
   socket the Orion's serial cable normally uses. Nothing on the Pi changes.

       boat:  [Orion BMS] --RS232--> [MAX3232] --TTL--> [Pi /dev/ttyS0]
       bench: [LAPTOP sim] --RS232--> [MAX3232] --TTL--> [Pi /dev/ttyS0]

   Laptop:  python3 bms_simulator.py --port /dev/tty.usbserial-XXXX
   Pi:      python3 orion_bms_logger.py        (unchanged)

   If the Pi sees nothing, swap TX/RX (use a null-modem adapter or gender
   changer) - same "straight vs crossover" gotcha called out in SETUP.md.

B) NO PI AT ALL (pure software loopback on the laptop)
   Creates a virtual serial port (pty). Pojint the logger at the path it prints.

       Terminal 1:  python3 bms_simulator.py --pty
                    -> prints e.g.  VIRTUAL PORT: /dev/ttys012
       Terminal 2:  edit SERIAL_PORT in orion_bms_logger.py to that path,
                    then  python3 orion_bms_logger.py

----------------------------------------------------------------------------
STRESS SCENARIOS  (--scenario)
----------------------------------------------------------------------------
  nominal          calm discharge, balanced cells, no faults (default)
  thermal-runaway  hottest cell climbs past 60C then 80C -> over-temp DTC
  cell-imbalance   one cell drifts away until spread > 0.3V -> imbalance DTC
  overvoltage      charging pushes cells past 4.20V -> over-voltage DTC
  undervoltage     discharge drags cells below 2.80V -> under-voltage DTC
  deep-discharge   SOC bleeds toward 0% -> low-SOC DTC
  comms-flaky      healthy pack, but the link drops/garbles frames
  faults           rotating active DTCs (Mode $3) appearing and clearing
  endurance        slow charge/discharge cycling for long/large-file runs
  chaos            everything at once, including comms failures

Useful knobs:
  --cells N         pack size (default 16, matches the logger)
  --capacity-ah F   pack capacity for SOC math (default 100)
  --timescale F     speed up the battery model (e.g. 60 = 1 min per sec) so a
                    "multi-hour" run or an endurance file happens in minutes
  --drop-rate F     0..1 probability a response is silently dropped
  --garbage-rate F  0..1 probability a response is garbled/truncated
  --seed N          reproducible runs (same numbers every time)

Install (laptop):  pip3 install pyserial   (only needed for --port mode)
"""

import argparse
import math
import os
import random
import sys
import time

# ── Protocol constants (must match orion_bms_logger.py exactly) ───────────
# Mode $22 scalar PIDs:  pid -> (state_key, scaling, signed, num_data_bytes)
SCALAR_PIDS = {
    0xF00F: ("soc_pct", 0.5, False, 2),
    0xF00D: ("pack_v", 0.1, False, 2),
    0xF00C: ("pack_a", 0.1, True, 2),
    0xF028: ("temp_high_c", 1.0, True, 1),
    0xF029: ("temp_low_c", 1.0, True, 1),
    0xF02A: ("temp_avg_c", 1.0, True, 1),
    0xF032: ("low_cell_v", 0.0001, False, 2),
    0xF033: ("high_cell_v", 0.0001, False, 2),
    0xF034: ("avg_cell_v", 0.0001, False, 2),
    0xF013: ("pack_health_pct", 1.0, False, 2),
    0xF018: ("pack_cycles", 1.0, False, 2),
}

# Cell-voltage block PIDs: each block carries up to 12 cells (2 bytes each).
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

SCENARIOS = [
    "nominal",
    "thermal-runaway",
    "cell-imbalance",
    "overvoltage",
    "undervoltage",
    "deep-discharge",
    "comms-flaky",
    "faults",
    "endurance",
    "chaos",
]


# ── OBD2 ASCII-hex framing ────────────────────────────────────────────────
def encode_frame(body: bytes) -> bytes:
    """Wrap a response body as `:<LEN><body-hex>\\n` (LEN = len(body))."""
    raw = bytes([len(body)]) + body
    return b":" + raw.hex().upper().encode("ascii") + b"\n"


def clamp_int(value: float, signed: bool, nbytes: int) -> int:
    """Round + clamp an engineering value to fit the requested byte width."""
    raw = int(round(value))
    if signed:
        lo, hi = -(1 << (8 * nbytes - 1)), (1 << (8 * nbytes - 1)) - 1
    else:
        lo, hi = 0, (1 << (8 * nbytes)) - 1
    return max(lo, min(hi, raw))


def scalar_response(pid: int, state: dict) -> bytes:
    key, scaling, signed, nbytes = SCALAR_PIDS[pid]
    raw = clamp_int(state[key] / scaling, signed, nbytes)
    data = raw.to_bytes(nbytes, "big", signed=signed)
    body = bytes([0x62, (pid >> 8) & 0xFF, pid & 0xFF]) + data  # Mode $22 -> $62
    return encode_frame(body)


def cell_block_response(pid: int, state: dict, num_cells: int) -> bytes | None:
    block = CELL_BLOCK_PIDS.index(pid)
    start = block * 12
    if start >= num_cells:
        return None  # block beyond the pack -> the real BMS would not answer
    cells = state["cells"][start : start + 12]
    data = b""
    for v in cells:
        raw = clamp_int(v / 0.0001, False, 2)
        data += raw.to_bytes(2, "big")
    body = bytes([0x62, (pid >> 8) & 0xFF, pid & 0xFF]) + data
    return encode_frame(body)


def faults_response(state: dict) -> bytes:
    """Mode $3 -> $43: count byte then 2 bytes per DTC."""
    dtcs = state["dtcs"]
    body = bytes([0x43, len(dtcs)])
    for hi, lo in dtcs:
        body += bytes([hi, lo])
    return encode_frame(body)


def identity_response() -> bytes:
    """Mode $9 PID $0B -> the string the logger's handshake looks for."""
    body = bytes([0x49, 0x0B]) + b"ORIONBMS"  # Mode $9 -> $49
    return encode_frame(body)


# ── Battery model ─────────────────────────────────────────────────────────
class BatteryModel:
    """A small physics-lite pack that drifts over time and per scenario."""

    OCV_MIN, OCV_MAX = 3.00, 4.15  # per-cell open-circuit volts at 0/100%
    R_CELL = 0.0010  # ohm, internal resistance per cell
    AMBIENT_C = 22.0

    def __init__(
        self, scenario: str, num_cells: int, capacity_ah: float, rng: random.Random
    ):
        self.scenario = scenario
        self.n = num_cells
        self.cap = capacity_ah
        self.rng = rng
        self.t = 0.0  # model seconds elapsed
        self.soc = 78.0
        self.current = -0.0
        self.health = 97.0
        self.cycles = 142
        self.hot_offset = 0.0  # extra heat on the hottest cell
        self.imbalance = 0.0  # volts of drift on the worst cell
        # small fixed per-cell manufacturing spread
        self.cell_bias = [rng.uniform(-0.004, 0.004) for _ in range(num_cells)]
        self.cells = [3.9] * num_cells
        self.temps = (25.0, 24.0, 24.0)  # high, avg, low
        self.dtcs: list[tuple[int, int]] = []
        self._step(0.0)  # initialise derived values

    # current the scenario asks for (A; + charge / - discharge)
    def _target_current(self) -> float:
        s, t, rng = self.scenario, self.t, self.rng
        jitter = rng.uniform(-2.0, 2.0)
        if s == "overvoltage":
            return 45.0 + jitter
        if s in ("undervoltage", "deep-discharge"):
            return -55.0 + jitter
        if s == "endurance":
            # slow triangle wave: charge up, discharge down, repeat (~20 min/leg)
            phase = math.sin(t / 1200.0 * math.pi)
            return 40.0 * phase + jitter
        if s == "chaos":
            return rng.uniform(-60.0, 50.0)
        # nominal / thermal / imbalance / comms / faults: gentle cruise
        return -28.0 + 8.0 * math.sin(t / 90.0) + jitter

    def _step(self, dt: float):
        self.t += dt
        self.current = self._target_current()

        # integrate charge: dSOC(%) = current * dt[h] / cap * 100
        self.soc += self.current * (dt / 3600.0) / self.cap * 100.0
        self.soc = max(0.0, min(100.0, self.soc))

        # open-circuit voltage from SOC, plus IR term and per-cell bias
        ocv = self.OCV_MIN + (self.OCV_MAX - self.OCV_MIN) * (self.soc / 100.0)
        ir = self.current * self.R_CELL

        # scenario-specific drift
        if self.scenario == "cell-imbalance":
            self.imbalance = min(0.45, self.imbalance + dt * 0.0009)
        if self.scenario == "thermal-runaway":
            self.hot_offset = min(75.0, self.hot_offset + dt * 0.18)

        self.cells = []
        for i in range(self.n):
            v = ocv + ir + self.cell_bias[i] + self.rng.uniform(-0.001, 0.001)
            if i == 0:  # cell 1 is the "worst" cell
                v -= self.imbalance
            self.cells.append(round(max(2.0, min(4.6, v)), 4))

        # temperatures: ambient + self-heating from current, hottest cell extra
        heat = abs(self.current) * 0.06
        avg_t = self.AMBIENT_C + heat
        high_t = avg_t + 2.5 + self.hot_offset
        low_t = avg_t - 1.5
        self.temps = (round(high_t, 1), round(avg_t, 1), round(low_t, 1))

        # cycles tick up slowly during endurance
        if self.scenario == "endurance":
            self.cycles = 142 + self.t / 3600.0

        self._update_dtcs()

    def _update_dtcs(self):
        hi_v, lo_v = max(self.cells), min(self.cells)
        spread = hi_v - lo_v
        codes = []
        if hi_v > 4.20:
            codes.append((0x0A, 0x0F))  # cell over-voltage
        if lo_v < 2.80:
            codes.append((0x0A, 0x10))  # cell under-voltage
        if self.temps[0] > 60.0:
            codes.append((0x0A, 0x80))  # over-temperature
        if spread > 0.30:
            codes.append((0x0A, 0x1B))  # cell imbalance
        if self.soc < 5.0:
            codes.append((0x0A, 0x05))  # low state of charge
        if self.scenario == "faults":
            # rotate a synthetic active fault every ~8 model-seconds
            rotating = [(0x03, 0x10), (0x06, 0x20), (0x0A, 0x1B), (0x05, 0x55)]
            codes.append(rotating[int(self.t // 8) % len(rotating)])
        self.dtcs = codes

    def state(self) -> dict:
        cells = self.cells
        return {
            "soc_pct": self.soc,
            "pack_v": sum(cells),
            "pack_a": self.current,
            "temp_high_c": self.temps[0],
            "temp_avg_c": self.temps[1],
            "temp_low_c": self.temps[2],
            "low_cell_v": min(cells),
            "high_cell_v": max(cells),
            "avg_cell_v": sum(cells) / len(cells),
            "pack_health_pct": self.health,
            "pack_cycles": self.cycles,
            "cells": cells,
            "dtcs": self.dtcs,
        }


# ── Transports: real serial port, or a local pty ──────────────────────────
class SerialTransport:
    def __init__(self, port: str, baud: int):
        try:
            import serial
        except ImportError:
            sys.exit("Missing dependency for --port mode. Run: pip3 install pyserial")
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
        )
        self.label = f"{port} @ {baud} 8E1 (real serial)"
        self._buf = b""

    def read_request(self) -> bytes | None:
        chunk = self.ser.read(64)
        if chunk:
            self._buf += chunk
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            return line
        return None

    def send(self, frame: bytes):
        self.ser.write(frame)
        self.ser.flush()


class PtyTransport:
    def __init__(self, baud: int):
        self.master, slave = os.openpty()
        try:
            import tty

            tty.setraw(self.master)
        except Exception:
            pass
        self.slave_name = os.ttyname(slave)
        self.label = f"{self.slave_name} (virtual port / pty)"
        os.set_blocking(self.master, False)
        self._buf = b""

    def read_request(self) -> bytes | None:
        try:
            chunk = os.read(self.master, 256)
        except (BlockingIOError, OSError):
            chunk = b""
        if chunk:
            self._buf += chunk
        if b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            return line
        return None

    def send(self, frame: bytes):
        try:
            os.write(self.master, frame)
        except OSError:
            pass


# ── Request handling ──────────────────────────────────────────────────────
def build_response(request: bytes, model: BatteryModel) -> bytes | None:
    """Parse one logger request frame and return the response frame (or None)."""
    line = request.strip()
    if not line.startswith(b":"):
        return None
    try:
        raw = bytes.fromhex(line[1:].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if len(raw) < 2:
        return None
    length, mode = raw[0], raw[1]
    pid_bytes = raw[2 : 1 + length]
    pid = int.from_bytes(pid_bytes, "big") if pid_bytes else None
    state = model.state()

    if mode == 0x09 and pid == 0x0B:
        return identity_response()
    if mode == 0x03:
        return faults_response(state)
    if mode == 0x22 and pid is not None:
        if pid in SCALAR_PIDS:
            return scalar_response(pid, state)
        if pid in CELL_BLOCK_PIDS:
            return cell_block_response(pid, state, model.n)
    return None  # unknown PID -> silence, like a real BMS


def maybe_corrupt(
    frame: bytes, drop_rate: float, garbage_rate: float, rng: random.Random
) -> bytes | None:
    """Inject comms failures: drop, garble, or truncate the response."""
    r = rng.random()
    if r < drop_rate:
        return None  # timeout on the logger
    if r < drop_rate + garbage_rate:
        if rng.random() < 0.5:  # random garbage line
            n = rng.randint(2, 10)
            return (
                b":"
                + bytes(rng.randint(0, 255) for _ in range(n)).hex().upper().encode()
                + b"\n"
            )
        return frame[: max(1, len(frame) // 2)]  # truncated frame
    return frame


# ── Main loop ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Simulate an Orion BMS Jr. for stress-testing the logger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--port",
        help="real serial device (laptop USB-RS232 adapter), e.g. /dev/tty.usbserial-XXXX",
    )
    g.add_argument(
        "--pty", action="store_true", help="create a local virtual serial port instead"
    )
    p.add_argument("--scenario", choices=SCENARIOS, default="nominal")
    p.add_argument("--cells", type=int, default=16)
    p.add_argument("--capacity-ah", type=float, default=100.0)
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument(
        "--timescale",
        type=float,
        default=1.0,
        help="model-time multiplier (e.g. 60 = 1 model-minute per real second)",
    )
    p.add_argument(
        "--drop-rate",
        type=float,
        default=None,
        help="0..1 chance a response is dropped",
    )
    p.add_argument(
        "--garbage-rate",
        type=float,
        default=None,
        help="0..1 chance a response is garbled",
    )
    p.add_argument("--seed", type=int, default=None, help="reproducible run")
    p.add_argument(
        "--quiet", action="store_true", help="suppress the periodic status line"
    )
    args = p.parse_args()

    # scenario-driven defaults for the comms knobs
    drop = args.drop_rate
    garbage = args.garbage_rate
    if args.scenario in ("comms-flaky", "chaos"):
        drop = 0.15 if drop is None else drop
        garbage = 0.10 if garbage is None else garbage
    drop = drop or 0.0
    garbage = garbage or 0.0

    rng = random.Random(args.seed)
    model = BatteryModel(args.scenario, args.cells, args.capacity_ah, rng)
    transport = (
        PtyTransport(args.baud) if args.pty else SerialTransport(args.port, args.baud)
    )

    print("=" * 64)
    print(" Orion BMS Jr. SIMULATOR  (stress tester)")
    print("=" * 64)
    if args.pty:
        print(f" VIRTUAL PORT : {transport.slave_name}")
        print("   -> set SERIAL_PORT in orion_bms_logger.py to this path")
    else:
        print(f" SERIAL PORT  : {transport.label}")
        print("   -> on the Pi, run orion_bms_logger.py as usual (/dev/ttyS0)")
    print(f" Scenario     : {args.scenario}")
    print(f" Pack         : {args.cells} cells, {args.capacity_ah:.0f} Ah")
    print(
        f" Comms        : drop={drop:.0%}  garble={garbage:.0%}  timescale={args.timescale}x"
    )
    print(f" Seed         : {args.seed}")
    print(" Stop         : Ctrl+C")
    print("=" * 64 + "\n")

    last = time.time()
    last_print = 0.0
    requests = 0
    try:
        while True:
            now = time.time()
            model._step((now - last) * args.timescale)
            last = now

            req = transport.read_request()
            if req is not None:
                requests += 1
                frame = build_response(req, model)
                if frame is not None:
                    frame = maybe_corrupt(frame, drop, garbage, rng)
                    if frame is not None:
                        transport.send(frame)
            else:
                time.sleep(0.002)  # idle; don't burn the CPU

            if not args.quiet and now - last_print >= 1.0:
                st = model.state()
                faults = "|".join(f"P{h:02X}{l:02X}" for h, l in st["dtcs"]) or "NONE"
                print(
                    f" t={model.t:7.0f}s  SOC {st['soc_pct']:5.1f}%  "
                    f"V {st['pack_v']:6.1f}  A {st['pack_a']:6.1f}  "
                    f"T {st['temp_high_c']:4.1f}/{st['temp_low_c']:4.1f}C  "
                    f"spread {(st['high_cell_v'] - st['low_cell_v']) * 1000:4.0f}mV  "
                    f"faults {faults}  (reqs {requests})"
                )
                last_print = now
    except KeyboardInterrupt:
        print(f"\nStopped. Answered {requests} requests.")


if __name__ == "__main__":
    main()
