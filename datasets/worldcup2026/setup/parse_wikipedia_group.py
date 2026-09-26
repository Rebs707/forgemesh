#!/usr/bin/env python3
"""Deterministic parser for a 2026 FIFA World Cup *group* page's raw wikitext.

We collect from Wikipedia's raw wikitext (action=parse&prop=wikitext) — NOT a
summarising fetch — so the snapshot is a faithful, revision-pinned record. Match
results live in a collapsible match-box (|score=/|goals1=/|stadium=/|attendance=/
|referee=) followed by two-column lineup tables whose player rows carry inline
{{yel|m}} / {{sent off|first|m}} cards, formation position + shirt number, and (after
a '''Substitutions:''' marker) the substitutes.

What is reliably parseable from structure (nothing is invented):
  teams (FIFA code + name), venues (name/city), matches (score/outcome/attendance/
  referee), goals (scorer+minute; penalty/own-goal flagged when marked), cards
  (type+minute+team+match), players (identity), appearances (player x match: position,
  shirt, is_starter — the M:N junction).
Not in the structured source (left for a second source / build step): confederation,
fifa_ranking, venue capacity, kickoff time, player DOB, goal subtypes beyond pen/o.g.

Usage:  parse_wikipedia_group.py <snapshot.json>   # prints a verification report
Library: parse_file(path) -> {"store", "revid", "title", "played"}
"""
from __future__ import annotations
import json, re, sys, pathlib

DASH = r"[–\-]"

def strip_links(s: str) -> str:
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    return s.strip()

def link_target(s: str) -> str:
    m = re.match(r"\[\[([^\]|]+)", s)
    return m.group(1).strip() if m else s.strip()

def heading_chunks(wt: str):
    """Yield (title, body) for each '=== X vs Y ===' level-3 section."""
    heads = list(re.finditer(r"^={3}\s*(.+?)\s*={3}\s*$", wt, re.M))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(wt)
        yield h.group(1).strip(), wt[h.end():end]

GOAL_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")

def parse_goals(block: str, team: str, match_id: int, out: list, gid: list):
    # Goal lines vary: goals1 uses '*[[..]] 9'', goals2 sometimes drops the bullet.
    # Key off (first link = scorer) + (minute tokens); ignore bullet presence.
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        lk = GOAL_LINK.search(line)
        mins = re.findall(r"(\d+(?:\+\d+)?)'", line)
        if not lk or not mins:
            continue
        target = lk.group(1).strip()
        disp = (lk.group(2) or lk.group(1)).strip()
        gtype = "open_play"
        if re.search(r"pen\.?|penalty", line, re.I): gtype = "penalty"
        if re.search(r"o\.g\.|own goal", line, re.I): gtype = "own_goal"
        for mtok in mins:
            gid[0] += 1
            out.append(dict(goal_id=gid[0], match_id=match_id, player=disp,
                            player_key=target, team_id=team, minute=mtok, goal_type=gtype))

PLAYER_ROW = re.compile(
    r"^\|\s*([A-Z]{2,3})\s*\|\|\s*'''(\d+)'''\s*\|\|\s*(\[\[[^\]]+\]\])(.*)$")

def parse_lineup_half(text, team, match_id, players, cards, appearances, cid, aid):
    # Rows before the '''Substitutions:''' marker are the starting XI; rows after it
    # are substitutes who appeared. The manager row does not match PLAYER_ROW.
    is_starter = 1
    for line in text.splitlines():
        s = line.strip()
        if "Substitutions:" in s:
            is_starter = 0
            continue
        m = PLAYER_ROW.match(s)
        if not m:
            continue
        pos, shirt, namecell, rest = m.groups()
        key = link_target(namecell)
        disp = strip_links(namecell)
        players.setdefault(key, dict(player=disp, player_key=key, team_id=team))
        aid[0] += 1
        appearances.append(dict(appearance_id=aid[0], match_id=match_id, player_key=key,
                                team_id=team, position=pos, shirt_number=int(shirt),
                                is_starter=is_starter))
        for ym in re.findall(r"\{\{yel\|(\d+(?:\+\d+)?)\}\}", rest):
            cid[0] += 1
            cards.append(dict(card_id=cid[0], match_id=match_id, player=disp,
                              player_key=key, team_id=team, minute=ym, card_type="yellow"))
        for first, mm in re.findall(r"\{\{sent off\|(\d+)\|(\d+(?:\+\d+)?)\}\}", rest):
            cid[0] += 1
            ctype = "red" if first == "0" else "second_yellow"
            cards.append(dict(card_id=cid[0], match_id=match_id, player=disp,
                              player_key=key, team_id=team, minute=mm, card_type=ctype))

