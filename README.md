# Fridge-App
This application is controlling and monitoring a large fridge room / shed for game (boar, dear, etc.).
This is used to cool down the body slowly and then keep it at a controlled temperature 
to let the meat tender before cutting the meat.
It can also be used to keep a massive amount of beer cold.

The fridge is equipped with a temperature and humidity sensor, a heater and a cooler
and a fan to ventilate.

This is a work in progress and not all features are implemented yet.

# Typical usage
The system is Idle mode when the fridge is not in use.
The user can push the cool down button to start the cooling down process
or the cool button to start the cooling process.
The user can push the off button to switch the system to Idle mode.
The exact settings are defined in a config file, the values below are the default values.
The application remembers the state of the system,
and is used to restore the state when the application is restarted after a power loss.

## Modes
The application has three modes: Idle (default), Cooling Down and Cool that are described below.
There are three buttons that can be used to switch between these modes.

There is a hysteresis of ±1.5°C on the cooler and heater, to prevent it from switching on and off too often.

### Idle mode
The fridge is in Idle mode when the off button is pressed.
Temperature and humidity is monitored, but heater, fan and cooler are switched off.

### Cooling down mode
The fridge is set in Cool Down mode when the Cool Down button is pressed.
The temperature is controlled to be between 8 and 15 degrees Celsius, with a target of 12 degrees Celsius.
The temperature is controlled to be between 10 and 14 degrees Celsius with the cooler.
The heater is switched on if the temperature is below 8 degrees Celsius 
and switched off if the temperature is above 9 degrees Celsius.
The fan is switched on, and the running continuously in this mode.
The system should automatically switch to Cool mode after 10 hours of Cooling Down mode.

### Cool mode
The fridge is set in Cool mode when the cool button is pressed.
This mode is used to let the meat tender before cutting it, or to keep a massive amount of beer cold.
The temperature is controlled to be between 2 and 8 degrees Celsius, with a target of 5 degrees Celsius.
The cooler is used to regulate the temperature when it is too hot.
The heater is used to as a freeze guard to prevent the temperature to drop below 0°C.
The fan should be active if humidity is above 55%, and switched off if below ths (with a hysteresis of ±5%)

