# Getting started

From a fresh clone to asking questions of a knowledge graph, with a provenance
receipt for every answer. It uses the bundled Synthea sample (synthetic patient
records), so you don't need your own data.

> **Using Claude Code?** Open the repo and ask: *"Follow GETTING_STARTED.md to get
> ForgeMesh running."* Every step below is a plain shell command with a check.

## Prerequisites

| Need | Why |
|---|---|
| Docker (Desktop or Engine, with Compose v2) | MySQL (bronze/silver/gold tables) and Apache Jena Fuseki (triple store) |
| Python ≥ 3.11 | The pipeline, web app and MCP server |
| An OpenAI API key | Value mapping, natural-language → SPARQL. Or run fully local with Ollama instead ([README → Running fully local](README.md#running-fully-local-air-gapped)) |
| ~4 GB free disk | Python deps (the embedding model pulls in PyTorch) and Docker images |

Free ports: **3307** (MySQL), **3030** (Fuseki), **5173** (web app). You can change
them (see [Troubleshooting](#troubleshooting)).

## 1. Install

```bash
git clone https://github.com/tagben/forgemesh.git
cd forgemesh
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Check: `python -c "import semantic_mapper; print('ok')"` prints `ok`.

## 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set `LLM_API_KEY=sk-...`. That's the only required value.

## 3. Start MySQL and Fuseki

```bash
docker compose up -d db fuseki
```

Check: `docker compose ps` shows both running, with `db` reaching `healthy`
(about 30 s on first start while MySQL initialises).

## 4. Load the sample data (bronze layer)

```bash
python scripts/load_synthea.py
```

This creates `bronze_synthea`, `silver_synthea` and `gold_synthea`, and loads five
tables (~195k rows) from `datasets/synthea/sample/`. It takes a few seconds.

Check: the last table line reads `OK    raw_procedures: 102,064 rows`.

## 5. Run the pipeline

```bash
python main.py --project synthea
```

This runs `inspect → map → silver → gold → rdf`: profile the source schema, map
columns to the bundled ontology (`owl/synthea_rigor_gpt-4o-mini_20JUN26.owl`),
clean and type the data, project one gold table per ontology class, and
materialise RDF into Fuseki with provenance on every triple.

It takes about 4–5 minutes and a few cents of `gpt-4o-mini` calls (value mapping).

Check: the last lines include `triples_written=1909310` and
`dangling gate: PASS — 0 unresolved object links`.

## 6. Open the web app

```bash
python web_app.py --project synthea
```

Open **http://127.0.0.1:5173** and click a question under **Try asking**, or type
your own. *"How many patients are there in total?"* should answer **585**. Click
**Prove it** to open the receipt, which shows the answer verified against the graph
and traced back through gold → silver → bronze rows.

Stop the web app with Ctrl+C, and the containers with `docker compose stop`.

Without `--project`, the app reopens the project you last picked in the UI, and
otherwise starts on `DEFAULT_PROJECT` (Synthea unless you change it in `.env`).

Next time, `./start-forgemesh.sh --project synthea` starts the containers and
the web app in one step (macOS/Linux). `./start-forgemesh.sh --stop` stops both.

## 7. (Optional) Query from Claude Code or Claude Desktop via MCP

The server must run from the repo's venv; it finds `.env` and the project files
itself. **Claude Code**, from the repo root:

```bash
claude mcp add forgemesh-synthea -- "$PWD/.venv/bin/python" "$PWD/mcp_server.py" --project synthea
```

**Claude Desktop**: add to `claude_desktop_config.json` (use absolute paths):

```json
{
  "mcpServers": {
    "forgemesh-synthea": {
      "command": "/abs/path/to/forgemesh/.venv/bin/python",
      "args": ["/abs/path/to/forgemesh/mcp_server.py", "--project", "synthea"]
    }
  }
}
```

Tools include `query` (natural language), `get_schema`, `execute_sparql` and
`prove_answer`. Try asking Claude *"Using forgemesh-synthea, how many patients are there?
Prove the answer."*

## Next steps

- **Your own database:** copy `projects/synthea.yaml` to `projects/mydb.yaml`, point
  the three database URLs at your data, and leave `ontology_path` empty. Then run
  `python main.py --project mydb --phases inspect,seed,full`, which generates an
  ontology from your schema. Set `ontology_path` to the new file and run
  `python main.py --project mydb`.
- **Other bundled datasets:** OpenFlights (`scripts/load_openflights.py`), Winter
  Olympics / World Cup (`datasets/*/setup/`), MIMIC-III (download the free
  demo from PhysioNet, then generate its ontology; see `datasets/mimic/README.md`).
- **How it works:** [README.md](README.md), [ARCHITECTURE.md](ARCHITECTURE.md).

## Troubleshooting

**A port is already in use.** Set different host ports in `.env`, and tell ForgeMesh
about them:

```
FORGEMESH_MYSQL_PORT=3317
FORGEMESH_FUSEKI_PORT=3031
FORGEMESH_DB_HOST=127.0.0.1
FORGEMESH_DB_PORT=3317
FUSEKI_URL=http://localhost:3031
```

The web app port is `./start-forgemesh.sh --port 5174`. For the loader, also export
`DATABASE_URL=mysql+pymysql://root:password@localhost:3317/bronze_synthea`.

**`Access denied for user 'root'`.** The MySQL volume was initialised with a
different root password. The project files expect `password`. Either set
`MYSQL_ROOT_PASSWORD` to match what the volume has, or remove the volume and start
over (this deletes the data): `docker compose down && docker volume rm forgemesh_mysql_data`.

**Queries return nothing / "mapping report not found".** Step 5 didn't finish.
Re-run `python main.py --project synthea` and check the output of each phase.

**Start over from scratch.**

```bash
python main.py --project synthea --phases purge
python main.py --project synthea
```
