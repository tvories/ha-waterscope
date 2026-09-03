# Metron WaterScope for Home Assistant

Home Assistant integration for [Metron](https://metron-us.com/) smart water meters via the
WaterScope 2.0 mobile API. Brings **1-minute resolution** water usage into the Energy
dashboard, with up to 90 days of history backfilled on first setup.

## Features

- Water usage in the **Energy dashboard** (long-term statistics, hourly buckets)
- Up to 90 days of history backfilled on first run
- **Leak and low-temperature** binary sensors, plus leak rate and meter minimum temperature
- **Budget tracking** — cycle usage against the utility's budget, split into indoor,
  irrigation and leak
- Today / yesterday usage and flow rate
- Automatic meter discovery — every meter on the account becomes a device
- No API key required — signs in with your normal WaterScope credentials

## Supported devices

Any water meter visible in the **WaterScope 2.0** consumer app
(`com.waterscope.mobile`) on a residential account. Development and testing were done
against a Metron Prism cellular register (Spectrum PD) on a residential account with a
municipal water utility.

Meters are discovered from the account automatically; there are no IDs to look up. Each
meter becomes its own device, and meters added to or removed from the account are picked
up on the next poll.

Not supported: utility or field-team accounts (`com.waterscope.utilitymobile`), and the
legacy `com.waterscope.app` client.

## Installation

**HACS** → Custom repositories → add `https://github.com/tvories/ha-waterscope`
(type: Integration) → install → restart Home Assistant.

**Manual** — copy `custom_components/waterscope` into your `config/custom_components/`
and restart Home Assistant.

Then **Settings → Devices & Services → Add Integration → Metron WaterScope**.

### Configuration

| Field | Description |
|---|---|
| **Email** | The email address you sign in to the WaterScope app with. |
| **Password** | Your WaterScope password. It is stored in the config entry so the integration can renew its access token without prompting you. |

One config entry covers the whole account, so you only enter your credentials once no
matter how many meters you have.

If your password changes, Home Assistant raises a reauthentication prompt. To change the
email address as well, use **Reconfigure** on the integration entry.

### Removing the integration

Delete the entry from **Settings → Devices & Services**. This removes the devices and
entities. Statistics already written to the recorder are kept — Home Assistant retains
long-term statistics independently. To remove those too, use **Developer Tools →
Statistics** and delete the `waterscope:water_*` entries.

## What you get

Per meter:

| Entity | Description |
|---|---|
| **Water used today** | Gallons since local midnight. `total_increasing`, so it feeds the Energy dashboard. Reads 0 until the day's data arrives — see [data freshness](#data-freshness-read-this-first). |
| **Water used yesterday** | Yesterday's total, for day-over-day comparison. |
| **Flow rate** | Gallons per minute at the most recent interval that recorded usage. |
| **Last reading** | Timestamp of that interval — i.e. how far the data actually reaches. Diagnostic. |
| **Leak** | Binary sensor. On when the meter reports an active leak. |
| **Low temperature** | Binary sensor. On when the meter reports a low-temperature condition. |
| **Leak rate** | Leak flow, converted from the service's gallons-per-hour to gal/min. |
| **Minimum temperature** | The meter's *daily low* in °F — one value per day, not a live reading. Diagnostic. |
| **Cycle usage** | Total usage in the utility's current billing cycle. Carries `cycle_start`, `cycle_end` and `day_of_cycle` attributes — see [the note below](#cycle-usage-wont-match-the-web-dashboard). |
| **Indoor usage** / **Irrigation usage** / **Leak usage** | The service's split of cycle usage by category. |
| **Cycle budget** | The utility's budget for the cycle. Diagnostic. |
| **Budget used** | Percentage of the cycle budget consumed. |
| **Cycle daily average** | Average daily use so far this cycle. |
| **Daily target** | Daily allowance implied by the budget. Diagnostic. |
| **Budget status** | The utility's rate-tier label, e.g. "Tier 3 Rates". Diagnostic. |

Plus an external statistics series per meter (`waterscope:water_<meter id>`) holding
hourly totals — this is what the Energy dashboard and the 90-day backfill use.

### Adding it to the Energy dashboard

**Settings → Dashboards → Energy → Water consumption → Add water source**, then pick the
meter's statistic. Backfilled history appears immediately, so the dashboard is useful the
day you install it rather than a month later.

### Example automations

Alert when the meter itself reports a leak:

```yaml
automation:
  - alias: "Water leak detected"
    triggers:
      - trigger: state
        entity_id: binary_sensor.spectrum_pd_leak
        to: "on"
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            WaterScope reports a leak at
            {{ states('sensor.spectrum_pd_leak_rate') }} gal/min.
```

Warn before blowing the utility's budget:

```yaml
automation:
  - alias: "Water budget nearly spent"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.spectrum_pd_budget_used
        above: 90
    actions:
      - action: notify.persistent_notification
        data:
          message: >-
            {{ states('sensor.spectrum_pd_budget_used') }}% of the water budget
            used ({{ states('sensor.spectrum_pd_budget_status') }}).
```

Catch a freeze risk at the meter:

```yaml
automation:
  - alias: "Meter temperature low"
    triggers:
      - trigger: state
        entity_id: binary_sensor.spectrum_pd_low_temperature
        to: "on"
    actions:
      - action: notify.persistent_notification
        data:
          message: "Meter low temperature reported ({{
            states('sensor.spectrum_pd_minimum_temperature') }}°F daily low)."
```

### Cycle usage may briefly disagree with the web dashboard

The billing cycle is set by the utility, and the API's idea of it **lags the web dashboard
by a day or two when a cycle rolls over**. During that window the budget routes still
report the outgoing cycle, so cycle usage can differ from the dashboard by a large margin —
observed once as 10,022 gal (the closing cycle) against 4,610 gal (the new one). It
resolves itself once the API catches up.

Because of that, the cycle window is surfaced explicitly rather than left implicit:

- **Cycle start**, **Cycle end** and **Day of cycle** sensors (diagnostic).
- The same three values as `cycle_start` / `cycle_end` / `day_of_cycle` attributes on every
  cycle figure.

If a number looks wrong, check those first — it is almost always a different window rather
than a different total. For a fixed month-to-date figure regardless of the utility's cycle,
build one from the statistics series with a
[utility meter](https://www.home-assistant.io/integrations/utility_meter/) helper.

## Data freshness (read this first)

**Your meter's readings arrive in a daily batch, roughly a day behind.** This is the
service's behaviour, not a limitation of this integration — verified by querying both the
mobile API and the older web portal for the same day and getting identical results.

In practice, asking for the current day returns a full 1440-minute day where every minute
is flagged `isDataMissing`, while the previous day is complete. So:

- **"Water used today" reads 0** for most of the day, then fills in once the batch lands.
  That is accurate, not broken.
- **Flow rate** and **last reading** fall back to the newest interval that actually has
  data, rather than being pinned at zero. "Last reading" is the honest indicator of how
  current your data is.
- The **statistics series** — what the Energy dashboard uses — is unaffected; it is built
  from completed hours and simply trails by about a day.
- Polling faster than the batch cadence gains nothing.

## How data is updated

The integration polls the cloud API every **15 minutes** (`cloud_polling`). Each poll
fetches the current interval data for every meter, updates the sensors, and appends any
newly completed hours to the statistics series.

On first setup a background task imports up to **90 days** of history. It runs in the
background because that download is several megabytes and would otherwise delay Home
Assistant's startup. Until it finishes, the sensors work but statistics are not yet
extended.

Access tokens last about 30 minutes and are renewed automatically with a refresh token
that lasts about 14 days; your stored password is only used if that refresh token expires.

## How it works

Metron publishes a [WebAPI](https://webapi.waterscope.us/Help), but consumer accounts are
not authorized for it. This integration instead uses the same endpoints the WaterScope 2.0
mobile app uses:

1. **`POST /consumertoken`** — the app's token issuer. `grant_type=password` exchanges your
   email + password for a bearer token plus a refresh token;
   `grant_type=refresh_token` renews it without the password.
2. **`POST /waterscope/mobile/meter/getlistwithclusters`** — the meters on the account.
3. **`POST /waterscope/mobile/consumptionHistory/get`** — one record per minute for a date
   range.
4. **`POST /waterscope/mobile/dashboard/getConsumptionForWeekWithBudget`** — leak state,
   leak rate, meter temperature and budget status for recent days.
5. **`POST /waterscope/mobile/usageOverview/getBudgetCycleMonthlyDetails`** — budget cycle
   totals and the indoor/irrigation/leak split.

### Notes on the data

Verified against the live service:

| Behaviour | Detail |
|---|---|
| **Resolution** | 1 record/minute. |
| **Downsampling** | Requests over ~90 days silently drop to daily records. The client chunks requests at 31 days to stay at 1-minute and keep responses a sane size. |
| **Timestamps** | ISO local wall-clock (`2026-08-09T00:00:00`). Parsed as local time and made timezone-aware. |
| **Negative values** | The register emits occasional tiny negative corrections. Clamped to zero to keep sums monotonic. |
| **`isDataMissing`** | Intervals flagged missing are excluded from statistics rather than counted as zero. |

### `consumption` is incremental, not a register read

Each record is gallons used *in that minute*, not a cumulative meter reading. The
cumulative total the Energy dashboard needs is built by rolling minutes into hourly sums
and feeding them to the recorder as external statistics — the same approach the built-in
Opower integration uses. That is what makes backfill work.

## Known limitations

- **Usage is a day behind.** See [data freshness](#data-freshness-read-this-first).
- **No per-fixture breakdown.** Cycle usage is split into indoor / irrigation / leak, but
  the finer fixture attribution (toilet, shower, washing machine…) that
  `residential/getAnalytics` and `timeline/get` expose is not yet surfaced as entities.
- **Notification history is not exposed.** Leak and low-temperature *state* are surfaced
  as binary sensors, but the notification feed routes were mapped against an account with
  zero notifications, so the shape of an individual notification is still unknown.
- **Backfill is capped at 90 days**, because the API silently drops to daily resolution
  beyond roughly that window.
- **Usage lags real time.** Readings arrive in batches, so the newest interval is
  typically some minutes old. Polling faster would not help.
- **Undocumented API.** These endpoints are not published by Metron and can change without
  notice.
- **Multi-meter accounts are untested.** The code handles them, and they are covered by
  tests, but development was done on a single-meter account.

## Troubleshooting

**"Invalid email or password" when adding the integration.**
Confirm the credentials work in the WaterScope 2.0 mobile app. The older web portal at
waterscope.us can use a different sign-in path; the app is the reference.

**"No meters were found on this account."**
The account authenticated but has no meters attached to it. This usually means a utility
or field account rather than a consumer one.

**Statistics are missing or the Energy dashboard is empty.**
The backfill runs in the background after setup and can take a minute or two. Check the
log for `Wrote N hourly statistics rows`. The statistic is
`waterscope:water_<meter id>`, visible under **Developer Tools → Statistics**.

**The entry keeps reloading, or entities go unavailable.**
Enable debug logging and check for token errors:

```yaml
logger:
  default: warning
  logs:
    custom_components.waterscope: debug
```

**Reporting a problem.** Download diagnostics from the integration entry
(**⋮ → Download diagnostics**) and attach them to the issue. Credentials, account id and
meter id are redacted automatically.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for how to run the integration in a throwaway Home
Assistant container and how to run the linter, type checker, and test suite.

## Credits

Endpoint discovery began from [marizmendi/python-waterscope](https://github.com/marizmendi/python-waterscope),
which first documented a WaterScope consumption endpoint.

## Disclaimer

Not affiliated with or endorsed by Metron. It depends on undocumented mobile-app endpoints,
which can change without notice.
