#!/usr/bin/env python3
"""Yasno Kyiv outages — CLI.

Reproduces the flow of https://static.yasno.ua/kyiv/outages without a browser:

1. Look up the street ("Вулиця") via the blackout-service API.
2. Look up the house ("Буд") on that street.
3. Resolve the house's outage group.
4. Fetch the planned-outages schedule and print the resulting
   group x 24h table to the terminal in ASCII.

With `--mqtt`, the current outage state of the house's group is also
published to Home Assistant over MQTT (auto-discovery + retained state).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

import requests

API_BASE = "https://app.yasno.ua/api/blackout-service/public/shutdowns"

REGION_NAME = "Київ"
STREET_QUERY = "ул. Драгоманова"
HOUSE_QUERY = "3А"

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

TYPE_MARKS = {"Definite": "##", "Possible": "..", "NotPlanned": "..", "Unknown": ".."}

# Home Assistant MQTT auto-discovery / state topics
HA_DISCOVERY_PREFIX = "homeassistant"  # homeassistant/<component>/<object_id>/config
STATE_PREFIX = "powergrid"           # yasnoutages/<object_id>/state|attrs


def now_local_minutes() -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    dt = datetime.now(ZoneInfo("Europe/Kiev"))
    return dt.hour * 60 + dt.minute


def fmt_hm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def current_slot(slots: list[dict], minutes: int) -> dict | None:
    """Return the slot active at `minutes` (0..1440), else None."""
    for s in slots:
        if int(s["start"]) <= minutes < int(s["end"]):
            return s
    return None


@dataclass
class OutageState:
    """Snapshot of the current power state for a house's group."""

    address: str
    group: str
    date: str
    status: str
    power: bool                    # True if electricity is on right now
    slot_type: str | None          # Definite / Possible / NotPlanned / None
    next_outage_start: int | None  # minutes since midnight, or None
    next_outage_end: int | None

    @classmethod
    def from_entry(cls, address: str, group: str, entry: dict) -> "OutageState":
        slots = sorted(entry.get("slots") or [], key=lambda s: int(s["start"]))
        minutes = now_local_minutes()
        slot = current_slot(slots, minutes)

        next_start = next_end = None
        for s in slots:
            if s["type"] in ("Definite", "Possible") and int(s["end"]) > minutes:
                next_start = int(s["start"])
                next_end = int(s["end"])
                break

        return cls(
            address=address,
            group=group,
            date=entry.get("date", ""),
            status=entry.get("status", ""),
            power=slot is None or slot["type"] not in ("Definite", "Possible"),
            slot_type=slot["type"] if slot else None,
            next_outage_start=next_start,
            next_outage_end=next_end,
        )


@dataclass
class Client:
    session: requests.Session

    def get(self, path: str, **params: Any) -> Any:
        resp = self.session.get(
            f"{API_BASE}{path}", params=params, timeout=30,
            headers={"User-Agent": "outages-cli/1.0"},
        )
        resp.raise_for_status()
        return resp.json()


def find_region(client: Client, name: str) -> dict:
    regions = client.get("/addresses/v2/regions")
    for region in regions:
        if region.get("value", "").lower() == name.lower():
            return region
    raise SystemExit(f"Region {name!r} not found")


def find_street(client: Client, region: dict, query: str) -> dict:
    region_id = region["id"]
    dso_id = region["dsos"][0]["id"]
    results = client.get(
        "/addresses/v2/streets",
        regionId=region_id, dsoId=dso_id, query=query,
    )
    if not results:
        raise SystemExit(f"Street {query!r} not found")
    exact = [r for r in results if r["value"].lower() == query.lower()]
    return (exact or results)[0]


def find_house(client: Client, region: dict, street: dict, query: str) -> dict:
    region_id = region["id"]
    dso_id = region["dsos"][0]["id"]
    results = client.get(
        "/addresses/v2/houses",
        regionId=region_id, streetId=street["id"],
        dsoId=dso_id, query=query,
    )
    if not results:
        raise SystemExit(f"House {query!r} not found on street {street['value']!r}")
    exact = [r for r in results if r["value"].lower() == query.lower()]
    return (exact or results)[0]


