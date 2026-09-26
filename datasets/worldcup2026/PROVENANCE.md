# World Cup 2026 — collection provenance

The value of this dataset is that it is **provably post-cutoff and self-collected
against a hand-designed schema**. This file is the audit trail. Keep it current with
every collection batch.

## Method

- **Source:** English Wikipedia per-group articles (`2026 FIFA World Cup Group <X>`),
  fetched as **raw wikitext** via the MediaWiki API (`action=parse&prop=wikitext|revid`).
  No summarising/LLM fetch is used — an early summarising fetch hallucinated a
  cross-match card, so all collection is deterministic parsing of the raw source.
- **Parser:** `setup/parse_wikipedia_group.py` → `setup/build_csvs.py` (deterministic).
- **Snapshots:** the verbatim API JSON (with its revision id) is saved under
  `raw_snapshots/` and is the canonical record; a decoded `.wikitext` sits alongside.
- **Collector:** Ben Tagger.
- **Backfills from stable facts** (not from the match source, and they do not change):
  team `confederation`, venue `country_code`. These are **derived deterministically**
  (not typed from memory) from a revision-pinned snapshot of the main article,
  `raw_snapshots/maincup_20260704.json` (`2026 FIFA World Cup`, revid 1362524513) —
  `confederation` from its Teams-by-confederation list, `country_code` from the venues
  wikitable's per-row flag icon — and cross-checked to cover exactly the team codes and
  venue ids the group snapshots produce. Everything else is parsed from the group snapshot.

## Batches

Collection is live and incremental (PRE_REGISTRATION.md §7): each group page is
re-fetched as the group stage progresses, and `setup/build_csvs.py` builds the CSVs
from the **highest-revid snapshot per group** — earlier snapshots are retained as the
audit record but superseded for the build. Every snapshot is a verbatim MediaWiki
`action=parse&prop=wikitext|revid` response.

| Snapshot | Group | Revision id | Collected | Played matches in snapshot | In current build? |
|---|---|---|---|---|---|
| `raw_snapshots/group_a_20260611.json` | A | 1359031576 | 2026-06-11 | 2 (MD1: MEX–RSA, KOR–CZE) | superseded |
| `raw_snapshots/group_a_20260614.json` | A | 1359248858 | 2026-06-14 | 2 (MD1, unchanged from 06-11) | superseded |
| `raw_snapshots/group_b_20260614.json` | B | 1359248723 | 2026-06-14 | 2 (MD1: CAN–BIH, QAT–SUI) | superseded |
| `raw_snapshots/group_c_20260614.json` | C | 1359267394 | 2026-06-14 | 2 (MD1: BRA–MAR, HAI–SCO) | superseded |
| `raw_snapshots/group_a_20260704.json` | A | 1362418894 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_b_20260704.json` | B | 1362418908 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_c_20260704.json` | C | 1361875126 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_d_20260704.json` | D | 1362232293 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_e_20260704.json` | E | 1362142759 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_f_20260704.json` | F | 1362560553 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_g_20260704.json` | G | 1362560628 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_h_20260704.json` | H | 1362560682 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_i_20260704.json` | I | 1362320155 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_j_20260704.json` | J | 1362511324 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_k_20260704.json` | K | 1362187117 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/group_l_20260704.json` | L | 1362560738 | 2026-07-04 | 6 (group stage complete) | **yes** |
| `raw_snapshots/maincup_20260704.json` | — (backfill source) | 1362524513 | 2026-07-04 | n/a (confederation + venue-country source) | source only |
| `raw_snapshots/roundof32_20260704.json` | knockout | 1368129133 | 2026-07-04 | 16 (Round of 32) | **yes** |
| `raw_snapshots/knockout_20260704.json` | knockout | 1367736969 | 2026-07-04 | 15 (R16 ×8, QF ×4, SF ×2, 3rd-place ×1; Final transcluded → taken from the final page) | **yes** |
| `raw_snapshots/final_20260704.json` | knockout | 1368170825 | 2026-07-04 | 1 (Final: ESP 1–0 ARG a.e.t.) | **yes** |

The current frozen dataset is built from the twelve **2026-07-04** group snapshots
(A–L) **plus the three knockout-stage snapshots** — the **complete tournament**: 104
matches (72 group + 32 knockout: R32 ×16, R16 ×8, QF ×4, SF ×2, third-place ×1, final
×1), 282 goals, 272 cards, 3,287 appearances, 1,042 players, 48 teams, 16 venues.
The `maincup` snapshot is a backfill *source* (confederation, venue country) and is not
parsed as matches — `build_csvs.py` consumes `group_*.json` plus the three named
knockout snapshots. Knockout matches add **no new teams or venues** (both already fully
covered), so those two CSVs' checksums are unchanged.

**Scope note (2026-07-04):** scope was expanded twice, both *before any ontology was
generated* (so pre-registration integrity holds): first from a 2–3 group subset to all
12 groups, then to the full tournament including the knockout stage. See
`PRE_REGISTRATION.md` §2. **The knockout expansion retires calibration probe C**:
`stage` is no longer degenerate (distinct=1, `'group'`) — it is now a 7-value vocab
(`group`, `round_of_32`, `round_of_16`, `quarter_final`, `semi_final`, `third_place`,
`final`). That was a deliberate, recorded trade of a designed probe for full coverage.

Collection-timing honesty: Groups A–C matchday 1 was captured live on 11–14 Jun; all
matchdays 2–3, Groups D–L, and the entire knockout stage were captured on 04 Jul from
the (now-complete) pages, not the night each match was played. The revision ids are
permanent anchors regardless of capture date, so the "provably post-cutoff, my schema,
my collection" claim is unaffected.

To reproduce a snapshot's revision exactly:
`https://en.wikipedia.org/w/index.php?oldid=<revid>`

