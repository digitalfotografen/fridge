# Fridge-App

A Raspberry Pi-based controller for a large game fridge — used to cool down animal
carcasses slowly and then hold them at a controlled temperature for tenderising, or to
keep large quantities of beer cold.

The fridge is equipped with a temperature/humidity sensor, a heater, a cooler and a
ventilation fan. The app saves electricity by nudging the target temperature based on
real-time energy prices via [Tibber](https://tibber.com/se).

Runs on a Raspberry Pi 4B with I2C-connected sensors and GPIO.

> **License:** MIT — see [LICENSE](LICENSE).  
> **Implementation details:** hardware pinout, state machines, architecture — see [IMPLEMENTATION.md](IMPLEMENTATION.md).  
> **Contributing / AI agents:** see [agents.md](agents.md) and [agents_guide.md](agents_guide.md).

---

## Why I built this

My wife found a scrapped telecom equipment shed with a working cooler (originally
housing a 3G cell system). I removed the free-cooling system and kept the refrigerator
and heater. Since I build lab and factory automation systems in Python for a living —
often on Raspberry Pi — I already had most of the hardware in my workshop.

This project was also an experiment in agentic programming with LLMs. I wrote a
detailed specification (this document) and `agents.md` before writing any code, then
iterated with Claude handling most of the implementation.

## Want to know more?

Feel free to use this as inspiration for your own projects.
You can email [ulrik@oddtech.se](mailto:ulrik@oddtech.se) with questions if you want to
build your own game fridge, but don't expect a fast reply or detailed help — this was
built to solve a private need.

---

## What it looks like

![Interior](/docs/img/IMG_8466.jpg "Interior")
![Raspberry Pi controller, electrical panel and ventilation fan](/docs/img/IMG_8467.jpg "Controller")
![Cooler and heater](/docs/img/IMG_8468.jpg "Cooler and heater")
![Exterior](/docs/img/IMG_8465.jpg "Exterior")
![Cooler exterior](/docs/img/IMG_8469.jpg "Cooler exterior")

---

## Modes

The application has four operating modes, selected via the physical buttons or remotely
over MQTT / the `fridge mode` CLI command.

There is a hysteresis of ±1.5 °C on the cooler and heater to prevent rapid cycling.
The refrigeration compressor also runs on a duty cycle to prevent the heat exchanger
from freezing up (see [IMPLEMENTATION.md — Cooler duty cycle](IMPLEMENTATION.md#cooler-duty-cycle-coolercontroller)).

### Idle (default)
Temperature and humidity are monitored, but the heater, fan and cooler are all off.

### Cool Down
Cools the fridge to a target of 12 °C (range 10–14 °C). The heater acts as a freeze
guard below 9 °C. The vent fan follows the same humidity logic as Cool mode.
The system automatically switches to Cool mode after 10 hours.

### Cool
Holds the fridge at a target of 5 °C (±1.5 °C hysteresis). The heater acts as a
freeze guard below 2 °C. The vent fan is active when humidity exceeds 55 %
(±5 % hysteresis).

### Special
Behaves like Cool mode but reads its own target temperature, hysteresis, freeze guard
and humidity thresholds from the `[SpecialMode]` config section. Useful for a different
set-point (e.g. longer tenderising at a higher temperature, or a lower serving
temperature for drinks) without disturbing the calibrated Cool mode values.

Defaults mirror Cool mode (target 5 °C, hysteresis ±1.5 °C, humidity 55 % ±5 %,
freeze guard 0 °C); adjust in `fridge.ini`.

---

## Energy cost saving

The system nudges the target temperature based on electricity prices fetched from the
[Tibber API](https://developer.tibber.com/) in 15-minute slots:

| State          | Effect                                                        |
|----------------|---------------------------------------------------------------|
| `LOW_PRICE`    | Pre-cools: target lowered by 2 °C, freeze guard raised 1 °C  |
| `NORMAL_PRICE` | No adjustment                                                 |
| `HIGH_PRICE`   | Coasts: target raised by 2 °C, freeze guard lowered 2 °C     |

A pre-cooling look-ahead relabels slots immediately before a HIGH block as LOW, so the
fridge cools down before the expensive period. See
[IMPLEMENTATION.md — EnergyState](IMPLEMENTATION.md#energystate) for the full algorithm.

---

## Installation

### Prerequisites
- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/) for dependency management
- Raspberry Pi 4B with the custom interface board (for hardware operation)

### Install
```bash
git clone <repository-url>
cd fridge
uv sync
```

### Create the configuration file
```bash
uv run fridge config
```
This copies `config_default.ini` to `~/fridge_data/fridge.ini` if it does not already
exist, then displays all current settings.

---

## Configuration

All runtime data (configuration, logs, database, cache) is stored in `~/fridge_data/`
so the application can be updated without losing state.

Edit `~/fridge_data/fridge.ini` to customise values. The main sections are:

| Section        | Description                                                |
|----------------|------------------------------------------------------------|
| `General`      | Refresh rate, vent-fan rate limit, cooler duty-cycle times |
| `Hardware`     | I2C addresses, interrupt GPIO pin                          |
| `CoolDownMode` | Target temperature, hysteresis, auto-switch duration       |
| `CoolMode`     | Target temperature, hysteresis, freeze guard, humidity     |
| `SpecialMode`  | Independent set-points for Special mode                    |
| `Energy`       | Temperature adjustments for low/high energy prices         |
| `Logging`      | Log file, database file, measurement interval              |
| `MQTT`         | Broker settings, topic prefix, publish interval            |
| `Tibber`       | API key, price cache and energy-plan parameters            |

### Tibber configuration

| Key                         | Default             | Description                                              |
|-----------------------------|---------------------|----------------------------------------------------------|
| `api_key`                   | _(required)_        | Tibber API token.                                        |
| `home_id`                   | _(empty)_           | Optional home ID when the account has several homes.     |
| `fetch_hour`                | `16`                | Local hour (0–23) for the daily price fetch.             |
| `timezone`                  | `Europe/Stockholm`  | IANA timezone for the daily fetch.                       |
| `precool_lookahead_slots`   | `12`                | Max 15-min slots (3 h) to look ahead before a HIGH block.|
| `max_consecutive_low_slots` | `4`                 | Cap on consecutive LOW slots to avoid over-cooling.      |

---

## CLI commands

```bash
uv run fridge --help
```

### `config` — Configuration management
Creates `~/fridge_data/fridge.ini` from defaults if it does not exist, then displays
all current settings.
```bash
uv run fridge config
```

### `show` — Display current state (read-only)
Reads the GPIO hardware and diskcache to display relay, LED, button and thermostat
state. Safe to run while the daemon is running.
```bash
uv run fridge show
```

### `run` — Run the fridge controller
Starts the controller as a long-running process. Runs until `SIGINT` (Ctrl+C) or
`SIGTERM`; on shutdown all relays are turned off gracefully.
```bash
uv run fridge run
```

To run as a systemd service:
```ini
[Unit]
Description=Fridge Controller
After=network.target

[Service]
Type=simple
User=ulrik
WorkingDirectory=/home/ulrik/fridge
ExecStart=/home/ulrik/.local/bin/uv run fridge run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable fridge.service
sudo systemctl start fridge.service
```

### `mode` — Switch mode locally
Switches the running controller to the given mode without requiring MQTT or the
physical buttons. Communicates over a local Unix-domain socket at
`~/fridge_data/fridge.sock`.
```bash
uv run fridge mode idle
uv run fridge mode cool-down
uv run fridge mode cool
uv run fridge mode special
```

### `export` — Export logged data
Exports the database (prices, energy states, measurements, hourly stats) to CSV or
Parquet.
```bash
# Merged CSV (default)
uv run fridge export --output fridge.csv

# One file per table
uv run fridge export --output fridge.csv --separate-tables

# Parquet, restricted to a time range (ISO-8601, UTC)
uv run fridge export --output fridge.parquet --format parquet \
    --start 2026-01-01T00:00:00 --end 2026-02-01T00:00:00
```

### `test` — Hardware test sequence
Runs a predefined sequence to verify all components (sensor, LEDs, relays) during
installation.
```bash
uv run fridge test
```

### `scan` — Scan the I2C bus
Lists all detected I2C device addresses. Useful for verifying sensor and GPIO
connections.
```bash
uv run fridge scan
```

---

## MQTT

The application publishes state and measurements to an MQTT broker and listens for
mode commands. MQTT is optional — the thermostat keeps running even if the broker is
unreachable.

All topics are prefixed with `mqtt_topic_prefix` from `[MQTT]` (default `fridge/`).

### Published topics

| Topic          | Payload                                        | Retain | When                              |
|----------------|------------------------------------------------|--------|-----------------------------------|
| `state/online` | `online` / `offline` (LWT)                    | yes    | On connect / disconnect           |
| `state/mode`   | `IDLE` / `COOL_DOWN` / `COOL` / `SPECIAL`     | yes    | On connect and on change          |
| `state/action` | `IDLE` / `COOLING` / `HEATING`                | yes    | On connect and on change          |
| `state/energy` | `LOW_PRICE` / `NORMAL_PRICE` / `HIGH_PRICE`   | yes    | On connect and on change          |
| `status`       | JSON snapshot (see below)                      | yes    | Every `update_interval` seconds   |
| `hourly`       | JSON hourly-stats snapshot (see below)         | yes    | Top of every UTC hour, and on (re)connect |

**`status` payload:**
```json
{
  "mode": "COOL",
  "action": "COOLING",
  "energy": "HIGH_PRICE",
  "temperature": 4.2,
  "humidity": 58.6,
  "target_temperature": 5.0,
  "target_humidity": 55.0,
  "price": 1.2345,
  "price_currency": "SEK"
}
```

**`hourly` payload:**
```json
{
  "hour_start": "2026-08-02T20:00:00+00:00",
  "mode": "COOL",
  "min_temperature": 4.1,
  "max_temperature": 5.9,
  "mean_temperature": 5.02,
  "min_humidity": 58.2,
  "max_humidity": 63.7,
  "mean_humidity": 60.4,
  "cooler_duty": 62.5,
  "compressor_duty": 41.7,
  "sample_count": 720
}
```

`cooler_duty` is the percentage of the hour the cooler mains relay was HIGH.
`compressor_duty` is the percentage the compressor was actually running (mains HIGH
**and** compressor-pause relay LOW).

### Subscribed topics

| Topic      | Accepted payloads                      | Effect                                    |
|------------|----------------------------------------|-------------------------------------------|
| `cmd/mode` | `IDLE`, `COOL_DOWN`, `COOL`, `SPECIAL` | Same as pressing the corresponding button.|

### Example
```bash
# Watch all updates
mosquitto_sub -h mqtt.local -u fridge -t 'fridge/#' -v

# Switch to cool-down mode remotely
mosquitto_pub -h mqtt.local -u fridge -t fridge/cmd/mode -m COOL_DOWN
```

---

## Logging

Events (mode changes, relay switches) are logged to `~/fridge.log` and to the console.

State-change lines include the current sensor reading:
```
Mode changed to COOL (temp=5.2°C, hum=88.4%)
Energy state changed to NORMAL_PRICE (temp=5.2°C, hum=88.4%)
Action state changed to COOLING (temp=7.0°C, hum=81.0%)
CoolerController: OFF -> SOFT_START (temp=7.0°C, hum=81.0%)
```

Temperature, humidity and controller states are also logged to a shared SQLite database
once per `measurement_interval` seconds (default 60). The same database holds Tibber
prices and computed energy states, so equipment performance and energy-cost savings can
be analysed together.

| Key                    | Default                          | Description                                         |
|------------------------|----------------------------------|-----------------------------------------------------|
| `log_file`             | `~/fridge.log`                   | Event log file path.                                |
| `database_file`        | `~/fridge_data/fridge_prices.db` | Shared SQLite database for prices, states and logs. |
| `measurement_interval` | `60`                             | Seconds between logged measurement snapshots.       |

Alongside per-sample measurements, one row per wall-clock UTC hour is aggregated into
the `hourly_stats` table (dominant mode, min/max/mean temperature and humidity, cooler
and compressor duty cycles). The running accumulator is snapshotted to diskcache so a
mid-hour restart resumes without losing samples.
