"""Small, auditable case repository.

The hackathon build uses JSON snapshots on disk so every intermediate result can
be inspected.  The Strands session manager separately persists agent state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .config import settings
from .schemas import CaseRecord


UTC = timezone.utc


class CaseNotFoundError(KeyError):
    pass


class CaseStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or settings.case_directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, case_id: str) -> Path:
        safe_id = "".join(char for char in case_id if char.isalnum() or char in "-_")
        return self.directory / f"{safe_id}.json"

    def save(self, record: CaseRecord) -> CaseRecord:
        record.updated_at = datetime.now(UTC)
        payload = record.model_dump(mode="json")
        destination = self._path(record.case_id)
        temporary = destination.with_suffix(".json.tmp")
        with self._lock:
            temporary.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(destination)
        return record

    def get(self, case_id: str) -> CaseRecord:
        path = self._path(case_id)
        if not path.exists():
            raise CaseNotFoundError(case_id)
        with self._lock:
            return CaseRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[CaseRecord]:
        records: list[CaseRecord] = []
        for path in sorted(
            self.directory.glob("*.json"),
            key=lambda value: value.stat().st_mtime,
            reverse=True,
        ):
            try:
                records.append(
                    CaseRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
        return records


case_store = CaseStore()

