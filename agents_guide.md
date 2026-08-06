# Fridge-App — Agent Quick Reference

Compact guide for AI coding agents. Full guidelines are in [agents.md](agents.md).
Project overview and usage are in [README.md](README.md).
Architecture and hardware details are in [IMPLEMENTATION.md](IMPLEMENTATION.md).

---

## Project at a glance

| Item | Value |
|------|-------|
| Language | Python 3.13+ |
| Package manager | `uv` |
| Entry point | `uv run fridge <command>` |
| Target hardware | Raspberry Pi 4B, custom I2C interface board |
| Remote path | `/home/<user>/fridge` |
| Data / logs / cache | `/home/<user>/fridge_data/` |
| Test runner | `pytest` (run **on the Pi**, not locally) |
| Linter / formatter | `ruff` |

---

## Key source files

| File | Purpose |
|------|---------|
| `src/fridge/thermostat.py` | Temperature regulation loop, `CoolerController`, `Thermostat` |
| `src/fridge/enums.py` | `ModeState`, `ActionState`, `EnergyState` |
| `src/fridge/energy_controller.py` | Background thread — reads cached plan, calls `set_energy_state` |
| `src/fridge/energy_plan.py` | Price classification and pre-cooling look-ahead |
| `src/fridge/tibber_client.py` | Tibber GraphQL API client with exponential backoff |
| `src/fridge/mqtt.py` | MQTT publish/subscribe bridge |
| `src/fridge/hourly_stats.py` | Per-hour duty-cycle and climate aggregator |
| `src/fridge/measurement_logger.py` | Periodic SQLite measurement snapshots |
| `src/fridge/local_control.py` | Unix-socket server for `fridge mode` CLI command |
| `src/fridge/cli.py` | Click CLI entry point |
| `src/fridge/i2c.py` | SHT30 sensor and MCP23017 GPIO drivers |
| `config_default.ini` | Default configuration (copied to `~/fridge_data/fridge.ini`) |

---

## Workflow for every task

1. **Plan** — write a plan; get approval before coding.
2. **Tests first** — write failing tests before implementation (TDD).
3. **Implement** — minimal change, one concern at a time.
4. **Deploy & test** — copy changed files to the Pi, run `pytest` there.
5. **Lint** — `uv run ruff check src/ tests/ && uv run ruff format src/ tests/`
6. **Docs** — update README / IMPLEMENTATION.md if behaviour changes.

---

## SSH access

Ask the developer for the SSH connection string — **do not store it in the repo**.

Only copy files that changed:
```bash
scp -i ~/.ssh/<key> src/fridge/thermostat.py <user>@fridge.local:/home/<user>/fridge/src/fridge/
```

Run tests on the Pi:
```bash
ssh -i ~/.ssh/<key> <user>@fridge.local \
  'cd ~/fridge && $HOME/.local/bin/uv run pytest tests/ --ignore=tests/test_thermostat_hw.py -q'
```

---

## Coding rules (summary)

- Type hints on every function (Python 3.10+ union syntax `X | Y`).
- reStructuredText docstrings on non-obvious functions.
- Prefer stdlib → well-known third-party → custom. Ask before adding dependencies.
- Raise specific exceptions; never bare `except Exception`.
- Do not change code unrelated to the current task.
- Do not commit — the developer handles git.

---

## Testing rules

- Always write tests **before** implementation.
- Run tests on the Pi, not locally.
- Never skip, comment out or weaken failing tests to make them pass.
- Use `pytest-mock` / `freezegun` for mocking; `pytest-asyncio` for async tests.

---

## State machines (quick reference)

```
ModeState:   IDLE | COOL_DOWN | COOL | SPECIAL
ActionState: IDLE | COOLING | HEATING
EnergyState: LOW_PRICE | NORMAL_PRICE | HIGH_PRICE

CoolerState: OFF → SOFT_START → RUNNING ⇄ PAUSED
                              ↘ SPIN_DOWN → OFF
```

State is persisted via `diskcache` in `~/fridge_data/cache/` and restored on restart.

---

## Notable module: `hourly_stats.py`

`HourlyAggregator` samples the thermostat once per second, accumulates cooler /
compressor duty cycles and climate aggregates per wall-clock UTC hour, finalises each
closed hour into the `hourly_stats` DB table and the retained MQTT `<prefix>/hourly`
topic. State is persisted to diskcache so a mid-hour restart resumes accumulation.
