"""
Command-line interface for the fridge controller.

Provides commands for configuration, status display, and daemon operation.
"""

import logging
import os
import shutil
import signal
import threading
from configparser import ConfigParser
from pathlib import Path

import click
from diskcache import Cache

from fridge import (
    I2cGpio,
    RelayGpio,
    ButtonsGpio,
    Sht30,
    ModeState,
    ActionState,
    EnergyState,
    cache_path,
    data_path,
    i2c_scan,
    mode_idle,
    mode_cooling_down,
    mode_cool,
)
from fridge.thermostat import Thermostat

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_NAME = "config_default.ini"
_CONFIG_NAME = "fridge.ini"


def _get_default_config_path() -> Path:
    """
    Locate the bundled ``config_default.ini`` shipped with the package.

    :return: Path to the default configuration file
    """
    return Path(__file__).resolve().parent.parent.parent / _DEFAULT_CONFIG_NAME


def load_config(data_dir: str | None = None) -> ConfigParser:
    """
    Load the fridge configuration from the data directory.

    Falls back to ``config_default.ini`` values for any missing keys.

    :param data_dir: Override for the data directory path
    :return: Parsed configuration
    """
    if data_dir is None:
        data_dir = os.path.expanduser(data_path)

    config = ConfigParser()
    # Read default config first as fallback
    default_path = _get_default_config_path()
    if default_path.exists():
        config.read(str(default_path))

    # Override with user config if it exists
    user_ini = os.path.join(data_dir, _CONFIG_NAME)
    if os.path.exists(user_ini):
        config.read(user_ini)

    return config


def _setup_logging(config: ConfigParser, data_dir: str) -> None:
    """
    Configure file-based logging for daemon operation.

    :param config: Application configuration
    :param data_dir: Data directory path
    """
    log_file = config.get("Logging", "log_file", fallback="fridge.log")
    log_file = os.path.expanduser(log_file)

    # If log_file is just a filename, place it in the data directory
    if not os.path.isabs(log_file):
        log_file = os.path.join(data_dir, log_file)

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """Fridge controller — manages cooler, heater and ventilation."""
    pass


@cli.command()
def config():
    """Create configuration file if missing and display current settings."""
    expanded_data_path = os.path.expanduser(data_path)
    os.makedirs(expanded_data_path, exist_ok=True)

    ini_path = os.path.join(expanded_data_path, _CONFIG_NAME)

    if not os.path.exists(ini_path):
        default_path = _get_default_config_path()
        if default_path.exists():
            shutil.copy2(str(default_path), ini_path)
            click.echo(f"Created {ini_path} from defaults.")
        else:
            click.echo("Warning: default config not found.", err=True)
    else:
        click.echo(f"Configuration file already exists: {ini_path}")

    cfg = ConfigParser()
    cfg.read(ini_path)

    click.echo("")
    for section in cfg.sections():
        click.echo(f"[{section}]")
        for key, value in cfg.items(section):
            click.echo(f"  {key} = {value}")
        click.echo("")


