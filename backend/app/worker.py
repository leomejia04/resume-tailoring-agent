"""The durable worker (ADR 0007). Run from backend/: ``python -m app.worker``.

Claims queued jobs and runs them as their owners, with a few threads
(WORKER_CONCURRENCY). Every MAINTENANCE_SECONDS it also keeps the queue
healthy: jobs whose worker died are requeued (or failed once out of
attempts) and expired budget reservations are returned. Stop with Ctrl+C or
SIGTERM: running jobs finish first; anything cut off is recovered by the
next worker when its lease expires.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading

from app.config import Settings, get_settings
from app.jobs import queue, runner

logger = logging.getLogger("app.worker")
MAINTENANCE_SECONDS = 15


def run_once(settings: Settings, worker_id: str) -> bool:
    """Claim and run one due job; False when nothing is due."""
    claimed = queue.claim(settings, worker_id)
    if claimed is None:
        return False
    runner.execute(settings, claimed.job_id, claimed.owner_id, worker_id)
    return True


def maintain(settings: Settings) -> tuple[int, int]:
    """Recover jobs whose lease expired and return expired reservations."""
    recovered = queue.recover(settings)
    for job_id, owner_id, kind, status in recovered:
        logger.warning("recovered job kind=%s job_id=%s now=%s", kind, job_id, status)
        runner.after_recovery(settings, job_id, owner_id, status)
    released = queue.release_expired_reservations(settings)
    if released:
        logger.warning("returned %d expired budget reservations", released)
    return len(recovered), released


def drain(settings: Settings, worker_id: str = "drain", limit: int = 100) -> int:
    """Run due jobs until none is left (tests and one-off operations)."""
    done = 0
    while done < limit and run_once(settings, worker_id):
        done += 1
    return done


def serve(settings: Settings, stop: threading.Event) -> None:
    base = f"{socket.gethostname()}-{os.getpid()}"

    def loop(index: int) -> None:
        worker_id = f"{base}-{index}"[:64]
        while not stop.is_set():
            try:
                busy = run_once(settings, worker_id)
            except Exception as exc:  # e.g. the database is restarting; try again shortly
                logger.error("worker loop error error=%s", type(exc).__name__)
                busy = False
            if not busy:
                stop.wait(settings.worker_poll_seconds)

    threads = [
        threading.Thread(target=loop, args=(i,), name=f"worker-{i}")
        for i in range(settings.worker_concurrency)
    ]
    for thread in threads:
        thread.start()
    while True:
        try:
            maintain(settings)
        except Exception as exc:
            logger.error("maintenance error error=%s", type(exc).__name__)
        if stop.wait(MAINTENANCE_SECONDS):
            break
    for thread in threads:
        thread.join()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Request logs would include signed URLs (capabilities); never log them.
    for name in ("httpx", "httpx2", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    settings = get_settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is not set")
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    logger.info(
        "worker started concurrency=%d lease=%ds",
        settings.worker_concurrency,
        settings.job_lease_seconds,
    )
    serve(settings, stop)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
