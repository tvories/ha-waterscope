"""Constants for the WaterScope integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "waterscope"

CONF_ACCOUNT_ID: Final = "account_id"

# WaterScope 2.0 mobile API (reverse-engineered from com.waterscope.mobile).
API_BASE: Final = "https://webapi.waterscope.us"
TOKEN_PATH: Final = "/consumertoken"
METER_LIST_PATH: Final = "/waterscope/mobile/meter/getlistwithclusters"
CONSUMPTION_PATH: Final = "/waterscope/mobile/consumptionHistory/get"
# Leak state, meter temperature and budget status for the last ~week of days.
WEEK_BUDGET_PATH: Final = "/waterscope/mobile/dashboard/getConsumptionForWeekWithBudget"
# Budget cycle totals plus the indoor/irrigation/leak split.
BUDGET_CYCLE_PATH: Final = (
    "/waterscope/mobile/usageOverview/getBudgetCycleMonthlyDetails"
)

# The mobile client's own UA; harmless but keeps requests looking expected.
USER_AGENT: Final = "WaterScope/3.55.0 (Android)"

# Meter data trickles in in batches; polling faster just spends tokens.
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=15)

# Renew the access token this long before its stated expiry, so a request
# cannot be issued with a token that lapses in flight.
TOKEN_EXPIRY_MARGIN: Final = timedelta(seconds=120)

# How far back to reach on first setup.
BACKFILL_DAYS: Final = 90

# The API keeps 1-minute resolution up to ~90 days per request, then silently
# downsamples to daily. Chunk well under that both to stay at 1-minute and to
# keep any single response a sane size (~3.7 MB at 31 days; 90 days is ~11 MB).
REQUEST_CHUNK_DAYS: Final = 31

ATTRIBUTION: Final = "Data provided by Metron WaterScope"
