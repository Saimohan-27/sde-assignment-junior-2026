# Interaction Ledger & Reporting Pipeline

Copy `solution/`, `tests/`, `requirements.txt`, `SUBMISSION.md`, and `AI_USAGE.md` into the root of the provided assignment repository. Keep the original `assignment-a-data/` directory unchanged.

The CLI is:
```bash
python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2
python -m solution.pipeline report --out reports
```

SQLite database defaults to `interaction_ledger.sqlite3`. PII key defaults to `.pii.key`. Both are local runtime artifacts and must not be committed.
