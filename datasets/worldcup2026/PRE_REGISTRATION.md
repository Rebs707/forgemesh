# World Cup 2026 — blind generalisation test (pre-registration)

**Status:** LOCKED before ontology generation. Revision history, all *before any
ontology was generated* (so there are no generator results to retrofit against and
pre-registration integrity holds):
- 2026-06-11 — committed; schema revised same day to add the `raw_appearances` M:N junction.
- 2026-07-04 (a) — scope expanded from a 2–3 group subset to the full 12-group group
  stage (§2). Schema and probe/spec design unchanged; only row volume grew.
- 2026-07-04 (b) — scope expanded again to the **full tournament including the knockout
  stage** (104 matches). Schema still unchanged, but this **retires calibration probe C**:
  `stage` is no longer degenerate — it becomes a 7-value vocab (see §4). A deliberate,
  recorded trade of one designed probe for complete coverage.

**Author:** Ben Tagger. **Schema designed by hand; data collected live.**

This document is pinned *before* any data is collected so the result cannot be
retrofitted (the lesson from the qwen runs: pin everything up front or you get a
confounded result). The bronze schema (`setup/schema.sql`), the structural spec
(§5), the calibration-trap intents (§4), and the run protocol (§8) are frozen. If a
generator result disagrees with the schema or the spec, that is a **finding**, not a
licence to edit either.

---

## 1. Purpose and the honest limit of the confound

Every prior validation (MIMIC, OpenFlights, Synthea) used a public dataset in the
model's training data, which flatters ontology generation: the model may be
recognising a known schema rather than reasoning about an unseen one. cortina
(olympics2026) removed the *data* confound but uses the Kaggle author's column
layout. This test removes both: **the schema is my own design and the data is
collected live from the running 2026 World Cup** (group stage 11–27 Jun 2026,
provably after the Jan 2026 cutoff).

**The residual confound, stated plainly:** the model knows what football *is*. This
is **unseen data + unseen schema + familiar domain** — the realistic customer case,
and far stronger than MIMIC, but *not* maximal novelty. We do **not** claim "the
model has never seen the domain."

**Collection-timing reality:** as of 2026-06-11 only the opening match has been
played; the group stage fills out to 27 Jun. Data is collected **incrementally as
matches are played**, which is a provenance asset, not just a constraint (§7).

---

## 2. Scope

> **SCOPE REVISIONS — 2026-07-04, both recorded before any ontology was generated.**
> Original scope was a *deliberately minimal* 2–3 group subset (~400–600 rows), retained
> verbatim below for the audit trail.
>
> **(a) Full group stage.** At Ben's explicit direction, scope was expanded to the
> **complete group stage of all 12 groups (A–L): 48 teams, 72 matches, ~3,800 rows**.
> Schema, structural spec (§5), probe intents (§4), and run protocol (§8) **unchanged** —
> only row volume grew. It strengthens the existing stressors (wider FK cardinality,
> wider union domains, a 6-value `confederation` vocab, a 12-value `group_label`) without
> adding or removing any designed pattern.
>
> **(b) Full tournament (knockout stage added).** When the tournament finished, scope was
> expanded again to the **complete tournament: 104 matches** (72 group + 32 knockout —
> R32 ×16, R16 ×8, QF ×4, SF ×2, third-place ×1, final ×1). The schema is still unchanged
> (knockout matches are ordinary `raw_matches` rows; the deliberately-omitted
> `next_match_id` bracket self-FK is still omitted — bracket lineage is not modelled).
> **This retires calibration probe C:** `stage` is no longer degenerate (distinct=1); it
> is now a 7-value vocab (`group`, `round_of_32`, `round_of_16`, `quarter_final`,
> `semi_final`, `third_place`, `final`). Probe C becomes an ordinary multi-value vocab
> — see the §4 note. Two knockout representation limitations are recorded in
> `PROVENANCE.md`: penalty shoot-outs (prose-only → `outcome='draw'`, advancing team not
> encoded) and no penalties column in the frozen schema. All other probes (A, B, D) and
> the structural spec are unaffected.
>
> **Why neither compromises the pre-registration:** the integrity rule is that the
> schema/spec/probes are not edited to *retrofit a generator result*. No ontology has
> been generated yet (same standing as the 2026-06-11 `raw_appearances` revision), so
> there is nothing to retrofit against. Row-count targets below are the original
> minimal-scope figures and no longer bound the dataset; the frozen counts are in
> `PROVENANCE.md`.

