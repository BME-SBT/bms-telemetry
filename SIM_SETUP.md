# BMS Simulator — Bench Test Setup

Test the logger on a laptop instead of on the boat. `bms_simulator.py` is a
**stunt double for the Orion BMS Jr.**: it answers the same OBD2 questions the
real pack would, so you can watch how `orion_bms_logger.py` behaves — including
under faults and a flaky link — without a battery anywhere in the room.

---

## Option A — With the Raspberry Pi (tests the *whole* real path)

This is the setup you asked about. You keep the Pi and the MAX3232 exactly as
they are; you just unplug the boat's BMS and plug the **laptop** in where the
BMS used to be.

```
On the boat:   [Orion BMS] --RS232--> [MAX3232] --TTL--> [Pi  /dev/ttyS0]
On the bench:  [LAPTOP   ] --RS232--> [MAX3232] --TTL--> [Pi  /dev/ttyS0]
                    ^ runs bms_simulator.py        ^ runs orion_bms_logger.py
```

What you need on the laptop: a **USB ↔ RS-232 (DB9) adapter** (e.g. an FTDI or
Prolific cable). The MAX3232 is just a voltage translator — it doesn't care
whether real RS-232 levels come from the Orion or from your laptop's adapter.

Wiring:

1. Plug the USB-RS232 adapter into the laptop. Find its device name:
   - macOS: `ls /dev/tty.usbserial-*` (or `/dev/tty.usbmodem*`)
   - Linux: `ls /dev/ttyUSB*`
2. Plug the adapter's **DB9 into the MAX3232's DB9** — the same socket the
   Orion's serial cable normally uses. (Leave the MAX3232↔Pi TTL wiring alone.)
3. Run the simulator on the laptop:

   ```bash
   pip3 install pyserial            # one time
   python3 bms_simulator.py --port /dev/tty.usbserial-XXXX --scenario nominal
   ```

4. Run the logger on the Pi, **unchanged**:

   ```bash
   python3 orion_bms_logger.py
   ```

You should see `BMS handshake OK.` on the Pi and rows streaming to the CSV.

> **No data? Swap TX/RX.** Same "straight vs crossover" gotcha as SETUP.md §A-3.
> Two PCs/adapters facing each other usually need a **null-modem** adapter (or a
> DB9 gender-changer that crosses pins 2 and 3) between the laptop adapter and
> the MAX3232. If you get garbage or silence, that crossover is the first thing
> to try.

---

## Option B — No Pi at all (pure software on the laptop)

Fastest for developing the logger itself. The simulator makes a **virtual
serial port**; you point the logger at it. Both run on the same laptop.

```bash
# Terminal 1
python3 bms_simulator.py --pty --scenario thermal-runaway
#   -> prints:  VIRTUAL PORT : /dev/ttys012

# Terminal 2 — set SERIAL_PORT in orion_bms_logger.py to that path, then:
python3 orion_bms_logger.py
```

(No USB adapter, no MAX3232, no Pi — just the two scripts.)

---

## Stress scenarios (`--scenario`)

| Scenario          | What it does                                              | DTC it should raise |
| ----------------- | -------------------------------------------------------- | ------------------- |
| `nominal`         | calm discharge, balanced cells (default)                 | none                |
| `thermal-runaway` | hottest cell climbs past 60 °C → 80 °C+                   | `P0A80` over-temp   |
| `cell-imbalance`  | one cell drifts until spread > 0.3 V                     | `P0A1B` imbalance   |
| `overvoltage`     | charging pushes cells past 4.20 V                        | `P0A0F`             |
| `undervoltage`    | discharge drags cells below 2.80 V                       | `P0A10`             |
| `deep-discharge`  | SOC bleeds toward 0 %                                     | `P0A05` low SOC     |
| `comms-flaky`     | healthy pack, but the link drops/garbles frames          | (tests reconnect)   |
| `faults`          | rotating active DTCs appearing and clearing              | various             |
| `endurance`       | slow charge/discharge cycling for long / large-file runs | none                |
| `chaos`           | everything at once, including comms failures             | various             |

Handy knobs:

- `--timescale 60` — run the battery model 60× faster, so a "multi-hour"
  endurance run or a huge CSV happens in minutes. Pair with a small
  `LOG_INTERVAL` in the logger to fill big files quickly (volume testing).
- `--drop-rate 0.2 --garbage-rate 0.1` — force comms failures on any scenario.
- `--seed 7` — reproducible run (same numbers every time), good for regression.
- `--cells 16 --capacity-ah 100` — match your pack.

Example — fill a large CSV fast to stress whatever reads the logs:

```bash
# laptop (Option B):  fast model + fast polling
python3 bms_simulator.py --pty --scenario endurance --timescale 120 --seed 1
# then in orion_bms_logger.py set LOG_INTERVAL = 0.05 and run it against the pty
```

---

## What "passing" looks like

- Pi prints `BMS handshake OK.`
- CSV rows show SOC / V / A / temps / per-cell voltages in believable ranges.
- Fault scenarios populate the `faults` column with the expected `Pxxxx` code.
- `comms-flaky` produces blank cells and, if sustained, trips the logger's
  "reopening serial port…" reconnect — and the logger never crashes on garbage.

The framing was checked against the logger's own parser functions
(`connection_ok`, `read_scaled`, `read_faults`, `read_cell_voltages`), so the
bytes on the wire are exactly what the logger expects.
