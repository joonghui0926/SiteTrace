"""Live SiteTrace investigation pipeline.

The business order is deterministic and owned by Strands.  Sponsor services
remain independently testable adapters:

* TwelveLabs extracts and cites multi-camera video evidence.
* OpenAI gives the evidence construction-safety meaning.
* Neo4j stores both graphs and computes plan-versus-observation differences.
* Strands validates, retries, persists, and pauses before publication.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from .config import settings
from .schemas import (
    CaseRecord,
    CaseStatus,
    CitedNarrativeClaim,
    CorrectiveAction,
    DeviationFinding,
    EvidenceClip,
    FindingStatus,
    InvestigationPackage,
    ObservedEvent,
    PlannedStep,
)
from .services.aws_service import AWSStorageService
from .services.neo4j_service import Neo4jService
from .services.openai_service import (
    EventStepMappingBatch,
    OpenAIService,
    PlanDocument,
)
from .services.twelvelabs_service import TwelveLabsService
from .store import CaseStore, case_store
from .strands_orchestration import (
    SiteTraceStrandsRuntime,
    WorkflowExecutionContext,
    WorkflowStage,
)


UTC = timezone.utc


def _identifier(prefix: str, value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    return f"{prefix}:{normalized or 'UNKNOWN'}"


def _safe_model_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages)
    if suffix in {".txt", ".md", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(
        f"Unsupported plan document {path.name}; upload PDF, TXT, MD, CSV, or JSON"
    )


def _metadata_for_case(record: CaseRecord) -> dict[str, Any]:
    if not record.site_metadata or not record.site_metadata.local_path:
        return {}
    path = Path(record.site_metadata.local_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _camera_metadata(payload: Mapping[str, Any], filename: str) -> dict[str, Any]:
    candidates = payload.get("cameras") or payload.get("videos") or []
    if isinstance(candidates, Mapping):
        candidates = [
            {"filename": key, **(value if isinstance(value, dict) else {})}
            for key, value in candidates.items()
        ]
    stem = Path(filename).stem.casefold()
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, Mapping):
            continue
        names = {
            str(candidate.get("filename", "")).casefold(),
            str(candidate.get("file", "")).casefold(),
            str(candidate.get("camera_id", "")).casefold(),
        }
        if filename.casefold() in names or stem in {Path(name).stem for name in names}:
            return dict(candidate)
    return {}


def _epoch_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _closest_clip(
    candidate: Mapping[str, Any],
    clips: list[EvidenceClip],
) -> EvidenceClip | None:
    camera = str(candidate.get("camera_id", "")).casefold()
    start = float(candidate.get("start_sec", 0.0) or 0.0)
    eligible = [
        clip
        for clip in clips
        if not camera or clip.camera_id.casefold() == camera
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda clip: abs(clip.start_sec - start),
    )


def _jockey_observations(
    resolved: Mapping[str, Any],
    clips: list[EvidenceClip],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for offset, candidate in enumerate(resolved.get("events") or [], start=1):
        if not isinstance(candidate, Mapping):
            continue
        clip = _closest_clip(candidate, clips)
        if clip is None:
            continue
        observations.append(
            {
                "event_id": f"JOCKEY_EVENT_{offset:04d}",
                "event_type": str(candidate.get("event_type", "CROSS_CAMERA_EVENT")),
                "summary": str(candidate.get("summary", "")),
                "camera_id": str(candidate.get("camera_id") or clip.camera_id),
                "start_sec": float(candidate.get("start_sec", clip.start_sec)),
                "end_sec": float(candidate.get("end_sec", clip.end_sec)),
                "actor_ids": list(candidate.get("actor_descriptions") or []),
                "object_ids": list(candidate.get("object_descriptions") or []),
                "zone_ids": list(candidate.get("zone_ids") or []),
                "evidence_clip_ids": [clip.evidence_clip_id],
                "confidence": float(candidate.get("confidence", clip.confidence)),
                "observation_type": "inferred",
                "connection_basis": str(candidate.get("connection_basis", "")),
            }
        )
    return observations


class InvestigationPipeline:
    """Coordinate one evidence-bounded case through the Strands workflow."""

    def __init__(
        self,
        *,
        store: CaseStore | None = None,
        openai: OpenAIService | None = None,
        twelvelabs: TwelveLabsService | None = None,
        neo4j: Neo4jService | None = None,
        aws_storage: AWSStorageService | None = None,
    ) -> None:
        self.store = store or case_store
        self.openai = openai or OpenAIService()
        self.twelvelabs = twelvelabs or TwelveLabsService()
        self.neo4j = neo4j or Neo4jService(
            uri=settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password,
            database=settings.neo4j_database,
        )
        self.aws_storage = aws_storage or AWSStorageService()
        self._runtimes: dict[str, SiteTraceStrandsRuntime] = {}

    def runtime_for(self, case_id: str) -> SiteTraceStrandsRuntime:
        if case_id not in self._runtimes:
            self._runtimes[case_id] = SiteTraceStrandsRuntime(session_id=case_id)
        return self._runtimes[case_id]

    async def start(self, case_id: str) -> CaseRecord:
        record = self.store.get(case_id)
        record.status = CaseStatus.PROCESSING
        record.current_stage = "QUEUED"
        record.completed_stages = []
        record.error = None
        self.store.save(record)
        runtime = self.runtime_for(case_id)
        run = runtime.workflow.create_run(
            case_id=case_id,
            request={"case_id": case_id, "title": record.title},
        )
        record.workflow_run_id = run.run_id
        self.store.save(record)
        result = await runtime.workflow.start_async(
            run.run_id,
            self.handlers(case_id, runtime),
        )
        record = self.store.get(case_id)
        if result.status.value == "AWAITING_APPROVAL":
            record.status = CaseStatus.AWAITING_APPROVAL
            record.current_stage = WorkflowStage.HUMAN_APPROVAL.value
        elif result.status.value == "FAILED":
            record.status = CaseStatus.FAILED
            record.error = result.failure
        self.store.save(record)
        return record

    async def approve(
        self,
        case_id: str,
        *,
        approved: bool,
        reviewer: str,
    ) -> CaseRecord:
        record = self.store.get(case_id)
        if not record.workflow_run_id:
            raise ValueError("Case has no Strands workflow run")
        runtime = self.runtime_for(case_id)
        result = await runtime.workflow.resume_after_approval_async(
            record.workflow_run_id,
            approved=approved,
            approved_by=reviewer,
            handlers=self.handlers(case_id, runtime),
        )
        record = self.store.get(case_id)
        if approved and result.status.value == "COMPLETED":
            record.status = CaseStatus.COMPLETED
            record.current_stage = None
            if WorkflowStage.HUMAN_APPROVAL.value not in record.completed_stages:
                record.completed_stages.append(WorkflowStage.HUMAN_APPROVAL.value)
            record.approved_by = reviewer
            record.approved_at = datetime.now(UTC)
        elif not approved:
            record.status = CaseStatus.UPLOADED
            record.current_stage = None
        self.store.save(record)
        return record

    def handlers(
        self,
        case_id: str,
        runtime: SiteTraceStrandsRuntime,
    ) -> dict[WorkflowStage, Any]:
        raw_handlers: dict[WorkflowStage, Any] = {
            WorkflowStage.PARSE_JHA: self._parse_jha,
            WorkflowStage.ANALYZE_CAMERAS: self._analyze_cameras,
            WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS: self._connect_events,
            WorkflowStage.WRITE_CONTEXT_GRAPH: self._write_graph,
            WorkflowStage.COMPARE_PLANNED_OBSERVED: self._compare,
            WorkflowStage.VERIFY_EVIDENCE: lambda context: self._verify(
                context, runtime
            ),
            WorkflowStage.DRAFT_INVESTIGATION_REPORT: lambda context: self._draft(
                context, runtime
            ),
            WorkflowStage.PUBLISH_REPORT: self._publish,
        }
        return {
            stage: self._tracked_handler(stage, handler)
            for stage, handler in raw_handlers.items()
        }

    def _tracked_handler(self, stage: WorkflowStage, handler: Any) -> Any:
        """Persist stage progress around an otherwise unchanged handler."""

        async def tracked(context: WorkflowExecutionContext) -> Any:
            record = self.store.get(context.case_id)
            record.current_stage = stage.value
            self.store.save(record)
            output = handler(context)
            if inspect.isawaitable(output):
                output = await output
            record = self.store.get(context.case_id)
            if stage.value not in record.completed_stages:
                record.completed_stages.append(stage.value)
            self.store.save(record)
            return output

        return tracked

    async def _parse_jha(self, context: WorkflowExecutionContext) -> dict[str, Any]:
        record = self.store.get(context.case_id)
        inputs = [record.jha, *record.supporting_documents]
        documents: list[PlanDocument] = []
        for item in inputs:
            if not item.local_path:
                continue
            text = await asyncio.to_thread(_read_document, Path(item.local_path))
            if text.strip():
                documents.append(PlanDocument(filename=item.filename, text=text))
        if not documents:
            raise ValueError("No readable JHA or supporting plan text was supplied")
        result = await self.openai.parse_plan_documents(documents)
        if not result.data.planned_steps:
            raise ValueError("OpenAI found no actionable JHA steps")
        return {
            "planned_steps": [
                step.model_dump(mode="json") for step in result.data.planned_steps
            ],
            "unresolved_requirements": result.data.unresolved_requirements,
            "response_id": result.response_id,
            "model": result.model,
        }

    async def _analyze_cameras(
        self,
        context: WorkflowExecutionContext,
    ) -> dict[str, Any]:
        record = self.store.get(context.case_id)
        metadata = _metadata_for_case(record)
        store_id = await asyncio.to_thread(
            self.twelvelabs.create_knowledge_store,
            context.case_id,
        )

        async def analyze(index: int, item: Any) -> dict[str, Any]:
            if not item.local_path:
                raise ValueError(f"Video {item.filename} is not available locally")
            camera_info = _camera_metadata(metadata, item.filename)
            camera_id = str(
                camera_info.get("camera_id")
                or item.camera_id
                or Path(item.filename).stem
                or f"CAM-{index + 1:02d}"
            )
            asset_id = await asyncio.to_thread(
                self.twelvelabs.upload_asset,
                Path(item.local_path),
            )
            item_id = await asyncio.to_thread(
                self.twelvelabs.add_asset_to_store,
                store_id,
                asset_id,
                camera_id=camera_id,
                filename=item.filename,
            )
            clips, events = await asyncio.to_thread(
                self.twelvelabs.segment_asset,
                asset_id,
                camera_id=camera_id,
                filename=item.filename,
                item_id=item_id,
            )
            return {
                "filename": item.filename,
                "camera_id": camera_id,
                "asset_id": asset_id,
                "item_id": item_id,
                "recording_started_at": camera_info.get("recording_started_at"),
                "clips": [clip.model_dump(mode="json") for clip in clips],
                "events": [event.model_dump(mode="json") for event in events],
            }

        analyzed = await asyncio.gather(
            *(analyze(index, item) for index, item in enumerate(record.videos))
        )
        by_filename = {result["filename"]: result for result in analyzed}
        for item in record.videos:
            result = by_filename[item.filename]
            item.camera_id = result["camera_id"]
            item.asset_id = result["asset_id"]
            item.knowledge_store_item_id = result["item_id"]
        self.store.save(record)
        return {
            "knowledge_store_id": store_id,
            "videos": analyzed,
            "pegasus_model": settings.twelve_labs_pegasus_model,
        }

    async def _connect_events(
        self,
        context: WorkflowExecutionContext,
    ) -> dict[str, Any]:
        analyzed = context.artifacts[WorkflowStage.ANALYZE_CAMERAS.value]
        clips = [
            EvidenceClip.model_validate(clip)
            for video in analyzed["videos"]
            for clip in video["clips"]
        ]
        raw_events = [
            event
            for video in analyzed["videos"]
            for event in video["events"]
        ]
        jockey, session_id, intermediate = await asyncio.to_thread(
            self.twelvelabs.resolve_cross_video_events,
            analyzed["knowledge_store_id"],
        )
        raw_events.extend(_jockey_observations(jockey, clips))
        normalized = await self.openai.normalize_observed_events(
            raw_events,
            allowed_evidence_clip_ids=[
                clip.evidence_clip_id for clip in clips
            ],
        )
        return {
            "events": [
                event.model_dump(mode="json") for event in normalized.data.events
            ],
            "clips": [clip.model_dump(mode="json") for clip in clips],
            "unverified_observations": normalized.data.unverified_observations,
            "jockey_session_id": session_id,
            "jockey_unresolved_connections": jockey.get(
                "unresolved_connections", []
            ),
            "jockey_intermediate_outputs": intermediate,
            "response_id": normalized.response_id,
            "model": normalized.model,
        }

    async def _write_graph(self, context: WorkflowExecutionContext) -> dict[str, Any]:
        record = self.store.get(context.case_id)
        parsed = context.artifacts[WorkflowStage.PARSE_JHA.value]
        analyzed = context.artifacts[WorkflowStage.ANALYZE_CAMERAS.value]
        connected = context.artifacts[
            WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS.value
        ]
        steps = [PlannedStep.model_validate(value) for value in parsed["planned_steps"]]
        events = [ObservedEvent.model_validate(value) for value in connected["events"]]
        clips = [EvidenceClip.model_validate(value) for value in connected["clips"]]
        mappings = await self.openai.match_events_to_steps(
            steps,
            events,
            allowed_evidence_clip_ids=[
                clip.evidence_clip_id for clip in clips
            ],
        )
        jha, control_lookup = self._neo4j_plan(record, steps)
        camera_start_ms = {
            str(video["camera_id"]).casefold(): _epoch_ms(
                video.get("recording_started_at")
            )
            for video in analyzed["videos"]
        }
        graph_events = self._neo4j_events(
            events,
            mappings.data,
            control_lookup,
            camera_start_ms,
        )
        graph_videos = await self._neo4j_videos(record, analyzed, clips)
        coverage = [
            {
                "id": f"{context.case_id}:COVERAGE:{step.step_id}",
                "sufficient": False,
                "reason": (
                    "No independent camera-coverage assessment was provided; "
                    "missing footage is not proof of non-performance."
                ),
                "assessed_by": "SiteTrace coverage validator",
                "step_ids": [step.step_id],
                "video_ids": [
                    bundle["video"]["id"] for bundle in graph_videos
                ],
                "evidence_clip_ids": [],
            }
            for step in steps
        ]
        await self.neo4j.upsert_case_graph(
            case_id=context.case_id,
            jha=jha,
            case_title=record.title,
            case_status="INVESTIGATING",
            videos=graph_videos,
            events=graph_events,
            coverage=coverage,
        )
        return {
            "jha": jha,
            "mappings": mappings.data.model_dump(mode="json"),
            "events": graph_events,
            "videos": graph_videos,
            "coverage": coverage,
            "response_id": mappings.response_id,
            "model": mappings.model,
        }

    async def _compare(self, context: WorkflowExecutionContext) -> dict[str, Any]:
        parsed = context.artifacts[WorkflowStage.PARSE_JHA.value]
        connected = context.artifacts[
            WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS.value
        ]
        written = context.artifacts[WorkflowStage.WRITE_CONTEXT_GRAPH.value]
        steps = [PlannedStep.model_validate(value) for value in parsed["planned_steps"]]
        events = [ObservedEvent.model_validate(value) for value in connected["events"]]
        mappings = EventStepMappingBatch.model_validate(written["mappings"])
        allowed_clips = {
            clip["evidence_clip_id"] for clip in connected["clips"]
        }
        graph_rows = await self.neo4j.run_graph_diff(context.case_id)
        graph_findings = self._findings_from_graph_rows(
            context.case_id,
            graph_rows,
            events,
        )
        analysis = await self.openai.analyze_deviations(
            steps,
            events,
            mappings,
            allowed_evidence_clip_ids=allowed_clips,
        )
        findings = self._merge_findings(
            graph_findings,
            analysis.data.findings,
        )
        await self.neo4j.upsert_findings(
            case_id=context.case_id,
            findings=[self._neo4j_finding(value) for value in findings],
        )
        subgraph = await self.neo4j.get_evidence_subgraph(context.case_id)
        metrics = await self.neo4j.case_metrics(context.case_id)
        return {
            "findings": [
                finding.model_dump(mode="json") for finding in findings
            ],
            "observed_sequence": [
                claim.model_dump(mode="json")
                for claim in analysis.data.observed_sequence
            ],
            "limitations": [
                *analysis.data.limitations,
                *connected.get("unverified_observations", []),
                *connected.get("jockey_unresolved_connections", []),
            ],
            "graph_rows": graph_rows,
            "subgraph": subgraph,
            "metrics": metrics,
            "response_id": analysis.response_id,
            "model": analysis.model,
            "sol_escalated": analysis.escalated,
        }

    async def _verify(
        self,
        context: WorkflowExecutionContext,
        runtime: SiteTraceStrandsRuntime,
    ) -> dict[str, Any]:
        connected = context.artifacts[
            WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS.value
        ]
        compared = context.artifacts[
            WorkflowStage.COMPARE_PLANNED_OBSERVED.value
        ]
        clip_ids = {
            clip["evidence_clip_id"] for clip in connected["clips"]
        }
        invalid: list[str] = []
        for finding in compared["findings"]:
            status = finding["status"]
            citations = set(finding.get("evidence_clip_ids") or [])
            if status != FindingStatus.UNVERIFIABLE.value and not citations:
                invalid.append(f'{finding["finding_id"]}: no citation')
            unknown = citations - clip_ids
            if unknown:
                invalid.append(
                    f'{finding["finding_id"]}: unknown citations {sorted(unknown)}'
                )
        if invalid:
            raise ValueError("; ".join(invalid))
        specialist = await asyncio.to_thread(
            runtime.invoke_specialist,
            "evidence",
            {
                "events": connected["events"],
                "findings": compared["findings"],
                "allowed_evidence_clip_ids": sorted(clip_ids),
            },
        )
        return {
            "verified": True,
            "allowed_evidence_clip_ids": sorted(clip_ids),
            "strands_evidence_agent": specialist.model_dump(mode="json"),
        }

    async def _draft(
        self,
        context: WorkflowExecutionContext,
        runtime: SiteTraceStrandsRuntime,
    ) -> dict[str, Any]:
        record = self.store.get(context.case_id)
        parsed = context.artifacts[WorkflowStage.PARSE_JHA.value]
        analyzed = context.artifacts[WorkflowStage.ANALYZE_CAMERAS.value]
        connected = context.artifacts[
            WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS.value
        ]
        compared = context.artifacts[
            WorkflowStage.COMPARE_PLANNED_OBSERVED.value
        ]
        verified = context.artifacts[WorkflowStage.VERIFY_EVIDENCE.value]
        steps = [PlannedStep.model_validate(value) for value in parsed["planned_steps"]]
        events = [ObservedEvent.model_validate(value) for value in connected["events"]]
        findings = [
            DeviationFinding.model_validate(value)
            for value in compared["findings"]
        ]
        narrative = await self.openai.draft_report(
            case_id=context.case_id,
            title=record.title,
            planned_steps=steps,
            observed_events=events,
            findings=findings,
            limitations=compared["limitations"],
            allowed_evidence_clip_ids=verified["allowed_evidence_clip_ids"],
        )
        strands_report = await asyncio.to_thread(
            runtime.invoke_specialist,
            "report",
            {
                "report_id": f"{context.case_id}:DRAFT",
                "title": narrative.data.title,
                "findings": compared["findings"],
                "limitations": narrative.data.limitations,
            },
        )
        summary = " ".join(
            claim.text for claim in narrative.data.incident_overview
        ).strip()
        if not summary:
            summary = (
                "The uploaded footage was reconstructed into an evidence-backed "
                "event sequence for qualified human review."
            )
        package = InvestigationPackage(
            case_id=context.case_id,
            title=narrative.data.title,
            incident_summary=summary,
            incident_overview=[
                CitedNarrativeClaim.model_validate(
                    claim.model_dump(mode="json")
                )
                for claim in narrative.data.incident_overview
            ],
            event_timeline=[
                CitedNarrativeClaim.model_validate(
                    claim.model_dump(mode="json")
                )
                for claim in narrative.data.event_timeline
            ],
            deviation_summary=[
                CitedNarrativeClaim.model_validate(
                    claim.model_dump(mode="json")
                )
                for claim in narrative.data.deviation_summary
            ],
            planned_steps=steps,
            events=events,
            evidence_clips=[
                EvidenceClip.model_validate(value) for value in connected["clips"]
            ],
            findings=findings,
            corrective_actions=narrative.data.corrective_actions,
            limitations=narrative.data.limitations,
            knowledge_store_id=analyzed["knowledge_store_id"],
            jockey_session_id=connected.get("jockey_session_id"),
            graph_metrics=compared["metrics"],
            sponsor_trace=[
                {
                    "sponsor": "TwelveLabs",
                    "features": [
                        "Pegasus 1.5 segmentation",
                        "Marengo 3.0 embeddings",
                        "Jockey cross-video reasoning",
                        "clip citations",
                    ],
                    "model": analyzed["pegasus_model"],
                },
                {
                    "sponsor": "OpenAI",
                    "features": [
                        "Responses API",
                        "Structured Outputs",
                        "Luna normalization",
                        "Terra JHA reasoning",
                        "Sol conflict-only escalation",
                    ],
                    "model": narrative.model,
                    "response_id": narrative.response_id,
                },
                {
                    "sponsor": "Neo4j",
                    "features": [
                        "AuraDB",
                        "Marengo vector index",
                        "Cypher traversal",
                        "deterministic graph diff",
                    ],
                    "metrics": compared["metrics"],
                },
                {
                    "sponsor": "AWS Strands",
                    "features": [
                        "Workflow",
                        "agents as tools",
                        "hooks",
                        "skills",
                        "sessions",
                        "human approval interrupt",
                        "OpenTelemetry",
                    ],
                    "evidence_agent": verified["strands_evidence_agent"],
                    "report_agent": strands_report.model_dump(mode="json"),
                },
            ],
        )
        record.investigation = package
        record.status = CaseStatus.AWAITING_APPROVAL
        self.store.save(record)
        return {
            "investigation": package.model_dump(mode="json"),
            "human_approval_required": True,
        }

    async def _publish(self, context: WorkflowExecutionContext) -> dict[str, Any]:
        from .services.report_service import ReportService

        record = self.store.get(context.case_id)
        if record.investigation is None:
            raise ValueError("No approved investigation package is available")
        report_path = await asyncio.to_thread(
            ReportService().generate,
            record.investigation,
            output_directory=settings.report_directory,
        )
        record.report_path = str(report_path)
        record.report_s3_uri = await asyncio.to_thread(
            self.aws_storage.upload_file,
            Path(report_path),
            key=f"cases/{context.case_id}/reports/{Path(report_path).name}",
            content_type="application/pdf",
        )
        record.status = CaseStatus.COMPLETED
        self.store.save(record)
        return {
            "report_path": str(report_path),
            "report_s3_uri": record.report_s3_uri,
            "published": True,
        }

    @staticmethod
    def _neo4j_plan(
        record: CaseRecord,
        steps: list[PlannedStep],
    ) -> tuple[dict[str, Any], dict[tuple[str, str], str]]:
        control_lookup: dict[tuple[str, str], str] = {}
        rows: list[dict[str, Any]] = []
        for step in steps:
            controls: list[dict[str, Any]] = []
            for index, title in enumerate(step.required_controls, start=1):
                control_id = _identifier(
                    "CONTROL",
                    f"{step.step_id}_{index}_{title}",
                )
                control_lookup[(step.step_id, title)] = control_id
                controls.append(
                    {
                        "id": control_id,
                        "code": f"{step.step_id}-C{index}",
                        "title": title,
                        "description": title,
                        "required_role": (
                            step.required_roles[0]
                            if step.required_roles
                            else ""
                        ),
                    }
                )
            rows.append(
                {
                    "id": step.step_id,
                    "step_order": step.sequence,
                    "title": step.name,
                    "description": step.description,
                    "required_controls": controls,
                    "prohibited_zones": [
                        {
                            "id": _identifier("ZONE", name),
                            "name": name,
                            "kind": "prohibited",
                        }
                        for name in step.must_avoid_zones
                    ],
                    "approved_zones": [],
                }
            )
        return (
            {
                "id": _identifier("JHA", record.jha.filename),
                "title": record.jha.filename,
                "version": "",
                "source_uri": record.jha.s3_uri or record.jha.local_path or "",
                "steps": rows,
            },
            control_lookup,
        )

    @staticmethod
    def _neo4j_events(
        events: list[ObservedEvent],
        mappings: EventStepMappingBatch,
        control_lookup: dict[tuple[str, str], str],
        camera_start_ms: Mapping[str, int | None] | None = None,
    ) -> list[dict[str, Any]]:
        camera_start_ms = camera_start_ms or {}
        by_event = {mapping.event_id: mapping for mapping in mappings.mappings}
        output: list[dict[str, Any]] = []
        by_camera: dict[str, list[ObservedEvent]] = defaultdict(list)
        for event in events:
            by_camera[event.camera_id].append(event)
        next_by_event: dict[str, list[str]] = defaultdict(list)
        for camera_events in by_camera.values():
            ordered = sorted(camera_events, key=lambda value: value.start_sec)
            for first, second in zip(ordered, ordered[1:]):
                next_by_event[first.event_id].append(second.event_id)
        events_with_absolute_time = [
            (
                camera_start_ms.get(event.camera_id.casefold())
                + int(event.start_sec * 1000),
                event,
            )
            for event in events
            if camera_start_ms.get(event.camera_id.casefold()) is not None
        ]
        events_with_absolute_time.sort(key=lambda item: item[0])
        for (_, first), (_, second) in zip(
            events_with_absolute_time,
            events_with_absolute_time[1:],
        ):
            if second.event_id not in next_by_event[first.event_id]:
                next_by_event[first.event_id].append(second.event_id)
        for event in events:
            mapping = by_event.get(event.event_id)
            recording_start = camera_start_ms.get(event.camera_id.casefold())
            step_ids = mapping.matched_step_ids if mapping else []
            satisfied = []
            if mapping:
                for step_id in mapping.matched_step_ids:
                    for control in mapping.satisfied_controls:
                        control_id = control_lookup.get((step_id, control))
                        if control_id:
                            satisfied.append(control_id)
            entities = [
                {
                    "id": _identifier("PERSON", value),
                    "name": value,
                    "kind": "person_candidate",
                }
                for value in event.actor_ids
            ] + [
                {
                    "id": _identifier("OBJECT", value),
                    "name": value,
                    "kind": "equipment_or_material",
                }
                for value in event.object_ids
            ]
            zone_ids = [_identifier("ZONE", value) for value in event.zone_ids]
            blocked = (
                zone_ids
                if any(
                    token in event.event_type.upper()
                    for token in ("BLOCK", "EGRESS_STATE_CHANGE")
                )
                else []
            )
            output.append(
                {
                    "id": event.event_id,
                    "event_type": event.event_type,
                    "summary": event.summary,
                    "observation_status": (
                        "OBSERVED"
                        if event.observation_type == "observed"
                        else "INFERRED"
                    ),
                    "confidence": event.confidence,
                    "global_start_ms": (
                        recording_start + int(event.start_sec * 1000)
                        if recording_start is not None
                        else None
                    ),
                    "global_end_ms": (
                        recording_start + int(event.end_sec * 1000)
                        if recording_start is not None
                        else None
                    ),
                    "evidence_clip_ids": event.evidence_clip_ids,
                    "segment_ids": [
                        _identifier("SEGMENT", clip_id)
                        for clip_id in event.evidence_clip_ids
                    ],
                    "matched_step_ids": step_ids,
                    "satisfied_control_ids": sorted(set(satisfied)),
                    "zone_ids": zone_ids,
                    "blocked_zone_ids": blocked,
                    "entities": entities,
                    "precedes_event_ids": next_by_event[event.event_id],
                    "sequence_basis": (
                        "camera metadata absolute timestamp"
                        if recording_start is not None
                        else "same-camera relative timestamp"
                    ),
                    "sequence_confidence": 1.0,
                }
            )
        return output

    async def _neo4j_videos(
        self,
        record: CaseRecord,
        analyzed: Mapping[str, Any],
        clips: list[EvidenceClip],
    ) -> list[dict[str, Any]]:
        clip_by_filename: dict[str, list[EvidenceClip]] = defaultdict(list)
        for clip in clips:
            clip_by_filename[clip.source_filename].append(clip)
        input_by_filename = {video.filename: video for video in record.videos}
        output: list[dict[str, Any]] = []
        for video in analyzed["videos"]:
            source = input_by_filename[video["filename"]]
            start_epoch = _epoch_ms(video.get("recording_started_at"))
            segments: list[dict[str, Any]] = []
            for index, clip in enumerate(
                sorted(
                    clip_by_filename[video["filename"]],
                    key=lambda value: value.start_sec,
                )
            ):
                embedding = await asyncio.to_thread(
                    self.twelvelabs.embed_text,
                    " ".join(
                        value
                        for value in (
                            clip.summary,
                            clip.transcript,
                            clip.visible_text,
                        )
                        if value
                    ),
                )
                segments.append(
                    {
                        "id": _identifier("SEGMENT", clip.evidence_clip_id),
                        "segment_index": index,
                        "start_sec": clip.start_sec,
                        "end_sec": clip.end_sec,
                        "global_start_ms": (
                            start_epoch + int(clip.start_sec * 1000)
                            if start_epoch is not None
                            else None
                        ),
                        "global_end_ms": (
                            start_epoch + int(clip.end_sec * 1000)
                            if start_epoch is not None
                            else None
                        ),
                        "summary": clip.summary,
                        "transcript": clip.transcript,
                        "on_screen_text": clip.visible_text,
                        "embedding_model": settings.twelve_labs_marengo_model,
                        "embedding": embedding,
                        "evidence": {
                            "id": clip.evidence_clip_id,
                            "start_sec": clip.start_sec,
                            "end_sec": clip.end_sec,
                            "source_uri": source.s3_uri
                            or source.local_path
                            or "",
                            "item_reference": clip.item_id or "",
                            "citation_label": (
                                f"{clip.camera_id} "
                                f"{clip.start_sec:.2f}-{clip.end_sec:.2f}s"
                            ),
                        },
                    }
                )
            digest = ""
            if source.local_path and Path(source.local_path).exists():
                digest = await asyncio.to_thread(
                    _sha256_file,
                    Path(source.local_path),
                )
            output.append(
                {
                    "video": {
                        "id": _identifier("VIDEO", video["filename"]),
                        "camera_id": video["camera_id"],
                        "title": video["filename"],
                        "recording_started_at": video.get(
                            "recording_started_at"
                        ),
                        "duration_sec": max(
                            (segment["end_sec"] for segment in segments),
                            default=0,
                        ),
                        "storage_uri": source.s3_uri
                        or source.local_path
                        or "",
                        "sha256": digest,
                        "twelvelabs_asset_id": video["asset_id"],
                    },
                    "segments": segments,
                }
            )
        return output

    @staticmethod
    def _findings_from_graph_rows(
        case_id: str,
        rows: list[dict[str, Any]],
        events: list[ObservedEvent],
    ) -> list[DeviationFinding]:
        event_lookup = {event.event_id: event for event in events}
        findings: list[DeviationFinding] = []
        for index, row in enumerate(rows, start=1):
            status = FindingStatus(str(row.get("status", "UNVERIFIABLE")))
            event_id = row.get("event_id")
            observed = (
                event_lookup[event_id].summary
                if event_id in event_lookup
                else None
            )
            finding_type = str(row.get("finding_type", "GRAPH_DIFF"))
            title = {
                "REQUIRED_CONTROL": "Required control needs verification",
                "PROHIBITED_ZONE": "Observed work conflicts with a restricted zone",
                "SEQUENCE_REVERSAL": "Observed sequence differs from the approved plan",
            }.get(finding_type, "Plan and observed work differ")
            node_ids = [
                str(value)
                for value in (
                    row.get("step_id"),
                    row.get("control_id"),
                    event_id,
                    row.get("zone_id"),
                )
                if value
            ]
            findings.append(
                DeviationFinding(
                    finding_id=f"{case_id}:GRAPH:{index:03d}",
                    jha_step_id=str(row.get("step_id") or "UNRESOLVED"),
                    status=status,
                    title=title,
                    planned_control=str(row.get("planned_control") or ""),
                    observed_work=observed,
                    evidence_clip_ids=list(
                        row.get("evidence_clip_ids") or []
                    ),
                    graph_path_node_ids=node_ids,
                    graph_path_relationships=[
                        "REQUIRES",
                        "MATCHES_STEP",
                        "SUPPORTED_BY",
                    ][: max(len(node_ids) - 1, 0)],
                    confidence=0.92
                    if status == FindingStatus.CONFIRMED_DEVIATION
                    else 0.45,
                    evidence_gap_reason=(
                        str(row.get("rationale") or "")
                        if status == FindingStatus.UNVERIFIABLE
                        else None
                    ),
                    requires_human_review=True,
                )
            )
        return findings

    @staticmethod
    def _merge_findings(
        graph_findings: list[DeviationFinding],
        model_findings: list[DeviationFinding],
    ) -> list[DeviationFinding]:
        merged: list[DeviationFinding] = []
        signatures: set[tuple[str, str, str]] = set()
        for finding in [*graph_findings, *model_findings]:
            signature = (
                finding.jha_step_id,
                finding.planned_control.casefold(),
                finding.status.value,
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            merged.append(finding)
        return merged

    @staticmethod
    def _neo4j_finding(finding: DeviationFinding) -> dict[str, Any]:
        return {
            "id": finding.finding_id,
            "finding_type": finding.status.value,
            "status": finding.status.value,
            "rationale": finding.observed_work
            or finding.evidence_gap_reason
            or finding.title,
            "confidence": finding.confidence,
            "human_confirmed": False,
            "step_ids": [finding.jha_step_id],
            "control_ids": [],
            "event_ids": [],
            "zone_ids": [],
            "evidence_clip_ids": finding.evidence_clip_ids,
        }
