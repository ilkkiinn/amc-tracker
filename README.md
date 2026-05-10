# marquee

A small Flask app that pulls 30 days of AMC showtimes for a theatre near you,
optionally cross-referenced against your Letterboxd recently-watched feed so
you know what's a re-watch.

## Setup

1. Get an AMC vendor API key from <https://developers.amctheatres.com/>.
2. Copy `.env.example` to `.env` and paste your key in.
3. Install and run:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

App runs at <http://127.0.0.1:5050>.

## Production

Don't use `python app.py` in production — that's the Flask dev server and
it's single-threaded. Use gunicorn:

```bash
gunicorn -w 1 --threads 8 -b 127.0.0.1:5050 app:app
```

Use **one worker** with multiple threads, not multiple workers — the in-memory
cache wouldn't be shared across workers, so each worker would fetch the AMC
API independently. Put nginx (or similar) in front to handle TLS and proxy to
gunicorn.

## What's in here

- `app.py` — Flask app, in-memory caches, parallel showtime fetcher
- `templates/index.html` — single-page UI (Setup → Loading → Theatre)
- `requirements.txt` / `.env.example` — install + config
- `.gitignore` — keeps `.env` and OS junk out of git

## Notes

- **Letterboxd RSS only returns the last ~50 watched entries.** The "Recently
  watched" stamp on movies is honest about that — older movies you've seen
  but didn't recently log won't be flagged.
- **Showtimes are in the theatre's local time**, not yours. The hero meta
  block says so.
- The AMC API is rate-limited; the parallel fetcher uses 8 workers and
  retries 5xx responses once. If you fork this and load it heavily, throttle
  yourself.