## Energy cost saving
The system can be configured to avoid running the heater and cooler during high-price hours.
Electricity prices are fetched from [Tibber](https://developer.tibber.com/) in 15-minute slots
and each slot is classified as `LOW_PRICE`, `NORMAL_PRICE` or `HIGH_PRICE`. The target temperature
is then nudged for the active slot so the fridge pre-cools before expensive periods and coasts
through them. See the `EnergyState` section below for details on the algorithm and the `[Tibber]`
configuration keys.

# Hardware
This application runs on a Raspberry Pi with I2C connected sensors and GPPIO.
It has been developed on a Raspberry Pi 4B with 2GB RAM.
The Raspberry Pi is equipped with a custom interface card (that You can build Yourself).
The interface board has protected 5 Volt input to power the Raspberry Pi, and the IO described below.

## Temperature and humidity sensor
A SHT30 sensor is used to measure the temperature and humidity, this sensor is connected via I2C at address 0x44.
An optional SHT30 sensor with address 0x45 can be connected to monitor the outside temperature.

## GPIO
A MCP23017 is used as GPIO expander, this is connected via I2C at address 0x20.

Port A is used for inputs (buttons).
The INTA pin is connected to GPIO 27 and is used to generate interrupts when a user presses a button.
Bit 0: Not used
Bit 1: Not used
Bit 2: Not used
Bit 3: Not used
Bit 4: Off button
Bit 5: Cool down button
Bit 6: Cool button
Bit 7: Reserved

Port B is used for outputs (LEDs and relays). All outputs pins are connected to a ULN2803A driver.
Both relays and LED:s are active low (open collector) and are grounded when the GPIO pin goes high.
Bit 0: Cooler relay
Bit 1: Heater relay
Bit 2: Vent fan relay
Bit 3: Reserved
Bit 4: Off LED
Bit 5: Cool down LED
Bit 6: Cool LED
Bit 7: Reserved

The button handler uses radio-button behavior for mode LEDs: when a mode button is pressed,
its corresponding LED is set and the other mode LEDs are cleared while relay bits remain unchanged.
The same LED behavior can also be applied programmatically via `ButtonsGpio.set_mode(ModeState)`
when mode changes are triggered by other parts of the application.
Button events are handled via MCP23017 port A interrupts routed through INTA on Raspberry Pi GPIO 27 for fast response.

The devices that the relays control are slow and should not be switched too often
to avoid damage to the relays or the device.
The maximum switching frequency is one event per five-minute period.

# Software implementation
The software is written in Python. It is a CLI application implemented using the Click library.
It is normally run as a daemon, and the CLI is used to control the application.

The application has the following components:
- Temperature regulation
- Logging for local logging of events, temperature and humidity
- MQTT for external control and logging

Temperature and other settings are defined in an .ini config file read when the application starts.
The default values as defined above in the Typical usage section.

CLI commands are used to control the application.
- **config**: Create the configuration file from defaults if missing, then display current settings.
- **show**: Display the current relay, LED and button states by reading the GPIO (read-only, safe while daemon is running).
- **run**: Run the fridge controller daemon. Starts temperature regulation, button interrupt listener, and blocks until SIGINT/SIGTERM.
- **scan**: Scan the I2C bus for connected devices.
- **export**: Export the logged database (prices, energy states and measurements) to CSV or Parquet.
- **test**: Run a hardware test sequence to verify all components during installation.
- **-h / --help**: Display help message.

Configuration files, logs and databases are stored in a folder outside the application with the path ~/fridge_data.
This makes it possible to update the application without losing the configuration and database.

## Temperature regulation
The temperature is regulated using simple on / off logic.
The regulator has three state engines; ModeState, ActionState and EnergyState.

The temperature regulation runs as a thread,
and this thread is only started when the application is started as a daemon using the --daemon cli option.
The regulation loop should run at a frequency of one turn per second, and temperature is measured at every turn.
The loop checks the current state of the three state engines and updates the state accordingly.

The last measured temperature and humidity is stored using diskcache.
The last state of all three state engines is stored using diskcache,
and is used to restore the state when the application is restarted.

### ModeState
The ModeState engine is used to switch between the three modes. The modes are described above in the Modes section.
- **IDLE**: The system is in idle mode, heater and cooler are turned off.
- **COOL_DOWN**: The system is cooling down.
- **COOL**: The system is keep at a constant low temperature.

### ActionState
This is used for the actual temperature regulation.
The ActionState engine is used to switch between the three actions based on target temperature and current temperature.
The target temperature is defined in the configuration file but can be nudged by the EnergyState engine.
The hysteresis is defined in the configuration file.
- **COOLING**: The cooler is running.
The state is switched to IDLE if the temperature is below the target temperature minus the hysteresis.
- **IDLE**: Heater and cooler are turned off.
The state is switched to COOLING if the temperature is above the target temperature plus the hysteresis.
The state is switched to HEATING if the temperature is below the target temperature minus the hysteresis.
- **HEATING**: The heater is running.
The state is switched to IDLE if the temperature is above the target temperature plus hysteresis.

The thermostat only applies heater/cooler relay changes when the `ActionState` changes.
This reduces unnecessary relay writes while still relying on hysteresis for stable control.
In **IDLE mode**, thermostat thresholds are not used for control decisions and all relays are forced off.

### EnergyState
The system can be configured to avoid running the heater and cooler during high-price hours.
The EnergyState engine is used to switch between the three energy saving states.
The active state is picked from a cached plan once per **15-minute slot**.
- **LOW_PRICE**: The target temperature is lowered by 2 degrees Celsius when cooling is needed,
and raised by 1 degrees Celsius when heating is needed.
- **NORMAL_PRICE**: The target temperature is kept at the same value.
- **HIGH_PRICE**: The target temperature is raised by 2 degrees Celsius when cooling is needed,
and lowered by 2 degrees Celsius when heating is needed.

#### Price source and planner
Prices are fetched from the Tibber API in 15-minute slots and stored in the shared local
SQLite database (`~/fridge_data/fridge_prices.db` by default, configured by `[Logging]
database_file`). The fetch runs on startup if the cache lacks
the current slot, and then once per day at the configured local hour (`fetch_hour`, default
16:00 `Europe/Stockholm`) in a background thread. Network calls use exponential backoff and
failures are logged — the controller keeps running on cached data when offline.

Each slot is classified using percentile bands over the planning window (more robust than
absolute thresholds):
- `LOW_PRICE` — price below the 33rd percentile
- `NORMAL_PRICE` — price between the 33rd and 67th percentile
- `HIGH_PRICE` — price above the 67th percentile

A pre-cooling look-ahead then relabels up to `precool_lookahead_slots` `NORMAL` slots that
precede a `HIGH` block as `LOW`, so the fridge cools down before the expensive period.
The look-back stops at any earlier `HIGH` block (never overshooting into another high block),
and consecutive `LOW` slots are capped at `max_consecutive_low_slots` to avoid over-cooling.

The shared SQLite database used for prices, energy states and logged measurements is
configured by `[Logging] database_file` (see the *Logging* section). The relevant
`[Tibber]` configuration keys are:

| Key                          | Default                              | Description                                                                 |
|------------------------------|--------------------------------------|-----------------------------------------------------------------------------|
| `api_key`                    | _(required)_                         | Tibber API token.                                                           |
| `home_id`                    | _(empty)_                            | Optional home id when the account has several homes.                        |
| `fetch_hour`                 | `16`                                 | Local hour (0–23) for the daily fetch.                                      |
| `timezone`                   | `Europe/Stockholm`                   | IANA timezone for the daily fetch.                                          |
| `precool_lookahead_slots`    | `12`                                 | Maximum 15-min slots (3 h) to look back and pre-cool before a HIGH block.   |
| `max_consecutive_low_slots`  | `4`                                  | Cap on consecutive LOW slots to avoid over-cooling.                         |

### Threshold generation helper
The thermostat uses a helper function to generate mode-specific thresholds from configuration and `EnergyState`.
- In **IDLE**, all thresholds are inactive (`None`) and should be handled as a special case in the regulation loop.
- In **COOL_DOWN**, heater and cooler thresholds are derived generically from configured target temperature and hysteresis,
  and fan thresholds are fixed to `1%` (on) / `0%` (off) to keep the fan running continuously.
- In **COOL**, heater and cooler thresholds are also derived from target and hysteresis with energy adjustments,
  while heater thresholds are clamped by `freeze_guard`.
- For all values used by the helper, missing config entries fall back to the default values listed in this README.

## Configuration
The configuration is stored in an .ini file and read using the configparser library.
The configuration file is named fridge.ini in the users home directory.
The default values are defined in the Typical usage section above and are used if the configuration is missing.
The configuration file is read when the application starts.

## Logging
The application logs events (like state changes) to a file named fridge.log in the current working directory
using the standard Python logging library.

The application logs temperature, humidity and controller states to an SQLite database once per
`measurement_interval` seconds (default 60). These measurements are stored in the **same** database
as the Tibber prices and computed energy states (`[Logging] database_file`, default
`~/fridge_data/fridge_prices.db`), so equipment performance and energy-cost savings can be analysed
together. Each measurement row records the temperature, humidity, mode, action state, energy state,
active targets and the price of the current slot.

The relevant `[Logging]` configuration keys are:

| Key                    | Default                            | Description                                          |
|------------------------|------------------------------------|------------------------------------------------------|
| `log_file`             | `~/fridge.log`                     | Event log file path.                                 |
| `database_file`        | `~/fridge_data/fridge_prices.db`   | Shared SQLite database for prices, states and logs.  |
| `measurement_interval` | `60`                               | Seconds between logged measurement snapshots.        |

### Exporting data
The database can be exported to CSV or Parquet with the `export` command:

```bash
# Merged CSV (default): all tables joined on time into one file
uv run fridge export --output fridge.csv

# One CSV file per table (fridge_prices.csv, fridge_energy_states.csv, fridge_measurements.csv)
uv run fridge export --output fridge.csv --separate-tables

# Parquet export
uv run fridge export --output fridge.parquet --format parquet

# Restrict to a time range (ISO-8601, UTC)
uv run fridge export --start 2026-01-01T00:00:00 --end 2026-02-01T00:00:00
```

By default the three tables are merged into a single file (joined on their timestamp). Use
`--separate-tables` to write one file per table, and `--format parquet` for columnar Parquet output.

## MQTT
The application publishes state and measurements to an MQTT broker and listens
for mode commands. This is implemented in `fridge.mqtt.MqttBridge` using the
`paho-mqtt` library. MQTT is not used for temperature regulation: the
thermostat keeps running even if the broker is unreachable.

All topics are prefixed with `mqtt_topic_prefix` from the `[MQTT]` section of
the configuration (default `fridge/`).

### Published topics

| Topic                          | Payload                              | Retain | When                                  |
|--------------------------------|--------------------------------------|--------|---------------------------------------|
| `state/online`                 | `online` / `offline` (LWT)           | yes    | On connect / disconnect               |
| `state/mode`                   | `IDLE` / `COOL_DOWN` / `COOL`        | yes    | On connect and whenever it changes    |
| `state/action`                 | `IDLE` / `COOLING` / `HEATING`       | yes    | On connect and whenever it changes    |
| `state/energy`                 | `LOW_PRICE` / `NORMAL_PRICE` / `HIGH_PRICE` | yes | On connect and whenever it changes |
| `status`                       | JSON snapshot (see below)            | yes    | Every `update_interval` seconds       |

The `status` topic carries a single JSON object containing all current
states, measurements and active targets. Targets that are not active in the
current mode (e.g. humidity target in `COOL_DOWN`, or all targets in
`IDLE`) are reported as `null`.

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

`price` is the total Tibber price (including tax) for the current 15-minute slot and
`price_currency` is the currency reported by Tibber. Both are `null` when no cached price
is available for the current slot (e.g. before the first successful fetch).

### Subscribed topics

| Topic         | Accepted payloads                | Effect                                              |
|---------------|----------------------------------|-----------------------------------------------------|
| `cmd/mode`    | `IDLE`, `COOL_DOWN`, `COOL`      | Same as pressing the corresponding physical button. |

Mode commands received over MQTT are routed through the same internal signals
as the buttons, so the front-panel mode LEDs are updated together with the
thermostat state.

### Example
```bash
# Watch all state and measurement updates
mosquitto_sub -h mqtt.local -u fridge -t 'fridge/#' -v

# Switch the fridge to cool-down mode remotely
mosquitto_pub -h mqtt.local -u fridge -t fridge/cmd/mode -m COOL_DOWN
```

# Installation and usage

## Prerequisites
- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/) for dependency management and running the application
- Raspberry Pi 4B with the custom interface board (for hardware operation)

