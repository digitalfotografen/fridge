"""
SHT30 diagnostic for the fridge Pi.

Stop the service first so nothing else talks on the bus:
    sudo systemctl stop fridge
    cd ~/fridge && uv run python scripts/sht30_diag.py            # observe only
    cd ~/fridge && uv run python scripts/sht30_diag.py --recover  # also try a software recovery
    sudo systemctl start fridge

Default mode changes no sensor state except consuming one pending sample.
"""

import subprocess
import sys
import time

from smbus2 import SMBus, i2c_msg

ADDR = 0x44
MCP = 0x20
FETCH = 0xE000
STATUS = 0xF32D  # datasheet status read (the app uses 0x3022, which is not a documented command)
CLEAR_STATUS = 0x3041
BREAK = 0x3093
SOFT_RESET = 0x30A2
ART = 0x2B32


def crc8(data):
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def cmd(bus, c):
    bus.i2c_rdwr(i2c_msg.write(ADDR, [c >> 8, c & 0xFF]))


def read(bus, n):
    m = i2c_msg.read(ADDR, n)
    bus.i2c_rdwr(m)
    return list(m)


def show(label, fn):
    try:
        print(f"  {label:<38} OK   {fn()}")
        return True
    except OSError as e:
        print(f"  {label:<38} FAIL {e}")
        return False


def decode(d):
    ok = crc8(d[0:2]) == d[2] and crc8(d[3:5]) == d[5]
    t = -45 + 175 * ((d[0] << 8 | d[1]) / 65535)
    h = 100 * ((d[3] << 8 | d[4]) / 65535)
    return f"T={t:.2f}C RH={h:.1f}% crc={'ok' if ok else 'BAD'} raw={[hex(x) for x in d]}"


def fetch(bus):
    cmd(bus, FETCH)
    return decode(read(bus, 6))


def status(bus):
    cmd(bus, STATUS)
    time.sleep(0.002)
    d = read(bus, 3)
    s = d[0] << 8 | d[1]
    flags = {
        15: "alert",
        13: "heater",
        11: "RH-alert",
        10: "T-alert",
        4: "RESET-detected",
        1: "last-cmd-failed",
        0: "write-crc-failed",
    }
    on = [n for b, n in flags.items() if s & (1 << b)]
    return f"0x{s:04x} crc={'ok' if crc8(d[:2]) == d[2] else 'BAD'} {on}"


print("== Bus lines (SDA=GPIO2, SCL=GPIO3; both should read 'hi' when idle) ==")
for tool in (["pinctrl", "get", "2,3"], ["raspi-gpio", "get", "2-3"]):
    try:
        print(subprocess.run(tool, capture_output=True, text=True, timeout=5).stdout.strip())
        break
    except FileNotFoundError:
        continue

with SMBus(1) as bus:
    print("\n== Presence ==")
    show("MCP23017 0x20 ack", lambda: bus.i2c_rdwr(i2c_msg.write(MCP, [])) or "ack")
    show("SHT30 0x44 ack", lambda: bus.i2c_rdwr(i2c_msg.write(ADDR, [])) or "ack")

    print("\n== Current state (no commands that change mode) ==")
    print("  A fetch NACK here means the sensor is NOT in periodic/ART mode (no data).")
    for i in range(3):
        show(f"fetch #{i + 1} (clean i2c_rdwr)", lambda: fetch(bus))
        time.sleep(0.3)

    if "--recover" not in sys.argv:
        print("\nRun again with --recover to test break -> soft reset -> ART.")
        sys.exit(0)

    print("\n== Recovery sequence ==")
    show(
        "general-call reset 0x00/0x06", lambda: bus.i2c_rdwr(i2c_msg.write(0x00, [0x06])) or "sent"
    )
    time.sleep(0.01)
    show("break 0x3093", lambda: cmd(bus, BREAK) or "sent")
    time.sleep(0.01)
    show("soft reset 0x30A2", lambda: cmd(bus, SOFT_RESET) or "sent")
    time.sleep(0.01)
    show("status 0xF32D", lambda: status(bus))
    show("clear status 0x3041", lambda: cmd(bus, CLEAR_STATUS) or "sent")
    time.sleep(0.01)
    show("start ART 0x2B32", lambda: cmd(bus, ART) or "sent")
    time.sleep(0.5)
    for i in range(3):
        show(f"fetch #{i + 1} after ART", lambda: fetch(bus))
        time.sleep(0.3)