Original minimal scope (retained for audit): seven tables, a 2–3 group subset, target
**~400–600 rows total**. This is below cortina (~6,200) and far below MIMIC (21 tables).
The goal is a fair stress of the patterns in §3, not World Cup coverage. We resist
anything larger unless a specific stressor demands it. Explicitly **out of scope** (would
add collection cost without a new stressor): full 26-man squads for non-appearing
players, knockout matches, a separate substitutions table, a `referees` dimension table.

| Table | ~rows (2–3 groups, group stage) | Role |
|---|---|---|
| raw_teams | 8–12 | dimension; bare-code PK |
| raw_venues | up to 16 | dimension; collectable today (schedule is public) |
| raw_players | ~100–250 | identity dimension (name, team, DOB) |
| raw_appearances | ~300–600 | **M:N junction** (player × match: position, shirt, is_starter) |
| raw_matches | ~12–18 played (+ optional unplayed fixtures, probe B) | fact |
| raw_goals | ~30 | event |
| raw_cards | ~50 | event |

---

## 3. Designed stressors (what each part exercises)

- **Undeclared, code-based FKs (Strategies 1–4), no constraints declared.**
  - `home_team_id`/`away_team_id` → teams = **Strategy 4 role-prefix** (the
    OpenFlights `dest_airport_id` pattern, re-tested off-distribution).
  - `players.team_id`, `matches.venue_id` = Strategy 2 (table-name) + 3 (PK-name).
  - `goals`/`cards`.`{match,player,team}_id` = Strategy 3, with `player_id`/`team_id`
    also reachable by Strategy 1 (natural-key uniqueness).
- **Value-mapped categoricals (vocab-vs-literal routing):** `position`, `stage`,
  `outcome`, `goal_type`, `card_type`, `confederation`, `group_label`,
  `venues.country_code`.
- **Many-to-many associative entity (`appearances`):** the one deliberately
  non-trivial relational pattern. Because the player×match junction carries attributes
  (`is_starter`, `position`, `shirt_number`) it MUST be reified as an associative class,
  not collapsed to a bare link — the reify-or-collapse decision LLM generators get
  wrong. `is_starter` also introduces a boolean (a 0/1 type absent elsewhere).
