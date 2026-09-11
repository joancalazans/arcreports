#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import Base, SessionLocal, local_engine
from app.glpi_import import run_connector_import, seed_connector_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Importa dados do GLPI producao para o banco local configurado.")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    parser.add_argument("--table-id", type=int, help="ID local da tabela em connector_sync_tables.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    Base.metadata.create_all(bind=local_engine)
    db = SessionLocal()
    try:
        seed_connector_settings(db)
        runs = run_connector_import(db, args.mode, args.table_id)
        for run in runs:
            print(f"{run.status}\t{run.mode}\t{run.table_name}\t{run.row_count}\t{run.error_message or ''}")
        return 1 if any(run.status == "error" for run in runs) else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
