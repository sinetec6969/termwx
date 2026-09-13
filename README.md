# termwx

Ultimate terminal weather: live conditions with an animated ASCII sky, 24–36h
hourly graph, 3-day forecast, NWS severe weather alerts, and a NOAA space
weather dashboard. No API keys.

```
 termwx  Charlotte, NC  35.23°N 80.84°W  │  Sun Sep 13 11:13a EDT  │  °F  │  ⟳ just now
  ⚠  SEVERE THUNDERSTORM WARNING  from Sun 11:13a until Sun 11:58a   +1 more   [w] details
╭─ Now ────────────────────────────────────────────────╮╭─ Partly Cloudy ─────────────╮╭─ Space Weather ─────────────╮
│ ☁ Partly Cloudy                                      ││    \ | /     .--.           ││ NOAA   G0   S0   R0   quiet │
│ ▀▀█ █▀█ °F   feels 88°                               ││   ― ( ) ― .-(    ).     v   ││ Kp    2.3 quiet  peak 3.7   │
│   █ ▀▀█     ↑ 89°  ↓ 73°                             ││    / | \ (___.__)__)        ││ ▄▃▇▄█▄▇▆▄▃▅▄▆█▆▅▓▓▓▓▓▓▅▓▓▓  │
│   ▀ ▀▀▀     ☂ 12% today                              ││                  ^     /\   ││ Wind  442 km/s  1.5 p/cm³   │
│ Wind   ↘ WNW 4 mph   Humid  84%  dew 74°             ││                 /^\   /__\  ││ IMF   Bz -1.0  Bt 6.0 nT    │
│ Press  29.97 inHg ↘  UV     5 Moderate               ││ ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁|▁▁▁▁|▪▪|▁▁ ││ Aurora 0% overhead          │
```

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/termwx
```

Requires Python 3.11+ and a terminal with truecolor + Unicode (most modern terminals).

## Usage

```sh
termwx                          # auto-detects location by IP on first run, then remembers it
termwx -l "Denver, CO"          # city, "City, ST", ZIP code, or "lat,lon"
termwx -u metric                # override units
termwx --once                   # print a snapshot and exit (great for a shell MOTD)
termwx --demo-alert             # inject sample alerts to preview the alert UI
termwx --no-anim                # static sky
```

### Keys

| key | action |
|---|---|
| `l` | change location (live search: city, ZIP, lat,lon) |
| `a` | auto-detect location from IP |
| `u` | toggle °F / °C |
| `w` | active alert details (or click the banner) |
| `x` | full space weather dashboard (Kp history + forecast, solar wind, Bz, SWPC bulletins) |
| `s` / `f` | save current location as favorite / cycle favorites |
| `r` | refresh now |
| `p` | pause animation |
| `?` | help · `q` quit |

## What's on screen

- **Now** — big temperature, feels-like, today's high/low, wind + gusts + direction,
  humidity/dew point, pressure + 3h trend, UV, visibility, US AQI, cloud cover,
  sunrise/sunset, moon phase.
- **Sky** — animated scene driven by current conditions: sun/moon, clouds drifting with the
  wind, rain slanting with wind direction, snow, drizzle, fog bands, lightning, stars at night.
- **Next hours** — temperature graph colored on a cold→hot ramp, precip-chance bars, condition glyphs.
- **3-Day Forecast** — wttr.in-style icon cards with highs/lows, precip chance and amount, wind, UV.
- **NWS Alerts** — active watches/warnings/advisories for your exact point. New alerts
  ring the terminal bell, pop a toast, and send a desktop notification (`notify-send`).
  Extreme/Severe alerts pulse the banner.
- **Space Weather** — NOAA G/S/R scales, planetary Kp (observed + 3-day forecast), real-time
  solar wind speed/density, IMF Bt/Bz, GOES X-ray flare class, F10.7, OVATION aurora probability
  overhead and on your horizon, 3-day storm/blackout probabilities, latest SWPC bulletin.
  SWPC warnings at G3/S3/R3 or higher from the last 24h also appear in the alert banner.

## Refresh & offline

Weather refreshes every 10 min, NWS alerts every 2 min, space weather every 5 min. Every
response is cached in `~/.cache/termwx`; if the network drops, the last data is shown and the
top bar says **offline – showing cached data**.

## Configuration

`~/.config/termwx/config.toml` (written automatically):

```toml
units = "imperial"      # or "metric"
desktop_notify = true   # notify-send for new alerts
bell = true             # terminal bell for new alerts
animate = true

[location]
name = "Charlotte"
lat = 35.22709
lon = -80.84313
region = "North Carolina"
country_code = "US"
timezone = "America/New_York"

[[favorites]]
# …same fields as [location]
```

## Data sources

| | |
|---|---|
| Forecast, air quality, geocoding | [Open-Meteo](https://open-meteo.com/) (CC BY 4.0, non-commercial) |
| Severe weather alerts | [NWS API](https://www.weather.gov/documentation/services-web-api) — US & territories only |
| Space weather | [NOAA SWPC](https://services.swpc.noaa.gov/) |
| IP location | [ipinfo.io](https://ipinfo.io/) (fallback ipapi.co) |

Inspired by [wttr.in](https://github.com/chubin/wttr.in), weathr, weather-tui and Stormy.

## Development

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests run entirely against recorded API responses in `tests/fixtures/`, including Textual
Pilot tests of the location search, units toggle, alerts and space weather screens.
