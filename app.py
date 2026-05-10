"""
marquee — AMC theatre showtime tracker.

Run locally:
    cp .env.example .env   # then edit AMC_API_KEY
    pip install -r requirements.txt
    python app.py          # dev only

Run in production:
    gunicorn -w 1 --threads 8 -b 127.0.0.1:5050 app:app
"""

from flask import Flask, render_template, jsonify, request, abort
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from xml.etree import ElementTree as ET
import requests
import threading
import time as _time
import logging
import re
import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional in production where env vars come from the platform

# === LOGGING ===
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("marquee")

# === CONFIG ===
API_KEY = os.environ.get("AMC_API_KEY")
if not API_KEY:
    sys.stderr.write(
        "FATAL: AMC_API_KEY environment variable is not set.\n"
        "Copy .env.example to .env and fill in your key, or export AMC_API_KEY=...\n"
    )
    sys.exit(1)

BASE_URL = "https://api.amctheatres.com/v2"
HEADERS = {"X-AMC-Vendor-Key": API_KEY, "Accept": "application/json"}
PORT = int(os.environ.get("PORT", 5050))
HOST = os.environ.get("HOST", "127.0.0.1")  # bind to localhost by default
DAYS_AHEAD = 30
REFRESH_INTERVAL = 1800
LETTERBOXD_TTL = 1800
HTTP_TIMEOUT = 8                    # was 20 — far too generous
PARALLEL_WORKERS = 8                # parallel day-fetches per theatre
MAX_THEATRE_CACHES = 64             # LRU cap
MAX_LETTERBOXD_CACHE = 256

# === MOVIE QUOTES ===
MOVIE_QUOTES = [
    ('"Here\'s looking at you, kid."', "Casablanca · 1942"),
    ('"You\'re gonna need a bigger boat."', "Jaws · 1975"),
    ('"Carpe diem. Seize the day, boys."', "Dead Poets Society · 1989"),
    ('"To infinity and beyond."', "Toy Story · 1995"),
    ('"Why so serious?"', "The Dark Knight · 2008"),
    ('"I\'m gonna make him an offer he can\'t refuse."', "The Godfather · 1972"),
    ('"Roads? Where we\'re going, we don\'t need roads."', "Back to the Future · 1985"),
    ('"You can\'t handle the truth!"', "A Few Good Men · 1992"),
    ('"Get busy living, or get busy dying."', "The Shawshank Redemption · 1994"),
    ('"You had me at hello."', "Jerry Maguire · 1996"),
    ('"There\'s no place like home."', "The Wizard of Oz · 1939"),
    ('"Life is like a box of chocolates."', "Forrest Gump · 1994"),
    ('"Frankly, my dear, I don\'t give a damn."', "Gone with the Wind · 1939"),
    ('"You shall not pass!"', "The Fellowship of the Ring · 2001"),
    ('"I see dead people."', "The Sixth Sense · 1999"),
    ('"As you wish."', "The Princess Bride · 1987"),
    ('"E.T. phone home."', "E.T. · 1982"),
    ('"Houston, we have a problem."', "Apollo 13 · 1995"),
    ('"Bond. James Bond."', "Dr. No · 1962"),
    ('"I\'ll be back."', "The Terminator · 1984"),
    ('"You talkin\' to me?"', "Taxi Driver · 1976"),
    ('"I love the smell of napalm in the morning."', "Apocalypse Now · 1979"),
    ('"Snap out of it!"', "Moonstruck · 1987"),
    ('"They may take our lives, but they\'ll never take our freedom!"', "Braveheart · 1995"),
    ('"With great power comes great responsibility."', "Spider-Man · 2002"),
    ('"I drink your milkshake!"', "There Will Be Blood · 2007"),
    ('"What\'s in the box?!"', "Se7en · 1995"),
    ('"Just keep swimming."', "Finding Nemo · 2003"),
    ('"Magic mirror on the wall, who is the fairest one of all?"', "Snow White · 1937"),
    ('"You\'re killin\' me, Smalls."', "The Sandlot · 1993"),
    ('"Are you not entertained?"', "Gladiator · 2000"),
    ('"Show me the money!"', "Jerry Maguire · 1996"),
    ('"There\'s no crying in baseball!"', "A League of Their Own · 1992"),
    ('"It was beauty killed the beast."', "King Kong · 1933"),
    ('"Wax on, wax off."', "The Karate Kid · 1984"),
    ('"Greed, for lack of a better word, is good."', "Wall Street · 1987"),
    ('"Nobody puts Baby in a corner."', "Dirty Dancing · 1987"),
    ('"I am your father."', "The Empire Strikes Back · 1980"),
    ('"May the Force be with you."', "Star Wars · 1977"),
    ('"Inconceivable!"', "The Princess Bride · 1987"),
    ('"What we\'ve got here is failure to communicate."', "Cool Hand Luke · 1967"),
    ('"I\'m the king of the world!"', "Titanic · 1997"),
    ('"I feel the need — the need for speed!"', "Top Gun · 1986"),
    ('"Hasta la vista, baby."', "Terminator 2 · 1991"),
    ('"After all, tomorrow is another day."', "Gone with the Wind · 1939"),
    ('"Round up the usual suspects."', "Casablanca · 1942"),
    ('"My precious."', "The Two Towers · 2002"),
    ('"Toto, I\'ve a feeling we\'re not in Kansas anymore."', "The Wizard of Oz · 1939"),
    ('"We\'ll always have Paris."', "Casablanca · 1942"),
    ('"Stella!"', "A Streetcar Named Desire · 1951"),
]

