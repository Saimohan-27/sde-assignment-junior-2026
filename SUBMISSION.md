# Submission

## Candidate
- **Name:** Sai Mohan
- **Assignment chosen:** A (Data)
- **Time spent:** 11 hours

## How to run
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports
pytest -q
```

Run commands from repository root. The first run creates `.pii.key`; preserve it across runs and never commit it. Alternatively set `PII_ENCRYPTION_KEY` to a stable Fernet key.

## Design
SQLite stores leads, event facts, event-to-lead links, seen event IDs, processed-file fingerprints, rejections, and quality counters. Lead identity is `(workflow_id, HMAC-SHA256(phone))`. Canonical phone and customer name are encrypted with Fernet; the keyed HMAC supports equality matching without storing plaintext phone. Events omit raw phone and retain only the keyed hash. Event facts are append-only; `event_links` is separate so initially orphaned events can be linked when a lead arrives later.

## Decisions & assumptions
| Decision | Why | Alternative considered |
|---|---|---|
| One SQLite DB and local Fernet key file | Reproducible local setup with encrypted PII | PostgreSQL/KMS |
| Event rows are immutable; links are separate | Late-arriving leads can resolve orphan links without editing event facts | Updating event rows |
| A valid event ID is reserved even for unknown dispositions | Re-delivery must not repeatedly count the same unknown event | Ignore event ID entirely |
| File path plus content hash marks completed input | Safe idempotent re-runs; changed file at same path is reprocessed | Drop-level manifest |
| Date values are stored as ISO dates; timestamps converted to IST | Stable comparisons and report grouping | Keep original formats |

## Failure modes considered
Malformed JSON and missing event IDs are counted and skipped. Duplicate IDs are dropped globally. Orphans remain unresolved until a matching lead/call appears. PII key loss makes encrypted PII unreadable; back up the key securely. The local key file is a development convenience, not a production KMS integration.

## Testing
Unit tests cover phone formats, date parsing, decimal currency parsing,
naive-IST timestamps, override precedence and bucket priority.
All six available unit tests passed successfully.

## Results

- Successfully generated all five reports.
- Ingested two data batches into SQLite.
- Generated batch summaries for both workflows.
- All 6 available unit tests passed.
- No test failures.

**Test execution:**
Executed using `python -m pytest -v`.

Result: 6 passed.

## If I had another day
- Add end-to-end golden tests for all five report files.
- Move key management to a KMS or secrets manager.
- Add transactional per-file ingestion and recovery tests for process termination.
- Profile large-scale reporting and push aggregations into SQL where beneficial.
