# Interaction Ledger & Reporting Pipeline

## Overview

A Python-based data engineering pipeline that ingests
customer leads and call events, stores them in SQLite,
and generates daily campaign reports.

## Technologies

- Python
- SQLite
- SQL
- CSV and JSONL processing
- Pytest
- PII encryption and HMAC hashing

## Features

- Lead validation and normalization
- Duplicate lead merging
- Append-only interaction ledger
- Event deduplication and orphan handling
- PII protection
- Idempotent ingestion
- Five automated reporting outputs

## Running the project

Generate data:

python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

Ingest:

python -m solution.pipeline ingest data/drop_1
python -m solution.pipeline ingest data/drop_2

Generate reports:

python -m solution.pipeline report --out reports

Run tests:

pytest -qPut your solution code here. Keep the provided files outside this folder unmodified.
