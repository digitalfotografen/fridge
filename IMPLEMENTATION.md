# Fridge-App — Implementation Details

This document describes the software architecture, hardware interface, and internal
design decisions for the Fridge-App. For installation, usage and configuration see
[README.md](README.md).

---

## Hardware

The application runs on a Raspberry Pi 4B with a custom interface board connected
over I2C.

### Temperature and humidity sensor

A SHT30 sensor measures temperature and humidity, connected via I2C at address `0x44`.
An optional second SHT30 at address `0x45` can monitor outside temperature.

### GPIO expander — MCP23017 (I2C address `0x20`)

**Port A — inputs (buttons)**

| Bit | Function        |
|-----|-----------------|
| 4   | Off button      |
| 5   | Cool Down button|
| 6   | Cool button     |
| 7   | Special button  |

Bits 0–3 are unused. The INTA pin is connected to Raspberry Pi GPIO 27 and generates
interrupts when a button is pressed.

**Port B — outputs (relays and LEDs, active-low via ULN2803A)**

| Bit | Function                                                                 |
|-----|--------------------------------------------------------------------------|
| 0   | Cooler relay — mains switch; energises both the circulation fan and the refrigeration compressor |
| 1   | Heater relay                                                             |
| 2   | Vent fan relay (humidity control, separate from the circulation fan)     |
| 3   | Compressor pause relay — pauses the compressor while the circulation fan keeps running |
| 4   | Off LED                                                                  |
| 5   | Cool Down LED                                                            |
| 6   | Cool LED                                                                 |
| 7   | Special LED                                                              |

The button handler uses radio-button behaviour for mode LEDs: pressing a mode button
sets its LED and clears the others while relay bits remain unchanged. The same LED
behaviour is applied programmatically via `ButtonsGpio.set_mode(ModeState)` when mode
changes are triggered by other parts of the application. Button events are handled via
MCP23017 port A interrupts routed through INTA on Raspberry Pi GPIO 27 for fast response.

Relays are slow devices and must not be switched too often. The cooler and
compressor-pause relays are driven by the *Cooler duty cycle* state machine (see below),
which enforces the required minimum on/off/pause times. The vent fan is rate-limited
separately: it may not switch more often than once per `min_vent_fan` interval (default
5 minutes). The heater is not additionally rate-limited beyond its hysteresis band.

---

## Software architecture

