#!/usr/bin/env python3
"""Yasno Kyiv outages — CLI.

Reproduces the flow of https://static.yasno.ua/kyiv/outages without a browser:

1. Look up the street ("Вулиця") via the blackout-service API.
2. Look up the house ("Буд") on that street.
3. Resolve the house's outage group.
4. Fetch the planned-outages schedule and print the resulting
   group x 24h table to the terminal in ASCII.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import requests

API_BASE = "https://app.yasno.ua/api/blackout-service/public/shutdowns"

REGION_NAME = "Київ"
STREET_QUERY = "вул. Драгоманова"
HOUSE_QUERY = "3А"

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

TYPE_MARKS = {"Definite": "##", "Possible": "..", "NotPlanned": "..", "Unknown": ".."}


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


def render_table(group: str, entry: dict, hours: range | None = None) -> str:
    """Render the group's 24h slot schedule as an ASCII table."""
    slots = entry.get("slots") or []
    date = entry.get("date", "")
    status = entry.get("status", "")

    if not slots:
        body = f"  (no schedule for group {group}: status={status})"
    else:
        # Build a 24-column timeline, one cell per hour.
        hour_marks = [None] * 24
        for s in slots:
            start_h, start_m = divmod(int(s["start"]), 60)
            end_h, end_m = divmod(int(s["end"]), 60)
            mark = TYPE_MARKS.get(s["type"], "..")
            for h in range(start_h, min(end_h + (1 if end_m else 0), 24)):
                hour_marks[h] = mark if hour_marks[h] is None else hour_marks[h]

        rows = []
        header = "  " + "-+-".join(f"{h:02d}" for h in range(24))
        rows.append(header)
        rows.append("  " + "-+-".join("---" for _ in range(24)))
        mark_row = "  " + "-+-".join((hour_marks[h] or "  ") for h in range(24))
        rows.append(mark_row)
        rows.append("  " + "-+-".join("---" for _ in range(24)))

        # Interval list for readability
        rows.append("")
        for s in slots:
            sh, sm = divmod(int(s["start"]), 60)
            eh, em = divmod(int(s["end"]), 60)
            label = {
                "Definite": "Відключення (definite)",
                "Possible": "Можливе відключення (possible)",
                "NotPlanned": "Постачання (not planned)",
            }.get(s["type"], s["type"])
            rows.append(f"  {sh:02d}:{sm:02d} - {eh:02d}:{em:02d}  {label}")
        body = "\n".join(rows)

    lines = [
        f"  Address : {STREET_QUERY}, буд. {HOUSE_QUERY}",
        f"  Group   : {group}",
        f"  Date    : {date}",
        f"  Status  : {status}",
        "",
        body,
        "",
        "  Legend: ## = definite outage, .. = power available / possible outage",
    ]
    title = f"YASNO — Kyiv outages, group {group}"
    width = 2 + 3 * 24 + 23  # widest row
    frame = "=" * width
    return "\n".join([frame, " " + title, frame, *lines, frame])


def main() -> None:
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

    entry = fetch_schedule(client, region, group, day="today")
    print()
    print(render_table(group, entry))


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as exc:
        raise SystemExit(f"Request error: {exc}") from exc
