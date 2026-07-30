"""Lifecycle management for in-process investigation tasks.

The API accepts an investigation quickly and the frontend polls the persisted
``CaseRecord`` while the existing Strands workflow runs.  Holding strong task
references here prevents background jobs from being garbage-collected and
also makes repeated start requests idempotent within one API process.
"""

from __future__ import annotations

import asyncio
import logging
from threading import RLock
from typing import Protocol

from .schemas import CaseRecord, CaseStatus
from .store import CaseStore


logger = logging.getLogger(__name__)


class InvestigationStarter(Protocol):
    async def start(self, case_id: str) -> CaseRecord: ...


class InvestigationTaskRegistry:
    """Start, retain, deduplicate, and clean up investigation tasks."""

    _TERMINAL_WITH_RESULT = {
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.APPROVED,
        CaseStatus.COMPLETED,
    }

    def __init__(
        self,
        *,
        pipeline: InvestigationStarter,
        store: CaseStore,
    ) -> None:
        self.pipeline = pipeline
        self.store = store
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    def is_running(self, case_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(case_id)
            return task is not None and not task.done()

    @property
    def active_case_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                case_id
                for case_id, task in self._tasks.items()
                if not task.done()
            )

    async def start(self, case_id: str) -> CaseRecord:
        """Queue one case without waiting for the full workflow.

        A PROCESSING record without a live task is treated as orphaned work
        from a previous API process and is safe to start again.  Cases already
        at the approval boundary or beyond are returned unchanged.
        """

        with self._lock:
            existing = self._tasks.get(case_id)
            if existing is not None and not existing.done():
                return self.store.get(case_id)
            if existing is not None:
                self._tasks.pop(case_id, None)

            record = self.store.get(case_id)
            if record.status in self._TERMINAL_WITH_RESULT:
                return record

            record.status = CaseStatus.PROCESSING
            record.current_stage = "QUEUED"
            record.completed_stages = []
            record.error = None
            accepted = self.store.save(record)

            task = asyncio.create_task(
                self._run(case_id),
                name=f"sitetrace-investigation-{case_id}",
            )
            self._tasks[case_id] = task
            return accepted

    async def _run(self, case_id: str) -> None:
        current = asyncio.current_task()
        try:
            await self.pipeline.start(case_id)
        except asyncio.CancelledError:
            # Shutdown cancellation deliberately leaves PROCESSING persisted.
            # A later process can recognize it as orphaned and resume safely.
            raise
        except Exception as exc:
            logger.exception("Investigation %s failed", case_id)
            try:
                record = self.store.get(case_id)
                record.status = CaseStatus.FAILED
                record.error = f"{type(exc).__name__}: {exc}"
                self.store.save(record)
            except Exception:
                logger.exception(
                    "Could not persist investigation failure for %s",
                    case_id,
                )
        finally:
            with self._lock:
                if self._tasks.get(case_id) is current:
                    self._tasks.pop(case_id, None)

    async def shutdown(self) -> None:
        """Cancel and await all live tasks during API shutdown."""

        with self._lock:
            tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

