"""Run the outbox consumer separately from the HTTP server: python -m app.worker."""
from __future__ import annotations

import argparse
import logging
import time

from app.config import settings
from app.database import build_engine, build_session_factory
from app.models import Base
from app.services.outbox import process_one


def main() -> None:
    parser = argparse.ArgumentParser(description="OrderFlow transactional outbox worker")
    parser.add_argument("--once", action="store_true", help="process at most one available event and exit")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = build_engine(settings.database_url)
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    session_factory = build_session_factory(engine)
    logging.getLogger("orderflow.worker").info("Outbox worker started")
    try:
        while True:
            db = session_factory()
            try:
                worked = process_one(db)
            finally:
                db.close()
            if args.once:
                break
            if not worked:
                time.sleep(max(0.25, settings.worker_poll_seconds))
    except KeyboardInterrupt:
        logging.getLogger("orderflow.worker").info("Outbox worker stopped")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