## Installation
Clone the repository and install dependencies:
```bash
git clone <repository-url>
cd fridge
uv sync
```

## Configuration
The application stores its configuration, data, logs and cache in `~/fridge_data/`.
This directory is separate from the application code, so the application can be updated
without losing configuration or state data.

### Creating the configuration file
Run the `config` command to create the configuration file from defaults:
```bash
uv run fridge config
```
This copies `config_default.ini` to `~/fridge_data/fridge.ini` if it does not already exist,
then displays all current settings. Edit `~/fridge_data/fridge.ini` to customise values such as
target temperatures, hysteresis, refresh rate, and energy price adjustments.

### Configuration sections
| Section        | Description                                               |
|----------------|-----------------------------------------------------------|
| `General`      | Refresh rate and minimum relay switching interval          |
| `Hardware`     | I2C addresses for sensors and GPIO, interrupt GPIO pin     |
| `CoolDownMode` | Target temperature, hysteresis and auto-switch duration    |
| `CoolMode`     | Target temperature, hysteresis, freeze guard and humidity  |
| `Energy`       | Temperature adjustments for low and high energy prices     |
| `Logging`      | Log file, shared database file and measurement interval    |
| `MQTT`         | MQTT broker settings, topic prefix and publish interval   |
| `Tibber`       | Tibber API key, price cache and energy-plan parameters     |

