# MIMIC-III Demo Dataset Setup

MIMIC-III Clinical Database Demo (100 patients) is freely available from
[PhysioNet](https://physionet.org/content/mimiciii-demo/1.4/) with no
credentialing requirement — only a free account is needed.

---

## Why MIMIC-III?

MIMIC is the hardest possible test for the mapping engine:

| Challenge | Detail |
|---|---|
| Cryptic column naming | `HADM_ID`, `ICUSTAY_ID`, `CGID` — no natural language signal |
| Integer discriminator codes | ICD-9 codes (`410.01`) are opaque without a dictionary table |
| Merged source systems | `INPUTEVENTS_CV` (CareVue) vs `INPUTEVENTS_MV` (MetaVision) — same concept, different schemas |
| Dictionary tables | `D_ICD_DIAGNOSES`, `D_ITEMS` define what integer codes mean |
| Three-level hierarchy | Patient → Admission → ICU Stay → Diagnosis |

---

## Quick Start

### 1. Download the demo data

1. Register at [PhysioNet](https://physionet.org/) (free)
2. Navigate to [MIMIC-III Clinical Database Demo v1.4](https://physionet.org/content/mimiciii-demo/1.4/)
3. Download and unzip to a local directory, e.g. `~/mimic-iii-demo/`

The directory should contain files like: `PATIENTS.csv`, `ADMISSIONS.csv`, `ICUSTAYS.csv`, etc.

### 2. Start Docker services

```bash
# Start MySQL and Fuseki (uses existing docker-compose.yml)
docker compose up db fuseki -d

# Wait for healthy status
docker compose ps
```

### 3. Create the database and schema

```bash
docker exec -i $(docker compose ps -q db) \
  mysql -uroot -ppassword < datasets/mimic/setup/schema.sql
```

### 4. Load the CSV files

```bash
python datasets/mimic/setup/load_mimic.py --data-dir ~/mimic-iii-demo
```

This loads into the bronze database named in `projects/mimic.yaml`
(`mimic_bronze`). Expected output:
```
MIMIC-III LOADER
  Data dir : /home/user/mimic-iii-demo
  Database : localhost:3307/mimic_bronze
  Tables   : 21

  Loading PATIENTS → raw_patients … 100 rows
  Loading ADMISSIONS → raw_admissions … 129 rows
  Loading ICUSTAYS → raw_icustays … 139 rows
  ...
```

### 5. Generate the ontology and run the pipeline

No MIMIC ontology ships with ForgeMesh, since it would embed MIMIC vocabulary.
The first run generates one from your copy of the data:

```bash
python main.py --project mimic --phases inspect,full,map,silver,gold,rdf
```

The `full` phase writes `owl/mimic_full_<model>_<date>.owl` and uses it for the rest
of the run. Set `ontology_path` in `projects/mimic.yaml` to that file so later runs
(and the web app) reuse it. Reviewing it in Protégé first is optional but
recommended. For the higher-quality iterative generator, set
`ONTOLOGY_GENERATOR=iterative` in `.env` before running.

The Fuseki dataset (`mimic-mesh`) is created automatically on the first `rdf` run.

### 6. Launch the web app

```bash
python web_app.py --project mimic
```

Or set `DEFAULT_PROJECT=mimic` in `.env` to make MIMIC the project the web app and
MCP server start on.

---

## Configuration

Everything dataset-specific lives in `projects/mimic.yaml`:

| Key | Value |
|---|---|
| `bronze_database_url` | `mysql+pymysql://root:password@localhost:3307/mimic_bronze` |
| `silver_database_url` | `mysql+pymysql://root:password@localhost:3307/mimic_silver` |
| `gold_database_url` | `mysql+pymysql://root:password@localhost:3307/mimic_gold` |
| `ontology_path` | empty until you generate one (step 5) |
| `rdf_dataset_name` | `mimic-mesh` |

The mapping report is written to `output/mapping_report_mimic.json`.

---

## Table Map

| MIMIC CSV | Bronze Table | OMOP CDM Concept |
|---|---|---|
| `PATIENTS.csv` | `raw_patients` | Person |
| `ADMISSIONS.csv` | `raw_admissions` | Visit_occurrence |
| `ICUSTAYS.csv` | `raw_icustays` | Visit_detail |
| `DIAGNOSES_ICD.csv` | `raw_diagnoses_icd` | Condition_occurrence |
| `D_ICD_DIAGNOSES.csv` | `raw_d_icd_diagnoses` | Concept (condition) |
| `PROCEDURES_ICD.csv` | `raw_procedures_icd` | Procedure_occurrence |
| `D_ICD_PROCEDURES.csv` | `raw_d_icd_procedures` | Concept (procedure) |
| `LABEVENTS.csv` | `raw_labevents` | Measurement |
| `D_LABITEMS.csv` | `raw_d_labitems` | Concept (lab) |
| `CHARTEVENTS.csv` | `raw_chartevents` | Measurement / Observation |
| `D_ITEMS.csv` | `raw_d_items` | Concept (chart item) |
| `INPUTEVENTS_CV.csv` | `raw_inputevents_cv` | Drug_exposure (CareVue) |
| `INPUTEVENTS_MV.csv` | `raw_inputevents_mv` | Drug_exposure (MetaVision) |
| `OUTPUTEVENTS.csv` | `raw_outputevents` | Observation |
| `PRESCRIPTIONS.csv` | `raw_prescriptions` | Drug_exposure |
| `NOTEEVENTS.csv` | `raw_noteevents` | Note_nlp |
| `MICROBIOLOGYEVENTS.csv` | `raw_microbiologyevents` | Measurement |
| `TRANSFERS.csv` | `raw_transfers` | Visit_detail |
| `CAREGIVERS.csv` | `raw_caregivers` | Provider |
| `SERVICES.csv` | `raw_services` | Visit_occurrence (service) |
| `DRGCODES.csv` | `raw_drgcodes` | Visit_occurrence (DRG) |

---

## Target Metrics

After a successful pipeline run, validate with:

```bash
python main.py --project mimic --phases validate
```

| Metric | Target |
|---|---|
| Mapping precision | > 90% |
| Quarantine rate | < 5% |
| Benchmark queries passing | 100% |
| LLM cost per run | < $0.10 |
