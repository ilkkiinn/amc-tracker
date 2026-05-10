# marquee

A self-hosted, local-first AMC theatre tracker. Punch in a zip code, pick a
theatre, and get a 30-day program of every showtime — cross-referenced
against your Letterboxd recently-watched feed so you know what's a re-watch.

Built because I kept missing limited runs at my local AMC. The AMC app
is fine, but it doesn't tell you "you've already seen this one," and it
doesn't give you a clean week-at-a-glance.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Flask 3](https://img.shields.io/badge/flask-3.x-black)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Zip-based theatre search** — finds the 5 closest AMC theatres, sorted by
  distance.
- **30-day showtime window** — pulled in parallel from the AMC API, typically
  loads in 2–4 seconds.
- **Letterboxd integration** — paste your username, and any movie you've
  recently logged on Letterboxd is stamped "Recently watched" with a "Re-watch"
  marker on its first showtime.
- **Theatre-local time** — showtimes are always rendered in the theatre's
  timezone, not yours, so a 7:00 PM show in NYC reads "7:00 PM" whether
  you're in Phoenix or Berlin.
- **Multi-day runs collapsed** — one card per movie, with all dates and
  times nested inside. No duplicates.
- **Per-theatre cache** — refreshes every 30 minutes, manual refresh button
  in the footer.
- **Dark, theatrical UI** — Instrument Serif headlines, JetBrains Mono
  details, a filmstrip progress bar while loading, and a rotating reel of
  classic movie quotes during the wait.
- **Themes** — three palettes (default, sepia, noir), togglable from the footer.

## Stack

- **Backend** — Flask 3, Python 3.10+, plain `requests` for HTTP, in-memory
  caches with LRU caps, a `ThreadPoolExecutor` for parallel day-fetches.
- **Frontend** — single Jinja template, vanilla JS, no build step, no
  framework. CSS uses custom properties for theming.
- **External APIs** — [AMC Theatres API](https://developers.amctheatres.com/)
  (vendor key required), Letterboxd RSS (no auth, ~50 most recent entries).

## Setup

You'll need Python 3.10+ and an AMC vendor key. Get a key by signing up at
<https://developers.amctheatres.com/GettingStarted/CreateAccount> — it's
free for personal use.

```bash
git clone https://github.com/ilkkiinn/amc-tracker.git
cd amc-tracker

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# open .env in any editor, paste your AMC key after AMC_API_KEY=

python app.py
```

Then open <http://127.0.0.1:5050> in a browser.

## Configuration

All config lives in `.env`. Only `AMC_API_KEY` is required.

| Variable        | Default       | What it does                                   |
| --------------- | ------------- | ---------------------------------------------- |
| `AMC_API_KEY`   | _(required)_  | Your AMC vendor key                            |
| `HOST`          | `127.0.0.1`   | Interface to bind to                           |
| `PORT`          | `5050`        | Port to listen on                              |
| `LOG_LEVEL`     | `INFO`        | `DEBUG`, `INFO`, `WARNING`, `ERROR`            |

## Production

Don't use `python app.py` in production — that's the Flask dev server. Use
gunicorn:

```bash
gunicorn -w 1 --threads 8 -b 127.0.0.1:5050 app:app
```

**Use one worker, multiple threads** — not multiple workers. The in-memory
cache wouldn't be shared across workers, so each worker would fetch the
AMC API independently. Put nginx (or Caddy) in front to handle TLS and
proxy to gunicorn.

## How it works

1. **Setup screen** — user enters a zip, picks a theatre, optionally adds a
   Letterboxd username. State persists in `localStorage`.
2. **Loading screen** — backend fans out 30 parallel requests to AMC's
   showtimes endpoint (one per day), 8 in flight at a time. The frontend
   polls `/api/status/<theatre_id>` every 800ms and progressively preloads
   poster images so they're warm by the time the page renders.
3. **Theatre screen** — movies grouped under day-headers by their first
   showing date. Each card lists every date the movie runs, with all
   times. Letterboxd-watched titles get a "Recently watched" stamp.

## Project layout

```
amc-tracker/
├── app.py                 Flask app, caches, parallel fetcher
├── templates/
│   └── index.html         Single-page UI (setup → loading → theatre)
├── requirements.txt
├── .env.example           Template — copy to .env and fill in your key
├── .gitignore             Keeps .env out of git
├── LICENSE                MIT
└── README.md
```

## Notes & caveats

- **Letterboxd RSS only returns the last ~50 watched entries.** The
  "Recently watched" stamp is honest about that — older logged movies
  won't be flagged.
- **Showtimes are in the theatre's local time**, not yours. The hero
  meta block says so explicitly.
- **AMC vendor keys are scoped.** If your key returns 403 "Unauthorized
  VendorKey," it's not enabled for the endpoints this app uses
  (`/location-suggestions`, `/locations`, `/theatres`, `/showtimes`).
  Email AMC's developer support to request access, or use a different
  key.
- **Rate limits.** `/api/letterboxd/<username>` is rate-limited to 20
  requests per minute per IP. AMC's own rate limits aren't documented;
  if you fork this and load it heavily, throttle yourself.

## License

MIT — see [LICENSE](./LICENSE).

Not affiliated with, endorsed by, or sponsored by AMC Theatres. AMC,
the AMC logo, and theatre names are trademarks of their owner.
