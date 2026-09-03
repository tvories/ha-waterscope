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

```mermaid
flowchart LR
    subgraph host["Your machine"]
        repo["repo<br/>custom_components/waterscope"]
        venv[".venv<br/>ruff · mypy · pytest"]
        cfg["~/.local/share/ha-waterscope-dev/hacfg<br/>HA config — holds your password"]
    end

    subgraph docker["Docker container: ha-waterscope-test"]
        ha["Home Assistant :stable"]
    end

    repo -->|"bind mount, read-only"| ha
    cfg <-->|"bind mount, read-write"| ha
    venv -.->|"scripts/dev check"| repo
    ha -->|"port 8123"| browser["localhost:8123"]

    classDef secret stroke-dasharray: 4 3
    class cfg secret
```

The dashed box is the reason the config directory lives outside the repo: Home Assistant
writes your WaterScope password into it in plain text.

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

## How it fits together

### One poll

The coordinator runs every 15 minutes and drives everything from a single pass over the
account's meters.

```mermaid
flowchart TD
    poll["WaterscopeCoordinator<br/>every 15 minutes"]
    meters["meter/getlistwithclusters"]
    poll --> meters
    meters --> each{"for each meter"}

    each --> hist["consumptionHistory/get<br/>1-minute readings"]
    each --> week["getConsumptionForWeekWithBudget<br/>leak · leak rate · daily low temp"]
    each --> cycle["getBudgetCycleMonthlyDetails<br/>budget · indoor/irrigation/leak split"]

    hist --> snap["MeterData snapshot"]
    week --> snap
    cycle --> snap
    snap --> ents["sensors + binary sensors"]

    hist --> stats["hourly sums →<br/>async_add_external_statistics"]
    stats --> energy["Energy dashboard"]

    meters --> stale["detach devices for<br/>meters no longer present"]
```

The two secondary routes are best-effort: if either fails the poll still succeeds, and only
the entities fed by that route go unavailable. A failure of the consumption route fails the
whole update, because that is the point of the integration.

### Importing statistics without corrupting them

This is the subtle part, and where the integration is easiest to get wrong. External
statistics carry a **cumulative** `sum`, so a batch must continue from whatever total
preceded it — never restart at zero — and must not rewrite hours that already exist.

```mermaid
sequenceDiagram
    participant C as Coordinator
    participant R as Recorder
    participant A as WaterScope API

    C->>R: get_last_statistics(statistic_id)

    alt no history yet
        R-->>C: nothing
        Note over C: start = today − 90 days<br/>running sum = 0<br/>last bucket = none
    else series already exists
        R-->>C: newest recorded hour
        C->>R: statistics_during_period(that hour)
        R-->>C: cumulative sum at that hour
        Note over C: start = that local date<br/>running sum = recorded sum<br/>last bucket = that hour
    end

    C->>A: consumptionHistory/get(start … today)
    A-->>C: 1-minute readings

    Note over C: drop isDataMissing intervals<br/>clamp negatives to zero<br/>bucket into local hours

    Note over C: skip every bucket at or before<br/>the last recorded hour

    C->>R: async_add_external_statistics(only newer hours)
```

Two failure modes this shape rules out:

- **Restarting the sum.** Each poll re-fetches a window overlapping hours already stored.
  Accumulating those from zero would leave a cliff where the rewritten hours meet the older
  ones — the sum is cumulative across the entire series, not per batch.
- **Never backfilling.** The 90-day import runs as a background task, and polls do not
  touch statistics until it has finished. Otherwise the first poll would write today's
  hours, and the import would then conclude the series was already up to date.

### Token lifecycle

One password grant, then refresh tokens, with the stored password only as a fallback.

```mermaid
stateDiagram-v2
    [*] --> NoToken
    NoToken --> Valid: grant_type=password
    Valid --> Valid: data request succeeds
    Valid --> Refreshing: within 120s of expiry
    Refreshing --> Valid: grant_type=refresh_token
    Refreshing --> NoToken: refresh rejected — fall back to password
    Valid --> NoToken: data route returns 401
    NoToken --> [*]: credentials rejected — reauth flow
```

Access tokens last about 30 minutes and refresh tokens about 14 days. The 120-second margin
exists so a token cannot lapse between the check and the request landing.

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
