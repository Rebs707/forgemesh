-- MIMIC-III Demo bronze schema — issue #53
-- All tables use lowercase column names and raw_ prefix.
-- Run against the mimic_bronze database after creating it.
-- Source: MIMIC-III v1.4 clinical database schema.

CREATE DATABASE IF NOT EXISTS mimic_bronze
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE mimic_bronze;

-- ── Core clinical tables ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_patients (
    row_id      INT          NOT NULL,
    subject_id  INT          NOT NULL,
    gender      VARCHAR(5)   NOT NULL,
    dob         DATETIME,
    dod         DATETIME,
    dod_hosp    DATETIME,
    dod_ssn     DATETIME,
    expire_flag TINYINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (row_id),
    UNIQUE KEY uq_subject (subject_id)
);

CREATE TABLE IF NOT EXISTS raw_admissions (
    row_id               INT           NOT NULL,
    subject_id           INT           NOT NULL,
    hadm_id              INT           NOT NULL,
    admittime            DATETIME      NOT NULL,
    dischtime            DATETIME,
    deathtime            DATETIME,
    admission_type       VARCHAR(50)   NOT NULL,
    admission_location   VARCHAR(50),
    discharge_location   VARCHAR(50),
    insurance            VARCHAR(255),
    language             VARCHAR(10),
    religion             VARCHAR(50),
    marital_status       VARCHAR(50),
    ethnicity            VARCHAR(200),
    edregtime            DATETIME,
    edouttime            DATETIME,
    diagnosis            VARCHAR(255),
    hospital_expire_flag TINYINT       NOT NULL DEFAULT 0,
    has_chartevents_data TINYINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (row_id),
    UNIQUE KEY uq_hadm (hadm_id),
    KEY idx_subject (subject_id)
);

CREATE TABLE IF NOT EXISTS raw_icustays (
    row_id          INT           NOT NULL,
    subject_id      INT           NOT NULL,
    hadm_id         INT           NOT NULL,
    icustay_id      INT           NOT NULL,
    dbsource        VARCHAR(20)   NOT NULL,    -- 'carevue' | 'metavision'
    first_careunit  VARCHAR(15),
    last_careunit   VARCHAR(15),
    first_wardid    SMALLINT,
    last_wardid     SMALLINT,
    intime          DATETIME      NOT NULL,
    outtime         DATETIME,
    los             DOUBLE,
    PRIMARY KEY (row_id),
    UNIQUE KEY uq_icustay (icustay_id),
    KEY idx_subject (subject_id),
    KEY idx_hadm    (hadm_id)
);

-- ── Diagnosis / procedure tables ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_diagnoses_icd (
    row_id     INT         NOT NULL,
    subject_id INT         NOT NULL,
    hadm_id    INT         NOT NULL,
    seq_num    INT         NOT NULL,
    icd9_code  VARCHAR(10),
    PRIMARY KEY (row_id),
    KEY idx_hadm   (hadm_id),
    KEY idx_icd9   (icd9_code)
);

CREATE TABLE IF NOT EXISTS raw_d_icd_diagnoses (
    row_id      INT           NOT NULL,
    icd9_code   VARCHAR(10)   NOT NULL,
    short_title VARCHAR(50)   NOT NULL,
    long_title  VARCHAR(255)  NOT NULL,
    PRIMARY KEY (row_id),
    -- Bronze ingests raw source data as-is. MIMIC reference tables can carry
    -- duplicate ICD-9 codes, so no UNIQUE constraint on the code column.
    KEY idx_code (icd9_code)
);

CREATE TABLE IF NOT EXISTS raw_procedures_icd (
    row_id     INT         NOT NULL,
    subject_id INT         NOT NULL,
    hadm_id    INT         NOT NULL,
    seq_num    INT         NOT NULL,
    icd9_code  VARCHAR(10),
    PRIMARY KEY (row_id),
    KEY idx_hadm (hadm_id)
);

CREATE TABLE IF NOT EXISTS raw_d_icd_procedures (
    row_id      INT           NOT NULL,
    icd9_code   VARCHAR(10)   NOT NULL,
    short_title VARCHAR(50)   NOT NULL,
    long_title  VARCHAR(255)  NOT NULL,
    PRIMARY KEY (row_id),
    -- Bronze ingests raw source data as-is. MIMIC reference tables can carry
    -- duplicate ICD-9 codes, so no UNIQUE constraint on the code column.
    KEY idx_code (icd9_code)
);