# === STATE TZ MAPPING (fallback if API doesn't return ianaTimeZone) ===
STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}

# Strict username pattern for Letterboxd — letters, digits, underscore, hyphen, 1-30 chars.
LETTERBOXD_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,30}$")

# Allow only http/https poster URLs. Anything else gets dropped at fetch time.
SAFE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# === CACHES ===
class TheatreCache:
    def __init__(self, theatre_id, info=None):
        self.theatre_id = theatre_id
        self.info = info or {}
        self.data = []
        self.ts = 0.0
        self.fetched = 0
        self.total = 0
        self.running = False
        self.poster_urls = []   # ordered, for cursor-based delta
        self.poster_seen = set()
        self.last_error = None
        self.lock = threading.Lock()


# OrderedDict + lock = simple LRU
theatre_caches = OrderedDict()
caches_dict_lock = threading.Lock()
fetching_theatres = set()
fetching_lock = threading.Lock()

letterboxd_cache = OrderedDict()
letterboxd_lock = threading.Lock()


def _evict_if_needed(od, cap):
    while len(od) > cap:
        od.popitem(last=False)  # drop oldest


def get_or_create_theatre_cache(theatre_id, info=None):
    with caches_dict_lock:
        if theatre_id in theatre_caches:
            theatre_caches.move_to_end(theatre_id)
            cache = theatre_caches[theatre_id]
        else:
            cache = TheatreCache(theatre_id, info)
            theatre_caches[theatre_id] = cache
            _evict_if_needed(theatre_caches, MAX_THEATRE_CACHES)
        if info:
            with cache.lock:
                cache.info.update(info)
        return cache


def is_cache_fresh(cache):
    if not cache.data or cache.ts <= 0:
        return False
    return (_time.time() - cache.ts) < REFRESH_INTERVAL


# === HELPERS ===
def fmt_time(dt):
    return dt.strftime("%I:%M %p").lstrip("0")


