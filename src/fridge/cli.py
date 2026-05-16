import click
from fridge.i2c import i2c_scan


@click.group()
def cli():
    pass


@click.command()
def scan():
    """Scan the I2C bus for connected devices."""
    click.echo("Scanning I2C bus...")
    addresses = i2c_scan()
    click.echo(f"Found {len(addresses)} devices:")
    for addr in addresses:
        click.echo(f"  {addr:02x}")


@click.command()
def show():
    """Display the contents of the fridge."""
    click.echo("Fridge contents:")
