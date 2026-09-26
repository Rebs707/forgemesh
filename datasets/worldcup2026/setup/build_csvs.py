#!/usr/bin/env python3
"""Assemble bronze CSVs for worldcup2026 from revision-pinned Wikipedia snapshots.

Runs setup/parse_wikipedia_group.py over each snapshot in raw_snapshots/, merges
across snapshots, assigns surrogate keys (player_id and global match/goal/card ids),
and applies the locked representation rules (PRE_REGISTRATION.md §4):
  * minute 90+2 -> 92 (base+added)
  * position kept granular (faithful bronze)
  * goal_type taken from structure only
  * confederation + venue country_code backfilled from stable facts (data, not
    pipeline logic — these don't change)
  * fifa_ranking / capacity / kickoff_utc / date_of_birth left NULL

Usage:  python build_csvs.py [snapshot.json ...]   (default: all in ../raw_snapshots)
"""
from __future__ import annotations
import csv, re, sys, pathlib
from parse_wikipedia_group import parse_file, parse_knockout_file

HERE = pathlib.Path(__file__).resolve().parent
DATASET = HERE.parent
SNAP_DIR = DATASET / "raw_snapshots"
DATA_DIR = DATASET / "data"

# Stable-fact backfills (confederation membership / stadium host country don't change —
# safe to encode here; this is data, not pipeline logic).
#
# All 12 groups (2026-07-04): both maps were DERIVED, not hand-typed, from the
# revision-pinned snapshot raw_snapshots/maincup_20260704.json (English Wikipedia
# "2026 FIFA World Cup", revid 1362524513) — CONFED from the Teams-by-confederation
# list, VENUE_COUNTRY from the venues wikitable's per-row flag icon. Cross-checked to
# cover exactly the 48 team codes and 16 venue_ids the group snapshots produce.
CONFED = {
    "ALG": "CAF", "ARG": "CONMEBOL", "AUS": "AFC", "AUT": "UEFA", "BEL": "UEFA",
    "BIH": "UEFA", "BRA": "CONMEBOL", "CAN": "CONCACAF", "CIV": "CAF", "COD": "CAF",
    "COL": "CONMEBOL", "CPV": "CAF", "CRO": "UEFA", "CUW": "CONCACAF", "CZE": "UEFA",
    "ECU": "CONMEBOL", "EGY": "CAF", "ENG": "UEFA", "ESP": "UEFA", "FRA": "UEFA",
    "GER": "UEFA", "GHA": "CAF", "HAI": "CONCACAF", "IRN": "AFC", "IRQ": "AFC",
    "JOR": "AFC", "JPN": "AFC", "KOR": "AFC", "KSA": "AFC", "MAR": "CAF",
    "MEX": "CONCACAF", "NED": "UEFA", "NOR": "UEFA", "NZL": "OFC", "PAN": "CONCACAF",
    "PAR": "CONMEBOL", "POR": "UEFA", "QAT": "AFC", "RSA": "CAF", "SCO": "UEFA",
    "SEN": "CAF", "SUI": "UEFA", "SWE": "UEFA", "TUN": "CAF", "TUR": "UEFA",
    "URU": "CONMEBOL", "USA": "CONCACAF", "UZB": "AFC",
}
VENUE_COUNTRY = {
    "AKRON": "MEX", "AZTECA": "MEX", "BBVA": "MEX",                      # Mexico (3)
    "BCPLACE": "CAN", "BMO": "CAN",                                      # Canada (2)
    "ARROWHEA": "USA", "ATT": "USA", "GILLETTE": "USA", "HARDROCK": "USA",
    "LEVIS": "USA", "LINCOLNF": "USA", "LUMEN": "USA", "MERCEDES": "USA",
    "METLIFE": "USA", "NRG": "USA", "SOFI": "USA",                       # USA (11)
}


def coerce_minute(m):
    if m in (None, ""):
        return None
    return sum(int(x) for x in str(m).split("+"))


def group_letter(title: str | None):
    g = re.search(r"Group ([A-Z])", title or "")
    return g.group(1) if g else None