@cli.command()
def show():
    """Display current relay and button states (read-only)."""
    try:
        gpio = I2cGpio()
    except Exception as exc:
        click.echo(f"Error initialising GPIO: {exc}", err=True)
        raise SystemExit(1) from exc

    relay = RelayGpio(gpio)
    expanded_cache = os.path.expanduser(cache_path)
    cache = Cache(expanded_cache)

    try:
        click.echo("=== Relay States ===")
        click.echo(f"  Cooler : {'ON' if relay.read_cooler_relay() else 'OFF'}")
        click.echo(f"  Heater : {'ON' if relay.read_heater_relay() else 'OFF'}")
        click.echo(f"  Fan    : {'ON' if relay.read_vent_fan_relay() else 'OFF'}")

        click.echo("")
        click.echo("=== LED States ===")
        port_b = gpio.read("B")
        click.echo(f"  Off LED       : {'ON' if port_b & (1 << 4) else 'OFF'}")
        click.echo(f"  Cool Down LED : {'ON' if port_b & (1 << 5) else 'OFF'}")
        click.echo(f"  Cool LED      : {'ON' if port_b & (1 << 6) else 'OFF'}")

        click.echo("")
        click.echo("=== Button States ===")
        port_a = gpio.read("A")
        click.echo(f"  Off       : {'PRESSED' if not (port_a & (1 << 4)) else 'released'}")
        click.echo(f"  Cool Down : {'PRESSED' if not (port_a & (1 << 5)) else 'released'}")
        click.echo(f"  Cool      : {'PRESSED' if not (port_a & (1 << 6)) else 'released'}")

        click.echo("")
        click.echo("=== Cached State ===")
        mode = cache.get("mode")
        action = cache.get("action_state")
        energy = cache.get("energy_state")
        click.echo(f"  Mode         : {ModeState(mode).name if mode is not None else 'N/A'}")
        click.echo(f"  Action       : {ActionState(action).name if action is not None else 'N/A'}")
        click.echo(f"  Energy       : {EnergyState(energy).name if energy is not None else 'N/A'}")

        cool_down_start = cache.get("cool_down_start")
        if cool_down_start is not None:
            click.echo(f"  Cool-down started at: {cool_down_start}")

        click.echo("")
        click.echo("=== Sensor Readings ===")
        try:
            sensor = Sht30()
            temperature, humidity = sensor.read_data()
            click.echo(f"  Temperature : {temperature:.1f} °C")
            click.echo(f"  Humidity    : {humidity:.1f} %")
        except Exception as exc:
            click.echo(f"  Error reading sensor: {exc}")
    finally:
        cache.close()


@cli.command()
def scan():
    """Scan the I2C bus for connected devices."""
    click.echo("Scanning I2C bus...")
    addresses = i2c_scan()
    click.echo(f"Found {len(addresses)} devices:")
    for addr in addresses:
        click.echo(f"  0x{addr:02x}")


@cli.command()
def run():
    """Run the fridge controller daemon."""
    expanded_data = os.path.expanduser(data_path)
    os.makedirs(expanded_data, exist_ok=True)

    cfg = load_config(expanded_data)
    _setup_logging(cfg, expanded_data)

    logger.info("Starting fridge controller")

    gpio = I2cGpio()
    thermostat = Thermostat(gpio=gpio, config=cfg)
    buttons = ButtonsGpio(gpio)

    try:
        # Connect blinker signals to thermostat and button LEDs
        def _on_mode_idle(sender, **kwargs):
            logger.info("Mode changed to IDLE")
            thermostat.set_mode(ModeState.IDLE)
            buttons.set_mode(ModeState.IDLE)

        def _on_mode_cool_down(sender, **kwargs):
            logger.info("Mode changed to COOL_DOWN")
            thermostat.set_mode(ModeState.COOL_DOWN)
            buttons.set_mode(ModeState.COOL_DOWN)

        def _on_mode_cool(sender, **kwargs):
            logger.info("Mode changed to COOL")
            thermostat.set_mode(ModeState.COOL)
            buttons.set_mode(ModeState.COOL)

        mode_idle.connect(_on_mode_idle)
        mode_cooling_down.connect(_on_mode_cool_down)
        mode_cool.connect(_on_mode_cool)

        # Set initial LED state from cached mode
        buttons.set_mode(thermostat.get_mode())

        # Start components
        thermostat.start()
        buttons.start_interrupt_listener()

        logger.info("Fridge controller running (press Ctrl+C to stop)")

        # Block until SIGINT / SIGTERM
        shutdown_event = threading.Event()

        def _handle_signal(signum, frame):
            logger.info("Received signal %s, shutting down", signum)
            shutdown_event.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        shutdown_event.wait()
    finally:
        # Graceful shutdown
        logger.info("Stopping fridge controller")
        thermostat.stop()
        buttons.stop_interrupt_listener()
        logger.info("Fridge controller stopped")
        logging.shutdown()