## CLI commands
The application is invoked with `uv run fridge` followed by a command.
Use `-h` or `--help` on any command to see its options.

```bash
uv run fridge --help
```

### `config` — Configuration management
Creates the configuration file from defaults if it does not exist, then displays all
current settings in a human-readable format.
```bash
uv run fridge config
```

### `show` — Display current state (read-only)
Reads the GPIO hardware and diskcache to display the current state of relays,
LEDs, buttons and cached thermostat state. This command is read-only and safe to
run while the daemon is already running.
```bash
uv run fridge show
```
Example output:
```
=== Relay States ===
  Cooler : OFF
  Heater : OFF
  Fan    : OFF

=== LED States ===
  Off LED       : ON
  Cool Down LED : OFF
  Cool LED      : OFF

=== Button States ===
  Off       : released
  Cool Down : released
  Cool      : released

=== Cached State ===
  Mode         : IDLE
  Action       : IDLE
  Energy       : NORMAL_PRICE

=== Energy Price ===
  Current price : 1.2345 SEK
  Tibber level  : NORMAL

=== Sensor Readings ===
  Temperature : 6.5 °C
  Humidity    : 72.3 %
```

The `Energy Price` section shows the cached Tibber price for the current 15-minute slot.
It is reported as `N/A` when no cached price is available for the current slot.