def main(argv):
    # Only per-group match snapshots feed the build. Other snapshots in raw_snapshots/
    # (e.g. maincup_*.json, the backfill source for CONFED/VENUE_COUNTRY) are not matches.
    snaps = [pathlib.Path(a) for a in argv] or sorted(SNAP_DIR.glob("group_*.json"))
    teams, venues, players = {}, {}, {}
    matches, goals, cards, appearances = [], [], [], []
    pid = {}                       # player_key -> player_id
    n = dict(pid=0, mid=0, gid=0, cid=0, aid=0)

    def get_pid(key, team, full=None):
        if key not in pid:
            n["pid"] += 1
            pid[key] = n["pid"]
            players[key] = dict(player_id=n["pid"], full_name=full or key, team_id=team,
                                date_of_birth=None)
        return pid[key]

    def gletter(title, name):
        g = group_letter(title)
        if g:
            return g
        m = re.search(r"group_([a-z])", name.lower())
        return m.group(1).upper() if m else name

    # Recurring collection drops a fresh dated snapshot per group each run. Use only
    # the most recent (highest revid) snapshot per group so the same matches are not
    # parsed twice and double-counted (surrogate keys are assigned per build).
    parsed = [(sp, parse_file(str(sp))) for sp in snaps]
    best = {}
    for sp, r in parsed:
        g = gletter(r["title"], sp.name)
        rev = r["revid"] or 0
        if g not in best or rev > best[g][1]:
            best[g] = (sp, rev, r)
    selected = {v[0] for v in best.values()}
    superseded = [sp.name for sp, _ in parsed if sp not in selected]
    if superseded:
        print(f"superseded snapshots skipped: {superseded}")

    # Combine group stores (ordered A..L) then knockout stores (round of 32 → knockout
    # page R16/QF/SF/third place → final), so global match_ids run 1..72 for the group
    # stage and 73..104 for the knockout stage. Knockout snapshots are auto-included only
    # in the default full build (no explicit snapshot args passed).
    ordered = [(r["store"], group_letter(r["title"]))
               for _sp, _rev, r in sorted(best.values(), key=lambda v: v[0].name)]
    if not argv:
        def _latest(pat):
            fs = sorted(SNAP_DIR.glob(pat))
            return fs[-1] if fs else None
        for f in filter(None, (_latest("roundof32_*.json"), _latest("knockout_*.json"),
                               _latest("final_*.json"))):
            print(f"knockout snapshot: {f.name}")
            ordered.append((parse_knockout_file(str(f))["store"], None))

    for st, gl in ordered:
        for code, t in st["teams"].items():
            tt = teams.setdefault(code, dict(team_id=code, name=t.get("name"),
                group_label=gl, confederation=CONFED.get(code), fifa_ranking=None))
            if not tt.get("name") and t.get("name"):
                tt["name"] = t["name"]
        for vid, v in st["venues"].items():
            venues.setdefault(vid, dict(venue_id=vid, name=v["name"], city=v["city"],
                country_code=VENUE_COUNTRY.get(vid), capacity=None))
        for key, p in st["players"].items():
            get_pid(key, p["team_id"], p["player"])
        local2global = {}
        for m in st["matches"]:
            n["mid"] += 1
            local2global[m["match_id"]] = n["mid"]
            mm = dict(m); mm["match_id"] = n["mid"]
            matches.append(mm)
        for g in st["goals"]:
            n["gid"] += 1
            goals.append(dict(goal_id=n["gid"], match_id=local2global[g["match_id"]],
                player_id=get_pid(g["player_key"], g["team_id"], g["player"]),
                team_id=g["team_id"], minute=coerce_minute(g["minute"]), goal_type=g["goal_type"]))
        for c in st["cards"]:
            n["cid"] += 1
            cards.append(dict(card_id=n["cid"], match_id=local2global[c["match_id"]],
                player_id=get_pid(c["player_key"], c["team_id"], c["player"]),
                team_id=c["team_id"], minute=coerce_minute(c["minute"]), card_type=c["card_type"]))
        for a in st["appearances"]:
            n["aid"] += 1
            appearances.append(dict(appearance_id=n["aid"], match_id=local2global[a["match_id"]],
                player_id=get_pid(a["player_key"], a["team_id"]), team_id=a["team_id"],
                position=a["position"], shirt_number=a["shirt_number"], is_starter=a["is_starter"]))

    DATA_DIR.mkdir(exist_ok=True)

    def write(name, rows, cols):
        with open(DATA_DIR / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for row in rows:
                w.writerow({c: ("" if row.get(c) is None else row.get(c)) for c in cols})
        print(f"  {name}.csv: {len(rows)} rows")

    print(f"snapshots: {[p.name for p in snaps]}")
    write("teams", list(teams.values()), ["team_id", "name", "group_label", "confederation", "fifa_ranking"])
    write("venues", list(venues.values()), ["venue_id", "name", "city", "country_code", "capacity"])
    write("players", list(players.values()), ["player_id", "full_name", "team_id", "date_of_birth"])
    write("matches", matches, ["match_id", "stage", "home_team_id", "away_team_id", "venue_id", "kickoff_utc", "home_score", "away_score", "outcome", "attendance", "referee"])
    write("goals", goals, ["goal_id", "match_id", "player_id", "team_id", "minute", "goal_type"])
    write("cards", cards, ["card_id", "match_id", "player_id", "team_id", "minute", "card_type"])
    write("appearances", appearances, ["appearance_id", "match_id", "player_id", "team_id", "position", "shirt_number", "is_starter"])


if __name__ == "__main__":
    main(sys.argv[1:])
