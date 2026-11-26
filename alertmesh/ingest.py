"""Run the official source adapters against the configured AlertMesh database."""

from __future__ import annotations

import argparse
import json
import logging
import os

from .api import SMTPVerificationMailer
from .service import AlertMesh
from .sources import IngestionRunner, NWSAdapter, SourceIdentityStore, USGSAdapter, UrllibJsonTransport


def build_runner() -> tuple[IngestionRunner, AlertMesh, SourceIdentityStore]:
    database = os.environ.get("ALERTMESH_DB_PATH", "alertmesh.sqlite3")
    smtp_host = os.environ.get("ALERTMESH_SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("Set ALERTMESH_SMTP_HOST before starting source ingestion")
    mailer = SMTPVerificationMailer(
        smtp_host,
        int(os.environ.get("ALERTMESH_SMTP_PORT", "587")),
        os.environ.get("ALERTMESH_SMTP_SENDER", "alerts@localhost"),
        username=os.environ.get("ALERTMESH_SMTP_USERNAME"),
        password=os.environ.get("ALERTMESH_SMTP_PASSWORD"),
    )
    app = AlertMesh(mailer, db_path=database)
    identities = SourceIdentityStore(database)
    transport = UrllibJsonTransport(user_agent=os.environ.get("ALERTMESH_USER_AGENT", "AlertMesh/0.1"))
    adapters = {
        "nws": NWSAdapter(transport, identities),
        "usgs": USGSAdapter(transport),
    }
    runner = IngestionRunner(
        app,
        adapters,
        max_attempts=int(os.environ.get("ALERTMESH_SOURCE_MAX_ATTEMPTS", "3")),
        base_delay=float(os.environ.get("ALERTMESH_SOURCE_RETRY_BASE_SECONDS", "1")),
        max_delay=float(os.environ.get("ALERTMESH_SOURCE_RETRY_MAX_SECONDS", "30")),
    )
    return runner, app, identities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch NWS and USGS alerts into AlertMesh")
    parser.add_argument("--once", action="store_true", help="Fetch each source once and exit")
    parser.add_argument("--interval", type=float, default=60, help="Polling interval in seconds (default: 60)")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        runner, app, identities = build_runner()
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))
    try:
        if arguments.once:
            results = runner.run_once()
            print(json.dumps([{
                "source": result.source,
                "success": result.success,
                "attempts": result.attempts,
                "received": result.received,
                "ingested": result.ingested,
                "failed": result.failed,
                "last_checked": result.last_checked.isoformat() if result.last_checked else None,
            } for result in results], separators=(",", ":")))
            return 0 if all(result.success for result in results) else 1
        runner.run_forever(arguments.interval)
        return 0
    finally:
        identities.close()
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
