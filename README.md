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
The application has four modes: Idle (default), Cooling Down, Cool and Special that are described below.
There are four buttons that can be used to switch between these modes.

There is a hysteresis of ±1.5°C on the cooler and heater, to prevent it from switching on and off too often.
The refrigeration compressor also runs on a duty cycle (see *Cooler duty cycle* below) to prevent the
heat exchanger from freezing up and blocking the air flow.

### Idle mode
The fridge is in Idle mode when the off button is pressed.
Temperature and humidity is monitored, but heater, fan and cooler are switched off.

### Cooling down mode
The fridge is set in Cool Down mode when the Cool Down button is pressed.
The temperature is controlled to be between 10 and 14 degrees Celsius with the cooler, with a target of 12 degrees Celsius.
The heater acts as a freeze guard: it is switched on if the temperature drops below `freeze_guard` (default 9°C)
and switched off when the temperature rises above `freeze_guard + hysteresis`.
The vent fan (humidity control) follows the same humidity logic as in Cool mode.
The system should automatically switch to Cool mode after 10 hours of Cooling Down mode.

### Cool mode
The fridge is set in Cool mode when the cool button is pressed.
This mode is used to let the meat tender before cutting it, or to keep a massive amount of beer cold.
The temperature is controlled around a target of 5 degrees Celsius (±`hysteresis`/2, default 1.5°C).
The cooler is used to regulate the temperature when it is too hot (see *Cooler duty cycle* below).
The heater acts purely as a freeze guard: it is switched on when the temperature drops below `freeze_guard`
(default 2°C) and switched off when it rises above `freeze_guard + hysteresis`.
The vent fan is used to control humidity and is active if humidity is above 55% (with a hysteresis of ±5%).
Note: the vent fan is separate from the circulation fan inside the cooler unit — the circulation fan
is energised automatically by the *Cooler relay* (Port B Bit 0).

### Special mode
The fridge is set in Special mode when the special button is pressed.
This mode behaves like Cool mode but reads its own target temperature, hysteresis,
freeze guard and humidity thresholds from the `[SpecialMode]` section of the config
file. It is intended as a user-configurable slot for a different set-point (for example
longer tenderising at a slightly higher temperature, or a lower serving temperature
for drinks) without disturbing the calibrated `[CoolMode]` values.

The defaults in `[SpecialMode]` mirror `[CoolMode]` (target 5°C, hysteresis ±1.5°C,
target humidity 55% ±5%, freeze guard 0°C); adjust them in `fridge.ini` to suit.

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
Bit 7: Special button

Port B is used for outputs (LEDs and relays). All outputs pins are connected to a ULN2803A driver.
Both relays and LED:s are active low (open collector) and are grounded when the GPIO pin goes high.
Bit 0: Cooler relay (mains switch for the cooler unit — enables both the circulation fan and the refrigeration compressor)
Bit 1: Heater relay
Bit 2: Vent fan relay (humidity control, not the circulation fan)
Bit 3: Compressor pause relay (pauses the refrigeration compressor while the circulation fan keeps running)
Bit 4: Off LED
Bit 5: Cool down LED
Bit 6: Cool LED
Bit 7: Special LED

The button handler uses radio-button behavior for mode LEDs: when a mode button is pressed,
its corresponding LED is set and the other mode LEDs are cleared while relay bits remain unchanged.
The same LED behavior can also be applied programmatically via `ButtonsGpio.set_mode(ModeState)`
when mode changes are triggered by other parts of the application.
Button events are handled via MCP23017 port A interrupts routed through INTA on Raspberry Pi GPIO 27 for fast response.

The devices that the relays control are slow and should not be switched too often to avoid damage.
The cooler and compressor-pause relays are driven by the *Cooler duty cycle* state machine (see below),
which enforces the required minimum on/off/pause times. The vent fan (humidity control) is rate-limited
separately: it may not switch on or off more often than once per `min_vent_fan` interval (default 5 minutes).
The heater is not additionally rate-limited beyond its own hysteresis band.

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
- **SPECIAL**: Like COOL, but with its own configurable target temperature and humidity.

### ActionState
This is used for the actual temperature regulation.
The ActionState engine is used to switch between the three actions based on target temperature and current temperature.
The target temperature and `freeze_guard` are defined in the configuration file but can be nudged by the EnergyState engine.
The hysteresis is defined in the configuration file.

- **COOLING**: The cooler duty cycle is active (see below).
  Switched on when the temperature is above `target_temperature + hysteresis / 2`.
  Switched off when the temperature drops below `target_temperature - hysteresis / 2`.