def parse_match(title: str, body: str, match_id: int, store: dict, stage: str = "group") -> bool:
    t1 = re.search(r"\{\{#invoke:flag\|fb-rt\|([A-Z]{3})\}\}", body)
    t2 = re.search(r"\|team2=\{\{#invoke:flag\|fb\|([A-Z]{3})\}\}", body)
    # score link usually has two params ({{score link|target|N–N}}) but milestone
    # matches carry a third alt-text param ({{score link|target|N–N|alt}}); allow any
    # trailing params after the score so those boxes still parse.
    sc = re.search(rf"\|score=\{{\{{score link\|[^|]*\|(\d+){DASH}(\d+)(?:\|[^}}]*)?\}}\}}", body)
    if not (t1 and t2 and sc):
        return False  # not a played match (future fixture / placeholder)
    home, away = t1.group(1), t2.group(1)
    hs, as_ = int(sc.group(1)), int(sc.group(2))
    outcome = "draw" if hs == as_ else ("home_win" if hs > as_ else "away_win")

    g1 = re.search(r"\|goals1=(.*?)\|goals2=", body, re.S)
    g2 = re.search(r"\|goals2=(.*?)\|(?:stadium|key|location)=", body, re.S)
    cid = store["_cid"]; gid = store["_gid"]; aid = store["_aid"]
    if g1: parse_goals(g1.group(1), home, match_id, store["goals"], gid)
    if g2: parse_goals(g2.group(1), away, match_id, store["goals"], gid)

    stad = re.search(r"\|stadium=(.+)", body)
    venue_name = city = ""
    if stad:
        parts = strip_links(stad.group(1)).split(",")
        venue_name = parts[0].strip()
        city = parts[1].strip() if len(parts) > 1 else ""
    _stop = {"ESTADIO", "STADIUM", "ARENA", "STADE", "STADIO", "FIELD", "PARK"}
    _words = [w for w in re.sub(r"[^A-Za-z ]", "", venue_name).upper().split() if w not in _stop]
    venue_id = "".join(_words)[:8] or re.sub(r"[^A-Z]", "", venue_name.upper())[:8] or f"V{match_id:02d}"
    att = re.search(r"\|attendance=\s*([\d,]+)", body)
    attendance = int(att.group(1).replace(",", "")) if att else None
    ref = re.search(r"\|referee=\s*(\[\[[^\]]+\]\])", body)
    referee = strip_links(ref.group(1)) if ref else None

    store["venues"].setdefault(venue_id, dict(venue_id=venue_id, name=venue_name,
                                              city=city, country_code=None, capacity=None))
    names = [s.strip() for s in title.split(" vs ")] if title and " vs " in title else []
    for code, nm in ((home, names[0] if names else None),
                     (away, names[1] if len(names) > 1 else None)):
        t = store["teams"].setdefault(code, dict(team_id=code, name=None, group_label=None,
                                                 confederation=None, fifa_ranking=None))
        if nm and not t.get("name"):
            t["name"] = nm
    store["matches"].append(dict(match_id=match_id, stage=stage, home_team_id=home,
        away_team_id=away, venue_id=venue_id, kickoff_utc=None, home_score=hs,
        away_score=as_, outcome=outcome, attendance=attendance, referee=referee))

    # lineups: the {| width="100%" two-column table after the box. Each XI cell has
    # 11+ shirt numbers; the pitch-graphic divider cell has none.
    # tolerate spacing variants ('{|width', '{| width') seen across match/final pages
    _lu = re.search(r'\{\|\s*width="?100%"?', body)
    lu = _lu.start() if _lu else -1
    if lu != -1:
        halves = re.split(r'\|\s*valign="top"', body[lu:])
        with_players = [h for h in halves if len(re.findall(r"'''\d+'''", h)) >= 5]
        if len(with_players) >= 2:
            parse_lineup_half(with_players[0], home, match_id, store["players"],
                              store["cards"], store["appearances"], cid, aid)
            parse_lineup_half(with_players[-1], away, match_id, store["players"],
                              store["cards"], store["appearances"], cid, aid)
    return True

def parse_file(path: str) -> dict:
    data = json.loads(pathlib.Path(path).read_text())
    p = data["parse"]; wt = p["wikitext"]
    store = dict(teams={}, venues={}, matches=[], goals=[], cards=[], players={},
                 appearances=[], _cid=[0], _gid=[0], _aid=[0])
    match_id = 0; played = []
    for htitle, body in heading_chunks(wt):
        if " vs " not in htitle:
            continue
        match_id += 1
        if parse_match(htitle, body, match_id, store):
            played.append(htitle)
        else:
            match_id -= 1
    return dict(store=store, revid=p.get("revid"), title=p.get("title"), played=played)

