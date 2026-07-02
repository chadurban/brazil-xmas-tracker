#!/usr/bin/env python3
"""
Real cash-fare source via the SerpApi Google Flights engine.

Activates only when `serpapi_key` is present in the secrets file. To stay inside the
free tier (100 searches/month), it refreshes at most every MIN_DAYS_BETWEEN days and
queries just the key long-haul round-trips (CHS -> GRU/GIG, premium + business) = 4
searches per refresh -> ~40/month. Results cache to cash.json; scan.py folds them into
data.json as `bestCash`. Falls back silently (returns None) when no key / quota / error,
so the award scan is never affected.

Run:  python3 cash.py --mock     # test the parser offline (no key/network)
      python3 cash.py --force    # force a live refresh now (needs key)
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = "/Users/admin/Library/CloudStorage/GoogleDrive-urbanc@acba.edu/.shortcut-targets-by-id/1Tc-st1PSSbOMdWmS2DRk_5DLXRb8WpFb/ATS1/Operations/Claude/Personal/.flight-scanner-secrets.json"
CACHE = os.path.join(HERE, "cash.json")

PAX = 3
HOME = "CHS"
GATEWAYS = ["GRU", "GIG"]
CASH_DATES = ("2026-12-19", "2026-12-30")        # representative depart/return (11 nights, in the 7-15 window)
CABINS = {"premium": 2, "business": 3, "first": 4}            # SerpApi travel_class: 1 econ, 2 prem-econ, 3 business, 4 first
LAY_MIN, LAY_MAX = 150, 360                       # Chad's layover window (minutes)
FULL_EVERY = 6                                    # business/first refresh every N days (SerpApi free-tier quota); premium econ refreshes DAILY for anomaly/pounce detection
POUNCE_FACTOR = 0.88                              # premium cash <= 88% of the trailing median => flag a pounce
# quota math: prem 2 searches/day × 30 ≈ 60/mo + full 4 searches × ~5 ≈ 20/mo ≈ 80/mo, under the 100/mo free tier
VIX_HOP_CASH = 105                                # one-way gateway<->VIX cash pp (real: Jun-2026 LATAM premium-econ ran ~$108/pp/leg)

def get_key():
    try:
        return json.load(open(SECRETS)).get("serpapi_key")
    except Exception:
        return None

def serpapi(key, params):
    p = {**params, "api_key": key, "engine": "google_flights", "currency": "USD", "hl": "en"}
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(p)
    with urllib.request.urlopen(url, timeout=70) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def parse_itineraries(data):
    """Flatten SerpApi best_flights + other_flights into vetted option dicts."""
    out = []
    for it in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        segs = it.get("flights") or []
        lays = it.get("layovers") or []
        lay_mins = [l.get("duration") for l in lays if l.get("duration") is not None]
        # Google Flights itineraries never split across separate airports within one connection,
        # so same-airport is implied; just enforce the duration window.
        ok = all(LAY_MIN <= (m or 0) <= LAY_MAX for m in lay_mins)
        path = ""
        if segs:
            path = "-".join([segs[0]["departure_airport"]["id"]] + [s["arrival_airport"]["id"] for s in segs])
        out.append({
            "priceRaw": it.get("price"), "durationMin": it.get("total_duration"),
            "stops": max(0, len(segs) - 1), "path": path, "layoverOK": ok, "layovers": lay_mins,
            "carriers": sorted({s.get("airline") for s in segs if s.get("airline")}),
        })
    out.sort(key=lambda x: (not x["layoverOK"], x["priceRaw"] if x["priceRaw"] is not None else 9e9))
    return out

def fetch(key, cabins=None):
    """Query SerpApi for each requested cabin/gateway; return the cheapest compliant RT per cabin."""
    res, searches, errs = {}, 0, []
    for cab in (cabins or list(CABINS.keys())):
        tc = CABINS[cab]
        best = None
        for gw in GATEWAYS:
            try:
                data = serpapi(key, {"departure_id": HOME, "arrival_id": gw,
                    "outbound_date": CASH_DATES[0], "return_date": CASH_DATES[1],
                    "travel_class": tc, "adults": PAX, "type": "1", "stops": "2"})
                searches += 1
            except urllib.error.HTTPError as e:
                errs.append(f"{cab} {gw} HTTP {e.code}"); continue
            except Exception as e:
                errs.append(f"{cab} {gw} {type(e).__name__}"); continue
            if data.get("error"):
                errs.append(f"{cab} {gw}: {data['error'][:60]}"); continue
            opts = parse_itineraries(data)
            compliant = [o for o in opts if o["layoverOK"]] or opts
            if compliant:
                o = dict(compliant[0]); o["gateway"] = gw
                if best is None or (o["priceRaw"] or 9e9) < (best["priceRaw"] or 9e9):
                    best = o
        if best:
            # SerpApi google_flights price is the TOTAL for all adults in the query.
            best["totalForPax"] = best["priceRaw"]
            best["vixRtPerPax"] = VIX_HOP_CASH * 2
            best["doorToDoorTotal"] = (best["priceRaw"] or 0) + VIX_HOP_CASH * 2 * PAX
            res[cab] = best
    return res, searches, errs

def _state():
    try:
        return json.load(open(CACHE))
    except Exception:
        return None

def _days_since(iso):
    try:
        return (date.today() - date.fromisoformat(iso)).days
    except Exception:
        return 999

def _anomaly(history, today_price):
    """Flag a pounce when today's premium door-to-door is well below the trailing norm."""
    if not today_price or not history:
        return None
    today = date.today().isoformat()
    prior = [h["premium"] for h in history if h.get("premium") and h.get("date") != today]
    if len(prior) < 5:                            # need a few days of baseline first
        return None
    s = sorted(prior); n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    prior_min = min(prior)
    return {"baseline": round(median), "priorMin": prior_min,
            "pctBelowMedian": round((median - today_price) / median * 100),
            "newLow": today_price < prior_min, "pounce": today_price <= POUNCE_FACTOR * median,
            "days": len(prior) + 1}