def find_group(client: Client, region: dict, street: dict, house: dict) -> str:
    dso_id = region["dsos"][0]["id"]
    info = client.get(
        "/addresses/v2/group",
        regionId=region["id"], streetId=street["id"],
        houseId=house["id"], dsoId=dso_id,
    )
    return f"{info['group']}.{info.get('subgroup', 0)}"


def fetch_schedule(client: Client, region: dict, group: str, day: str) -> dict:
    dso_id = region["dsos"][0]["id"]
    data = client.get(f"/regions/{region['id']}/dsos/{dso_id}/planned-outages")
    entry = data.get(group)
    if entry is None:
        raise SystemExit(f"Group {group} not present in schedule")
    return entry.get(day) or {}


def render_table(group: str, entry: dict) -> str:
    """Render the group's 24h slot schedule as a Markdown table."""
    slots = entry.get("slots") or []
    date = entry.get("date", "")
    status = entry.get("status", "")

    lines = [
        "## Kiev Power Grid outages",
        "",
        f"| Address | Group | Date | Status |",
        f"|---------|-------|------|--------|",
        f"| {STREET_QUERY}, буд. {HOUSE_QUERY} | {group} | {date} | {status} |",
        "",
    ]

    if not slots:
        lines.append(f"No schedule for group {group}: status={status}")
    else:
        # Build a 24-column timeline, one cell per hour.
        hour_marks = [None] * 24
        for s in slots:
            start_h, start_m = divmod(int(s["start"]), 60)
            end_h, end_m = divmod(int(s["end"]), 60)
            mark = TYPE_MARKS.get(s["type"], "..")
            for h in range(start_h, min(end_h + (1 if end_m else 0), 24)):
                hour_marks[h] = mark if hour_marks[h] is None else hour_marks[h]

        lines.append("### Timeline")
        lines.append("")
        lines.append("| " + " | ".join(f"{h:02d}" for h in range(24)) + " |")
        lines.append("|" + "|".join(["----"] * 24) + "|")
        lines.append("| " + " | ".join((hour_marks[h] or "  ") for h in range(24)) + " |")
        lines.append("")

        # Interval list for readability
        lines.append("### Schedule")
        lines.append("")
        lines.append("| Start |  End  |  Status  |")
        lines.append("|-------|-------|----------|")
        for s in slots:
            sh, sm = divmod(int(s["start"]), 60)
            eh, em = divmod(int(s["end"]), 60)
            label = {
                "Definite": "Grid oFF",
                "Possible": "Grid oFF (possible)",
                "NotPlanned": "Grid On ",
            }.get(s["type"], s["type"])
            lines.append(f"| {sh:02d}:{sm:02d} | {eh:02d}:{em:02d} | {label} |")
        lines.append("")
        lines.append("Legend: `##` = Grid oFF, `..` = Grid On / possible outage")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Home Assistant MQTT
# ---------------------------------------------------------------------------

def _object_id(name: str) -> str:
    return "powergrid_" + name.replace(" ", "_").replace("/", "_").lower()


def _device_info(state: OutageState) -> dict:
    return {
        "identifiers": [f"yasno-{state.group}"],
        "name": f"Yasno group {state.group}",
        "model": "Yasno blackout-service",
        "manufacturer": "ДТЕК КЕ",
        "sw_version": state.group,
    }


