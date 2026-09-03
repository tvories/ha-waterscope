# Development

Everything goes through `scripts/dev`. Run it with no arguments for the full list.

```bash
scripts/dev setup     # one-time: create .venv and install the toolchain
scripts/dev check     # ruff + mypy + pytest — exactly what CI runs
scripts/dev up        # start a real Home Assistant with this integration mounted
```

Then open <http://localhost:8123>.

## Quick reference

| Command | What it does |
|---|---|
| `scripts/dev setup` | Creates `.venv` and installs ruff, mypy and the HA test harness. |
| `scripts/dev check` | Lint, type check and test. Run this before pushing. |
| `scripts/dev test [args]` | Just pytest — `scripts/dev test tests/test_api.py -k leak` works. |
| `scripts/dev lint` / `fmt` | `ruff check` / `ruff check --fix`. |
| `scripts/dev types` | mypy, using Home Assistant core's own settings. |
| `scripts/dev hassfest` | HA's manifest, icon and translation validation. |
| `scripts/dev up` / `down` / `restart` | Manage the live Home Assistant container. |
| `scripts/dev logs` | Follow the log, filtered to waterscope lines and errors. |
| `scripts/dev where` | Print the paths, port and timezone in use. |
| `scripts/dev adopt <dir>` | Import an existing HA config directory. |

## Live testing

`scripts/dev up` runs `ghcr.io/home-assistant/home-assistant:stable` with
`custom_components/waterscope` **bind-mounted read-only from this repo**. Edit code here,
run `scripts/dev restart`, and the change is live — no copying.

Add the integration through the UI once, and it persists across restarts.

Two things that matter:

- **Timezone.** The API returns naive local wall-clock timestamps, so the container's zone
  must match the meter's or every reading lands in the wrong hour. The script takes it from
  `/etc/timezone`; override with `WATERSCOPE_DEV_TZ`.
- **Where the config lives.** Home Assistant's config directory is deliberately kept
  **outside the repo**, at `~/.local/share/ha-waterscope-dev/hacfg`, because
  `.storage/core.config_entries` holds your WaterScope password in plain text and must
  never sit in a git working tree. Override with `WATERSCOPE_DEV_CONFIG`.

The container writes that directory as root, so some files under `.storage` end up
root-owned. Copying it around needs `sudo`.

## Requirements

- **Docker.** On WSL this needs Docker Desktop → Settings → Resources → WSL Integration
  switched on for this distro, otherwise `docker` is only an inert shim.
- **Python 3.13+**, which is what Home Assistant requires.

## Two gotchas worth knowing

**C extensions need a compiler that exists.** Linuxbrew's Python hardcodes `gcc-12` when
building C extensions, and that compiler is not installed here — without an override,
installing the toolchain fails while building the `lru-dict` wheel. `scripts/dev setup`
detects `gcc-13` and sets `CC` accordingly. Any manual `pip install` into `.venv` that
compiles C needs the same treatment.

**Test and runtime Home Assistant versions differ, by design.**
`pytest-homeassistant-custom-component` pins its own Home Assistant (2026.2.3 at the time
of writing) while the container runs `:stable` (2026.8.1). This was checked and is safe for
the APIs in use — `StatisticMetaData`, `StatisticMeanType` and `VolumeConverter.UNIT_CLASS`
are identical in both. Re-check it whenever the recorder statistics API is touched, since
that is where the two versions are most likely to diverge.

## Layout

```
custom_components/waterscope/   the integration
  api.py                        WaterScope mobile API client (no HA imports)
  coordinator.py                polling, statistics import, device lifecycle
  entity.py                     shared base: device info, availability
  sensor.py / binary_sensor.py  entity definitions
tests/                          pytest suite, >95% coverage required by CI
scripts/dev                     this tooling
```
