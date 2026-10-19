"""Apply the additive department-ID migration to an explicitly selected database."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import connection

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-database", required=True, help="Must match current_database(); prevents targeting another DB")
    args = parser.parse_args()
    with connection() as conn:
        actual = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if actual != args.expected_database:
            raise SystemExit(f"Refusing migration: connected to {actual}, expected {args.expected_database}")
        print(f"Target database: {actual}")
        if not args.apply:
            print("Read-only preview. Use --apply after stopping this database's server/workers.")
            return
        migration = Path(__file__).resolve().parents[2] / "database" / "migrations" / "20261016_department_ids.sql"
        conn.execute(migration.read_text(encoding="utf-8"))
        print("Department-ID migration applied; account/complaint data retained.")

if __name__ == "__main__":
    main()
