from __future__ import annotations

import asyncio
from pathlib import Path

from app.pipeline import InvestigationPipeline
from app.schemas import CaseRecord, CaseStatus, UploadedInput
from app.store import CaseStore
from app.strands_orchestration import WorkflowExecutionContext, WorkflowStage
from app.task_registry import InvestigationTaskRegistry


def sample_record(status: CaseStatus = CaseStatus.UPLOADED) -> CaseRecord:
    return CaseRecord(
        case_id="CASE-ASYNC-1",
        title="Background investigation",
        status=status,
        jha=UploadedInput(
            filename="JHA.pdf",
            media_type="application/pdf",
            size_bytes=10,
        ),
        videos=[
            UploadedInput(
                filename="CAM-01.mp4",
                media_type="video/mp4",
                size_bytes=10,
            )
        ],
    )


class BlockingPipeline:
    def __init__(self, store: CaseStore) -> None:
        self.store = store
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def start(self, case_id: str) -> CaseRecord:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        record = self.store.get(case_id)
        record.status = CaseStatus.AWAITING_APPROVAL
        record.current_stage = WorkflowStage.HUMAN_APPROVAL.value
        return self.store.save(record)


class FailingPipeline:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()

    async def start(self, case_id: str) -> CaseRecord:
        self.calls += 1
        self.started.set()
        raise RuntimeError("upstream video analysis failed")


async def wait_until_finished(
    registry: InvestigationTaskRegistry,
    case_id: str,
) -> None:
    for _ in range(100):
        if not registry.is_running(case_id):
            return
        await asyncio.sleep(0)
    raise AssertionError("background task did not finish")


async def test_start_returns_processing_and_deduplicates_live_task(
    tmp_path: Path,
):
    store = CaseStore(tmp_path)
    store.save(sample_record())
    pipeline = BlockingPipeline(store)
    registry = InvestigationTaskRegistry(pipeline=pipeline, store=store)

    accepted = await registry.start("CASE-ASYNC-1")
    assert accepted.status == CaseStatus.PROCESSING
    assert accepted.current_stage == "QUEUED"
    assert registry.is_running("CASE-ASYNC-1")

    await pipeline.started.wait()
    duplicate = await registry.start("CASE-ASYNC-1")
    assert duplicate.status == CaseStatus.PROCESSING
    assert pipeline.calls == 1
    assert registry.active_case_ids == ("CASE-ASYNC-1",)

    pipeline.release.set()
    await wait_until_finished(registry, "CASE-ASYNC-1")
    assert store.get("CASE-ASYNC-1").status == CaseStatus.AWAITING_APPROVAL
    assert registry.active_case_ids == ()


async def test_background_failure_is_persisted_and_task_is_cleaned(
    tmp_path: Path,
):
    store = CaseStore(tmp_path)
    store.save(sample_record())
    pipeline = FailingPipeline()
    registry = InvestigationTaskRegistry(pipeline=pipeline, store=store)

    accepted = await registry.start("CASE-ASYNC-1")
    assert accepted.status == CaseStatus.PROCESSING
    await pipeline.started.wait()
    await wait_until_finished(registry, "CASE-ASYNC-1")

    failed = store.get("CASE-ASYNC-1")
    assert failed.status == CaseStatus.FAILED
    assert failed.error == "RuntimeError: upstream video analysis failed"
    assert pipeline.calls == 1
    assert not registry.is_running("CASE-ASYNC-1")


async def test_orphaned_processing_case_can_be_restarted(tmp_path: Path):
    store = CaseStore(tmp_path)
    store.save(sample_record(CaseStatus.PROCESSING))
    pipeline = BlockingPipeline(store)
    registry = InvestigationTaskRegistry(pipeline=pipeline, store=store)

    accepted = await registry.start("CASE-ASYNC-1")
    assert accepted.status == CaseStatus.PROCESSING
    await pipeline.started.wait()
    assert pipeline.calls == 1

    pipeline.release.set()
    await wait_until_finished(registry, "CASE-ASYNC-1")


async def test_terminal_case_is_not_restarted(tmp_path: Path):
    store = CaseStore(tmp_path)
    store.save(sample_record(CaseStatus.AWAITING_APPROVAL))
    pipeline = BlockingPipeline(store)
    registry = InvestigationTaskRegistry(pipeline=pipeline, store=store)

    record = await registry.start("CASE-ASYNC-1")
    assert record.status == CaseStatus.AWAITING_APPROVAL
    assert pipeline.calls == 0
    assert registry.active_case_ids == ()


async def test_tracked_handler_persists_current_and_completed_stage(
    tmp_path: Path,
):
    store = CaseStore(tmp_path)
    store.save(sample_record(CaseStatus.PROCESSING))
    pipeline = InvestigationPipeline(store=store)

    async def handler(_: WorkflowExecutionContext) -> dict[str, bool]:
        running = store.get("CASE-ASYNC-1")
        assert running.current_stage == WorkflowStage.PARSE_JHA.value
        return {"parsed": True}

    tracked = pipeline._tracked_handler(WorkflowStage.PARSE_JHA, handler)
    output = await tracked(
        WorkflowExecutionContext(
            run_id="RUN-1",
            case_id="CASE-ASYNC-1",
            stage=WorkflowStage.PARSE_JHA,
            request={},
            artifacts={},
            attempt=1,
        )
    )

    assert output == {"parsed": True}
    record = store.get("CASE-ASYNC-1")
    assert record.current_stage == WorkflowStage.PARSE_JHA.value
    assert record.completed_stages == [WorkflowStage.PARSE_JHA.value]