def publish_to_home_assistant(state: OutageState, host: str, port: int,
                              username: str | None, password: str | None) -> None:
    """Publish HA MQTT auto-discovery configs + retained state topics."""
    import paho.mqtt.client as mqtt_client

    power_oid = _object_id(f"power {state.group}")
    outage_oid = _object_id(f"next_outage {state.group}")

    attrs = {
        "address": state.address,
        "group": state.group,
        "date": state.date,
        "schedule_status": state.status,
        "current_slot": state.slot_type,
        "next_outage_start": fmt_hm(state.next_outage_start),
        "next_outage_end": fmt_hm(state.next_outage_end),
    }

    discovery = {
        f"{HA_DISCOVERY_PREFIX}/binary_sensor/{power_oid}/config": {
            "name": f"Yasno power {state.group}",
            "object_id": power_oid,
            "device_class": "power",
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_topic": f"{STATE_PREFIX}/{power_oid}/state",
            "json_attributes_topic": f"{STATE_PREFIX}/{power_oid}/attrs",
            "device": _device_info(state),
        },
        f"{HA_DISCOVERY_PREFIX}/sensor/{outage_oid}/config": {
            "name": f"Yasno next outage {state.group}",
            "object_id": outage_oid,
            "device_class": "duration",
            "state_class": "total_increasing",
            "unit_of_measurement": "min",
            "state_topic": f"{STATE_PREFIX}/{outage_oid}/state",
            "json_attributes_topic": f"{STATE_PREFIX}/{outage_oid}/attrs",
            "device": _device_info(state),
        },
    }

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2, client_id="yasno-outages-cli")
    if username:
        client.username_pw_set(username, password)
    client.connect(host, port, keepalive=60)
    client.loop_start()

    def pub(topic: str, payload: bytes | str, retain: bool = True):
        info = client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise SystemExit(f"MQTT publish failed for topic {topic!r} (rc={info.rc})")

    for topic, payload in discovery.items():
        pub(topic, json.dumps(payload, ensure_ascii=False))
    pub(f"{STATE_PREFIX}/{power_oid}/state", "ON" if state.power else "OFF")
    pub(f"{STATE_PREFIX}/{power_oid}/attrs", json.dumps(attrs, ensure_ascii=False))
    if state.next_outage_start is not None:
        minutes_left = max(0, state.next_outage_start - now_local_minutes())
        pub(f"{STATE_PREFIX}/{outage_oid}/state", str(minutes_left))
        pub(f"{STATE_PREFIX}/{outage_oid}/attrs", json.dumps(attrs, ensure_ascii=False))

    client.loop_stop()
    print(f"[mqtt] Home Assistant discovery + state published to {host}:{port}")
    print(f"[mqtt]   binary_sensor: {STATE_PREFIX}/{power_oid}/state = {'ON' if state.power else 'OFF'}")
    if state.next_outage_start is not None:
        print(f"[mqtt]   sensor:          {STATE_PREFIX}/{outage_oid}/state = "
              f"{max(0, state.next_outage_start - now_local_minutes())} min")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Yasno Kyiv outages CLI")
    parser.add_argument("--mqtt-host", help="MQTT broker host (enables HA publishing)")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT broker port (default 1883)")
    parser.add_argument("--mqtt-user", help="MQTT username")
    parser.add_argument("--mqtt-pass", help="MQTT password")
    parser.add_argument("--street", default=STREET_QUERY, help=f"street query (default {STREET_QUERY!r})")
    parser.add_argument("--house", default=HOUSE_QUERY, help=f"house query (default {HOUSE_QUERY!r})")
    parser.add_argument("--day", choices=["today", "tomorrow"], default="today",
                        help="which day of the schedule to show (default today)")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()

    global STREET_QUERY, HOUSE_QUERY
    STREET_QUERY = args.street
    HOUSE_QUERY = args.house

    client = Client(requests.Session())

    print(f"Searching street: {STREET_QUERY!r}")
    region = find_region(client, REGION_NAME)
    street = find_street(client, region, STREET_QUERY)
    print(f"  -> {street['value']} (id={street['id']})")

    print(f"Searching house: {HOUSE_QUERY!r}")
    house = find_house(client, region, street, HOUSE_QUERY)
    print(f"  -> {house['value']} (id={house['id']})")

    group = find_group(client, region, street, house)
    print(f"  -> outage group {group}")

    entry = fetch_schedule(client, region, group, day=args.day)
    print()
    print(render_table(group, entry))

    if args.mqtt_host:
        state = OutageState.from_entry(f"{street['value']} {house['value']}", group, entry)
        print()
        print(f"Current power state: {'ON' if state.power else 'OFF'} "
              f"(slot: {state.slot_type})")
        publish_to_home_assistant(state, args.mqtt_host, args.mqtt_port,
                                  args.mqtt_user, args.mqtt_pass)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        raise SystemExit(f"Request error: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"MQTT error: {exc}") from exc
