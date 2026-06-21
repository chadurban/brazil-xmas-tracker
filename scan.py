#!/usr/bin/env python3
"""
Brazil Christmas 2026 award/cash scanner.

Pulls seats.aero Partner API across many mileage programs + cabins, both directions,
filters to US-hub <-> Brazil-gateway (GRU/GIG) routings in the Dec 17 - Jan 4 window,
vets connection layovers (2.5-6h, same-airport only) via the trips endpoint, and writes
data.json consumed by index.html.

Run:  python3 scan.py            # pulls live, writes data.json
      python3 scan.py --dry      # pulls live, prints summary, no write

Constraints (Chad's prefs):
  - Trip length 7-15 nights, depart Dec 17-26 2026, return Dec 26 - Jan 4 2027
  - Open-jaw OK (arrive GIG, depart GRU, etc.); US hub agnostic; one-ways fine
  - Layover 150-360 min, same-airport connections only (no LGA->JFK silliness)
"""
import json, sys, os, time, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = "/Users/admin/Library/CloudStorage/GoogleDrive-urbanc@acba.edu/.shortcut-targets-by-id/1Tc-st1PSSbOMdWmS2DRk_5DLXRb8WpFb/ATS1/Operations/Claude/Personal/.flight-scanner-secrets.json"
KEY = json.load(open(SECRETS))["seats_aero_partner_authorization"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HDR = {"Partner-Authorization": KEY, "Accept": "application/json", "User-Agent": UA}

# --- Trip config ---
GATEWAYS = {"GRU", "GIG"}
US_HUBS = {"CHS","ATL","JFK","EWR","IAD","IAH","MIA","ORD","MCO","BOS","CLT","DFW","LAX","FLL","PHL","DTW","SFO"}
OUT_START, OUT_END = "2026-12-17", "2026-12-27"   # outbound depart window (+buffer)
RET_START, RET_END = "2026-12-24", "2027-01-04"   # return depart window
LAY_MIN, LAY_MAX = 150, 360                        # minutes
CABINS = ["business", "premium"]   # economy is below Chad's premium-econ floor; dropped to conserve the 1000/day seats.aero quota
# seats.aero source program codes relevant to US<->Brazil
PROGRAMS = ["aeroplan","united","delta","aeromexico","american","alaska",
            "virginatlantic","flyingblue","lifemiles","smiles","azul"]
VIX_PROGRAMS = ["smiles","azul"]
# which of Chad's currencies can book each source's space (display hint)
BOOK_VIA = {
    "aeroplan":"UR/MR → Aeroplan","united":"UR → United","delta":"UR → Virgin Atlantic (Delta metal)",
    "aeromexico":"UR → Flying Blue / Virgin","american":"AA miles (not UR/MR)","alaska":"Alaska miles (not UR/MR)",
    "virginatlantic":"UR/MR → Virgin Atlantic","flyingblue":"UR/MR → Flying Blue",
    "lifemiles":"Citi/Cap One → LifeMiles (not UR/MR)","smiles":"GOL Smiles","azul":"Azul / UR→United"}
CABKEY = {"business":"J","premium":"W","economy":"Y","first":"F"}

# Booking ease from Chad's points (Chase UR + Amex MR). Lower tier = simpler to book.
# tier 1 = one direct 1:1 UR/MR transfer, book on that program's site.
# tier 2 = reachable but via a SkyTeam/partner nuance.
# tier 3 = NOT reachable from UR/MR (needs miles he doesn't hold) -> avoid.
BOOK_EASE = {"united":1,"aeroplan":1,"virginatlantic":1,"flyingblue":1,"delta":1,
             "aeromexico":2,"smiles":2,"azul":2,"american":3,"alaska":3,"lifemiles":3}
STOP_PENALTY = 25000                       # "miles" an extra stop is worth avoiding
TIER_PENALTY = {1:0, 2:30000, 3:400000}    # ease-of-booking penalty by tier

def row_stops(r):
    if r.get("direct"): return 0
    rt = r.get("routing")
    return rt.get("stops", 1) if rt else 1

def eff_cost(r):
    """Ease-weighted effective cost: real miles + stop penalty + booking-ease penalty."""
    return int(r.get("miles") or 9e9) + row_stops(r)*STOP_PENALTY + TIER_PENALTY.get(BOOK_EASE.get(r["source"],2),30000)

def bookable(r):
    return BOOK_EASE.get(r["source"], 2) <= 2

# --- True door-to-door cost model (CHS <-> VIX) ---
PAX = 3
HOME = "CHS"
# estimated one-way cash to position CHS <-> US hub (economy, per person, USD). Refine with a real fare source.
POSITION_COST = {"ATL":110,"CLT":90,"IAD":110,"MCO":110,"PHL":120,"FLL":120,"MIA":130,
                 "JFK":150,"EWR":150,"ORD":150,"DTW":150,"BOS":160,"IAH":170,"DFW":170,"LAX":230,"SFO":250}
VIX_HOP_CASH = 80   # one-way gateway <-> VIX (GOL/Azul/LATAM economy, per person, USD)

def leg_extra(direction, o, d):
    """Per-person cash beyond the long-haul: CHS<->hub positioning (if not already home) + the VIX hop."""
    if direction == "out":   # US hub -> gateway
        pos = 0 if o == HOME else POSITION_COST.get(o, 150)
        return pos + VIX_HOP_CASH, {"positioning":pos, "posLeg":(None if o==HOME else f"CHS-{o}"),
                                    "vixHop":VIX_HOP_CASH, "vixLeg":f"{d}-VIX"}
    else:                    # gateway -> US hub
        pos = 0 if d == HOME else POSITION_COST.get(d, 150)
        return pos + VIX_HOP_CASH, {"positioning":pos, "posLeg":(None if d==HOME else f"{d}-CHS"),
                                    "vixHop":VIX_HOP_CASH, "vixLeg":f"VIX-{o}"}

def _full_path_out(o):
    core = o["routing"]["path"] if o.get("routing") else f'{o["o"]}-{o["d"]}'
    return (("" if o["o"]==HOME else "CHS-") + core + "-VIX")

def _full_path_ret(r):
    core = r["routing"]["path"] if r.get("routing") else f'{r["o"]}-{r["d"]}'
    return ("VIX-" + core + ("" if r["d"]==HOME else "-CHS"))

errors = []
REMAINING = None  # X-RateLimit-Remaining; seats.aero Pro cap is 1000 calls/day, resets 00:00 UTC

def quota_low():
    return REMAINING is not None and 0 <= REMAINING < 40

def api(path, params=None):
    global REMAINING
    url = f"https://seats.aero/partnerapi/{path}"
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HDR)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem is not None and rem.lstrip("-").isdigit(): REMAINING = int(rem)
                return json.loads(r.read().decode("utf-8","replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                REMAINING = 0; errors.append(f"{path} HTTP 429 (daily quota exhausted)"); return None
            if e.code in (500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt+1)); continue
            errors.append(f"{path} HTTP {e.code}"); return None
        except Exception as ex:
            if attempt < 2: time.sleep(1.5); continue
            errors.append(f"{path} {type(ex).__name__}"); return None

def bulk(source, cabin, o_region, d_region, start, end):
    """Paginated bulk availability."""
    out, cursor = [], None
    for _ in range(6):
        p = {"source":source,"cabin":cabin,"start_date":start,"end_date":end,
             "origin_region":o_region,"destination_region":d_region,"take":1000}
        if cursor: p["cursor"] = cursor
        d = api("availability", p)
        if not d or not isinstance(d, dict): break
        out += d.get("data", [])
        if not d.get("hasMore"): break
        cursor = d.get("cursor")
        if not cursor: break
    return out

def parse_dt(s):
    try: return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None

def vet_trip_layovers(segs):
    """Return (ok, layovers_min[], same_airport_ok) for a segment list."""
    lays, same_ok = [], True
    for i in range(len(segs)-1):
        a_arr = parse_dt(segs[i].get("ArrivesAt")); b_dep = parse_dt(segs[i+1].get("DepartsAt"))
        if segs[i].get("DestinationAirport") != segs[i+1].get("OriginAirport"):
            same_ok = False
        if a_arr and b_dep:
            lays.append(int((b_dep - a_arr).total_seconds() // 60))
    ok = same_ok and all(LAY_MIN <= L <= LAY_MAX for L in lays)
    return ok, lays, same_ok

def best_trip(avail_id, cabin):
    """Fetch trips for an availability id; return best routing dict for the cabin or None."""
    if quota_low(): return None
    d = api(f"trips/{avail_id}")
    trips = (d.get("data") if isinstance(d, dict) else d) or []
    cands = [t for t in trips if (t.get("Cabin") or "").lower() == cabin]
    if not cands:
        return None
    # prefer: layover-compliant, then fewest stops, then lowest mileage
    scored = []
    for t in cands:
        segs = t.get("AvailabilitySegments") or []
        ok, lays, same_ok = vet_trip_layovers(segs)
        scored.append((not ok, t.get("Stops", len(segs)-1), t.get("MileageCost", 1e9), t, segs, lays, ok))
    scored.sort(key=lambda x: (x[1], x[0], x[2]))  # fewest stops first, then compliant, then miles
    _, _, _, t, segs, lays, ok = scored[0]
    route = [{"from":s.get("OriginAirport"),"to":s.get("DestinationAirport"),
              "flt":s.get("FlightNumber"),"dep":s.get("DepartsAt"),"arr":s.get("ArrivesAt"),
              "ac":s.get("AircraftName")} for s in segs]
    for i,L in enumerate(lays):
        if i+1 < len(route): route[i+1]["layoverMin"] = L
    return {"stops":t.get("Stops", len(segs)-1),"miles":t.get("MileageCost"),
            "taxes":t.get("TotalTaxes"),"taxCur":t.get("TaxesCurrencySymbol") or t.get("TaxesCurrency"),
            "durationMin":t.get("TotalDuration"),"carriers":t.get("Carriers"),
            "route":route,"layovers":lays,"layoverOK":ok,
            "path":"-".join([route[0]["from"]] + [r["to"] for r in route]) if route else ""}

def collect(direction, prev_rows=None):
    """direction: 'out' (US->BR) or 'ret' (BR->US). Reuses prev_rows per-source when the daily quota is exhausted."""
    if direction == "out":
        o_reg, d_reg, start, end = "North America", "South America", OUT_START, OUT_END
        org_ok = lambda o: o in US_HUBS; dst_ok = lambda d: d in GATEWAYS
    else:
        o_reg, d_reg, start, end = "South America", "North America", RET_START, RET_END
        org_ok = lambda o: o in GATEWAYS; dst_ok = lambda d: d in US_HUBS
    prev_by_src = {}
    for r in (prev_rows or []):
        prev_by_src.setdefault(r.get("source"), []).append(r)
    found = []
    for source in PROGRAMS:
        if quota_low():
            reuse = [dict(r, cached=True) for r in prev_by_src.get(source, [])]
            if reuse:
                found += reuse
                errors.append(f"{source} {direction}: quota low → reused {len(reuse)} cached")
            continue
        for cabin in CABINS:
            ck = CABKEY[cabin]
            recs = bulk(source, cabin, o_reg, d_reg, start, end)
            ded = {}
            for r in recs:
                rt = r.get("Route", {})
                o, d = rt.get("OriginAirport"), rt.get("DestinationAirport")
                if not (org_ok(o) and dst_ok(d)): continue
                if not r.get(f"{ck}Available"): continue
                seats = r.get(f"{ck}RemainingSeats") or 0
                if seats < 1: continue  # drop phantom/0-seat cache rows
                row = {"id":r.get("ID"),"source":source,"cabin":cabin,"o":o,"d":d,
                    "date":r.get("Date"),"miles":r.get(f"{ck}MileageCost"),
                    "seats":seats,"direct":r.get(f"{ck}Direct"),"airlines":r.get(f"{ck}Airlines")}
                # dedupe by route+date+price, keep the one with the most seats
                key = (o, d, r.get("Date"), r.get(f"{ck}MileageCost"))
                if key not in ded or seats > ded[key]["seats"]:
                    ded[key] = row
            kept = sorted(ded.values(), key=lambda x: int(x["miles"] or 1e9))
            for k in kept[:3]:
                if k["direct"]:
                    k["routing"] = None  # nonstop, no layover concern
                else:
                    k["routing"] = best_trip(k["id"], cabin)
                k["extraPP"], k["extra"] = leg_extra(direction, k["o"], k["d"])
                k["bookEase"] = BOOK_EASE.get(source, 2)
                k["stops"] = row_stops(k)
                if k["stops"] > MAX_STOPS:
                    continue   # drop multi-stop long-hauls — prefer one long international flight
                found.append(k)
    return found

def collect_vix(prev=None):
    ded = {}
    for source in VIX_PROGRAMS:
        if quota_low(): continue
        recs = bulk(source, "economy", "South America", "South America", OUT_START, RET_END)
        for r in recs:
            rt = r.get("Route", {})
            o, d = rt.get("OriginAirport"), rt.get("DestinationAirport")
            seats = r.get("YRemainingSeats") or 0
            if o in GATEWAYS and d == "VIX" and r.get("YAvailable") and seats >= 1:
                key = (o, r.get("Date"), r.get("YMileageCost"))
                row = {"source":source,"leg":f"{o}-VIX","date":r.get("Date"),
                       "yMiles":r.get("YMileageCost"),"ySeats":seats}
                if key not in ded or seats > ded[key]["ySeats"]:
                    ded[key] = row
    out = sorted(ded.values(), key=lambda x: int(x["yMiles"] or 1e9))
    if not out and prev:                       # all sources quota-skipped → reuse cached VIX
        return [dict(r, cached=True) for r in prev]
    return out

def _best_leg(rows, cab):
    c = [r for r in rows if r["cabin"] == cab and (r["seats"] or 0) >= 3 and bookable(r)
         and (r["direct"] or (r.get("routing") and r["routing"].get("layoverOK")))]
    c.sort(key=lambda x: (eff_cost(x), -(x["seats"] or 0)))  # ease-weighted (booking+stops), then most seats
    return c[0] if c else None

def _path(r):
    if r.get("routing") and r["routing"].get("path"):
        return r["routing"]["path"]
    return f'{r["o"]}-{r["d"]}'

MIN_NIGHTS, MAX_NIGHTS = 7, 15
MAX_STOPS = 1   # max stops on the international long-haul (book short positioning/VIX legs separately)

def _nights(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:
        return -1

def _valid_pair(outb, retb, cab):
    """Cheapest ease-ranked (outbound, return) pair whose trip length is MIN_NIGHTS..MAX_NIGHTS nights."""
    def legs(rows):
        return [r for r in rows if r["cabin"] == cab and (r["seats"] or 0) >= 3 and bookable(r)
                and row_stops(r) <= MAX_STOPS
                and (r["direct"] or (r.get("routing") and r["routing"].get("layoverOK")))]
    outs, rets = legs(outb), legs(retb)
    best = None
    for o in outs:
        for r in rets:
            n = _nights(o["date"], r["date"])
            if n < MIN_NIGHTS or n > MAX_NIGHTS:
                continue
            cost = eff_cost(o) + eff_cost(r)
            if best is None or cost < best[0]:
                best = (cost, o, r, n)
    return best  # (cost, o, r, nights) or None

def _lowest_pair(outb, retb, cab):
    """Cheapest-by-miles valid-length pair, relaxing seats(>=1)/bookability/compliance — splurge reference."""
    def legs(rows):
        return [r for r in rows if r["cabin"] == cab and (r["seats"] or 0) >= 1 and row_stops(r) <= MAX_STOPS and (r["direct"] or r.get("routing"))]
    outs, rets = legs(outb), legs(retb)
    best = None
    for o in outs:
        for r in rets:
            n = _nights(o["date"], r["date"])
            if n < MIN_NIGHTS or n > MAX_NIGHTS:
                continue
            m = int(o["miles"] or 9e9) + int(r["miles"] or 9e9)
            if best is None or m < best[0]:
                best = (m, o, r, n)
    return best

def _caveats(o, r):
    out = []
    for tag, x in [("out", o), ("back", r)]:
        if (x["seats"] or 0) < 3: out.append(f"{tag} {x['seats'] or 0} seat")
        if not bookable(x): out.append(f"{tag} needs {x['bookVia']}")
        if not (x["direct"] or (x.get("routing") and x["routing"].get("layoverOK"))): out.append(f"{tag} layover off")
    return out

def build_alerts(outb, retb, vix):
    try:
        prior = json.load(open(os.path.join(HERE, "data.json"))).get("alerts", [])
    except Exception:
        prior = []
    pp = _valid_pair(outb, retb, "premium")
    bp = _valid_pair(outb, retb, "business")
    p = [f"🟢 Scan v0.4 — <b>{len(outb)}</b> outbound / <b>{len(retb)}</b> return award options (US hubs ↔ GRU/GIG, Dec 17–Jan 4)."]
    if pp:
        _, po, pr, pn = pp
        tot = (int(po["miles"]) + int(pr["miles"])) * 3
        p.append(f" <b>Best premium-econ RT (3 pax, ≥3 seats, layover-OK, {pn}-night):</b> {_path(po)} {po['date']} → {_path(pr)} {pr['date']} = <b>{tot:,} mi</b> ({po['bookVia']} / {pr['bookVia']}).")
    if bp:
        _, bo, br, bn = bp
        p.append(f" Business {bn}-night 3-seat combo also live: {(int(bo['miles'])+int(br['miles']))*3:,} mi.")
    else:
        p.append(" Business saver for 3 (valid length) still scarce (Christmas peak) — hunting daily.")
    if vix:
        p.append(f" VIX hop: {vix[0]['leg']} ~{int(vix[0]['yMiles']):,} GOL Smiles or ~$60 cash.")
    new = {"type": "green", "message": "".join(p), "time": datetime.now().strftime("%Y-%m-%d %I:%M %p")}
    return [new] + prior[:6]

def build_best_option(outb, retb):
    """Per cabin: the ideal valid pair (>=3 seats, bookable, compliant); else the lowest-priced valid-length pair (splurge ref)."""
    res = {}
    for cab in ["premium", "business"]:
        bp, tier = _valid_pair(outb, retb, cab), "ideal"
        if not bp:
            bp, tier = _lowest_pair(outb, retb, cab), "lowest"
        if bp:
            _, o, r, n = bp
            res[cab] = {
                "tier": tier, "caveats": _caveats(o, r) if tier == "lowest" else [],
                "outPath": _full_path_out(o), "outLong": _path(o), "outDate": o["date"],
                "outMiles": int(o["miles"]), "outSeats": o["seats"], "outVia": o["bookVia"], "outExtra": o["extra"],
                "retPath": _full_path_ret(r), "retLong": _path(r), "retDate": r["date"],
                "retMiles": int(r["miles"]), "retSeats": r["seats"], "retVia": r["bookVia"], "retExtra": r["extra"],
                "totalMiles": (int(o["miles"]) + int(r["miles"])) * PAX,
                "totalExtraCash": (o["extraPP"] + r["extraPP"]) * PAX, "pax": PAX, "nights": n,
            }
    return res

def history_snapshot(best_opt, best_cash, outb, retb):
    bc = (best_cash or {}).get("cabins", {})
    return {"date": date.today().isoformat(),
            "premMiles": (best_opt.get("premium") or {}).get("totalMiles"),
            "bizMiles": (best_opt.get("business") or {}).get("totalMiles"),
            "premCash": (bc.get("premium") or {}).get("doorToDoorTotal"),
            "bizCash": (bc.get("business") or {}).get("doorToDoorTotal"),
            "outN": len(outb), "retN": len(retb)}

def update_history(snap):
    path = os.path.join(HERE, "history.json")
    try: hist = json.load(open(path))
    except Exception: hist = []
    hist = [h for h in hist if h.get("date") != snap["date"]]   # one entry per day
    hist.append(snap)
    hist = hist[-150:]
    with open(path, "w") as f: json.dump(hist, f, indent=2)
    return hist

def inject_into_html(data, hist):
    import re
    path = os.path.join(HERE, "index.html")
    html = open(path).read()
    for bid, payload in [("live-data", json.dumps(data, separators=(",", ":"))),
                         ("history-data", json.dumps(hist, separators=(",", ":")))]:
        pat = re.compile(r'(<script id="' + re.escape(bid) + r'" type="application/json">).*?(</script>)', re.DOTALL)
        if pat.search(html):
            html = pat.sub(lambda m: m.group(1) + payload + m.group(2), html, count=1)
    with open(path, "w") as f: f.write(html)

def main():
    dry = "--dry" in sys.argv
    t0 = time.time()
    try:
        _prev = json.load(open(os.path.join(HERE, "data.json")))
    except Exception:
        _prev = {}
    outb = collect("out", _prev.get("outbound"))
    retb = collect("ret", _prev.get("return"))
    vix = collect_vix(_prev.get("vix"))
    for row in outb + retb:
        row["bookVia"] = BOOK_VIA.get(row["source"], row["source"])
    alerts = [] if dry else build_alerts(outb, retb, vix)
    best_cash = None
    if not dry:
        try:
            import cash as _cash
            best_cash = _cash.best_cash()   # real SerpApi cash if keyed+stale, else cached/None
        except Exception as e:
            errors.append(f"cash: {type(e).__name__}")
    best_opt = build_best_option(outb, retb)
    data = {
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scannerVersion": "0.4.0",
        "window": {"outDepart":"Dec 17-26","retDepart":"Dec 26 - Jan 4","nights":"7-15"},
        "counts": {"outbound":len(outb),"return":len(retb),"vix":len(vix)},
        "outbound": outb, "return": retb, "vix": vix,
        "bestOption": best_opt, "bestCash": best_cash,
        "alerts": alerts, "errors": errors, "scanSeconds": round(time.time()-t0,1),
    }
    if dry:
        print(json.dumps(data["counts"]), "errors:", errors, f"({data['scanSeconds']}s)")
        for label, rows in [("OUTBOUND US->BR", outb), ("RETURN BR->US", retb)]:
            print(f"\n=== {label}: {len(rows)} ===")
            for r in sorted(rows, key=lambda x:(x['cabin'],int(x['miles'] or 1e9)))[:18]:
                rt = r.get("routing")
                path = rt["path"] if rt else f"{r['o']}-{r['d']} (nonstop)"
                lay = ("lay " + "/".join(f"{l//60}h{l%60:02d}" for l in rt["layovers"]) + (" OK" if rt["layoverOK"] else " !!")) if rt and rt.get("layovers") else ""
                print(f"  {r['cabin']:8} {r['source']:13} {path:24} {r['date']} {str(r['miles']):>7}mi seats={r['seats']} [{r['airlines']}] {lay}")
        print(f"\n=== VIX legs: {len(vix)} ===")
        for v in vix[:8]:
            print(f"  {v['source']:7} {v['leg']} {v['date']} {v['yMiles']}mi seats={v['ySeats']}")
    else:
        snap = history_snapshot(best_opt, best_cash, outb, retb)
        hist = update_history(snap)
        data["history"] = hist
        with open(os.path.join(HERE, "data.json"), "w") as f:
            json.dump(data, f, indent=2)
        inject_into_html(data, hist)
        print(f"wrote data.json + injected in-page: {data['counts']}, history {len(hist)}d; errors={errors}")

if __name__ == "__main__":
    main()
