"""Async client for the Metron WaterScope mobile API.

Consumer accounts are denied on the documented ``webapi.waterscope.us/api/*``
surface, so this talks to the endpoints the WaterScope 2.0 mobile app uses:

* ``POST /consumertoken`` (form-urlencoded) is the app's own OAuth-style token
  issuer. ``grant_type=password`` exchanges email+password for a bearer
  ``access_token`` (~30 min) plus a ``refresh_token`` (~14 days);
  ``grant_type=refresh_token`` renews without the password.
* ``POST /waterscope/mobile/*`` data routes accept that bearer token and return
  clean JSON — 1-minute interval consumption, ISO local timestamps, an explicit
  ``isDataMissing`` flag.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

import aiohttp

from .const import (
    API_BASE,
    BUDGET_CYCLE_PATH,
    CONSUMPTION_PATH,
    METER_LIST_PATH,
    REQUEST_CHUNK_DAYS,
    TOKEN_EXPIRY_MARGIN,
    TOKEN_PATH,
    USER_AGENT,
    WEEK_BUDGET_PATH,
)

_LOGGER = logging.getLogger(__name__)


class WaterscopeError(Exception):
    """Base error."""


class WaterscopeAuthError(WaterscopeError):
    """Credentials were rejected."""


class WaterscopeConnectionError(WaterscopeError):
    """Transport or unexpected-response failure."""


@dataclass(slots=True)
class Meter:
    """A meter discovered on the account."""

    meter_id: str
    nickname: str
    model: str
    account_id: str
    is_residential: bool


@dataclass(slots=True)
class Reading:
    """One logging interval."""

    timestamp: datetime
    gallons: float
    flow_rate: float
    missing: bool = False


@dataclass(slots=True)
class MeterStatus:
    """Leak and temperature state reported for the most recent covered day."""

    leak: bool
    leak_rate: float
    low_temperature: bool
    temperature: float | None
    budget_status: str | None
    budget_percentage: float | None


@dataclass(slots=True)
class BudgetCycle:
    """Usage against the utility's budget for the current billing cycle."""

    total: float
    indoor: float
    irrigation: float
    leak: float
    budget: float
    budget_percentage: float
    daily_target: float
    daily_average: float
    day_of_cycle: int
    status: str | None
    cycle_start: datetime | None
    cycle_end: datetime | None


@dataclass(slots=True)
class _Token:
    access: str
    refresh: str
    # time.monotonic() value after which the access token must be renewed
    expires_at: float


def _parse_iso_local(raw: str, tz: tzinfo) -> datetime:
    """Parse ``2026-08-09T00:00:00`` (naive local wall-clock) as an aware dt.

    The API encodes local wall-clock time with no offset; a single local day
    returns 00:00:00 through 23:59:00. Attaching the account's tz is correct;
    treating it as UTC would shift every point by the local offset.
    """
    return datetime.fromisoformat(raw).replace(tzinfo=tz)