-- ── Lab / chart events ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_labevents (
    row_id     INT           NOT NULL,
    subject_id INT           NOT NULL,
    hadm_id    INT,
    itemid     INT           NOT NULL,
    charttime  DATETIME,
    value      VARCHAR(200),
    valuenum   DOUBLE,
    valueuom   VARCHAR(20),
    flag       VARCHAR(20),
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_itemid  (itemid)
);

CREATE TABLE IF NOT EXISTS raw_d_labitems (
    itemid    INT          NOT NULL,
    label     VARCHAR(100) NOT NULL,
    fluid     VARCHAR(100) NOT NULL,
    category  VARCHAR(100) NOT NULL,
    loinc_code VARCHAR(10),
    PRIMARY KEY (itemid)
);

CREATE TABLE IF NOT EXISTS raw_chartevents (
    row_id       INT           NOT NULL,
    subject_id   INT           NOT NULL,
    hadm_id      INT,
    icustay_id   INT,
    itemid       INT           NOT NULL,
    charttime    DATETIME,
    storetime    DATETIME,
    cgid         INT,
    value        VARCHAR(255),
    valuenum     DOUBLE,
    valueuom     VARCHAR(50),
    warning      INT,
    error        INT,
    resultstatus VARCHAR(50),
    stopped      VARCHAR(50),
    PRIMARY KEY (row_id),
    KEY idx_subject   (subject_id),
    KEY idx_icustay   (icustay_id),
    KEY idx_itemid    (itemid)
);

CREATE TABLE IF NOT EXISTS raw_d_items (
    itemid      INT           NOT NULL,
    label       VARCHAR(200),
    abbreviation VARCHAR(100),
    dbsource    VARCHAR(20),              -- 'carevue' | 'metavision'
    linksto     VARCHAR(50),
    category    VARCHAR(100),
    unitname    VARCHAR(100),
    param_type  VARCHAR(30),
    conceptid   INT,
    PRIMARY KEY (itemid)
);

-- ── Input / output events (two source systems) ────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_inputevents_cv (
    row_id            INT          NOT NULL,
    subject_id        INT          NOT NULL,
    hadm_id           INT,
    icustay_id        INT,
    charttime         DATETIME,
    itemid            INT,
    amount            DOUBLE,
    amountuom         VARCHAR(30),
    rate              DOUBLE,
    rateuom           VARCHAR(30),
    storetime         DATETIME,
    cgid              INT,
    orderid           INT,
    linkorderid       INT,
    stopped           VARCHAR(30),
    newbottle         INT,
    originalamount    DOUBLE,
    originalamountuom VARCHAR(30),
    originalroute     VARCHAR(30),
    originalrate      DOUBLE,
    originalrateuom   VARCHAR(30),
    originalsite      VARCHAR(30),
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_icustay (icustay_id)
);

CREATE TABLE IF NOT EXISTS raw_inputevents_mv (
    row_id                          INT           NOT NULL,
    subject_id                      INT           NOT NULL,
    hadm_id                         INT,
    icustay_id                      INT,
    starttime                       DATETIME,
    endtime                         DATETIME,
    itemid                          INT,
    amount                          DOUBLE,
    amountuom                       VARCHAR(30),
    rate                            DOUBLE,
    rateuom                         VARCHAR(30),
    storetime                       DATETIME,
    cgid                            INT,
    orderid                         INT,
    linkorderid                     INT,
    ordercategoryname               VARCHAR(100),
    secondaryordercategoryname      VARCHAR(100),
    ordercomponenttypedescription   VARCHAR(200),
    ordercategorydescription        VARCHAR(50),
    patientweight                   DOUBLE,
    totalamount                     DOUBLE,
    totalamountuom                  VARCHAR(50),
    isopenbag                       SMALLINT,
    continueinnextdept              SMALLINT,
    cancelreason                    SMALLINT,
    statusdescription               VARCHAR(30),
    comments_editedby               VARCHAR(30),
    comments_canceledby             VARCHAR(40),
    comments_date                   DATETIME,
    originalamount                  DOUBLE,
    originalrate                    DOUBLE,
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_icustay (icustay_id)
);

