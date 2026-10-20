# Yasno Kyiv outages — CLI

A small Python app that reproduces the flow of
<https://static.yasno.ua/kyiv/outages> without a browser:

1. Looks up the street in the **Улица** field → `ул. Драгоманова`
2. Looks up the house in the **Дом** field → `3А`
3. Resolves the house's outage group (e.g. `45.1`)
4. Fetches the planned-outages schedule and prints the resulting
   group × 24h table to the terminal in ASCII.

The website is a React SPA; all data comes from the public
`blackout-service` API at `https://app.yasno.ua`:

| Step | Endpoint |
|------|----------|
| Region (Киев → id 25, DSO id 902) | `/api/blackout-service/public/shutdowns/addresses/v2/regions` |
| Street search (`query=`) | `.../addresses/v2/streets?regionId&dsoId&query` |
| House search (`query=`) | `.../addresses/v2/houses?regionId&streetId&dsoId&query` |
| House → group | `.../addresses/v2/group?regionId&streetId&houseId&dsoId` |
| Schedule (today/tomorrow per group) | `.../regions/{region}/dsos/{dso}/planned-outages` |

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Home Assistant via MQTT

`--mqtt-host` publishes the current power state of the house's outage
group to Home Assistant through its [MQTT auto-discovery](
https://www.home-assistant.io/integrations/mqtt/):

```sh
.venv/bin/python outages.py --mqtt-host 127.0.0.1 --mqtt-port 1883 \
    [--mqtt-user USER --mqtt-pass PASS]
```

It publishes (all retained, QoS 1):

| Entity | Type | State topic |
|--------|------|-------------|
| `Yasno power 45.1` | `binary_sensor`, device_class `power` | `yasnoutages/yasnoutages_power_45.1/state` (`ON`/`OFF`) |
| `Yasno next outage 45.1` | `sensor`, duration in minutes | `yasnoutages/yasnoutages_next_outage_45.1/state` |

Discovery topics: `homeassistant/binary_sensor/yasnoutages_power_45.1/config`,
`homeassistant/sensor/yasnoutages_next_outage_45.1/config` — Home Assistant
picks the entities up automatically. Both entities carry JSON attributes
(address, group, schedule date/status, current slot, next outage start/end).

The power state is evaluated against the current time in `Europe/Kiev`
against the selected day's slots (`Definite`/`Possible` ⇒ OFF).

## Home Assistant calendar

`--calendar` publishes the **whole day's** outage intervals (not just the
current state) as an [RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545)
`VCALENDAR` stream over MQTT, so the outages show up on a calendar.

```sh
.venv/bin/python src/py/pgkiev_monitor.py --once --mqtt-host 192.168.1.101 \
    --calendar [--day tomorrow] [--ics-file ~/outages.ics]
```

One event per OFF interval (`Definite` / `Possible`); `NotPlanned` gaps are the
grid being on and are skipped. Touching/overlapping slots are merged, a slot
ending at 24:00 becomes next-day `00:00` (ICS has no hour 24), and times are
emitted with an embedded `VTIMEZONE` (`Europe/Kiev`, no DST since 2014) so the
hours are right no matter where Home Assistant runs.

Retained topics (QoS 1), `45.1` example:

| Topic | Contents |
|-------|----------|
| `powergrid/calendar_45-1/ics` | the ICS stream (one `VEVENT` per interval) |
| `powergrid/calendar_45-1/attrs` | JSON: date, timezone, event list, `total_minutes` |
| `powergrid/calendar_45-1/next` | ISO local start of the next outage, else `unknown` |
| `powergrid/calendar_45-1/available` | `online` |
| `homeassistant/sensor/powergrid_calendar_45-1/config` | discovery for a `timestamp` sensor fed by `next` + `attrs` |

### Wiring the calendar into Home Assistant

Home Assistant has **no calendar entity fed by MQTT**, and its MQTT discovery
does not support the `calendar` domain — so the ICS topic cannot become a
calendar entity by itself. Two supported options:

**A. `ics` integration (recommended).** It polls a resource and produces a
native `calendar.*` entity with the usual `calendar.create_event` /
`calendar.get_events` services:

```yaml
# configuration.yaml
calendar:
  - platform: ics
    url: "mqtt://192.168.1.101:1883#powergrid/calendar_45-1/ics"
    unique_id: powergrid_calendar_45_1
    summary_event_name: "Power outage"
    location_field: "LOCATION"
    # device_class: power_outage   # if your HA build exposes it
```

If your `ics` build cannot read `mqtt://`, serve the payload over HTTP instead —
e.g. with `--ics-file /config/www/powergrid/calendar_45-1.ics` (write it into
HA's `www/` directory) and use
`url: "http://192.168.1.101:8123/local/powergrid/calendar_45-1.ics"`.

**B. `local` integration + file bridge.** Point `--ics-file` at a path inside
HA's config dir and add:

```yaml
calendar:
  - platform: local
    name: "Power outages 45.1"
    unique_id: powergrid_local_45_1
```

then import the file once per day (the `local` platform stores its own events;
re-import via `calendar.create_event` from an automation reading the `attrs`
topic, since `local` has no file-polling mode).

The discovery config above always gives you `sensor.powergrid_calendar_45_1`
(next outage as a `timestamp`, all intervals in attributes) even without the
`ics` integration — handy for automations and the Energy/Calendar dashboard.

> Note: the schedule only covers *today* and *tomorrow*; `--day tomorrow` is
> often `WaitingForSchedule` with no slots. Re-run with `--poll-interval` (or
> cron) so the calendar refreshes when Yasno publishes changes.

### Polling interval

By default the script runs as a long-lived process: it checks the power
grid status, publishes the MQTT state topics, then sleeps and repeats.

- `--poll-interval` — time between checks. Accepts `'3'`, `'3h'`, `'90m'`,
  `'2.5h'` (bare number = hours). **Default: 3 hours.**
- `--once` (or `--poll-interval 0` / `'once'`) — single check, then exit
  (useful with cron).

To update Home Assistant regularly, run it in the background (default 3h
interval):

```sh
nohup .venv/bin/python outages.py --mqtt-host 127.0.0.1 --mqtt-port 1883 \
    --mqtt-user USER --mqtt-pass PASS >> outages.log 2>&1 &
```

or use the old-style cron approach with a single check per run:

```sh
*/180 * * * * cd /path/to/app && .venv/bin/python outages.py --once --mqtt-host 127.0.0.1 --mqtt-port 1883 --mqtt-user USER --mqtt-pass PASS >> outages.log 2>&1
```

## Run

```sh
.venv/bin/python outages.py
```

Output looks like:

```
### Timeline

| 00 | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 |
|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|
| .. | .. | .. | .. | .. | .. | .. | .. | .. | .. | .. | .. | .. | ## | ## | ## | .. | .. | .. | .. | .. | .. | ## | ## |

### Schedule

| Start |  End  |  Status  |
|-------|-------|----------|
| 00:00 | 12:30 | Grid On  |
| 12:30 | 16:00 | Grid oFF |
| 16:00 | 21:30 | Grid On  |
| 21:30 | 24:00 | Grid oFF |

Legend: `##` = Grid oFF, `..` = Grid On / possible outage
```

`##` marks hours with a definite outage; `..` marks hours with power
available (or a possible outage). A full interval list with start/end
times is printed below the timeline.