The application is a CLI tool built with [Click](https://click.palletsprojects.com/).
It is normally run as a long-running daemon (`uv run fridge run`); the CLI is also used
for one-shot commands such as `config`, `show`, `export` and `test`.

### Components

- **Thermostat** (`fridge.thermostat`) — temperature regulation loop running in a
  background thread. Reads the SHT30 sensor, evaluates the three state engines
  (ModeState, ActionState, EnergyState) and drives the relays accordingly.
- **CoolerController** (`fridge.thermostat.CoolerController`) — duty-cycle state
  machine for the refrigeration compressor (see *Cooler duty cycle* below).
- **EnergyController** (`fridge.energy_controller`) — background thread that reads
  the cached energy plan once per 15-minute slot and calls
  `Thermostat.set_energy_state`.
- **TibberClient** (`fridge.tibber_client`) — fetches electricity prices from the
  Tibber GraphQL API with exponential backoff.
- **EnergyPlan** (`fridge.energy_plan`) — classifies prices into LOW/NORMAL/HIGH
  bands and applies the pre-cooling look-ahead.
- **MqttBridge** (`fridge.mqtt`) — publishes state and measurements to an MQTT broker
  and subscribes to mode commands.
- **HourlyAggregator** (`fridge.hourly_stats`) — background thread that accumulates
  per-hour duty-cycle and climate statistics and writes them to the database and the
  retained MQTT `hourly` topic.
- **MeasurementLogger** (`fridge.measurement_logger`) — periodically writes
  temperature, humidity and state snapshots to the shared SQLite database.
- **LocalControl** (`fridge.local_control`) — Unix-domain socket server that accepts
  mode commands from the `fridge mode` CLI command.

Configuration files, logs and databases are stored in `~/fridge_data/`, separate from
the application code so updates do not overwrite runtime data.

---

## Temperature regulation

The regulation loop runs at approximately one iteration per second (configurable via
`refresh_rate`). Each iteration reads the sensor and evaluates three state engines.

### ModeState

| State       | Description                                                  |
|-------------|--------------------------------------------------------------|
| `IDLE`      | Monitoring only; all relays off.                             |
| `COOL_DOWN` | Cools to ~12 °C; heater acts as freeze guard at 9 °C.       |
| `COOL`      | Holds at ~5 °C; heater acts as freeze guard at 2 °C.        |
| `SPECIAL`   | Like COOL but with independently configurable set-points.    |

### ActionState

| State     | Condition                                                                 |
|-----------|---------------------------------------------------------------------------|
| `COOLING` | Temperature above `target + hysteresis / 2`; cooler duty cycle active.   |
| `IDLE`    | Temperature within the band; no active heating or cooling.                |
| `HEATING` | Temperature below `freeze_guard`; heater relay on.                        |

Heating and cooling are controlled independently: the heater only reacts to
`freeze_guard`, never to the cooling target, so it cannot be triggered by a
duty-cycle pause.

In **IDLE mode** all thresholds are inactive and all relays are forced off.

### Cooler duty cycle (`CoolerController`)

To prevent the heat exchanger from freezing up, the compressor may only run for a
limited time before a forced pause with the circulation fan still running.

| Config key               | Default | Meaning                                                    |
|--------------------------|---------|------------------------------------------------------------|
| `max_cooler_time`        | 30 min  | Maximum continuous compressor run time before a forced pause. |
| `cooler_pause_time`      | 15 min  | Duration of the forced pause.                              |
| `cooler_soft_start_time` | 2 min   | Compressor stays paused after the cooler unit is energised.|

**Switching the cooler on** (temperature rises above `target + hysteresis / 2`):
1. Port B Bit 3 HIGH (compressor paused).
2. Port B Bit 0 HIGH (cooler unit energised — circulation fan starts).
3. Wait `cooler_soft_start_time`.
4. Port B Bit 3 LOW (compressor starts).

**While cooling** (temperature still above off-threshold):
- After `max_cooler_time`, Bit 3 goes HIGH (compressor paused; Bit 0 stays HIGH).
- After `cooler_pause_time`, Bit 3 goes LOW and the compressor resumes.
- This cycle repeats until the temperature reaches the off-threshold.

**Switching the cooler off** (temperature drops below `target - hysteresis / 2`):
1. Port B Bit 3 HIGH (compressor paused).
2. Wait `cooler_pause_time` (circulation fan keeps running to defrost the exchanger).
3. Port B Bit 0 LOW (cooler unit off). Bit 3 stays HIGH.

If the off-threshold is reached while the compressor is already in a duty-cycle pause,
the remaining pause time is reused — no additional pause is inserted.

### EnergyState

| State          | Effect on `target_temperature`       | Effect on `freeze_guard`             |
|----------------|--------------------------------------|--------------------------------------|
| `LOW_PRICE`    | −`low_price_cool_adj` (default −2 °C)| +`low_price_heat_adj` (default +1 °C)|
| `NORMAL_PRICE` | No adjustment                        | No adjustment                        |
| `HIGH_PRICE`   | +`high_price_cool_adj` (default +2 °C)| −`high_price_heat_adj` (default −2 °C)|

The active state is picked from a cached plan once per 15-minute slot.

#### Price source and planner

Prices are fetched from the Tibber GraphQL API and stored in the shared SQLite database.
The fetch runs on startup if the cache lacks the current slot, and then once per day at
`fetch_hour` (default 16:00 `Europe/Stockholm`) in a background thread. Network calls
use exponential backoff; the controller keeps running on cached data when offline.

Each slot is classified using percentile bands over the planning window:
- `LOW_PRICE` — price below the 33rd percentile
- `NORMAL_PRICE` — price between the 33rd and 67th percentile
- `HIGH_PRICE` — price above the 67th percentile

A pre-cooling look-ahead relabels up to `precool_lookahead_slots` `NORMAL` slots that
immediately precede a `HIGH` block as `LOW`, so the fridge pre-cools before the
expensive period. The look-back stops at any earlier `HIGH` block, and consecutive
`LOW` slots are capped at `max_consecutive_low_slots` to avoid over-cooling.

### Threshold generation helper

`generate_thresholds(config, mode, energy_state)` produces the active set of
on/off thresholds for the regulation loop:

- **IDLE** — all thresholds `None`; regulation loop treats this as a special case.
- **COOL_DOWN** — heater and cooler thresholds derived from target temperature and
  hysteresis; fan thresholds fixed to 1 % (on) / 0 % (off) to keep the fan running
  continuously.
- **COOL** — cooler thresholds from target and hysteresis (with energy adjustment
  applied to `target_temperature`); heater thresholds from `freeze_guard` and
  hysteresis (with energy adjustment applied to `freeze_guard`).
- **SPECIAL** — identical logic to COOL but reads from the `[SpecialMode]` config
  section.

Missing config entries fall back to the compiled-in defaults.

---

## State persistence

[diskcache](https://grantjenks.com/docs/diskcache/) persists thermostat state (mode,
action state, energy state and cool-down timer) in `~/fridge_data/cache/`. This ensures
the fridge resumes its previous operating mode after a power loss or restart.

The `HourlyAggregator` also snapshots its in-progress accumulator to the diskcache
periodically, so a mid-hour restart resumes accumulation without losing samples.

---

## Error handling and resilience

The application is designed to run unattended and recover from transient errors:

- **Sensor reads** — `Sht30.read_data()` retries up to 3 times with a short delay.
  If all retries fail, the thermostat loop logs a warning and skips the current cycle
  instead of terminating the thread.
- **Button interrupts** — The interrupt handler reads the MCP23017 INTCAP register
  (captured pin state at interrupt time) so a quick button release does not cause the
  press to be missed. I2C errors during interrupt handling are caught and logged.
- **Network / Tibber** — Fetch failures are logged; the controller keeps running on
  the last cached energy plan.

---

## Logging format

State-change log lines include the current sensor reading using the short keys `temp`
and `hum`:

```
Mode changed to COOL (temp=5.2°C, hum=88.4%)
Energy state changed to NORMAL_PRICE (temp=5.2°C, hum=88.4%)
Action state changed to COOLING (temp=7.0°C, hum=81.0%)
CoolerController: OFF -> SOFT_START (temp=7.0°C, hum=81.0%)
```

If the sensor has not yet produced a valid reading the values are rendered as `nan`.
The "Energy state changed to …" line is only emitted when the state actually changes.

---

## Development

### Running tests

Tests must be run on the Raspberry Pi (hardware I2C is required for some tests).

```bash
# All non-hardware tests
uv run pytest tests/ --ignore=tests/test_thermostat_hw.py

# Hardware-backed tests (run on the Pi)
uv run pytest tests/test_thermostat_hw.py

# With coverage
uv run pytest tests/ --ignore=tests/test_thermostat_hw.py --cov=fridge
```

### Linting and formatting

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

### Building documentation

```bash
uv run sphinx-build docs/ docs/_build/
```
