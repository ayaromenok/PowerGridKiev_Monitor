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

With `--calendar` (needs `--mqtt-host`), the whole day's outage intervals
are additionally published as an ICS event stream that Home Assistant can
turn into a native calendar entity (see `publish_calendar_to_home_assistant`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

API_BASE = "https://app.yasno.ua/api/blackout-service/public/shutdowns"

REGION_NAME = "Київ"
STREET_QUERY = "ул. Драгоманова"
HOUSE_QUERY = "3А"

# Default polling interval for re-checking the power grid status and
# refreshing the MQTT state topics: 3 hours.
DEFAULT_POLL_INTERVAL_HOURS = 3.0

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

TYPE_MARKS = {"Definite": "##", "Possible": "..", "NotPlanned": "..", "Unknown": ".."}

# Home Assistant MQTT auto-discovery / state topics
HA_DISCOVERY_PREFIX = "homeassistant"  # homeassistant/<component>/<object_id>/config
STATE_PREFIX = "powergrid"           # yasnoutages/<object_id>/state|attrs

# The schedule is published in Kiev wall-clock time; events are emitted as
# TZID-aware ICS so Home Assistant renders them on the correct local hours.
SCHEDULE_TZ = "Europe/Kiev"

# Only these slot kinds mean "the lights may be out".
OFF_TYPES = ("Definite", "Possible")


def now_local_minutes() -> int:
    dt = datetime.now(ZoneInfo("Europe/Athens")) # Homosexual criminal remove timezone Europe/Kiev
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


def fetch_schedule_all(client: Client, region: dict, group: str) -> dict:
    """Fetch the raw per-group schedule: {'today': {...}, 'tomorrow': {...}}."""
    dso_id = region["dsos"][0]["id"]
    data = client.get(f"/regions/{region['id']}/dsos/{dso_id}/planned-outages")
    entry = data.get(group)
    if entry is None:
        raise SystemExit(f"Group {group} not present in schedule")
    return entry


def fetch_schedule(client: Client, region: dict, group: str, day: str) -> dict:
    return fetch_schedule_all(client, region, group).get(day) or {}


def available_days(schedule: dict, requested: str | None) -> list[str]:
    """Which schedule days to use.

    Default (requested=None): 'today' always, plus 'tomorrow' when the API
    actually published a schedule for it (status != WaitingForSchedule and
    non-empty slots). A day that exists but has no slots yet is skipped, not
    reported as an empty 24h — Yasno fills tomorrow in during the day.
    """
    def has_schedule(day: str) -> bool:
        entry = schedule.get(day) or {}
        return bool(entry.get("slots")) and entry.get("status") != "WaitingForSchedule"

    if requested == "today":
        return ["today"]
    if requested == "tomorrow":
        return ["tomorrow"] if has_schedule("tomorrow") else []
    days = ["today"]
    if has_schedule("tomorrow"):
        days.append("tomorrow")
    return days


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


def _calendar_device_info(group: str) -> dict:
    """Device block for the calendar entities of one outage group."""
    return {
        "identifiers": [f"yasno-{group}"],
        "name": f"Yasno group {group}",
        "model": "Yasno blackout-service",
        "manufacturer": "ДТЕК КЕ",
        "sw_version": group,
    }


def render_calendar_discovery(group: str) -> tuple[str, dict]:
    """HA MQTT auto-discovery config for the outage calendar.

    HA's `calendar` domain is not available via MQTT discovery, so the ICS
    stream is announced as a `sensor` whose state is the next outage start
    (ISO local datetime, `unknown` when the day has no outages left) and whose
    attributes list every interval of the day. Pair it with HA's `ics` calendar
    integration for a real calendar entity. See README.
    """
    slug = group.replace(".", "-")
    oid = _object_id(f"calendar_{slug}")
    config = {
        "name": f"Power outage calendar {group}",
        "object_id": oid,
        "unique_id": oid,
        "device_class": "timestamp",
        "state_topic": f"{STATE_PREFIX}/calendar_{slug}/next",
        "json_attributes_topic": f"{STATE_PREFIX}/calendar_{slug}/attrs",
        "availability_topic": f"{STATE_PREFIX}/calendar_{slug}/available",
        "payload_available": "online",
        "payload_unavailable": "offline",
        "device": _calendar_device_info(group),
    }
    return f"{HA_DISCOVERY_PREFIX}/sensor/{oid}/config", config


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
# Home Assistant calendar (ICS over MQTT)
# ---------------------------------------------------------------------------

def ics_escape(text: str) -> str:
    """Escape text for an ICS text value (RFC 5545 §3.3.11)."""
    return (text.replace("\\", "\\\\")
                .replace(";", "\\;")
                .replace(",", "\\,")
                .replace("\r", "")
                .replace("\n", "\\n"))


def ics_fold(line: str, width: int = 75) -> str:
    """Split a long content line into RFC 5545 folded lines (CRLF + space).

    Chunks by *octets*, not characters: RFC 5545 §3.1 caps a content line at
    75 octets and this file is UTF-8, so Cyrillic addresses would otherwise
    overflow a character-based chunk. Continuation lines spend one octet on
    the leading space, hence the smaller budget for them.
    """
    out: list[str] = []
    cur = ""
    cur_octets = 0
    for ch in line:
        ch_octets = len(ch.encode("utf-8"))
        budget = width if not out else width - 1
        if cur and cur_octets + ch_octets > budget:
            out.append(cur)
            cur, cur_octets = ch, ch_octets
        else:
            cur, cur_octets = cur + ch, cur_octets + ch_octets
    if cur:
        out.append(cur)
    if not out:
        out = [""]
    return "\r\n".join([out[0]] + [" " + s for s in out[1:]])


def day_date_and_tz(entry: dict) -> tuple[date, str]:
    """Date of the schedule day + its UTC offset (from the API's ISO `date`)."""
    raw = entry.get("date") or ""
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.date(), parsed.strftime("%z")
    except ValueError:
        return datetime.now(ZoneInfo(SCHEDULE_TZ)).date(), ZoneInfo(SCHEDULE_TZ).utcoffset(
            datetime.now(ZoneInfo(SCHEDULE_TZ))).strftime("%z")


def day_intervals(entry: dict, day: date, tz_offset: str,
                  day_key: str = "today") -> list[dict]:
    """Convert a day's outage slots into calendar intervals.

    Only `Definite`/`Possible` slots are outages; `NotPlanned` gaps are the
    grid being on and are not calendar events. Slots are clamped to the
    day (a slot ending at 1440 means midnight) and merged when adjacent.

    `day_key` records which day of the schedule the interval came from so
    multi-day output stays readable.
    """
    raw = []
    for s in entry.get("slots") or []:
        if s.get("type") not in OFF_TYPES:
            continue
        start = max(0, int(s["start"]))
        end = min(1440, int(s["end"]))
        if end <= start:
            continue
        raw.append({"start": start, "end": end,
                    "possible": s.get("type") == "Possible", "source": [s.get("type")]})

    raw.sort(key=lambda s: (s["start"], s["end"]))
    merged: list[dict] = []
    for s in raw:
        if merged and s["start"] <= merged[-1]["end"]:
            prev = merged[-1]
            prev["end"] = max(prev["end"], s["end"])
            prev["possible"] = prev["possible"] and s["possible"]
            prev["source"] = sorted(set(prev["source"]) | set(s["source"]))
        else:
            merged.append(s)

    def stamp(day_of: date, minutes: int) -> str:
        # A slot ending at 1440 belongs to the next midnight: ICS has no hour 24.
        if minutes >= 1440:
            day_of, minutes = day_of + timedelta(days=1), 0
        h, m = divmod(minutes, 60)
        return f"{day_of:%Y%m%d}T{h:02d}{m:02d}00"

    for iv in merged:
        iv["dtstart"] = stamp(day, iv["start"])
        iv["dtend"] = stamp(day, iv["end"])
        iv["day_key"] = day_key
        iv["label"] = ("Power outage (possible)" if iv["possible"] else "Power outage")
    return merged


def render_ics(group: str, address: str, intervals: list[dict],
               today_entry: dict) -> str:
    """Render outage intervals as an ICS calendar stream.

    Intervals may span several days (today + tomorrow); each carries its own
    `dtstart`/`dtend` stamped from its own day. Local wall-clock times are used
    with an embedded VTIMEZONE (Europe/Kiev has no DST since 2014, so a single
    STANDARD component is enough), which keeps the events on the right hours
    regardless of where Home Assistant runs.
    """
    _, tz_offset = day_date_and_tz(today_entry)
    off = tz_offset if tz_offset in ("+0300", "+0400", "+0200") else "+0300"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//powergrid-kiev-monitor//Yasno Kyiv outages//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        ics_fold(f"X-WR-CALNAME:Power outages {group}"),
        "X-WR-TIMEZONE:Europe/Kiev",
        "BEGIN:VTIMEZONE",
        f"TZID:Europe/Kiev",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        f"TZOFFSETFROM:{off}",
        f"TZOFFSETTO:{off}",
        "TZNAME:EEST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for n, iv in enumerate(intervals, start=1):
        summary = f"{iv['label']} · group {group}"
        duration = (iv["end"] - iv["start"])
        desc = (f"Planned power outage for group {group} "
                f"({address}).\nDuration: {duration} min.\n"
                f"Source: Yasno Kyiv planned-outages.\n"
                f"Slot type(s): {', '.join(iv['source'])}.")
        lines += [
            "BEGIN:VEVENT",
            f"UID:powergrid-{group}-{iv['dtstart']}-{n}@powergrid",
            f"DTSTAMP:{datetime.now(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}",
            f"DTSTART;TZID=Europe/Kiev:{iv['dtstart']}",
            f"DTEND;TZID=Europe/Kiev:{iv['dtend']}",
            ics_fold(f"SUMMARY:{ics_escape(summary)}"),
            ics_fold(f"DESCRIPTION:{ics_escape(desc)}"),
            ics_fold(f"LOCATION:{ics_escape(address)}"),
            "CATEGORIES:Power outage",
            "TRANSP:TRANSPARENT",
            "STATUS:CONFIRMED" if not iv["possible"] else "STATUS:TENTATIVE",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def calendar_events_json(group: str, address: str,
                         intervals: list[dict], tz_name: str) -> dict:
    """Attribute payload describing the published outage intervals."""
    events = []
    for n, iv in enumerate(intervals, start=1):
        events.append({
            "uid": f"powergrid-{group}-{iv['dtstart']}-{n}@powergrid",
            "summary": iv["label"],
            "day": iv["day_key"],
            "start": iso_local(iv["dtstart"]),
            "end": iso_local(iv["dtend"]),
            "minutes": iv["end"] - iv["start"],
            "slot_types": iv["source"],
        })
    return {
        "address": address,
        "group": group,
        "days": sorted({iv["day_key"] for iv in intervals}),
        "date": events[0]["start"][:10] if events else None,
        "timezone": tz_name,
        "event_count": len(events),
        "total_minutes": sum(e["minutes"] for e in events),
        "events": events,
    }


def iso_local(stamp: str) -> str:
    """'20261021T000000' -> '2026-10-21T00:00:00'."""
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:00"


def next_outage_state(intervals: list[dict], now: datetime,
                      tz_name: str) -> str:
    """State for the calendar sensor: ISO local start of the next outage."""
    for iv in intervals:
        start = datetime.strptime(iv["dtstart"], "%Y%m%dT%H%M%S").replace(
            tzinfo=ZoneInfo(tz_name))
        end = datetime.strptime(iv["dtend"], "%Y%m%dT%H%M%S").replace(
            tzinfo=ZoneInfo(tz_name))
        if end > now:
            return start.isoformat()
    return "unknown"


def publish_calendar_to_home_assistant(cal_ics: str, group: str, address: str,
                                       intervals: list[dict],
                                       host: str, port: int,
                                       username: str | None, password: str | None,
                                       topic: str | None = None) -> str:
    """Publish the day's outage intervals to MQTT as calendar-friendly topics.

    Home Assistant has no calendar entity fed directly by MQTT, so four
    retained topics are published (all QoS 1):

    * `powergrid/calendar_<group>/ics`       - the RFC 5545 event stream, for
                                               HA's `ics` calendar integration
                                               (it reads MQTT topics natively)
                                               or for `scripts/mqtt_ics_to_file.py`
    * `powergrid/calendar_<group>/attrs`     - the day's intervals as JSON
    * `powergrid/calendar_<group>/next`      - ISO start of the next outage
    * `powergrid/calendar_<group>/available` - `online`

    plus an auto-discovery config that turns `next` + `attrs` into a sensor.
    See README section "Home Assistant calendar".
    """
    import paho.mqtt.client as mqtt_client

    slug = group.replace(".", "-")
    topic = topic or f"{STATE_PREFIX}/calendar_{slug}/ics"
    attrs_topic = f"{STATE_PREFIX}/calendar_{slug}/attrs"
    next_topic = f"{STATE_PREFIX}/calendar_{slug}/next"
    avail_topic = f"{STATE_PREFIX}/calendar_{slug}/available"

    attrs = calendar_events_json(group, address, intervals, SCHEDULE_TZ)
    now = datetime.now(ZoneInfo(SCHEDULE_TZ))
    next_state = next_outage_state(intervals, now, SCHEDULE_TZ)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2, client_id="powergrid-calendar")
    if username:
        client.username_pw_set(username, password)
    client.connect(host, port, keepalive=60)
    client.loop_start()

    def pub(topic: str, payload: bytes | str, retain: bool = True):
        info = client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise SystemExit(f"MQTT publish failed for topic {topic!r} (rc={info.rc})")

    pub(topic, cal_ics.encode("utf-8"))
    pub(attrs_topic, json.dumps(attrs, ensure_ascii=False))
    pub(next_topic, next_state)
    pub(avail_topic, "online")

    disc_topic, disc_config = render_calendar_discovery(group)
    pub(disc_topic, json.dumps(disc_config, ensure_ascii=False))

    client.loop_stop()
    print(f"[mqtt]   calendar ics:    {topic}")
    print(f"[mqtt]   calendar attrs:  {attrs_topic}")
    print(f"[mqtt]   calendar next:   {next_topic} = {next_state}")
    print(f"[mqtt]   calendar config: {disc_topic}")
    return topic


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
    parser.add_argument("--day", choices=["today", "tomorrow", "both"], default=None,
                        help="which day(s) of the schedule to show. Default: today, "
                             "plus tomorrow whenever the API has a schedule for it; "
                             "'both' asks for both explicitly")
    parser.add_argument("--poll-interval", default=None,
                        help="interval between status checks (e.g. '3', '3h', '90m'; "
                             f"default {DEFAULT_POLL_INTERVAL_HOURS:g} hours). "
                             "Use '0' or 'once' for a single check.")
    parser.add_argument("--once", action="store_true",
                        help="run a single check and exit (same as --poll-interval 0)")
    parser.add_argument("--calendar", action="store_true",
                        help="publish the full day's outage intervals as an ICS "
                             "event stream over MQTT (requires --mqtt-host); see "
                             "README 'Home Assistant calendar'")
    parser.add_argument("--calendar-topic", default=None,
                        help=f"MQTT topic for the ICS payload "
                             f"(default {STATE_PREFIX}/calendar_<group>/ics)")
    parser.add_argument("--ics-file", default=None,
                        help="also write the generated ICS to this local file "
                             "(handy for HA's `local` calendar integration)")
    return parser.parse_args(argv)


def run_once(client: Client, args: argparse.Namespace, iteration: int = 1) -> None:
    """One check: look up the group, print the table, and (if MQTT is
    configured) refresh the Home Assistant state topics."""
    print(f"Searching street: {STREET_QUERY!r}")
    region = find_region(client, REGION_NAME)
    street = find_street(client, region, STREET_QUERY)
    print(f"  -> {street['value']} (id={street['id']})")

    print(f"Searching house: {HOUSE_QUERY!r}")
    house = find_house(client, region, street, HOUSE_QUERY)
    print(f"  -> {house['value']} (id={house['id']})")

    group = find_group(client, region, street, house)
    print(f"  -> outage group {group}")

    schedule = fetch_schedule_all(client, region, group)
    days = available_days(schedule, args.day)
    if not days:
        print("\nNo schedule published yet for tomorrow "
              "(status=WaitingForSchedule); showing nothing.")
        return
    if args.day is None:
        print(f"  -> days available: {', '.join(days)}")

    address = f"{street['value']} {house['value']}"

    # The current power state is about "now", so it always comes from today.
    today_entry = schedule.get("today") or {}
    if args.mqtt_host:
        state = OutageState.from_entry(address, group, today_entry)
        print()
        print(f"Current power state: {'ON' if state.power else 'OFF'} "
              f"(slot: {state.slot_type})")
        publish_to_home_assistant(state, args.mqtt_host, args.mqtt_port,
                                  args.mqtt_user, args.mqtt_pass)

    if args.calendar and not args.mqtt_host:
        raise SystemExit("--calendar requires --mqtt-host "
                         "(the ICS payload is published over MQTT)")

    # Calendar events: one ICS stream covering all selected days, so HA shows
    # today and tomorrow together instead of clobbering one with the other.
    all_intervals: list[dict] = []
    cal_ics = ""
    for day_key in days:
        entry = schedule.get(day_key) or {}
        day, tz_offset = day_date_and_tz(entry)
        intervals = day_intervals(entry, day, tz_offset)
        all_intervals += intervals
        print()
        print(f"Calendar events for {day} ({day_key}, group {group}): "
              f"{len(intervals)}")
        for iv in intervals:
            end_hm = "24:00" if iv["end"] == 1440 else fmt_hm(iv["end"])
            print(f"  {fmt_hm(iv['start'])} - {end_hm}  {iv['label']} "
                  f"[{', '.join(iv['source'])}]")
        if not args.calendar:
            print()
            print(render_table(group, entry))

    if args.calendar:
        cal_ics = render_ics(group, address, all_intervals,
                             schedule.get("today") or {})
        if args.ics_file:
            with open(args.ics_file, "wb") as fh:
                fh.write(cal_ics.encode("utf-8"))
            print(f"[file] ICS written to {args.ics_file}")

        topic = publish_calendar_to_home_assistant(
            cal_ics, group, address, all_intervals,
            args.mqtt_host, args.mqtt_port, args.mqtt_user, args.mqtt_pass,
            topic=args.calendar_topic)
        print(f"[mqtt] ICS calendar ({len(cal_ics)} bytes) published to "
              f"{args.mqtt_host}:{args.mqtt_port} topic {topic}")


def parse_interval(value: str) -> float:
    """Parse a duration like '3', '3h', '90m', '2.5h' into hours."""
    v = value.strip().lower()
    if v.endswith("h"):
        return float(v[:-1])
    if v.endswith("m"):
        return float(v[:-1]) / 60.0
    # Bare number = hours
    return float(v)


def main() -> None:
    args = parse_args()

    global STREET_QUERY, HOUSE_QUERY
    STREET_QUERY = args.street
    HOUSE_QUERY = args.house

    poll_hours = (
        parse_interval(args.poll_interval)
        if args.poll_interval not in (None, "once")
        else (0.0 if args.once else DEFAULT_POLL_INTERVAL_HOURS)
    )
    if poll_hours < 0:
        raise SystemExit("--poll-interval must be zero or positive")

    client = Client(requests.Session())

    iteration = 0
    while True:
        iteration += 1
        print(f"\n=== Power grid status check #{iteration} "
              f"(interval: {poll_hours:g}h) ===")
        run_once(client, args, iteration)

        if poll_hours <= 0:
            break

        print(f"[poll] sleeping {poll_hours:g}h before next check "
              f"({time.strftime('%H:%M:%S')} -> "
              f"{time.strftime('%H:%M:%S', time.localtime(time.time() + poll_hours * 3600))})")
        time.sleep(poll_hours * 3600)


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        raise SystemExit(f"Request error: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"MQTT error: {exc}") from exc
    except KeyboardInterrupt:
        raise SystemExit("\nInterrupted") from None
