"""Daily updater for the NBA props app.

Pulls finished NBA box scores from ESPN's free public JSON, stores player game
logs in data/games.json, then writes data/app.json (what the web page reads).
Run from the repo root:  python scripts/update.py
"""
import json
import os
import sys
import time
import datetime as dt
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
SEASON_START = dt.date(2026, 10, 20)
GAMES_FILE = "data/games.json"
APP_FILE = "data/app.json"
STATS = ["pts", "reb", "ast"]
DECAY = 0.88        # newer games count more
MIN_MINUTES = 8     # ignore blowout garbage-time / DNP-ish lines
OPP_DAMPEN = 0.5    # how much the opponent factor is allowed to move a number


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def num(v):
    try:
        return float(str(v).split(":")[0])
    except Exception:
        return 0.0


def parse_box(summary):
    """ESPN summary JSON -> list of player lines."""
    rows = []
    for t in summary.get("boxscore", {}).get("players", []):
        team = t.get("team", {}).get("abbreviation", "")
        for grp in t.get("statistics", []):
            names = grp.get("names") or grp.get("labels") or []
            for a in grp.get("athletes", []):
                if a.get("didNotPlay"):
                    continue
                stats = a.get("stats") or []
                if not stats:
                    continue
                d = dict(zip(names, stats))
                ath = a.get("athlete", {})
                rows.append({
                    "id": str(ath.get("id", "")),
                    "name": ath.get("displayName", "?"),
                    "pos": (ath.get("position") or {}).get("abbreviation", ""),
                    "team": team,
                    "min": num(d.get("MIN", 0)),
                    "pts": int(num(d.get("PTS", 0))),
                    "reb": int(num(d.get("REB", 0))),
                    "ast": int(num(d.get("AST", 0))),
                })
    return rows


def scoreboard(day):
    return get(f"{BASE}/scoreboard?dates={day.strftime('%Y%m%d')}&limit=100").get("events", [])


def teams_of(ev):
    comp = ev["competitions"][0]
    out = {}
    for c in comp["competitors"]:
        out[c["homeAway"]] = c["team"]["abbreviation"]
    return out


def is_regular(ev):
    t = (ev.get("season") or {}).get("type")
    return t in (None, 2)


def fetch_new_games(games, today):
    known = {g["id"] for g in games["games"]}
    last = dt.date.fromisoformat(games["last_date"]) if games.get("last_date") else SEASON_START
    day = max(SEASON_START, last - dt.timedelta(days=1))
    while day < today:
        for ev in scoreboard(day):
            if not is_regular(ev) or ev["id"] in known:
                continue
            if not ev["status"]["type"].get("completed"):
                continue
            try:
                players = parse_box(get(f"{BASE}/summary?event={ev['id']}"))
            except Exception as e:
                print("skip game", ev["id"], e)
                continue
            if not players:
                continue
            tm = teams_of(ev)
            games["games"].append({"id": ev["id"], "date": day.isoformat(),
                                   "home": tm.get("home"), "away": tm.get("away"),
                                   "players": players})
            known.add(ev["id"])
        day += dt.timedelta(days=1)
    games["last_date"] = (today - dt.timedelta(days=1)).isoformat()
    return games


def todays_schedule(today):
    sched = []
    for ev in scoreboard(today):
        if not is_regular(ev):
            continue
        tm = teams_of(ev)
        sched.append({"home": tm.get("home"), "away": tm.get("away"),
                      "time": ev.get("date"), "state": ev["status"]["type"].get("state")})
    return sched


def opp_factors(games):
    """Per team: how much each stat the team allows vs league average (1.0 = average)."""
    allowed = defaultdict(lambda: defaultdict(float))
    n = defaultdict(int)
    for g in games:
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            for s in STATS:
                allowed[team][s] += sum(p[s] for p in g["players"] if p["team"] == opp)
            n[team] += 1
    league = {s: (sum(allowed[t][s] for t in allowed) / max(1, sum(n.values()))) for s in STATS}
    out = {}
    for t in allowed:
        if n[t] < 3:
            out[t] = {s: 1.0 for s in STATS}
        else:
            out[t] = {s: (allowed[t][s] / n[t]) / league[s] if league[s] else 1.0 for s in STATS}
    return out