### `run` — Run the fridge controller
Starts the fridge controller as a long-running process. The thermostat loop reads
the temperature sensor, regulates relays according to the active mode, and listens
for button presses via hardware interrupts.

```bash
uv run fridge run
```

The process runs until it receives `SIGINT` (Ctrl+C) or `SIGTERM`. On shutdown it
turns off all relays and stops the thermostat loop gracefully.

To run as a systemd service, create a unit file such as `/etc/systemd/system/fridge.service`:
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

Then enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable fridge.service
sudo systemctl start fridge.service
```

### `export` — Export logged data
Exports the logged database (prices, energy states and measurements) to CSV (default) or
Parquet for analysis. By default all tables are merged into a single file; use
`--separate-tables` to write one file per table.

```bash
# Merged CSV (default)
uv run fridge export --output fridge.csv

# One file per table
uv run fridge export --output fridge.csv --separate-tables

# Parquet, restricted to a time range
uv run fridge export --output fridge.parquet --format parquet \
    --start 2026-01-01T00:00:00 --end 2026-02-01T00:00:00
```

### `test` — Hardware test sequence
Runs a predefined sequence of hardware checks to verify that all components
(sensor, LEDs, relays) are functioning correctly during installation.
This command overrides the one-event-per-five-minutes relay rate limit.

```bash
uv run fridge test
```

The test sequence performs the following steps:
1. Read and display temperature and humidity from the SHT30 sensor.
2. Running light over all three LEDs (1 second per LED, 5 seconds total). Only LEDs are activated, not relays.
3. Switch on the vent fan relay and Idle LED. Hold for 60 seconds.
4. Switch off fan and Idle LED. Switch on heater relay and Cool Down LED. Hold for 60 seconds.
5. Switch off heater and Cool Down LED. Switch on cooler relay and Cool LED. Hold for 60 seconds.
6. Switch everything off and exit.

Each step is logged via `logger.info`. If an error occurs, all relays and LEDs
are turned off before the command exits.

### `scan` — Scan the I2C bus
Scans the I2C bus and lists all detected device addresses. Useful for verifying
that the SHT30 sensor and MCP23017 GPIO expander are connected correctly.
```bash
uv run fridge scan
```

## State persistence
The application uses [diskcache](https://grantjenks.com/docs/diskcache/) to persist
thermostat state (mode, action state, energy state and cool-down timer) in
`~/fridge_data/cache/`. This ensures that the fridge resumes its previous operating
mode after a power loss or restart.

## Error handling and resilience
The application is designed to run unattended and recover from transient I2C bus
errors without crashing:

- **Sensor reads** – `Sht30.read_data()` retries up to 3 times with a short delay
  before giving up. If all retries fail, the thermostat loop logs a warning and
  skips the current cycle instead of terminating the thread.
- **Button interrupts** – The interrupt handler reads the MCP23017 INTCAP register
  (captured pin state at interrupt time) so that a quick button release does not
  cause the press to be missed. I2C errors during interrupt handling are caught
  and logged.

## Logging
Events such as mode changes and relay switches are logged to a file (default
`~/fridge.log`) and to the console. The log file path can be changed in the
`[Logging]` section of the configuration file.

In addition, temperature, humidity and controller states are logged once per
`measurement_interval` seconds to the shared SQLite database (`[Logging]
database_file`). See the *Logging* section above and the `export` command for
how to retrieve this data for analysis.

## Development

### Running tests
```bash
# Run all non-hardware tests
uv run pytest tests/ --ignore=tests/test_thermostat_hw.py

# Run hardware-backed tests on the Raspberry Pi
uv run pytest tests/test_thermostat_hw.py

# Run with coverage
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

