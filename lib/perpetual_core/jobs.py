"""Model-free jobs run by Hermes cron scripts (no LLM call, so they cost nothing).

    python -m perpetual_core.jobs snapshot-metrics [--backfill DAYS]
    python -m perpetual_core.jobs snapshot-viral
    python -m perpetual_core.jobs requests-pending <client>     # cron monitor script

Cron scripts run as plain subprocesses, outside any profile's secret scope, so shared
settings are read from the file the boot reconciler writes (PERPETUAL_SHARED_ENV).
"""

import os
import sys
from pathlib import Path

SHARED_ENV = Path(os.environ.get("PERPETUAL_SHARED_ENV", "/opt/data/perpetual-shared.env"))


def _load_shared_env():
    if not SHARED_ENV.exists():
        return
    for line in SHARED_ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(argv):
    _load_shared_env()
    from . import agency, ledger
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    conn = ledger.connect()
    try:
        if cmd == "snapshot-metrics":
            days = int(argv[2]) if len(argv) > 2 and argv[1] == "--backfill" else 1
            n = agency.snapshot_metrics(conn, days_back=days)
            # --no-agent cron delivers stdout verbatim; stay silent on success.
            if n == 0:
                print("⚠️ metrics snapshot stored nothing — is the scraping console reachable?")
        elif cmd == "snapshot-viral":
            agency.snapshot_viral(conn)
        elif cmd == "requests-pending":
            # Monitor script: output only changes when there is new work, so an unchanged
            # (empty) result suppresses the agent run entirely.
            for r in agency.pending_requests(conn, argv[1].lower()):
                print(f"request {r['id']} from {r['by_name'] or 'dashboard'}: {r['request']}")
        else:
            print(f"unknown command {cmd}")
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
