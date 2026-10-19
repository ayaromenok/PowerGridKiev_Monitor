# Yasno Kyiv outages — CLI

A small Python app that reproduces the flow of
<https://static.yasno.ua/kyiv/outages> without a browser:

1. Looks up the street in the **Вулиця** field → `вул. Драгоманова`
2. Looks up the house in the **Буд** field → `3А`
3. Resolves the house's outage group (e.g. `45.1`)
4. Fetches the planned-outages schedule and prints the resulting
   group × 24h table to the terminal in ASCII.

The website is a React SPA; all data comes from the public
`blackout-service` API at `https://app.yasno.ua`:

| Step | Endpoint |
|------|----------|
| Region (Київ → id 25, DSO id 902) | `/api/blackout-service/public/shutdowns/addresses/v2/regions` |
| Street search (`query=`) | `.../addresses/v2/streets?regionId&dsoId&query` |
| House search (`query=`) | `.../addresses/v2/houses?regionId&streetId&dsoId&query` |
| House → group | `.../addresses/v2/group?regionId&streetId&houseId&dsoId` |
| Schedule (today/tomorrow per group) | `.../regions/{region}/dsos/{dso}/planned-outages` |

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install requests
```

## Run

```sh
.venv/bin/python outages.py
```

Output looks like:

```
  00-+-01-+-02-+-03-+-...+-23
  ----+-----+-----+-----+
  ..-+-..-+-..-+-##-+-...
  04:30 - 08:00  Відключення (definite)
  ...
  Legend: ## = definite outage, .. = power available / possible outage
```

`##` marks hours with a definite outage; `..` marks hours with power
available (or a possible outage). A full interval list with start/end
times is printed below the timeline.