def best_cash(force=False):
    """Public entry: cached-or-fresh real cash, or None if unavailable. Never raises.
    Premium econ (the floor cabin) refreshes DAILY for anomaly/pounce detection; business+first
    every FULL_EVERY days to conserve the SerpApi free-tier quota. Maintains a premium price
    history and flags anomalously-cheap days."""
    try:
        key = get_key()
        if not key:
            return None
        prev = _state() or {}
        today = date.today().isoformat()
        prev_cabins = dict(prev.get("cabins") or {})
        fetch_prem = force or _days_since(prev.get("premAsOf") or "") >= 3   # every 3 days (trip conditional since 7/02; frees quota for the Aug hub cash port)
        fetch_full = force or prev.get("fullAsOf") is None or _days_since(prev.get("fullAsOf")) >= FULL_EVERY
        want = (["premium"] if fetch_prem else []) + (["business", "first"] if fetch_full else [])
        if not want:
            return prev or None                   # already current for today
        cabins, searches, errs = fetch(key, want)
        merged = dict(prev_cabins); merged.update(cabins)
        if not merged:
            return prev or None                   # failed refresh with no prior -> nothing
        hist = [h for h in (prev.get("priceHistory") or []) if h.get("date") != today]
        pdd = (merged.get("premium") or {}).get("doorToDoorTotal")
        if pdd:
            hist.append({"date": today, "premium": pdd})
            hist = hist[-90:]
        out = {"source": "SerpApi Google Flights", "asOf": today,
               "premAsOf": today if ("premium" in cabins) else prev.get("premAsOf"),
               "fullAsOf": today if any(c in cabins for c in ("business", "first")) else prev.get("fullAsOf"),
               "dates": f"{CASH_DATES[0]} to {CASH_DATES[1]}", "pax": PAX,
               "searches": searches, "errors": errs, "cabins": merged,
               "priceHistory": hist, "anomaly": _anomaly(hist, pdd),
               "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        with open(CACHE, "w") as f:
            json.dump(out, f, indent=2)
        return out
    except Exception:
        return _state()

MOCK = {"best_flights": [{"price": 4180, "total_duration": 880, "flights": [
    {"departure_airport": {"id": "CHS"}, "arrival_airport": {"id": "MIA"}, "airline": "American"},
    {"departure_airport": {"id": "MIA"}, "arrival_airport": {"id": "GRU"}, "airline": "American"}],
    "layovers": [{"duration": 205, "name": "Miami", "id": "MIA"}]},
  {"price": 3990, "total_duration": 2200, "flights": [
    {"departure_airport": {"id": "CHS"}, "arrival_airport": {"id": "JFK"}, "airline": "Delta"},
    {"departure_airport": {"id": "JFK"}, "arrival_airport": {"id": "GRU"}, "airline": "Delta"}],
    "layovers": [{"duration": 70, "name": "JFK", "id": "JFK"}]}], "other_flights": []}

if __name__ == "__main__":
    if "--mock" in sys.argv:
        opts = parse_itineraries(MOCK)
        print("parsed", len(opts), "itineraries (cheapest-compliant first):")
        for o in opts:
            print(f"  ${o['priceRaw']} | {o['path']} | {o['stops']} stop | layovers {o['layovers']} | OK={o['layoverOK']} | {o['carriers']}")
        comp = [o for o in opts if o["layoverOK"]]
        print("=> picked:", comp[0]["path"], f"${comp[0]['priceRaw']}" if comp else "none")
    else:
        r = best_cash(force="--force" in sys.argv)
        if r is None:
            print("no cash data (no serpapi_key in secrets, or first run without --force)")
        else:
            print(json.dumps({k: v for k, v in r.items() if k != "cabins"}, indent=2))
            for cab, v in r.get("cabins", {}).items():
                print(f"  {cab}: {v['path']} ${v.get('totalForPax')} (door-to-door ${v.get('doorToDoorTotal')}) layoverOK={v['layoverOK']}")