- **Shared abstract reused across tables (the `hasPatient` union-domain case):**
  `hasTeam` is shared across {Player, Goal, Card, Appearance}; `hasMatch` and
  `hasPlayer` across {Goal, Card, Appearance}. The generator must express these as
  `rdfs:domain [owl:unionOf …]`, not conjunctive domains. `goals` and `cards` are kept
  **separate** precisely to create this, and `appearances` widens each union to 3–4
  classes (closer to MIMIC's `hasPatient` intensity). A single merged event table would
  collapse the union and kill the stressor.
- **Scalar-adjacent-to-relationship contamination (qwen's `hasOuttime→Icustay`):**
  in `matches`, the datetime `kickoff_utc` and int scalars sit next to three FK
  columns; in `goals`/`cards`, the int `minute` sits between integer FK columns
  (`match_id`, `player_id`). The test: do the FKs route to ObjectProperties while the
  scalars stay DatatypeProperties (no class range leaks onto `minute`/`kickoff_utc`)?
- **Self-reference: deliberately omitted.** The knockout bracket (`next_match_id`)
  would be a real self-FK but ~100% NULL mid-tournament — no values to infer from.
  Forcing it would violate "only if real, not forced." Revisit only if we extend to
  knockouts.

---

## 4. Calibration trap (flagged probe columns)

The resolver's class-vs-literal routing is gated by
`ONTOLOGY_CATEGORICAL_MAX_NULL_RATE` (default `DEFAULT_MAX_NULL_RATE_FOR_CLASS =
0.90`, used in `is_closed_vocabulary()`): a categorical with null-rate **> 0.90** is
demoted from vocab-class to literal. That threshold was tuned on MIMIC's null
distribution and diverges from gpt-4o gold on `raw_d_labitems.category` (gold=string,
policy=class). This test asks whether it **travels**.

| Probe | Column(s) | Designed null/shape | Expected verdict | What a mis-fire means |
|---|---|---|---|---|
| **A** no-false-demote | `position`, `card_type`, `goal_type`, `confederation` | 0% null, small distinct | **class** (enum) | a fully-populated vocab demoted = guard too aggressive off MIMIC |
| **B** the null knob | `matches.outcome` (+ `home_score`/`away_score`/`attendance`) | null-rate = unplayed/total, **tunable** | **class** when played-only; **literal** when >90% unplayed | the same semantic column flipping at 0.90 is the calibration signal |
| **C** degenerate-in-subset ⟶ **RETIRED 2026-07-04(b)** | `matches.stage` | ~~distinct=1, only 'group'~~ → now distinct=7 (group + 6 knockout rounds), 0% null | ~~record 1-member enum vs demote~~ → now an ordinary multi-value vocab; expected **class** | probe voided by the full-tournament expansion (§2); `stage` is now a normal Stage vocab, no longer a degenerate-vocab edge case |
| **D** borderline string-vs-class | `matches.referee` | low-cardinality free text, 0% null | **literal** (string) by intent | policy classifies it = the direct `category` divergence reproduced |

**Probe B is an A/B knob.** Primary collection is **played-matches-only** (outcome 0%
null → expected class). An optional sub-run loads the *scheduled-but-unplayed* group
fixtures too (public now), pushing `outcome` null-rate >0.90 to confirm the verdict
flips to literal. This is a controlled A/B on one column straddling the threshold —
cleaner than MIMIC's accidental `category` case.

**Post-load verification step:** after loading, profile the data and confirm each
probe landed at its target null rate/shape; adjust the loaded subset if not. Record
the actual rates next to the intended ones.

**Locked collection & representation rules (2026-06-11, before collection):**
- *Source:* Wikipedia per-group raw wikitext via `action=parse&prop=wikitext`
  (revision-pinned snapshot in `raw_snapshots/`), parsed deterministically by
  `setup/parse_wikipedia_group.py` → `setup/build_csvs.py`. No summarising fetch in
  the loop (a summarising fetch hallucinated a cross-match card on the first try).
- *`minute` (INT):* stoppage time coerced base+added (`90+2 → 92`); documented loader rule.
- *`position`:* lives on `raw_appearances` (per-match), not `raw_players`. Stored as the
  source's granular formation codes (GK/RB/CB/LB/DM/CM/RWB/LWB/RF/CF/LF/RM/LM, plus
  MF/DF/FW for subs — ~16 values). Still a fully-populated must-stay-class vocab (probe
  A), just with more members; the pipeline value-mapping is expected to collapse it to
  GK/DF/MF/FW.
- *`appearances`:* the M:N junction is built from the same lineup tables in each
  snapshot — the starting XI (before the `'''Substitutions:'''` marker) gets
  is_starter=1, substitutes after it get 0. `is_starter` is a 0/1 TINYINT boolean.
- *`goal_type`:* vocab narrowed to {open_play, penalty, own_goal} — the only subtypes
  reliably present in structure (header/free_kick live only in prose).
- *Backfilled from stable facts:* team `confederation`, venue `country_code`.
- *Left NULL for now (some feed the null-rate probes):* `fifa_ranking`, venue
  `capacity`, `kickoff_utc`, player `date_of_birth`.

---

## 5. Structural spec — the no-gold quality oracle

There is no gold *ontology* for this schema, but because I designed it I have a gold
*structure*. The generated ontology is scored mechanically against this spec:
**object-property recall**, **vocab-class recall**, and **scalar precision
(no-contamination)**. This is defensible to a reviewer — "here is the FK graph I
designed; here is the ontology's agreement with it."

**Intended ObjectProperties (each real FK → range class):**

| Property | Source column(s) | Range | Domain |
|---|---|---|---|
| hasHomeTeam | matches.home_team_id | Team | Match |
| hasAwayTeam | matches.away_team_id | Team | Match |
| hasVenue | matches.venue_id | Venue | Match |
| hasTeam *(shared)* | players/goals/cards/appearances.team_id | Team | **union(Player, Goal, Card, Appearance)** |
| hasMatch *(shared)* | goals/cards/appearances.match_id | Match | **union(Goal, Card, Appearance)** |
| hasPlayer *(shared)* | goals/cards/appearances.player_id | Player | **union(Goal, Card, Appearance)** |

Plus the reified **Appearance** associative class (from `raw_appearances`): an
`owl:Class` carrying the three shared object properties above plus the attributes below
— it must NOT be collapsed into a plain link. Score: all 6 properties emitted with the
correct range; the 3 shared ones carry a **unionOf** domain (not conjunctive plain
domains); and `Appearance` exists as a class.

**Intended vocabulary/enum classes:** Position, Confederation, GroupLabel, GoalType,
CardType, MatchOutcome, Stage (now a 7-value vocab after the knockout expansion; the
former degenerate-probe-C intent is retired — see §4), HostCountry (venues.country_code).
Score: each emitted as an enumeration class, not a scalar literal (subject to the
probe-A/B/C/D expectations in §4).

**Intended scalar DatatypeProperties (must NOT receive a class range):**
teams.name(string), teams.fifa_ranking(integer); players.full_name(string),
players.date_of_birth(date); venues.name(string), venues.city(string),
venues.capacity(integer); matches.kickoff_utc(dateTime), matches.home_score(integer),
matches.away_score(integer), matches.attendance(integer), matches.referee(string,
probe D); goals.minute(integer), cards.minute(integer);
appearances.shirt_number(integer), appearances.is_starter(boolean).
Score: none receives a class range (especially `minute`, `kickoff_utc`, and the FK
ints inside `appearances`).

---

## 6. Judging quality with no gold standard (tiers)

Coherence checks are **necessary, not sufficient** — they prove well-formedness, not
meaning (a one-class ontology passes coherence and is useless).

1. **Mechanical score vs the §5 structural spec** — object-property recall, vocab
   recall, scalar precision/no-contamination. The primary quality number.
2. **Cross-model agreement (triage)** — run gpt-4o and qwen3 on the identical frozen
   snapshot; where they agree, confidence is high; **disagreements localise** the
   manual review.
3. **Bounded manual review** — every disagreement + a random sample of agreements,
   judged against the spec.
4. **(Optional, strongest) end-to-end query correctness** — run the full pipeline and
   pose NL questions whose answers are computed directly from the bronze SQL ("top
   scorer in Group A?", "yellow cards in match X?"). A mis-routed FK breaks the join
   and the answer is wrong, so **SQL-computed answers are a gold standard for
   function** without a gold ontology. Gated behind tiers 1–3.

---

## 7. Data sourcing and provenance (the audit trail is the asset)

Collection is **live and incremental** as matches are played — the cleanest possible
provenance ("I collected match X the night it was played, dd Jun 2026"). Method
(to confirm; either preserves "my schema, post-cutoff, collected by me"):

- **Wikipedia per-group pages + revision IDs** — match articles carry goalscorers and
  cards; CC BY-SA; **revision IDs are permanent provenance anchors**.
- **Manual transcription from FIFA match centre** — cleanest "I typed this myself"
  story; tedious but small at this scope.

**Provenance discipline (mandatory):**
- Snapshot every source (raw HTML/JSON + URL + revision ID where available +
  collection timestamp) into `datasets/worldcup2026/raw_snapshots/`.
- `datasets/worldcup2026/PROVENANCE.md`: per-match source URL, revision/ref,
  collection date, collector, method.
- Freeze the loaded CSVs and record a **checksum**; the two model runs use the
  **identical** checksummed snapshot.
- **I (Claude) cannot supply this data** — cutoff is Jan 2026; every value comes from
  a live source. That is the point (it is blind).

**Friction flagged:** free sports APIs usually paywall player-level goals/cards on the
free tier — avoid the API route, which would leave `goals`/`cards` unpopulated and gut
the union-domain + contamination stressors.

---

## 8. Experimental protocol (pinned)

Freeze the checksummed snapshot; identical generator config except the LLM endpoint.
`ONTOLOGY_GENERATOR=iterative` at run time (judge requirement).

**Run 1 — gpt-4o (incumbent baseline).** *Does the default generator hold
off-distribution, or were the clean MIMIC results also a training artefact?*
```yaml
# projects/worldcup2026.yaml (run 1)
name: worldcup2026
bronze_database_url: mysql+pymysql://root:password@localhost:3307/worldcup2026_bronze
silver_database_url: mysql+pymysql://root:password@localhost:3307/worldcup2026_silver
gold_database_url:   mysql+pymysql://root:password@localhost:3307/worldcup2026_gold
ontology_gen_model: gpt-4o          # NB default llm_model is gpt-4o-mini — set explicitly
ontology_judge_model: gpt-4o-mini
llm_model: gpt-4o-mini
query_model: gpt-4o
named_graph_uri: http://mesh.local/graph/worldcup2026
ontology_named_graph_uri: http://mesh.local/graph/worldcup2026/ontology
rdf_dataset_name: worldcup2026-mesh
rdf_source_name: worldcup2026
# ontology_path: filled after gen → owl/worldcup2026_rigor_gpt-4o_<DDMMMYY>_<n>.owl
```

**Run 2 — qwen3-32b-q8 + resolver (identical snapshot).** *Does the open-weight path +
the resolver hold on a schema it was never calibrated against?* Swap the LLM backend
(as `projects/mimic.yaml` does):
```yaml
llm_base_url: <runpod/ollama OpenAI-compatible endpoint>/v1
llm_api_key: ollama
llm_model: qwen3:32b-q8_0
llm_num_ctx: 32768
llm_request_timeout: 300
ontology_gen_model: qwen3:32b-q8_0
ontology_judge_model: qwen3:32b-q8_0
```

**Captured per run:**
- `range_coherence_check` four-way counts (MIXED_RANGE / WRONG_RANGE_TYPE /
  MULTI_RANGE / DANGLING_RANGE) — **before and after** the resolver pass.
- prefix-leak guard dict (`detect_prefix_leak`) — expect empty for gpt-4o; possibly
  non-empty pre-fix for qwen.
- resolver log `mixed.summary()` — `R1(FK)=… R2(scalar)=… R3-class=… R3-literal=…
  R3-routed=… unkeyable=…` (how many conflicts, resolved how).
- class / object-property / datatype-property / named-individual / enum-class counts
  (`scripts/generate_ontology_with_metrics.py`).
- §5 structural-spec scores; §4 probe verdicts vs intent.

**Definition of done (fixed up front):** both runs complete on the frozen snapshot;
post-resolver coherence clean (0 MIXED_RANGE, 0 DANGLING) for both; structural-spec
recall/precision recorded; cross-model diff recorded; probe verdicts recorded against
intent. The claim is supported if (a) gpt-4o holds (clean coherence, high structural
recall) and (b) qwen3+resolver holds (clean post-resolver, resolver log shows the
work, probes resolved sensibly).

---

## 9. Next steps

1. Commit this doc + `setup/schema.sql` (the binding pre-registration).
2. Confirm the sourcing method (§7) and the group subset.
3. At collection time: build `setup/load_worldcup2026.py`, collect the subset live
   into `data/` + `raw_snapshots/`, write `PROVENANCE.md`, checksum.
4. Run the §8 protocol; record results in a `RESULTS.md`.
