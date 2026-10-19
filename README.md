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
