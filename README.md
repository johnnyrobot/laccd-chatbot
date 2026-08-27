# LACCD Campus Chatbot

An open-source, multi-campus student-information assistant for the nine colleges
of the Los Angeles Community College District. The repository contains three
maintained products:

- a FastAPI backend with a static web chat interface;
- a LiveKit voice agent using the same shared campus services; and
- an Android application with optional on-device Gemma inference.

This is an independent project and is not an official LACCD service. Answers
can be incomplete or wrong; students should verify consequential information
with their college or counselor.

## What it connects to

The backend can route questions to Neo4j, Gemini File Search, live LACCD class
schedules, ASSIST articulation data, college program maps, and configured model
providers. Integrations fail closed or report an unconfigured/degraded state
when credentials or private data are absent.

## Run the web application

Python 3.12 is the supported container runtime.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.txt
cp .env.example .env
PYTHONPATH=backend:. uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Provider-backed features require the applicable
environment variables documented in `.env.example`.

## Run the voice agent

Configure LiveKit and Gemini credentials in an ignored `.env` file, then run:

```bash
PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python voice_agent/agent.py dev
```

The web backend exposes the standard voice-token endpoint when LiveKit is
configured. Voice audio is transported by LiveKit and processed by the
configured Google Gemini Live model.

## Build the containers

```bash
docker build -t laccd-chatbot .
docker build -f voice_agent/Dockerfile -t laccd-voice-agent .
```

`docker-compose.prod.yml` expects an external Docker network named `proxy` and
an ignored `.env` file. It is an operator example, not a turnkey hosted service.

## Build the Android app

Use JDK 17 and a configured Android SDK:

```bash
cd mobile
./gradlew :app:testDebugUnitTest :app:assembleDebug
```

The Android application is derived from Google AI Edge Gallery. Its upstream
attribution is preserved in `mobile/NOTICE`.

Public builds do not download model weights or collect model-provider tokens.
Use Model Manager to import a compatible `.litertlm` file. Developers can also
sideload the canonical filename to the app-specific external-files directory:

```text
gemma_4_e4b/gemma-4-e4b.litertlm
```

Release APK/AAB tasks fail unless all four signing variables are supplied:
`LACCD_RELEASE_KEYSTORE`, `LACCD_RELEASE_STORE_PASSWORD`,
`LACCD_RELEASE_KEY_ALIAS`, and `LACCD_RELEASE_KEY_PASSWORD`. Debug signing is
never used for a release artifact.

## Data boundary

Private corpora, downloaded catalogs, operational snapshots, model files, and
provider credentials are deliberately excluded from Git. Small curated inputs
under `data/` are the only data files shipped with the source. Ingestion tools
accept external inputs from ignored directories such as
`data/source_documents/`.

## Operational scripts

Scripts are explicit operator entry points; they are not imported at request
time. Run commands from the repository root with the same Python path as the
application, for example:

```bash
PYTHONPATH=backend:. python scripts/parse_catalog_pdf.py --help
```

- Acquisition and refresh: `crawl_campus_websites.py`,
  `crawl_campuses_c4a.py`, `refresh_openstax.py`,
  `scrape_elumen_courses.py`, and `scrape_elumen_prereqs.py`.
- Corpus and catalog preparation: `convert_catalogs_docling.py`,
  `parse_catalog_pdf.py`, `index_addenda.py`, `build_knowledge_pack.py`, and
  `build_schedule_availability.py`.
- Provider ingestion: `ingest_campus_corpus.py`, `ingest_catalog_md.py`,
  `ingest_catalog_pdfs.py`, and `ingest_fafsa_corpus.py`.
- Graph loading and synchronization: `neo4j_seed.py`, `load_assist_data.py`,
  `load_prerequisites.py`, `seed_ccn_renumbering.py`,
  `sync_catalog_to_neo4j.py`, `sync_elumen_to_neo4j.py`,
  `sync_instructors_to_neo4j.py`, and `sync_programs_to_neo4j.py`.
- Health, evaluation, and deterministic fixtures: `healthcheck.py`,
  `eval_guardrails.py`, `measure_external_prereqs.py`,
  `purge_campus_stores.py`, and `verify_schedule_completeness_replay.py`.

## Tests

```bash
PYTHONPATH=backend:. pytest
```

Tests use synthetic fixtures rather than live services unless a command
explicitly says otherwise.

## Security and privacy

Do not commit credentials, provider downloads, student records, or private
corpora. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities and
[PRIVACY.md](PRIVACY.md) for the software's data-flow boundaries.

## License

Licensed under the [Apache License 2.0](LICENSE). Third-party notices are in
[NOTICE](NOTICE) and component-specific notice files.