CREATE TABLE IF NOT EXISTS raw_outputevents (
    row_id     INT           NOT NULL,
    subject_id INT           NOT NULL,
    hadm_id    INT,
    icustay_id INT,
    charttime  DATETIME,
    itemid     INT,
    value      DOUBLE,
    valueuom   VARCHAR(30),
    storetime  DATETIME,
    cgid       INT,
    stopped    VARCHAR(30),
    newbottle  CHAR(1),
    iserror    INT,
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_icustay (icustay_id)
);

-- ── Prescriptions ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_prescriptions (
    row_id           INT           NOT NULL,
    subject_id       INT           NOT NULL,
    hadm_id          INT           NOT NULL,
    icustay_id       INT,
    startdate        DATETIME,
    enddate          DATETIME,
    drug_type        VARCHAR(100),
    drug             VARCHAR(100),
    drug_name_poe    VARCHAR(100),
    drug_name_generic VARCHAR(100),
    formulary_drug_cd VARCHAR(50),
    gsn              VARCHAR(200),
    ndc              VARCHAR(25),
    prod_strength    VARCHAR(120),
    dose_val_rx      VARCHAR(120),
    dose_unit_rx     VARCHAR(120),
    form_val_disp    VARCHAR(120),
    form_unit_disp   VARCHAR(120),
    route            VARCHAR(120),
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_hadm    (hadm_id)
);

-- ── Notes / microbiology ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_noteevents (
    row_id      INT           NOT NULL,
    subject_id  INT           NOT NULL,
    hadm_id     INT,
    chartdate   DATE,
    charttime   DATETIME,
    storetime   DATETIME,
    category    VARCHAR(50),
    description VARCHAR(255),
    cgid        INT,
    iserror     CHAR(1),
    text        MEDIUMTEXT,
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_hadm    (hadm_id)
);

CREATE TABLE IF NOT EXISTS raw_microbiologyevents (
    row_id               INT          NOT NULL,
    subject_id           INT          NOT NULL,
    hadm_id              INT,
    chartdate            DATE,
    charttime            DATETIME,
    spec_itemid          INT,
    spec_type_desc       VARCHAR(100),
    org_itemid           INT,
    org_name             VARCHAR(100),
    isolate_num          SMALLINT,
    ab_itemid            INT,
    ab_name              VARCHAR(30),
    dilution_text        VARCHAR(10),
    dilution_comparison  VARCHAR(20),
    dilution_value       DOUBLE,
    interpretation       VARCHAR(5),
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id)
);

-- ── Administrative tables ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS raw_transfers (
    row_id        INT           NOT NULL,
    subject_id    INT           NOT NULL,
    hadm_id       INT,
    icustay_id    INT,
    dbsource      VARCHAR(20),
    eventtype     VARCHAR(20),
    prev_careunit VARCHAR(15),
    curr_careunit VARCHAR(15),
    prev_wardid   SMALLINT,
    curr_wardid   SMALLINT,
    intime        DATETIME,
    outtime       DATETIME,
    los           DOUBLE,
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id)
);

CREATE TABLE IF NOT EXISTS raw_caregivers (
    cgid        INT          NOT NULL,
    label       VARCHAR(15),
    description VARCHAR(30),
    PRIMARY KEY (cgid)
);

CREATE TABLE IF NOT EXISTS raw_services (
    row_id       INT          NOT NULL,
    subject_id   INT          NOT NULL,
    hadm_id      INT          NOT NULL,
    transfertime DATETIME     NOT NULL,
    prev_service VARCHAR(10),
    curr_service VARCHAR(10),
    PRIMARY KEY (row_id),
    KEY idx_subject (subject_id),
    KEY idx_hadm    (hadm_id)
);

CREATE TABLE IF NOT EXISTS raw_drgcodes (
    row_id        INT           NOT NULL,
    subject_id    INT           NOT NULL,
    hadm_id       INT           NOT NULL,
    drg_type      VARCHAR(20)   NOT NULL,
    drg_code      VARCHAR(20)   NOT NULL,
    description   VARCHAR(255),
    drg_severity  SMALLINT,
    drg_mortality SMALLINT,
    PRIMARY KEY (row_id),
    KEY idx_hadm (hadm_id)
);
