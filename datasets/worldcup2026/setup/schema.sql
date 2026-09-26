-- 2026 FIFA World Cup (Canada/Mexico/USA, 11 Jun – 19 Jul 2026) — bronze schema.
--
-- This schema is HAND-DESIGNED by Ben Tagger as a blind generalisation test: the
-- data is collected live from the running tournament (provably post-cutoff) and the
-- column layout is original (not a public dump). See ../PRE_REGISTRATION.md for the
-- locked experimental design, the structural spec (no-gold quality oracle), and the
-- calibration-trap column intents. The schema is FROZEN once committed — do not edit
-- it to match a generator result; that would void the pre-registration.
--
-- Conventions (mirror datasets/olympics2026/setup/schema.sql):
--   * lowercase column names, raw_ prefix.
--   * PRIMARY KEYs declared so PK-name FK inference (Strategy 3) has something to match.
--   * NO FOREIGN KEY constraints — every relationship is left for ForgeMesh's inference
--     layer to discover (the whole point).
--   * reserved words (`position`, `minute`) are backtick-quoted.
--
-- Run against the MySQL instance on :3307 (creates the database if absent).

CREATE DATABASE IF NOT EXISTS worldcup2026_bronze
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE worldcup2026_bronze;

-- ── Teams ─────────────────────────────────────────────────────────────────────
-- PK: team_id (3-letter FIFA code, e.g. 'MEX' — a bare identifier like MIMIC's cgid).
-- group_label (A–L) and confederation are fully-populated closed vocabularies.
-- fifa_ranking is a scalar int sitting next to those categoricals (vocab-vs-scalar).
CREATE TABLE IF NOT EXISTS raw_teams (
    team_id        VARCHAR(8)   NOT NULL,
    name           VARCHAR(96),
    group_label    VARCHAR(2),
    confederation  VARCHAR(16),
    fifa_ranking   INT,
    PRIMARY KEY (team_id)
);

-- ── Venues ────────────────────────────────────────────────────────────────────
-- PK: venue_id. Referenced by raw_matches.venue_id (Strategy 2/3, non-role-prefixed).
-- country_code is a 3-value closed set (CAN/MEX/USA hosts). capacity is a scalar int.
CREATE TABLE IF NOT EXISTS raw_venues (
    venue_id       VARCHAR(8)   NOT NULL,
    name           VARCHAR(96),
    city           VARCHAR(64),
    country_code   VARCHAR(3),
    capacity       INT,
    PRIMARY KEY (venue_id)
);

-- ── Players ───────────────────────────────────────────────────────────────────
-- PK: player_id (surrogate int). Identity dimension only: team_id is an undeclared FK
-- to raw_teams (Strategy 2 table-name + Strategy 3 PK-name); date_of_birth is a scalar.
-- Per-match attributes (position, shirt_number, starter/sub) live on raw_appearances.
CREATE TABLE IF NOT EXISTS raw_players (
    player_id      INT          NOT NULL,
    full_name      VARCHAR(96),
    team_id        VARCHAR(8),
    date_of_birth  DATE,
    PRIMARY KEY (player_id)
);

-- ── Appearances ───────────────────────────────────────────────────────────────
-- Player × match M:N junction (who played in which match). This is the one
-- deliberately non-trivial relational pattern: because it carries attributes
-- (is_starter, position, shirt_number) it MUST become a reified associative entity,
-- not a bare link — the reify-or-collapse decision an ontology generator gets wrong.
-- Its three undeclared FKs (match_id, player_id, team_id) widen the hasMatch /
-- hasPlayer / hasTeam union domains to include Appearance (closer to MIMIC intensity).
-- `position` is the fully-populated small vocab (calibration probe A) — it lives here
-- (per-match) not on raw_players. is_starter is a 0/1 boolean.
CREATE TABLE IF NOT EXISTS raw_appearances (
    appearance_id  INT          NOT NULL,
    match_id       INT,
    player_id      INT,
    team_id        VARCHAR(8),
    `position`     VARCHAR(4),
    shirt_number   INT,
    is_starter     TINYINT(1),
    PRIMARY KEY (appearance_id)
);

-- ── Matches ───────────────────────────────────────────────────────────────────
-- PK: match_id (surrogate int).
-- home_team_id / away_team_id → raw_teams via Strategy 4 (role-prefix) — the
--   OpenFlights dest_airport_id pattern, now on an unseen schema.
-- venue_id → raw_venues. THREE relationship columns sit next to FOUR scalars
--   (kickoff_utc datetime, home_score/away_score/attendance ints) — the exact
--   scalar-adjacent-to-relationship adjacency that produced qwen's hasOuttime→Icustay.
-- stage: real vocab but only 'group' is observed mid-tournament → degenerate-vocab
--   probe C (distinct=1, 0% null).
-- outcome: vocab (home_win/away_win/draw) and the NULL-RATE KNOB — probe B. Its null
--   rate = unplayed/total, set by how many scheduled-but-unplayed fixtures we load.
-- referee: low-cardinality free text — the borderline string-vs-class case, the
--   direct analog of MIMIC's `category` divergence (probe D).
CREATE TABLE IF NOT EXISTS raw_matches (
    match_id       INT          NOT NULL,
    stage          VARCHAR(24),
    home_team_id   VARCHAR(8),
    away_team_id   VARCHAR(8),
    venue_id       VARCHAR(8),
    kickoff_utc    DATETIME,
    home_score     INT,
    away_score     INT,
    outcome        VARCHAR(12),
    attendance     INT,
    referee        VARCHAR(96),
    PRIMARY KEY (match_id)
);

-- ── Goals ─────────────────────────────────────────────────────────────────────
-- PK: goal_id (surrogate int). match_id/player_id/team_id are undeclared FKs
-- (Strategy 3 PK-name; player_id/team_id also Strategy 1 natural-key).
-- `minute` is an INT scalar sitting between INTEGER FK columns — the integer
-- contamination probe (does match_id route to a class while minute stays a literal?).
-- goal_type is a fully-populated small vocab. team_id = team credited with the goal.
CREATE TABLE IF NOT EXISTS raw_goals (
    goal_id        INT          NOT NULL,
    match_id       INT,
    player_id      INT,
    team_id        VARCHAR(8),
    `minute`       INT,
    goal_type      VARCHAR(16),
    PRIMARY KEY (goal_id)
);

-- ── Cards ─────────────────────────────────────────────────────────────────────
-- PK: card_id (surrogate int). Same FK trio as raw_goals — so hasMatch / hasPlayer /
-- hasTeam are each shared across goals AND cards → forces union-domain handling
-- (the hasPatient case). card_type is a fully-populated small vocab — calibration
-- probe A partner (must stay a class). `minute` is the integer contamination probe.
CREATE TABLE IF NOT EXISTS raw_cards (
    card_id        INT          NOT NULL,
    match_id       INT,
    player_id      INT,
    team_id        VARCHAR(8),
    `minute`       INT,
    card_type      VARCHAR(16),
    PRIMARY KEY (card_id)
);