# ── Knockout stage ──────────────────────────────────────────────────────────────
# Knockout data spans several pages: the round-of-32 page (matches under ==Matches==),
# the knockout-stage page (Round of 16 / Quarterfinals / Semifinals / Match for third
# place; the Final and R32 are transcluded there and skipped), and the final page (a
# single labelled box). Stage is read from the section/page context, never hard-coded.
# Penalty shoot-outs are described only in prose, which this parser deliberately does
# not read; a match still level after extra time therefore keeps its score-derived
# outcome ('draw'), recorded as a known limitation in PROVENANCE.md.
_KO_STAGE = {
    "round of 32": "round_of_32", "round of 16": "round_of_16",
    "quarterfinals": "quarter_final", "quarter-finals": "quarter_final",
    "semifinals": "semi_final", "semi-finals": "semi_final",
    "match for third place": "third_place", "third place": "third_place",
    "final": "final",
}

def _level2_sections(wt: str):
    heads = list(re.finditer(r"^==([^=].*?)==\s*$", wt, re.M))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(wt)
        yield h.group(1).strip(), wt[h.end():end]

def _labelled(wt: str, label: str) -> str:
    m = re.search(rf'<section begin="?{re.escape(label)}"?\s*/>(.*?)<section end="?{re.escape(label)}"?\s*/>',
                  wt, re.S)
    return m.group(1) if m else ""

def _new_store() -> dict:
    return dict(teams={}, venues={}, matches=[], goals=[], cards=[], players={},
                appearances=[], _cid=[0], _gid=[0], _aid=[0])

def parse_knockout_file(path: str) -> dict:
    data = json.loads(pathlib.Path(path).read_text())
    p = data["parse"]; wt = p["wikitext"]; title = (p.get("title") or "")
    store = _new_store(); match_id = 0; played = []
    tl = title.lower()
    page_stage = ("round_of_32" if "round of 32" in tl
                  else "final" if tl.endswith(" final") else None)

    def take(t_arg, body, stage):
        nonlocal match_id
        if "{{#lst:" in body:  # transcluded elsewhere (e.g. Final on the knockout page)
            return
        if not re.search(r"\|team1=\{\{#invoke:flag\|fb-rt\|[A-Z]{3}", body):
            return
        match_id += 1
        if parse_match(t_arg, body, match_id, store, stage=stage):
            played.append((stage, t_arg or f"[{stage}]"))
        else:
            match_id -= 1

    # The final page is a single labelled box (+ its line-ups) — parse it directly.
    if page_stage == "final":
        take(None, _labelled(wt, "Final") + "\n" + _labelled(wt, "Line-ups"), "final")
        return dict(store=store, revid=p.get("revid"), title=title, played=played)

    for header, sect in _level2_sections(wt):
        stage = _KO_STAGE.get(header.lower())
        if stage is None:
            stage = page_stage if header.lower() == "matches" else None
        if stage is None:
            continue
        subs = [(t, b) for t, b in heading_chunks(sect) if " vs " in t]
        if subs:
            for t, b in subs:
                take(t, b, stage)
        else:                                   # single-match section (third place)
            take(None, sect, stage)
    return dict(store=store, revid=p.get("revid"), title=title, played=played)

def main(path):
    r = parse_file(path); store = r["store"]
    print(f"# source: {r['title']}  revid={r['revid']}")
    print(f"# played matches: {len(r['played'])} -> {r['played']}\n")
    def show(name, rows, cols):
        print(f"== {name} ({len(rows)}) ==")
        print(" | ".join(cols))
        for row in rows:
            print(" | ".join(str(row.get(c, "")) for c in cols))
        print()
    show("teams", list(store["teams"].values()),
         ["team_id", "name", "group_label", "confederation", "fifa_ranking"])
    show("venues", list(store["venues"].values()),
         ["venue_id", "name", "city", "country_code", "capacity"])
    show("matches", store["matches"],
         ["match_id", "stage", "home_team_id", "away_team_id", "venue_id",
          "home_score", "away_score", "outcome", "attendance", "referee"])
    show("goals", store["goals"],
         ["goal_id", "match_id", "player", "team_id", "minute", "goal_type"])
    show("cards", store["cards"],
         ["card_id", "match_id", "player", "team_id", "minute", "card_type"])
    ap = store["appearances"]
    print(f"== players: {len(store['players'])}  |  appearances: {len(ap)} "
          f"(starters={sum(a['is_starter'] for a in ap)})")
    from collections import Counter
    print("   positions:", dict(Counter(a["position"] for a in ap)))

if __name__ == "__main__":
    main(sys.argv[1])