- **IDLE**: Neither the cooler duty cycle nor the heater is active.
- **HEATING**: The heater is running.
  Switched on when the temperature drops below `freeze_guard`.
  Switched off when the temperature rises above `freeze_guard + hysteresis`.

Heating and cooling are controlled independently: the heater only reacts to `freeze_guard`, not to the
cooling target, so it can never be triggered by the cooler spinning down or by a duty-cycle pause.
In **IDLE mode**, thermostat thresholds are not used for control decisions and all relays are forced off.

#### Cooler duty cycle
To prevent the heat exchanger from freezing up, the refrigeration compressor may only run for a limited
time and must then pause with the circulation fan still running. The behaviour is driven by three global
settings in `[General]` (all in minutes):

| Key                      | Default | Meaning                                                                          |
|--------------------------|---------|----------------------------------------------------------------------------------|
| `max_cooler_time`        | `30`    | Maximum continuous compressor run time before a forced pause.                    |
| `cooler_pause_time`      | `15`    | Duration of the forced pause (circulation fan keeps running).                    |
| `cooler_soft_start_time` | `2`     | Time the compressor stays paused after the cooler unit is energised (soft start).|

*Switching the cooler on* (temperature rises above `target_temperature + hysteresis / 2`):
1. Port B Bit 3 HIGH (compressor paused).
2. Port B Bit 0 HIGH (cooler unit energised — circulation fan starts).
3. Wait `cooler_soft_start_time` minutes.
4. Port B Bit 3 LOW (compressor starts).

*While cooling* (temperature still above off-threshold):
- After the compressor has run for `max_cooler_time` minutes, Port B Bit 3 goes HIGH (compressor paused,
  Bit 0 stays HIGH so the circulation fan keeps running).
- After `cooler_pause_time` minutes, Port B Bit 3 goes LOW again and the compressor resumes.
- This cycle repeats until the temperature reaches the off-threshold.

*Switching the cooler off* (temperature drops below `target_temperature - hysteresis / 2`):
1. Port B Bit 3 HIGH (compressor paused).
2. Wait `cooler_pause_time` minutes (circulation fan keeps running to defrost the exchanger).
3. Port B Bit 0 LOW (cooler unit off). Port B Bit 3 stays HIGH.

If the off-threshold is reached while the compressor is already in a duty-cycle pause, the remaining
pause time is reused — no additional pause is inserted before Port B Bit 0 goes LOW.

### EnergyState
The system can be configured to avoid running the heater and cooler during high-price hours.
The EnergyState engine is used to switch between the three energy saving states.
The active state is picked from a cached plan once per **15-minute slot**.
- **LOW_PRICE**: `target_temperature` is lowered by `low_price_cool_adj` (default −2°C) and `freeze_guard`
  is raised by `low_price_heat_adj` (default +1°C).
- **NORMAL_PRICE**: `target_temperature` and `freeze_guard` are kept at their configured values.
- **HIGH_PRICE**: `target_temperature` is raised by `high_price_cool_adj` (default +2°C) and `freeze_guard`
  is lowered by `high_price_heat_adj` (default −2°C).

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
- In **COOL**, cooler thresholds are derived from target and hysteresis (with the LOW/HIGH price adjustment
  applied to `target_temperature`), and heater thresholds are derived purely from `freeze_guard` and hysteresis
  (with the LOW/HIGH price adjustment applied to `freeze_guard`).
- In **SPECIAL**, thresholds are derived exactly like in **COOL** but read from the `[SpecialMode]`
  config section, allowing an independent user-configurable set-point.
- For all values used by the helper, missing config entries fall back to the default values listed in this README.

## Configuration
The configuration is stored in an .ini file and read using the configparser library.
The configuration file is named fridge.ini in the users home directory.
The default values are defined in the Typical usage section above and are used if the configuration is missing.
The configuration file is read when the application starts.

## Logging
The application logs events (like state changes) to a file named fridge.log in the current working directory
using the standard Python logging library.

State-change log lines (mode, energy state, action state and `CoolerController` transitions)
include the current sensor reading in a compact form using the short keys `temp` and `hum`,
for example:

```
Mode changed to COOL (temp=5.2°C, hum=88.4%)
Energy state changed to NORMAL_PRICE (temp=5.2°C, hum=88.4%)
Action state changed to COOLING (temp=7.0°C, hum=81.0%)
CoolerController: OFF -> SOFT_START (temp=7.0°C, hum=81.0%)
```