def combos(d):
    return {"pts": d["pts"], "reb": d["reb"], "ast": d["ast"],
            "pr": d["pts"] + d["reb"], "pa": d["pts"] + d["ast"],
            "ra": d["reb"] + d["ast"], "pra": d["pts"] + d["reb"] + d["ast"]}


def avg(rows, k):
    return sum(r[k] for r in rows) / len(rows) if rows else 0.0


def r1(d):
    return {k: round(v, 1) for k, v in d.items()}


def build_app(games, schedule, today):
    logs = defaultdict(list)
    for g in sorted(games, key=lambda x: x["date"], reverse=True):
        for p in g["players"]:
            if p["min"] < MIN_MINUTES:
                continue
            opp = g["away"] if p["team"] == g["home"] else g["home"]
            logs[p["id"]].append({**p, "date": g["date"], "opp": opp,
                                  "home": p["team"] == g["home"]})
    factors = opp_factors(games)
    today_map = {}
    for s in schedule:
        today_map[s["home"]] = (s["away"], True)
        today_map[s["away"]] = (s["home"], False)

    players = []
    for pid, rows in logs.items():  # rows are newest first
        if len(rows) < 3:
            continue
        newest = rows[0]
        if (today - dt.date.fromisoformat(newest["date"])).days > 21:
            continue
        use = rows[:10]
        w = [DECAY ** i for i in range(len(use))]
        wsum = sum(w)
        base = {s: sum(r[s] * wi for r, wi in zip(use, w)) / wsum for s in STATS}
        proj_min = sum(r["min"] * wi for r, wi in zip(use, w)) / wsum
        team = newest["team"]
        opp, home = today_map.get(team, (None, None))
        if opp and opp in factors:
            for s in STATS:
                f = factors[opp][s]
                base[s] *= 1 + (f - 1) * OPP_DAMPEN
        players.append({
            "id": pid, "name": newest["name"], "pos": newest["pos"], "team": team,
            "gp": len(rows), "min": round(proj_min, 1),
            "opp": opp, "home": home,
            "proj": r1(combos(base)),
            "l5": r1(combos({s: avg(rows[:5], s) for s in STATS})),
            "l10": r1(combos({s: avg(rows[:10], s) for s in STATS})),
            "szn": r1(combos({s: avg(rows, s) for s in STATS})),
            "log": [{"d": r["date"][5:], "opp": r["opp"], "h": r["home"], "min": round(r["min"]),
                     "pts": r["pts"], "reb": r["reb"], "ast": r["ast"]} for r in rows[:10]],
        })
    players.sort(key=lambda p: -p["proj"]["pra"])
    return {"updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
            "date": today.isoformat(), "games_today": schedule, "players": players}


def main():
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    os.makedirs("data", exist_ok=True)
    games = {"last_date": None, "games": []}
    if os.path.exists(GAMES_FILE):
        with open(GAMES_FILE) as f:
            games = json.load(f)
    games = fetch_new_games(games, today)
    try:
        schedule = todays_schedule(today)
    except Exception as e:
        print("no schedule", e)
        schedule = []
    app = build_app(games["games"], schedule, today)
    with open(GAMES_FILE, "w") as f:
        json.dump(games, f, separators=(",", ":"))
    with open(APP_FILE, "w") as f:
        json.dump(app, f, separators=(",", ":"))
    print(f"{len(games['games'])} games stored, {len(app['players'])} players, "
          f"{len(schedule)} games today")
    if not games["games"]:
        print("WARNING: no games found. Check the season start date or ESPN access.")
        sys.exit(1)


if __name__ == "__main__":
    main()
