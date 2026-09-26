-- Semantic Mapper — Bronze layer seed data
-- Runs automatically on first container start via /docker-entrypoint-initdb.d/

USE bronze_db;

-- ---------------------------------------------------------------------------
-- raw_party
-- Intentional data quality issues:
--   P-008: NULL cust_sub_type
--   P-009: NULL dob
--   P-010: UK date format (DD/MM/YYYY) mixed with ISO dates
-- ---------------------------------------------------------------------------
CREATE TABLE raw_party (
    party_id        VARCHAR(50)     NOT NULL,
    party_type      TINYINT         NOT NULL,
    full_nm         VARCHAR(255),
    org_nm          VARCHAR(255),
    dob             VARCHAR(20),
    email_addr      VARCHAR(255),
    risk_cat        VARCHAR(10),
    cust_sub_type   VARCHAR(30),
    created_dt      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
    last_upd_dt     TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

INSERT INTO raw_party VALUES
('P-001', 1, 'William Clarke',  NULL,                    '1978-03-14', 'bill.clarke@email.com',  'L',  'POLICYHOLDER', NOW(), NOW()),
('P-002', 1, 'Ben Hargreaves',  NULL,                    '1985-07-22', 'ben.h@email.com',        'M',  'SUBSCRIBER',   NOW(), NOW()),
('P-003', 1, 'Sarah Okafor',    NULL,                    '1990-11-05', NULL,                     NULL, 'MEMBER',       NOW(), NOW()),
('P-004', 1, 'James Trent',     NULL,                    '1955-01-30', 'j.trent@oldmail.co.uk',  'H',  'PATIENT',      NOW(), NOW()),
('P-005', 1, 'Lucy Brennan',    NULL,                    '2001-09-18', 'lucy.b@email.com',        'L',  'RESIDENT',     NOW(), NOW()),
('P-006', 2, NULL,              'Hargreaves Group Ltd',  NULL,         'admin@hargltd.co.uk',    NULL, 'SPONSOR',      NOW(), NOW()),
('P-007', 2, NULL,              'City Trust Foundation', NULL,         NULL,                     NULL, 'TRUSTEE',      NOW(), NOW()),
('P-008', 1, 'David Mensah',    NULL,                    '1969-06-11', 'd.mensah@corp.com',       'M',  NULL,           NOW(), NOW()),
('P-009', 1, 'Emma Walsh',      NULL,                    NULL,         'e.walsh@email.com',      'L',  'SUBSCRIBER',   NOW(), NOW()),
('P-010', 1, 'Tom Gallagher',   NULL,                    '14/03/1982', 'tom.g@email.com',         'M',  'MEMBER',       NOW(), NOW());

-- ---------------------------------------------------------------------------
-- raw_agreement
-- Intentional data quality issues:
--   AGR-007: NULL agr_status, NULL start_dt (quote not yet active)
--   AGR-008: lowercase agr_type 'sub' — case inconsistency vs 'SUB'
--   AGR-010: agr_status=2 (lapsed) with a closed end_dt
-- ---------------------------------------------------------------------------
CREATE TABLE raw_agreement (
    agr_ref         VARCHAR(50)     NOT NULL,
    party_ref       VARCHAR(50),
    agr_type        VARCHAR(20),
    agr_status      TINYINT,
    product_cd      VARCHAR(30),
    start_dt        DATE,
    end_dt          DATE,
    premium_amt     DECIMAL(10,2),
    currency_cd     CHAR(3)         DEFAULT 'GBP',
    created_dt      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO raw_agreement VALUES
('AGR-001', 'P-001', 'POL', 1,    'FULL_PMI',    '2022-01-01', NULL,         145.00, 'GBP', NOW()),
('AGR-002', 'P-002', 'SUB', 1,    'SILVER_PLUS', '2023-03-15', NULL,          89.50, 'GBP', NOW()),
('AGR-003', 'P-003', 'SUB', 3,    'SILVER_PLUS', '2021-06-01', '2023-06-01',  89.50, 'GBP', NOW()),
('AGR-004', 'P-004', 'POL', 2,    'FULL_PMI',    '2019-01-01', '2022-12-31', 210.00, 'GBP', NOW()),
('AGR-005', 'P-005', 'POL', 1,    'BRONZE',      '2024-01-01', NULL,          55.00, 'GBP', NOW()),
('AGR-006', 'P-006', 'CON', 1,    'CORP_SCHEME', '2020-09-01', NULL,        1200.00, 'GBP', NOW()),
('AGR-007', 'P-008', 'QUO', NULL, 'FULL_PMI',    NULL,         NULL,         178.00, 'GBP', NOW()),
('AGR-008', 'P-009', 'sub', 1,    'SILVER_PLUS', '2024-06-01', NULL,          89.50, 'GBP', NOW()),
('AGR-009', 'P-010', 'POL', 1,    'FULL_PMI',    '2023-11-01', NULL,         145.00, 'GBP', NOW()),
('AGR-010', 'P-007', 'CON', 2,    'CORP_SCHEME', '2018-01-01', '2023-12-31', 950.00, 'GBP', NOW());

-- ---------------------------------------------------------------------------
-- raw_related_person
-- Intentional data quality issues:
--   rel_role: mixed codes ('DM', 'DC') and free text ('decision maker')
-- ---------------------------------------------------------------------------
CREATE TABLE raw_related_person (
    rel_id          INT             AUTO_INCREMENT PRIMARY KEY,
    primary_party   VARCHAR(50),
    related_party   VARCHAR(50),
    rel_role        VARCHAR(30),
    valid_from      DATE,
    valid_to        DATE
);

INSERT INTO raw_related_person (primary_party, related_party, rel_role, valid_from, valid_to) VALUES
('P-006', 'P-008', 'DM',             '2020-09-01', NULL),
('P-006', 'P-003', 'DC',             '2020-09-01', NULL),
('P-007', 'P-004', 'decision maker', '2018-01-01', NULL),
('P-001', 'P-005', 'DC',             '2022-01-01', NULL);

-- ---------------------------------------------------------------------------
-- raw_potential_customer
-- Intentional data quality issues:
--   PROS-003: NULL email_addr, NULL dob (org prospect)
--   PROS-004: NULL dob, NULL quote_ref, NULL quote_amt, NULL quote_dt
--   PROS-005: NULL src_channel
-- ---------------------------------------------------------------------------
CREATE TABLE raw_potential_customer (
    prospect_id     VARCHAR(50)     NOT NULL,
    prospect_type   VARCHAR(10),
    stage           VARCHAR(20),
    full_nm         VARCHAR(255),
    email_addr      VARCHAR(255),
    dob             VARCHAR(20),
    org_nm          VARCHAR(255),
    quote_ref       VARCHAR(50),
    quote_amt       DECIMAL(10,2),
    quote_dt        DATE,
    src_channel     VARCHAR(20)
);

INSERT INTO raw_potential_customer VALUES
('PROS-001', 'IND', 'LEAD',     'Rachel Kim',   'r.kim@email.com',    '1992-04-17', NULL,                  'QUO-001',  112.00, '2024-09-01', 'WEB'),
('PROS-002', 'IND', 'PROSPECT', 'Marcus Webb',  'm.webb@email.com',   '1980-12-03', NULL,                  'QUO-002',  145.00, '2024-10-15', 'PHONE'),
('PROS-003', 'ORG', 'LEAD',     NULL,           NULL,                 NULL,         'Nexus Solutions Ltd', 'QUO-003',  890.00, '2024-11-01', 'BROKER'),
('PROS-004', 'IND', 'PROSPECT', 'Priya Sharma', 'p.sharma@email.com', NULL,         NULL,                  NULL,       NULL,   NULL,         'WEB'),
('PROS-005', 'ORG', 'PROSPECT', NULL,           'info@deltacorp.com', NULL,         'Delta Corp',          'QUO-004', 2100.00, '2024-11-20', NULL);