class WaterscopeClient:
    """Talks to the WaterScope mobile API on a private token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        timezone: tzinfo,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._tz = timezone
        self._token: _Token | None = None
        self._auth_lock = asyncio.Lock()
        self.account_id: str | None = None

    # ── token management ────────────────────────────────────────────────────

    async def async_login(self) -> None:
        """Obtain a token with the password grant. Also validates credentials."""
        async with self._auth_lock:
            await self._password_grant()

    async def _password_grant(self) -> None:
        data = {
            "grant_type": "password",
            "username": self._email,
            "password": self._password,
        }
        payload = await self._token_request(data)
        self._store_token(payload)

    async def _refresh_grant(self) -> None:
        assert self._token is not None
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._token.refresh,
        }
        payload = await self._token_request(data)
        self._store_token(payload)

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        try:
            async with self._session.post(
                API_BASE + TOKEN_PATH,
                data=data,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ) as resp:
                body = await resp.json(content_type=None)
                status = resp.status
        except aiohttp.ClientError as err:
            raise WaterscopeConnectionError(f"token request failed: {err}") from err
        except ValueError as err:
            raise WaterscopeConnectionError("token response was not JSON") from err

        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            return body

        # invalid_user / invalid_grant / bad password all land here.
        error = (body or {}).get("error") if isinstance(body, dict) else None
        desc = (body or {}).get("error_description") if isinstance(body, dict) else None
        if error in ("invalid_user", "invalid_grant") or status in (400, 401):
            raise WaterscopeAuthError(desc or error or f"HTTP {status}")
        raise WaterscopeConnectionError(f"unexpected token response (HTTP {status})")

    def _store_token(self, payload: dict[str, Any]) -> None:
        expires_in = float(payload.get("expires_in") or 1799)
        # Renew early: a token that expires mid-flight costs a failed request
        # and a retry, and the server's clock is not ours.
        lifetime = max(expires_in - TOKEN_EXPIRY_MARGIN.total_seconds(), 60.0)
        self._token = _Token(
            access=payload["access_token"],
            refresh=payload.get("refresh_token") or "",
            expires_at=time.monotonic() + lifetime,
        )
        if payload.get("accountId"):
            self.account_id = str(payload["accountId"])

    async def _ensure_token(self) -> None:
        """Guarantee a currently-valid access token, refreshing as needed."""
        async with self._auth_lock:
            if self._token is None:
                await self._password_grant()
                return
            if time.monotonic() < self._token.expires_at:
                return
            try:
                await self._refresh_grant()
            except WaterscopeError:
                # Refresh token expired or rejected — fall back to the password.
                _LOGGER.debug("Refresh failed; re-authenticating with password")
                await self._password_grant()

    # ── authenticated data calls ────────────────────────────────────────────

    async def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        """POST to a data route, transparently refreshing on a 401."""
        await self._ensure_token()
        for attempt in (1, 2):
            assert self._token is not None
            try:
                async with self._session.post(
                    API_BASE + path,
                    json=json_body,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Authorization": f"Bearer {self._token.access}",
                    },
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        # Token rejected mid-flight; force one renewal and retry.
                        async with self._auth_lock:
                            self._token = None
                        await self._ensure_token()
                        continue
                    if resp.status != 200:
                        raise WaterscopeConnectionError(
                            f"{path} returned HTTP {resp.status}"
                        )
                    body = await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                raise WaterscopeConnectionError(f"{path} failed: {err}") from err

            if not isinstance(body, dict):
                raise WaterscopeConnectionError(f"{path}: unexpected payload")
            if body.get("responseCode") not in (0, None):
                raise WaterscopeConnectionError(
                    f"{path}: {body.get('message', 'error')}"
                )
            return body
        raise WaterscopeConnectionError(f"{path}: authentication kept failing")

    async def async_get_meters(self) -> list[Meter]:
        """Return the account's meters (non-clustered)."""
        body = await self._post(METER_LIST_PATH, {})
        meters: list[Meter] = []
        for m in body.get("nonClusteredMetersInfo") or []:
            meter_id = str(m.get("meterId") or "")
            if not meter_id:
                continue
            meters.append(
                Meter(
                    meter_id=meter_id,
                    nickname=str(m.get("meterNickName") or meter_id),
                    model=str(m.get("meterModel") or ""),
                    account_id=str(m.get("accountId") or self.account_id or ""),
                    is_residential=bool(m.get("isResidential")),
                )
            )
        return meters

    async def async_get_consumption(
        self, meter_id: str, start: date, end: date
    ) -> list[Reading]:
        """Fetch 1-minute readings for an inclusive local date range.

        Splits into request-sized chunks to stay on the 1-minute-resolution side
        of the server's long-range downsampling.
        """
        readings: list[Reading] = []
        for chunk_start, chunk_end in _day_chunks(start, end):
            body = await self._post(
                CONSUMPTION_PATH,
                {
                    "MeterId": meter_id,
                    "StartDate": chunk_start.isoformat(),
                    "EndDate": chunk_end.isoformat(),
                },
            )
            for row in body.get("consumptionFlowrateData") or []:
                readings.append(self._to_reading(row))
        return readings

    async def async_get_meter_status(self, meter_id: str) -> MeterStatus | None:
        """Return leak/temperature state from the most recent covered day.

        The route returns roughly a week of days; the newest one that actually
        carries consumption is the closest thing to "current" the service has,
        given readings arrive about a day late.
        """
        body = await self._post(WEEK_BUDGET_PATH, {"MeterId": meter_id})
        details = body.get("details") or []
        if not isinstance(details, list) or not details:
            return None

        def _sort_key(row: dict[str, Any]) -> str:
            return str(row.get("date") or "")

        usable = [
            row
            for row in details
            if isinstance(row, dict) and row.get("isConsumptionAvailable")
        ]
        latest = max(usable or [r for r in details if isinstance(r, dict)],
                     key=_sort_key, default=None)
        if latest is None:
            return None

        status = latest.get("meterStatusInfo") or {}
        budget = latest.get("waterBudgetStatusInfo") or {}
        return MeterStatus(
            leak=bool(status.get("isLeakExists")),
            leak_rate=_as_float(status.get("leakRatePerHour")),
            low_temperature=bool(status.get("isLowTemperatureExists")),
            temperature=_as_optional_float(status.get("lowTemperature")),
            budget_status=budget.get("message") or None,
            budget_percentage=_as_optional_float(budget.get("percentageUse")),
        )

    async def async_get_budget_cycle(self, meter_id: str) -> BudgetCycle | None:
        """Return usage against the utility's budget for the current cycle."""
        body = await self._post(BUDGET_CYCLE_PATH, {"MeterId": meter_id})
        usage = body.get("usageData")
        stats = body.get("statistics")
        if not isinstance(usage, dict) or not isinstance(stats, dict):
            return None

        status = body.get("status")
        return BudgetCycle(
            total=_as_float(usage.get("totalConsumption")),
            indoor=_as_float(usage.get("indoorConsumption")),
            irrigation=_as_float(usage.get("irrigationConsumption")),
            leak=_as_float(usage.get("leakConsumption")),
            budget=_as_float(stats.get("cycleBudget")),
            budget_percentage=_as_float(usage.get("budgetPercentageUse")),
            daily_target=_as_float(stats.get("dailyTarget")),
            daily_average=_as_float(stats.get("cycleDailyAverage")),
            day_of_cycle=int(_as_float(stats.get("dayOfTheCycle"))),
            status=(status or {}).get("message") if isinstance(status, dict) else None,
            cycle_start=_parse_utc(stats.get("cycleStartDate")),
            cycle_end=_parse_utc(stats.get("cycleEndDate")),
        )

    def _to_reading(self, row: dict[str, Any]) -> Reading:
        return Reading(
            timestamp=_parse_iso_local(row["date"], self._tz),
            # The register emits occasional tiny negative corrections; clamp so
            # cumulative sums stay monotonic for total_increasing / statistics.
            gallons=max(0.0, float(row.get("consumption") or 0.0)),
            flow_rate=max(0.0, float(row.get("flowrate") or 0.0)),
            missing=bool(row.get("isDataMissing")),
        )


def _as_float(value: Any) -> float:
    """Coerce an API number to a float, treating anything unusable as zero."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_optional_float(value: Any) -> float | None:
    """Coerce an API number to a float, keeping "absent" distinct from zero."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_utc(raw: Any) -> datetime | None:
    """Parse the ``2026-07-15T00:00:00Z`` stamps these routes use.

    Unlike the consumption route's naive wall-clock, these carry an offset.
    The placeholder ``0001-01-01`` means "no value".
    """
    if not isinstance(raw, str) or raw.startswith("0001-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive range into request-sized windows."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=REQUEST_CHUNK_DAYS - 1), end)
        chunks.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return chunks