## Frozen dataset checksum (built 2026-07-04)

The CSVs in `data/` are a deterministic rebuild from the 2026-07-04 snapshots via
`setup/build_csvs.py` (they are git-ignored — the snapshots are the tracked source of
truth). Both model runs (PRE_REGISTRATION.md §8) must use this exact dataset. Per-file
SHA-256:

```
1836a43b9f67aa38e966efeaa9c2a0b413e40df2a6a5b8b4e1bbe922f926fd67  appearances.csv
18f135235ea30711220254eb9b6e020df2b494065160c37c20de49077adc002f  cards.csv
45560071aeddcb906356ccde09c797aeee605dfee9f07a66b17ae32bac557c98  goals.csv
05aee4d01562af9d69b9f4e95d109e57fc2c9ae671d5be7a3e1c2a099378cf1d  matches.csv
70d3e5088af2fe4716ccf7775895ef92d99f3c6e9485440667766d5ffdea77ed  players.csv
2ce3cc385736831522c36c9f7c86ad3ed70107fa91298ff2cb7250147ea65d53  teams.csv
f987419198d52b8dd5528b4afb44f9d2efc347127e31cc37e70c3e9836194623  venues.csv
```

Manifest hash (sha256 of the above list):
`66428d9b67a20dfb7cbaab67704ef59a2e8e06d043b4b19291d9800da71048f1`

Loaded into MySQL bronze (`worldcup2026_bronze` on `:3307`) 2026-07-04: 5,051 rows
(104 matches across all 7 stages), all FK relationships resolve with 0 orphans, 12
distinct group labels.

## Known source wrinkles (recorded, not silently fixed)

- **Zwane red card minute (Group A, MEX v RSA):** Wikipedia's prose says 73'; the
  structured lineup template says `{{sent off|0|84}}`. The parser uses the structured
  field (84') consistently. Both are in revid 1359031576 for audit.
- **Tunisia vs Japan score-link (Group F, revid 1362560553):** this was the 1,000th
  World Cup match, so its `|score=` carries a milestone alt-text third parameter
  (`{{score link|…|0–4|Tunisia v Japan (2026 FIFA World Cup)}}`) that the two-parameter
  score regex rejected, silently dropping the match (Group F parsed 5 of 6). Fixed
  **generally** in `parse_wikipedia_group.py` by allowing trailing score-link params, so
  any milestone box parses — not a Group-F-specific patch. Verified all 12 groups then
  parse 6 matches (72 total).
- **Knockout shoot-outs are recorded as `outcome='draw'` (known limitation).** Four
  knockout matches were level after extra time and decided on penalties: GER–PAR,
  NED–MAR, AUS–EGY (round of 32) and SUI–COL (round of 16). The penalty result appears
  **only in prose** on Wikipedia, and this collection parses structured fields only (the
  same discipline that avoided the Zwane hallucination). The frozen schema also has no
  penalties column. So these rows keep the on-pitch score (a draw) and a score-derived
  `outcome='draw'`; the advancing team is *not* encoded. This is deliberate and recorded
  rather than "fixed" by reading prose or bending the schema.
- **Knockout line-up tables use spacing variants** (`{| width="100%"` with `| valign=`
  vs the group pages' `|valign=`). The final-page line-ups initially parsed 0
  appearances; the lineup-table split in `parse_wikipedia_group.py` was made
  whitespace-tolerant (matches both), a general fix. Verified the final then yields 34
  appearances and the group pages are unchanged.
- **Knockout stage sources three pages:** the round-of-32 matches live on
  `2026 FIFA World Cup round of 32`; R16/QF/SF/third-place on `2026 FIFA World Cup
  knockout stage`; the final on `2026 FIFA World Cup final` (transcluded into the
  knockout page via `{{#lst:}}`, so it is taken from the final page, not double-counted).
  `parse_knockout_file` reads the stage from each section/page context — it is never
  hard-coded — yielding 16+8+4+2+1+1 = 32 knockout matches.

## Licensing

The snapshots in `raw_snapshots/` are verbatim English Wikipedia text by Wikipedia
contributors, licensed under CC BY-SA 4.0. They are distributed under that license,
not the project's Apache-2.0 license; attribution and details are in
`raw_snapshots/LICENSE.md`.
