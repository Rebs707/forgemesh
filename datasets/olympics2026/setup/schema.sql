-- Milano-Cortina 2026 Winter Olympics — bronze schema
-- Source: Kaggle  piterfm/milano-cortina-2026-olympic-winter-games  (Games 6–22 Feb 2026).
--
-- Conventions (mirrors datasets/mimic/setup/schema.sql):
--   * lowercase column names, raw_ prefix
--   * PRIMARY KEYs declared so PK-name FK inference (Strategy 3) has something to match
--   * NO FOREIGN KEY constraints — every relationship is left for ForgeMesh's inference
--     layer to discover (the whole point of this dataset).
--
-- Run against the MySQL instance on :3307 (creates the database if absent).

CREATE DATABASE IF NOT EXISTS olympics2026_bronze
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE olympics2026_bronze;

-- ── Athletes ────────────────────────────────────────────────────────────────
-- PK: code (athlete id). country_code is an undeclared reference to a NOC/country.
-- `events` is a stringified list of {discipline, event} dicts — opaque text for now.
CREATE TABLE IF NOT EXISTS raw_athletes (
    code          VARCHAR(32)  NOT NULL,
    name          VARCHAR(255),
    gender        VARCHAR(2),
    country_code  VARCHAR(8),
    flag_bearer   TINYINT,
    events        TEXT,
    PRIMARY KEY (code)
);

-- ── Coaches ─────────────────────────────────────────────────────────────────
-- PK: code. TWO country references — country_code (delegation) and nationality —
-- both pointing at the same conceptual country entity.
CREATE TABLE IF NOT EXISTS raw_coaches (
    code          VARCHAR(32)  NOT NULL,
    name          VARCHAR(255),
    gender        VARCHAR(2),
    country_code  VARCHAR(8),
    nationality   VARCHAR(8),
    `function`    VARCHAR(64),
    events        TEXT,
    PRIMARY KEY (code)
);

-- ── Medallists ──────────────────────────────────────────────────────────────
-- Fact table (no natural single key) → surrogate row_id PK.
-- `code` is an undeclared FK to raw_athletes.code; it ALSO matches the PK name of
-- raw_coaches and raw_teams, so it exercises value-overlap disambiguation
-- (medallists.code ⊂ athletes.code at 100%, vs 0% for coaches/teams).
CREATE TABLE IF NOT EXISTS raw_medallists (
    row_id          INT          NOT NULL AUTO_INCREMENT,
    date            DATE,
    medal_code      TINYINT,
    medal           VARCHAR(16),
    code            VARCHAR(32),
    name            VARCHAR(255),
    gender          VARCHAR(2),
    country_code    VARCHAR(8),
    country         VARCHAR(96),
    discipline      VARCHAR(64),
    discipline_code VARCHAR(16),
    event_name      VARCHAR(160),
    PRIMARY KEY (row_id)
);

-- ── Medal table ─────────────────────────────────────────────────────────────
-- PK: country_code (3-letter NOC — a bare identifier, like MIMIC's cgid).
-- NB: only the 29 medal-winning nations appear here, so it is NOT a complete
-- country dimension (athletes span 93 country_codes → ~31% overlap).
CREATE TABLE IF NOT EXISTS raw_medals (
    country       VARCHAR(96),
    country_code  VARCHAR(8)   NOT NULL,
    gold          INT,
    silver        INT,
    bronze        INT,
    total         INT,
    `rank`        INT,
    rank_total    INT,
    PRIMARY KEY (country_code)
);

-- ── Schedules ───────────────────────────────────────────────────────────────
-- `id` repeats (628 distinct / 1884 rows) so it is NOT a key → surrogate row_id PK.
-- venue_code is an undeclared FK to raw_venues.venue_code (≈93% overlap).
-- start_date/end_date keep their TZ offset as text for faithful bronze; the silver
-- DateNormaliser handles them downstream.
CREATE TABLE IF NOT EXISTS raw_schedules (
    row_id          INT          NOT NULL AUTO_INCREMENT,
    start_date      VARCHAR(40),
    end_date        VARCHAR(40),
    day             DATE,
    status          VARCHAR(16),
    discipline      VARCHAR(64),
    discipline_code VARCHAR(16),
    event           VARCHAR(160),
    event_medal     TINYINT,
    phase           VARCHAR(192),
    gender          VARCHAR(2),
    event_type      VARCHAR(16),
    venue           VARCHAR(160),
    venue_code      VARCHAR(8),
    location        VARCHAR(192),
    location_code   VARCHAR(16),
    id              VARCHAR(64),
    PRIMARY KEY (row_id)
);

-- ── Teams ───────────────────────────────────────────────────────────────────
-- PK: code (structured team id). country_code is an undeclared NOC reference.
CREATE TABLE IF NOT EXISTS raw_teams (
    code          VARCHAR(48)  NOT NULL,
    name          VARCHAR(255),
    team_type     VARCHAR(16),
    gender        VARCHAR(2),
    country_code  VARCHAR(8),
    events        TEXT,
    PRIMARY KEY (code)
);

-- ── Venues ──────────────────────────────────────────────────────────────────
-- PK: venue_code. Referenced by raw_schedules.venue_code.
CREATE TABLE IF NOT EXISTS raw_venues (
    venue            VARCHAR(160),
    venue_code       VARCHAR(8)  NOT NULL,
    disciplines      VARCHAR(160),
    disciplines_code VARCHAR(64),
    url_sport        VARCHAR(255),
    url_venue        VARCHAR(255),
    PRIMARY KEY (venue_code)
);
