#!/usr/bin/env python3
"""Reset WaterScope entity IDs to the ones Home Assistant would generate today.

Installs from before 0.4.0 named the meter device after its meter number, so
entity IDs look like ``sensor.3750055_water_used_today``. Renaming the device
does not always carry the entity IDs with it. This asks Home Assistant itself
for the automatic ID of every WaterScope entity (the same call behind the UI's
"Recreate entity IDs") and renames any that differ. History and statistics
follow the entity through a rename.

Usage:
    HA_URL=http://homeassistant.local:8123 HA_TOKEN=<long-lived token> \\
        python scripts/regenerate_entity_ids.py            # show the plan
    ... python scripts/regenerate_entity_ids.py --apply    # make the changes

Create the token under your profile → Security → Long-lived access tokens.
Automations or dashboards that reference the old IDs must be updated by hand.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import sys
from typing import Any

import aiohttp

PLATFORM = "waterscope"


class HomeAssistant:
    """Minimal websocket client for the entity registry commands."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._ids = itertools.count(1)

    async def call(self, **message: Any) -> Any:
        msg_id = next(self._ids)
        await self._ws.send_json({"id": msg_id, **message})
        while True:
            reply = await self._ws.receive_json()
            if reply.get("id") != msg_id:
                continue
            if not reply.get("success"):
                raise RuntimeError(f"{message['type']} failed: {reply.get('error')}")
            return reply.get("result")


async def main(apply: bool) -> int:
    url = os.environ.get("HA_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN", "")
    if not url or not token:
        print("Set HA_URL and HA_TOKEN first (see the docstring).", file=sys.stderr)
        return 2

    ws_url = url.replace("https://", "wss://").replace("http://", "ws://")
    async with aiohttp.ClientSession() as session, session.ws_connect(
        f"{ws_url}/api/websocket"
    ) as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json()).get("type") != "auth_ok":
            print("Authentication failed — check HA_TOKEN.", file=sys.stderr)
            return 1

        ha = HomeAssistant(ws)
        entities = await ha.call(type="config/entity_registry/list")
        current = sorted(
            e["entity_id"] for e in entities if e.get("platform") == PLATFORM
        )
        if not current:
            print("No WaterScope entities found.")
            return 1

        automatic: dict[str, str | None] = await ha.call(
            type="config/entity_registry/get_automatic_entity_ids",
            entity_ids=current,
        )
        plan = {
            old: new for old, new in automatic.items() if new and new != old
        }

        print(f"{len(current)} WaterScope entities, {len(plan)} to rename.\n")
        for old, new in plan.items():
            print(f"  {old}\n    -> {new}")
        if not plan:
            print("Nothing to do — every ID already matches.")
            return 0
        if not apply:
            print("\nDry run. Re-run with --apply to rename.")
            return 0

        for old, new in plan.items():
            await ha.call(
                type="config/entity_registry/update",
                entity_id=old,
                new_entity_id=new,
            )
        print(f"\nRenamed {len(plan)} entities.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv[1:])))