def parse_iso_local(iso):
    """showDateTimeLocal is a naive local datetime. Don't munge timezones."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        # Truncate at 'T' + 8 chars max for safety, but trust the field is naive.
        # If a writer ever adds an offset, fromisoformat will accept it; we then
        # drop tzinfo so it stays comparable with our other naive datetimes.
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


def normalize_title(title):
    if not title:
        return ""
    s = str(title).lower().strip()
    s = s.replace("\u2014", "-").replace("\u2013", "-").replace("\u2019", "'")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def pick_poster(media):
    if not isinstance(media, dict):
        return ""
    for key in ("posterStandard", "posterAlternateStandard", "posterDynamic",
                "posterLarge", "posterAlternateDynamic", "posterThumbnail"):
        url = media.get(key)
        if url and SAFE_URL_RE.match(url):
            return url
    return ""


def get_theatre_tz(info):
    if not info:
        return ZoneInfo("America/New_York")
    iana = info.get("ianaTimeZone") or info.get("timeZone")
    if iana:
        try:
            return ZoneInfo(iana)
        except Exception:
            pass
    state = ((info.get("location") or {}).get("state") or "").upper()
    tz_name = STATE_TZ.get(state, "America/New_York")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("America/New_York")


def format_address(location):
    if not location:
        return ""
    parts = []
    if location.get("addressLine1"):
        parts.append(location["addressLine1"])
    city = location.get("city")
    state = location.get("state")
    postal = location.get("postalCode")
    line2 = ", ".join(filter(None, [city, state]))
    if postal:
        line2 = (line2 + " " + postal).strip()
    if line2:
        parts.append(line2)
    return ", ".join(parts)


# === LETTERBOXD ===
def _evict_letterboxd():
    with letterboxd_lock:
        _evict_if_needed(letterboxd_cache, MAX_LETTERBOXD_CACHE)


def fetch_letterboxd_titles(username):
    """Fetch the user's recent watched titles from their Letterboxd RSS feed.

    NOTE: Letterboxd RSS only returns the most recent ~50 entries, NOT the
    user's entire watched history. The frontend should label this as
    'Recently watched' rather than 'Watched'.
    """
    if not username or not LETTERBOXD_USERNAME_RE.match(username):
        return set()

    key = username.lower()
    now = _time.time()
    with letterboxd_lock:
        cached = letterboxd_cache.get(key)
        if cached and (now - cached["ts"]) < LETTERBOXD_TTL:
            letterboxd_cache.move_to_end(key)
            return set(cached["titles"])

    titles = set()
    try:
        rss_url = f"https://letterboxd.com/{key}/rss/"
        r = requests.get(
            rss_url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "marquee/1.0 (+https://github.com/youruser/marquee)"},
        )
        if r.status_code == 200:
            # Parse as XML, with the letterboxd namespace.
            # Strip BOM if present; ElementTree chokes on it.
            text = r.text.lstrip("\ufeff")
            try:
                root = ET.fromstring(text)
                ns = {"letterboxd": "https://letterboxd.com"}
                # filmTitle is the canonical, namespaced field.
                for el in root.iter("{https://letterboxd.com}filmTitle"):
                    if el.text:
                        titles.add(normalize_title(el.text))
                # Fallback: parse <title> if no filmTitle elements were present.
                if not titles:
                    for el in root.iter("title"):
                        t = (el.text or "").strip()
                        if not t or t.lower().startswith("letterboxd"):
                            continue
                        if " - " in t:
                            t = t.split(" - ")[0]
                        m = re.match(r"^(.+),\s*\d{4}$", t)
                        if m:
                            t = m.group(1)
                        if t:
                            titles.add(normalize_title(t))
            except ET.ParseError as e:
                log.warning("Letterboxd RSS parse failed for %s: %s", key, e)
        elif r.status_code == 404:
            log.info("Letterboxd user not found: %s", key)
        else:
            log.warning("Letterboxd RSS returned %s for %s", r.status_code, key)
    except requests.RequestException as e:
        log.warning("Letterboxd fetch failed for %s: %s", key, e)

    with letterboxd_lock:
        letterboxd_cache[key] = {"titles": titles, "ts": now}
        letterboxd_cache.move_to_end(key)
    _evict_letterboxd()
    return titles


# === AMC API ===
def _zip_to_latlng(zip_code):
    """Use AMC's location-suggestions endpoint to translate a zip to lat/lng."""
    try:
        r = requests.get(
            f"{BASE_URL}/location-suggestions/",
            params={"query": zip_code, "page-size": 5},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return None, f"AMC location lookup error {r.status_code}"
        data = r.json()
        for s in (data.get("_embedded") or {}).get("suggestions", []):
            if s.get("type") != "zipcode":
                continue
            href = (((s.get("_links") or {})
                     .get("https://api.amctheatres.com/rels/v2/locations") or {})
                    .get("href") or "")
            m = re.search(r"latitude=(-?\d+(?:\.\d+)?).+?longitude=(-?\d+(?:\.\d+)?)", href)
            if m:
                return (float(m.group(1)), float(m.group(2))), None
        return None, "Zip code not recognized by AMC."
    except requests.RequestException as e:
        return None, f"Location lookup failed: {e}"


def search_theatres_by_zip(zip_code):
    """Find AMC theatres near a zip code, ordered by distance. Up to 5 closest."""
    latlng, err = _zip_to_latlng(zip_code)
    if err:
        return [], err
    lat, lng = latlng

    try:
        r = requests.get(
            f"{BASE_URL}/locations",
            params={"latitude": lat, "longitude": lng, "page-size": 10},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return [], f"AMC API error {r.status_code}"
        data = r.json()
        results = []
        for loc in (data.get("_embedded") or {}).get("locations", [])[:5]:
            t = (loc.get("_embedded") or {}).get("theatre") or {}
            tid = t.get("id")
            if not tid:
                continue
            results.append({
                "id": tid,
                "name": (t.get("name") or "").strip(),
                "address": format_address(t.get("location") or {}),
                "city": (t.get("location") or {}).get("city"),
                "state": (t.get("location") or {}).get("state"),
                "distance": round(loc.get("distance", 0), 1),
            })
        return results, None
    except requests.RequestException as e:
        return [], f"Search failed: {e}"


def fetch_theatre_info(theatre_id):
    try:
        r = requests.get(f"{BASE_URL}/theatres/{theatre_id}", headers=HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            t = r.json()
            return {
                "id": t.get("id"),
                "name": (t.get("name") or "").strip(),
                "location": t.get("location") or {},
                "address": format_address(t.get("location") or {}),
                "ianaTimeZone": t.get("ianaTimeZone") or t.get("timeZone"),
            }
    except requests.RequestException as e:
        log.warning("theatre info fetch failed for %s: %s", theatre_id, e)
    return None


def _get_with_retry(url, params=None, retries=2):
    """GET with one quick retry on 5xx / network errors."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if r.status_code == 404:
                return r
            if r.status_code >= 500 and attempt < retries:
                last_err = f"http {r.status_code}"
                _time.sleep(0.4 * (attempt + 1))
                continue
            return r
        except requests.RequestException as e:
            last_err = str(e)
            if attempt < retries:
                _time.sleep(0.4 * (attempt + 1))
                continue
            raise RuntimeError(f"request error: {last_err}")
    raise RuntimeError(last_err or "unknown error")


def fetch_for_date(theatre_id, date):
    date_str = f"{date.month}-{date.day}-{date.year}"
    url = f"{BASE_URL}/theatres/{theatre_id}/showtimes/{date_str}"
    showtimes = []
    page = 1
    max_pages = 20
    while page <= max_pages:
        r = _get_with_retry(url, params={"page-number": page, "page-size": 50})
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise RuntimeError(f"http {r.status_code}: {r.text[:120]}")
        data = r.json()
        for st in (data.get("_embedded") or {}).get("showtimes", []):
            iso_local = st.get("showDateTimeLocal")
            movie = (st.get("movieName") or "").strip()
            if iso_local and movie:
                showtimes.append({
                    "movie": movie,
                    "iso_local": iso_local,
                    "poster": pick_poster(st.get("media") or {}),
                })
        if not (data.get("_links") or {}).get("next"):
            break
        page += 1
    if page > max_pages:
        log.warning("hit max_pages=%s for theatre %s on %s", max_pages, theatre_id, date_str)
    return showtimes


def rebuild_result(by_movie):
    """One card per movie. Inside each card, times grouped by date.

    by_movie is keyed by movie title. Each value has:
      - dates: dict[date_iso -> set of datetimes]
      - poster: first poster URL we saw for that title
    """
    result = []
    for movie, info in by_movie.items():
        dates_dict = info.get("dates") or {}
        if not dates_dict:
            continue
        # Sort the per-date entries chronologically.
        date_entries = []
        for date_iso, times in dates_dict.items():
            ordered_times = sorted(times)
            if not ordered_times:
                continue
            first = ordered_times[0]
            date_entries.append({
                "date_iso": date_iso,
                "weekday": first.strftime("%A"),
                "weekday_short": first.strftime("%a").upper(),
                "month_day": first.strftime("%b %d").upper(),
                "times": [fmt_time(t) for t in ordered_times],
            })
        date_entries.sort(key=lambda d: d["date_iso"])
        if not date_entries:
            continue

        # The movie's overall "earliest" datetime — used to sort movies on the page.
        earliest_iso = min(min(times) for times in dates_dict.values()).isoformat()

        result.append({
            "title": movie,
            "poster": info.get("poster", ""),
            "dates": date_entries,             # list of {date_iso, weekday, month_day, times}
            "first_date_iso": date_entries[0]["date_iso"],
            "first_weekday": date_entries[0]["weekday"],
            "first_month_day": date_entries[0]["month_day"],
            "first_time": date_entries[0]["times"][0],
            "total_showings": sum(len(d["times"]) for d in date_entries),
            "sort_dt": earliest_iso,
        })
    # Sort movies by their earliest showtime.
    result.sort(key=lambda m: m["sort_dt"])
    return result


def fetch_showtimes_bg(theatre_id):
    """Background fetch for one theatre — parallelized across the date range."""
    cache = get_or_create_theatre_cache(theatre_id)

    if not cache.info or not cache.info.get("name"):
        info = fetch_theatre_info(theatre_id)
        if info:
            with cache.lock:
                cache.info.update(info)

    tz = get_theatre_tz(cache.info)
    today = datetime.now(tz).date()
    # Include today through DAYS_AHEAD-1 days out — total = DAYS_AHEAD days.
    dates = [today + timedelta(days=i) for i in range(0, DAYS_AHEAD)]

    with cache.lock:
        cache.fetched = 0
        cache.total = len(dates)
        cache.running = True
        cache.poster_urls = []
        cache.poster_seen = set()
        cache.last_error = None

    by_movie = {}          # movie -> {"dates": {date_iso: set(datetimes)}, "poster": str}
    by_movie_lock = threading.Lock()
    errors = []
    final_data = []
    start = _time.time()

    def _do_one(d):
        try:
            return d, fetch_for_date(theatre_id, d), None
        except Exception as e:
            return d, [], str(e)[:200]

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
            futures = [ex.submit(_do_one, d) for d in dates]
            for fut in as_completed(futures):
                d, showtimes, error = fut.result()
                new_posters = []
                if error:
                    errors.append(f"{d.isoformat()}: {error}")
                else:
                    with by_movie_lock:
                        for st in showtimes:
                            dt = parse_iso_local(st["iso_local"])
                            if not dt or dt.date() < today:
                                continue
                            entry = by_movie.setdefault(
                                st["movie"],
                                {"dates": {}, "poster": ""},
                            )
                            date_key = dt.date().isoformat()
                            entry["dates"].setdefault(date_key, set()).add(dt)
                            if not entry["poster"] and st.get("poster"):
                                entry["poster"] = st["poster"]
                                new_posters.append(st["poster"])

                with cache.lock:
                    cache.fetched += 1
                    for url in new_posters:
                        if url not in cache.poster_seen:
                            cache.poster_seen.add(url)
                            cache.poster_urls.append(url)
                    if errors:
                        cache.last_error = "; ".join(errors[-3:])

        with by_movie_lock:
            final_data = rebuild_result(by_movie)
        log.info("theatre %s: %s movies in %.1fs (%s errors)",
                 theatre_id, len(final_data), _time.time() - start, len(errors))
    finally:
        with cache.lock:
            cache.data = final_data
            cache.ts = _time.time()
            cache.running = False
        with fetching_lock:
            fetching_theatres.discard(theatre_id)


def trigger_fetch(theatre_id):
    with fetching_lock:
        if theatre_id in fetching_theatres:
            return False
        fetching_theatres.add(theatre_id)
    threading.Thread(target=fetch_showtimes_bg, args=(theatre_id,), daemon=True).start()
    return True


# === RATE LIMITING (light, in-memory, per-IP for letterboxd) ===
_rate_lock = threading.Lock()
_rate_buckets = {}  # ip -> [(ts, ts, ...)]
RATE_WINDOW = 60.0
RATE_LIMIT = 20     # 20 calls per minute per IP for /api/letterboxd


def _check_rate_limit(ip):
    now = _time.time()
    with _rate_lock:
        bucket = _rate_buckets.get(ip, [])
        bucket = [t for t in bucket if now - t < RATE_WINDOW]
        if len(bucket) >= RATE_LIMIT:
            _rate_buckets[ip] = bucket
            return False
        bucket.append(now)
        _rate_buckets[ip] = bucket
        # opportunistic cleanup
        if len(_rate_buckets) > 1000:
            stale = [k for k, v in _rate_buckets.items()
                     if not v or now - v[-1] > RATE_WINDOW]
            for k in stale:
                _rate_buckets.pop(k, None)
        return True


# === FLASK APP ===
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/theatres")
def api_theatres():
    zip_code = (request.args.get("zip") or "").strip()
    if not re.fullmatch(r"\d{5}", zip_code):
        return jsonify({"error": "Please enter a valid 5-digit zip code."}), 400
    theatres, err = search_theatres_by_zip(zip_code)
    if err:
        return jsonify({"error": err}), 502
    if not theatres:
        return jsonify({"error": "No AMC theatres found near that zip code.", "theatres": []})
    return jsonify({"theatres": theatres})


@app.route("/api/theatre/<int:theatre_id>")
def api_theatre(theatre_id):
    cache = get_or_create_theatre_cache(theatre_id)

    if not is_cache_fresh(cache) and not cache.running:
        trigger_fetch(theatre_id)

    with cache.lock:
        ready = is_cache_fresh(cache) and not cache.running
        return jsonify({
            "info": cache.info,
            "ready": ready,
            "running": cache.running,
            "fetched": cache.fetched,
            "total": cache.total,
            "data": cache.data if ready else [],
            "ts": cache.ts,
            "last_error": cache.last_error,
        })


@app.route("/api/status/<int:theatre_id>")
def api_status(theatre_id):
    cache = get_or_create_theatre_cache(theatre_id)
    # Cursor-based delta for poster URLs — client sends ?since=N (count
    # of posters it has already received) and we send only the new ones.
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    with cache.lock:
        new_posters = cache.poster_urls[since:] if since >= 0 else []
        return jsonify({
            "running": cache.running,
            "fetched": cache.fetched,
            "total": cache.total,
            "movies_count": len(cache.data),
            "new_poster_urls": new_posters,
            "poster_total": len(cache.poster_urls),
            "ready": is_cache_fresh(cache) and not cache.running,
            "last_error": cache.last_error,
        })


@app.route("/api/letterboxd/<username>")
def api_letterboxd(username):
    if not LETTERBOXD_USERNAME_RE.match(username):
        return jsonify({"error": "Invalid Letterboxd username.", "titles": [], "count": 0}), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "0.0.0.0").split(",")[0].strip()
    if not _check_rate_limit(ip):
        return jsonify({"error": "Rate limit exceeded. Try again in a minute.",
                        "titles": [], "count": 0}), 429
    titles = fetch_letterboxd_titles(username)
    return jsonify({"titles": list(titles), "count": len(titles)})


@app.route("/api/refresh/<int:theatre_id>", methods=["POST"])
def api_refresh(theatre_id):
    cache = get_or_create_theatre_cache(theatre_id)
    with cache.lock:
        cache.ts = 0.0
    trigger_fetch(theatre_id)
    return jsonify({"ok": True})


@app.route("/api/quotes")
def api_quotes():
    return jsonify({"quotes": MOVIE_QUOTES})


if __name__ == "__main__":
    log.warning(
        "Starting Flask dev server on %s:%s. "
        "DO NOT use this in production — run with gunicorn instead.",
        HOST, PORT,
    )
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