If the sensor has not yet produced a valid reading, the values are rendered as `nan`.
The "Energy state changed to ..." line is only emitted when the energy state actually
changes (no longer on every re-evaluation).

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

# One CSV file per table (fridge_prices.csv, fridge_energy_states.csv,
# fridge_measurements.csv, fridge_hourly_stats.csv)
uv run fridge export --output fridge.csv --separate-tables

# Parquet export
uv run fridge export --output fridge.parquet --format parquet

# Restrict to a time range (ISO-8601, UTC)
uv run fridge export --start 2026-01-01T00:00:00 --end 2026-02-01T00:00:00
```

By default the four tables are merged into a single file (joined on their timestamp).
The `hourly_stats` table is left-joined on the hour-truncated timestamp and its
columns are prefixed with `hourly_` (so the merged frame contains e.g.
`hourly_mode`, `hourly_cooler_duty`, `hourly_compressor_duty`). Use
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
| `state/mode`                   | `IDLE` / `COOL_DOWN` / `COOL` / `SPECIAL` | yes | On connect and whenever it changes    |
| `state/action`                 | `IDLE` / `COOLING` / `HEATING`       | yes    | On connect and whenever it changes    |
| `state/energy`                 | `LOW_PRICE` / `NORMAL_PRICE` / `HIGH_PRICE` | yes | On connect and whenever it changes |
| `status`                       | JSON snapshot (see below)            | yes    | Every `update_interval` seconds       |
| `hourly`                       | JSON hourly-stats snapshot (see below) | yes  | At the top of every UTC hour, and republished on (re)connect |

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

The `hourly` topic carries a retained JSON message with the aggregate
statistics of the just-closed wall-clock UTC hour. Duty cycles are reported
as percentages (0-100). Temperature and humidity aggregates are `min`, `max`
and a time-weighted `mean`. When no sensor readings were valid during the
hour, the aggregate is `null`.

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

`cooler_duty` is the percentage of the hour that the cooler mains relay
(Port B Bit 0) was HIGH. `compressor_duty` is the percentage of the hour
that the compressor was actually running (`CoolerController` state
`RUNNING`, i.e. mains HIGH **and** the compressor-pause relay LOW).
`mode` is the dominant `ModeState` for the hour (longest cumulative time;
the mode active at end-of-hour is used as the tie-break).

### Subscribed topics

| Topic         | Accepted payloads                | Effect                                              |
|---------------|----------------------------------|-----------------------------------------------------|
| `cmd/mode`    | `IDLE`, `COOL_DOWN`, `COOL`, `SPECIAL` | Same as pressing the corresponding physical button. |

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
| `General`      | Refresh rate, vent-fan rate limit, cooler duty-cycle times |
| `Hardware`     | I2C addresses for sensors and GPIO, interrupt GPIO pin     |
| `CoolDownMode` | Target temperature, hysteresis and auto-switch duration    |
| `CoolMode`     | Target temperature, hysteresis, freeze guard and humidity  |
| `SpecialMode`  | Independent target/hysteresis/humidity for the Special mode|
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

### `mode` — Switch the active mode locally
Switches the running fridge controller to the given `ModeState` without requiring
an MQTT broker or the physical buttons. The command talks to `fridge run` over a
local Unix-domain socket at `~/fridge_data/fridge.sock` (created with `0600`
permissions, owner-only), so it works fully offline.

```bash
uv run fridge mode idle
uv run fridge mode cool-down
uv run fridge mode cool
uv run fridge mode special
```

On success the command prints e.g. `Mode set to COOL`. If the daemon is not
running the command exits non-zero with an error message. Internally it emits
the same blinker signals a button press or MQTT `cmd/mode` message would, so
the thermostat state and the front-panel LEDs stay in sync.

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
This command overrides the normal vent-fan rate limit and the cooler duty-cycle timings.

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

### Hourly duty-cycle stats
Alongside the per-sample measurements, the application aggregates one row
per wall-clock UTC hour into the `hourly_stats` table of the same database.
Each row records the dominant mode, min/max/mean temperature and humidity,
the cooler and compressor duty cycles (percent of the hour that the mains
relay was HIGH and that the compressor was actually running, respectively)
and the number of samples that were aggregated.

The running accumulator is periodically snapshotted to the diskcache in
`~/fridge_data/cache/` so that a restart mid-hour resumes the in-progress
hour without losing samples. If, on start-up, the persisted snapshot
belongs to an older hour, that hour is finalised into the database before a
fresh accumulator is started for the current hour.

On every top-of-hour rollover the finalised row is also published as a
retained JSON message on the MQTT `hourly` topic (see the MQTT section).

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

