"""Strands orchestration primitives for the SiteTrace investigation workflow.

This module deliberately does not implement HTTP routes or sponsor API clients.
The business layer supplies deterministic stage handlers for TwelveLabs,
Neo4j, OpenAI, report storage, and publication.  The orchestration layer adds:

* an auditable, resumable stage order with bounded retries;
* Evidence, Compliance, and Report specialist Strands agents exposed as tools;
* typed Pydantic outputs and a citation guard;
* a native Strands human-approval interrupt before publication;
* local FileSessionManager or production S3SessionManager selection; and
* optional OpenTelemetry setup.

It remains importable when Strands, OpenAI credentials, or AWS credentials are
not installed.  In that mode the deterministic workflow still runs with
injected handlers, which keeps local tests and the evidence UI usable.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import settings as app_settings

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as otel_trace
except Exception:  # pragma: no cover - optional observability dependency.
    otel_trace = None

try:  # Optional at import time so the demo remains runnable without Strands.
    from strands import Agent, AgentSkills, tool
    from strands.hooks import (
        AfterToolCallEvent,
        BeforeToolCallEvent,
        HookProvider,
        HookRegistry,
    )
    from strands.models.openai_responses import OpenAIResponsesModel
    from strands.session import FileSessionManager, S3SessionManager
    from strands.telemetry import StrandsTelemetry

    STRANDS_AVAILABLE = True
    _STRANDS_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised in dependency-light envs.
    STRANDS_AVAILABLE = False
    _STRANDS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

    class HookProvider:  # type: ignore[no-redef]
        """Compatibility base class used when Strands is not installed."""

    class HookRegistry:  # type: ignore[no-redef]
        """Compatibility type used only for annotations."""

    AfterToolCallEvent = Any  # type: ignore[misc,assignment]
    BeforeToolCallEvent = Any  # type: ignore[misc,assignment]
    Agent = Any  # type: ignore[misc,assignment]
    AgentSkills = Any  # type: ignore[misc,assignment]
    OpenAIResponsesModel = Any  # type: ignore[misc,assignment]
    FileSessionManager = Any  # type: ignore[misc,assignment]
    S3SessionManager = Any  # type: ignore[misc,assignment]
    StrandsTelemetry = Any  # type: ignore[misc,assignment]

    def tool(function: Callable[..., Any]) -> Callable[..., Any]:  # type: ignore[no-redef]
        return function


UTC = timezone.utc
_SKILLS_DIRECTORY = Path(__file__).resolve().parents[1] / "skills"
_DEFAULT_SESSION_DIRECTORY = Path(app_settings.sitetrace_session_directory)


class FindingStatus(StrEnum):
    """The only finding states allowed in a SiteTrace investigation."""

    COMPLIANT = "COMPLIANT"
    CONFIRMED_DEVIATION = "CONFIRMED_DEVIATION"
    REQUIRED_CONTROL_NOT_OBSERVED = "REQUIRED_CONTROL_NOT_OBSERVED"
    UNVERIFIABLE = "UNVERIFIABLE"


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class WorkflowStage(StrEnum):
    """Deterministic, audit-friendly SiteTrace execution order."""

    PARSE_JHA = "parse_jha"
    ANALYZE_CAMERAS = "analyze_cameras"
    CONNECT_CROSS_CAMERA_EVENTS = "connect_cross_camera_events"
    WRITE_CONTEXT_GRAPH = "write_context_graph"
    COMPARE_PLANNED_OBSERVED = "compare_planned_observed"
    VERIFY_EVIDENCE = "verify_evidence"
    DRAFT_INVESTIGATION_REPORT = "draft_investigation_report"
    HUMAN_APPROVAL = "human_approval"
    PUBLISH_REPORT = "publish_report"


WORKFLOW_ORDER: tuple[WorkflowStage, ...] = (
    WorkflowStage.PARSE_JHA,
    WorkflowStage.ANALYZE_CAMERAS,
    WorkflowStage.CONNECT_CROSS_CAMERA_EVENTS,
    WorkflowStage.WRITE_CONTEXT_GRAPH,
    WorkflowStage.COMPARE_PLANNED_OBSERVED,
    WorkflowStage.VERIFY_EVIDENCE,
    WorkflowStage.DRAFT_INVESTIGATION_REPORT,
    WorkflowStage.HUMAN_APPROVAL,
    WorkflowStage.PUBLISH_REPORT,
)


class EvidenceClip(BaseModel):
    """A source-video citation with a stable clip identifier."""

    model_config = ConfigDict(extra="forbid")

    evidence_clip_id: str = Field(min_length=1)
    video_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    source_uri: str | None = None
    transcript: str | None = None
    observed_text: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "EvidenceClip":
        if self.end_sec <= self.start_sec:
            raise ValueError("Evidence clip end_sec must be greater than start_sec")
        return self


class ObservedEvent(BaseModel):
    """A normalized observation backed by one or more video clips."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    actor_or_object_ids: list[str] = Field(default_factory=list)
    zone_ids: list[str] = Field(default_factory=list)
    occurred_at: str | None = None
    evidence_clip_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class EvidenceAgentOutput(BaseModel):
    """Typed output from the Evidence specialist."""

    model_config = ConfigDict(extra="forbid")

    events: list[ObservedEvent] = Field(default_factory=list)
    evidence_clips: list[EvidenceClip] = Field(default_factory=list)
    unresolved_entity_candidates: list[str] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)


class InvestigationFinding(BaseModel):
    """A JHA comparison finding with enforced evidentiary discipline."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    jha_step_id: str = Field(min_length=1)
    status: FindingStatus
    claim: str = Field(min_length=1)
    planned_control: str = Field(min_length=1)
    observed_work: str | None = None
    evidence_clip_ids: list[str] = Field(default_factory=list)
    graph_path_node_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_gap_reason: str | None = None
    requires_human_review: bool = True

    @model_validator(mode="after")
    def enforce_evidence(self) -> "InvestigationFinding":
        if self.status is FindingStatus.UNVERIFIABLE:
            if not self.evidence_gap_reason:
                raise ValueError(
                    "UNVERIFIABLE findings require evidence_gap_reason"
                )
            return self
        if not self.evidence_clip_ids:
            raise ValueError(
                "Every factual finding must include at least one evidence clip ID"
            )
        return self


class ComplianceAgentOutput(BaseModel):
    """Typed output from the Compliance specialist."""

    model_config = ConfigDict(extra="forbid")

    findings: list[InvestigationFinding] = Field(default_factory=list)
    graph_query_ids: list[str] = Field(default_factory=list)
    escalation_required: bool = False
    escalation_reason: str | None = None


class ReportClaim(BaseModel):
    """A report sentence or paragraph that can be traced to source clips."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_clip_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)


class CorrectiveActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    action_type: str = Field(
        description="One of immediate, corrective, or preventive"
    )
    owner_role: str | None = None
    due_date: str | None = None
    supported_finding_ids: list[str] = Field(default_factory=list)


class ReportAgentOutput(BaseModel):
    """Typed, pre-approval investigation-report draft."""

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    executive_summary: list[ReportClaim] = Field(default_factory=list)
    event_timeline: list[ReportClaim] = Field(default_factory=list)
    jha_variance_review: list[ReportClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    corrective_actions: list[CorrectiveActionDraft] = Field(default_factory=list)
    evidence_clip_ids: list[str] = Field(default_factory=list)
    human_approval_required: bool = True

    @model_validator(mode="after")
    def collect_and_validate_citations(self) -> "ReportAgentOutput":
        claims = (
            self.executive_summary
            + self.event_timeline
            + self.jha_variance_review
        )
        cited = {clip for claim in claims for clip in claim.evidence_clip_ids}
        declared = set(self.evidence_clip_ids)
        if claims and not cited:
            raise ValueError("A report with factual claims must include citations")
        if not cited.issubset(declared):
            missing = sorted(cited - declared)
            raise ValueError(
                f"Report evidence_clip_ids is missing cited clips: {missing}"
            )
        return self


class OrchestrationSettings(BaseModel):
    """Environment-driven settings for Strands and AWS integration."""

    openai_api_key: str | None = Field(
        default_factory=lambda: app_settings.openai_api_key or None
    )
    evidence_model: str = Field(
        default_factory=lambda: app_settings.openai_normalization_model
    )
    reasoning_model: str = Field(
        default_factory=lambda: app_settings.openai_reasoning_model
    )
    session_bucket: str | None = Field(
        default_factory=lambda: app_settings.sitetrace_s3_bucket or None
    )
    session_prefix: str = Field(
        default_factory=lambda: os.getenv(
            "SITETRACE_SESSION_PREFIX", "sitetrace/sessions/"
        )
    )
    session_directory: Path = Field(default_factory=lambda: _DEFAULT_SESSION_DIRECTORY)
    max_stage_retries: int = Field(
        default_factory=lambda: int(os.getenv("SITETRACE_STAGE_RETRIES", "2")),
        ge=0,
        le=5,
    )
    max_tool_retries: int = Field(
        default_factory=lambda: int(os.getenv("SITETRACE_TOOL_RETRIES", "1")),
        ge=0,
        le=3,
    )
    enable_console_traces: bool = Field(
        default_factory=lambda: os.getenv(
            "SITETRACE_CONSOLE_TRACES", "false"
        ).lower()
        in {"1", "true", "yes"}
    )


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: WorkflowStage
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    output_key: str | None = None


class WorkflowRun(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    run_id: str
    case_id: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_stage: WorkflowStage | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    stages: list[StageRecord]
    artifacts: dict[str, Any] = Field(default_factory=dict)
    approval_interrupt: dict[str, Any] | None = None
    approved_by: str | None = None
    failure: str | None = None


class WorkflowExecutionContext(BaseModel):
    """The object provided to every injected business-stage handler."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    run_id: str
    case_id: str
    stage: WorkflowStage
    request: dict[str, Any]
    artifacts: dict[str, Any]
    attempt: int


StageHandler = Callable[[WorkflowExecutionContext], Any]
TModel = TypeVar("TModel", bound=BaseModel)


class UncitedFindingError(ValueError):
    """Raised when agent output contains a factual claim without clip IDs."""


@contextmanager
def workflow_stage_span(context: WorkflowExecutionContext) -> Any:
    """Add deterministic workflow stages to the Strands/OpenTelemetry trace."""

    if otel_trace is None:
        yield None
        return
    tracer = otel_trace.get_tracer("sitetrace.strands.workflow")
    with tracer.start_as_current_span(
        f"sitetrace.workflow.{context.stage.value}"
    ) as span:
        span.set_attribute("sitetrace.run_id", context.run_id)
        span.set_attribute("sitetrace.case_id", context.case_id)
        span.set_attribute("sitetrace.stage", context.stage.value)
        span.set_attribute("sitetrace.attempt", context.attempt)
        yield span


def _tool_name(tool_use: Any) -> str:
    if isinstance(tool_use, Mapping):
        return str(tool_use.get("name", ""))
    return str(getattr(tool_use, "name", ""))


def _extract_result_payload(result: Any) -> Any:
    """Extract JSON-like data from a Strands ToolResultBlock or mapping."""

    if not isinstance(result, Mapping):
        return result
    content = result.get("content")
    if not isinstance(content, list):
        return result
    texts: list[str] = []
    for block in content:
        if isinstance(block, Mapping):
            text_value = block.get("text")
            if isinstance(text_value, str):
                texts.append(text_value)
    if not texts:
        return result
    combined = "\n".join(texts).strip()
    try:
        return json.loads(combined)
    except json.JSONDecodeError:
        return combined


_FACTUAL_KEYS = {
    "claim",
    "summary",
    "observed_work",
    "statement",
    "text",
}
_CITATION_KEYS = {"evidence_clip_ids", "citations", "evidence_ids"}


def find_uncited_factual_paths(payload: Any, path: str = "$") -> list[str]:
    """Return JSON paths for factual mappings that lack clip citations.

    ``UNVERIFIABLE`` records are allowed to omit clips only when they include a
    non-empty evidence-gap reason.  A report section that contains ``text`` is
    treated as factual unless it is explicitly marked as a limitation.
    """

    failures: list[str] = []
    if isinstance(payload, list):
        for index, item in enumerate(payload):
            failures.extend(find_uncited_factual_paths(item, f"{path}[{index}]"))
        return failures
    if not isinstance(payload, Mapping):
        return failures

    factual = any(
        isinstance(payload.get(key), str) and bool(payload.get(key))
        for key in _FACTUAL_KEYS
    )
    status = str(payload.get("status", "")).upper()
    is_limitation = str(payload.get("kind", "")).lower() == "limitation"
    has_citations = any(
        isinstance(payload.get(key), list) and bool(payload.get(key))
        for key in _CITATION_KEYS
    )
    if factual and not is_limitation:
        if status == FindingStatus.UNVERIFIABLE.value:
            if not payload.get("evidence_gap_reason"):
                failures.append(path)
        elif not has_citations:
            failures.append(path)

    for key, value in payload.items():
        if isinstance(value, (list, Mapping)):
            failures.extend(find_uncited_factual_paths(value, f"{path}.{key}"))
    return failures


class CitationGuardHook(HookProvider):
    """Reject specialist-tool output containing uncited factual findings."""

    guarded_tools = {
        "evidence_specialist",
        "compliance_specialist",
        "report_specialist",
    }

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        if STRANDS_AVAILABLE:
            registry.add_callback(AfterToolCallEvent, self.validate_result)

    def validate_result(self, event: Any) -> None:
        if _tool_name(event.tool_use) not in self.guarded_tools:
            return
        payload = _extract_result_payload(event.result)
        failures = find_uncited_factual_paths(payload)
        if failures:
            raise UncitedFindingError(
                "Rejected uncited factual output at: " + ", ".join(failures)
            )


class RetryOnToolErrorHook(HookProvider):
    """Bounded retry for transient Strands tool failures."""

    def __init__(self, max_retries: int = 1) -> None:
        self.max_retries = max_retries
        self._attempt_counts: dict[str, int] = {}
        self._lock = RLock()

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        if STRANDS_AVAILABLE:
            registry.add_callback(AfterToolCallEvent, self.handle_retry)

    def handle_retry(self, event: Any) -> None:
        result = event.result if isinstance(event.result, Mapping) else {}
        tool_use = event.tool_use
        if isinstance(tool_use, Mapping):
            tool_use_id = str(
                tool_use.get("toolUseId") or tool_use.get("tool_use_id") or ""
            )
        else:
            tool_use_id = str(
                getattr(tool_use, "tool_use_id", None)
                or getattr(tool_use, "toolUseId", "")
            )
        key = tool_use_id or f"{_tool_name(tool_use)}:{id(event)}"
        with self._lock:
            attempt = self._attempt_counts.get(key, 0) + 1
            self._attempt_counts[key] = attempt
            if result.get("status") == "error" and attempt <= self.max_retries:
                logger.info(
                    "Retrying Strands tool %s (%s/%s)",
                    _tool_name(tool_use),
                    attempt,
                    self.max_retries,
                )
                event.retry = True
            elif result.get("status") != "error":
                self._attempt_counts.pop(key, None)


class ReportPublicationApprovalHook(HookProvider):
    """Pause before publication and require explicit human approval."""

    interrupt_name = "sitetrace-report-publication-approval"

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        if STRANDS_AVAILABLE:
            registry.add_callback(BeforeToolCallEvent, self.require_approval)

    def require_approval(self, event: Any) -> None:
        if _tool_name(event.tool_use) != "publish_investigation_report":
            return
        tool_use = event.tool_use
        tool_input = (
            tool_use.get("input", {})
            if isinstance(tool_use, Mapping)
            else getattr(tool_use, "input", {})
        )
        approval = event.interrupt(
            self.interrupt_name,
            reason={
                "report_id": tool_input.get("report_id"),
                "message": (
                    "A safety manager must approve the evidence-backed draft "
                    "before SiteTrace publishes it."
                ),
            },
        )
        normalized = str(approval).strip().lower()
        if normalized not in {"approve", "approved", "y", "yes"}:
            event.cancel_tool = "Report publication was not approved"


@tool
def publish_investigation_report(
    report_id: str,
    approved_by: str,
) -> dict[str, Any]:
    """Request publication of an already validated investigation report.

    The approval hook interrupts before this function runs.  This tool records
    intent only; the injected ``publish_report`` business handler performs the
    actual durable write.

    Args:
        report_id: Stable report identifier.
        approved_by: Name or identifier of the approving safety manager.
    """

    return {
        "publication_requested": True,
        "report_id": report_id,
        "approved_by": approved_by,
    }


def build_session_manager(
    session_id: str,
    settings: OrchestrationSettings,
    *,
    role: str,
) -> Any | None:
    """Select S3 persistence when configured, otherwise local files."""

    if not STRANDS_AVAILABLE:
        return None
    scoped_id = f"{session_id}-{role}"
    if settings.session_bucket:
        try:
            return S3SessionManager(
                session_id=scoped_id,
                bucket=settings.session_bucket,
                prefix=settings.session_prefix,
            )
        except Exception:
            logger.exception(
                "Could not configure S3SessionManager; using local sessions"
            )
    settings.session_directory.mkdir(parents=True, exist_ok=True)
    return FileSessionManager(
        session_id=scoped_id,
        storage_dir=str(settings.session_directory),
    )


def configure_strands_telemetry(
    settings: OrchestrationSettings,
) -> bool:
    """Enable Strands OpenTelemetry without replacing AgentCore auto-setup."""

    if not STRANDS_AVAILABLE:
        return False
    try:
        telemetry = StrandsTelemetry()
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            telemetry.setup_otlp_exporter()
        if settings.enable_console_traces:
            telemetry.setup_console_exporter()
            telemetry.setup_meter(enable_console_exporter=True)
        # If neither is configured, AgentCore/OpenTelemetry global providers are
        # still detected automatically by Strands, so no exporter is installed.
        return True
    except Exception:
        logger.exception("Strands telemetry setup failed; continuing without it")
        return False


class StrandsAgentBundle(BaseModel):
    """References to live Strands components, if credentials are available."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    live: bool
    unavailable_reason: str | None = None
    evidence_agent: Any | None = None
    compliance_agent: Any | None = None
    report_agent: Any | None = None
    orchestrator_agent: Any | None = None
    agent_tools: list[Any] = Field(default_factory=list)


def _structured_agent_tool(
    *,
    function_name: str,
    agent: Any,
    output_model: type[TModel],
) -> Any:
    """Wrap a specialist agent as a typed custom Strands tool."""

    if function_name == "evidence_specialist":

        @tool
        def evidence_specialist(request_json: str) -> str:
            """Normalize cited video observations into typed evidence events.

            Args:
                request_json: JSON containing TwelveLabs results and clip IDs.
            """

            result = agent(
                request_json,
                structured_output_model=output_model,
            )
            return result.structured_output.model_dump_json()

        return evidence_specialist

    if function_name == "compliance_specialist":

        @tool
        def compliance_specialist(request_json: str) -> str:
            """Compare cited observations with JHA controls without guessing.

            Args:
                request_json: JSON containing JHA steps and Neo4j graph paths.
            """

            result = agent(
                request_json,
                structured_output_model=output_model,
            )
            return result.structured_output.model_dump_json()

        return compliance_specialist

    @tool
    def report_specialist(request_json: str) -> str:
        """Draft an evidence-backed report from validated findings only.

        Args:
            request_json: JSON containing validated findings and clip citations.
        """

        result = agent(
            request_json,
            structured_output_model=output_model,
        )
        return result.structured_output.model_dump_json()

    return report_specialist


def build_strands_agents(
    session_id: str,
    settings: OrchestrationSettings | None = None,
) -> StrandsAgentBundle:
    """Build three specialists and expose them to a Strands orchestrator."""

    settings = settings or OrchestrationSettings()
    if not STRANDS_AVAILABLE:
        return StrandsAgentBundle(
            live=False,
            unavailable_reason=(
                "Strands is not installed"
                + (f" ({_STRANDS_IMPORT_ERROR})" if _STRANDS_IMPORT_ERROR else "")
            ),
        )
    if not settings.openai_api_key:
        return StrandsAgentBundle(
            live=False,
            unavailable_reason="OPENAI_API_KEY is not configured",
        )

    try:
        configure_strands_telemetry(settings)
        def plugin_args() -> dict[str, Any]:
            # A plugin instance owns per-agent activation state, so specialists
            # receive separate AgentSkills instances.
            if not _SKILLS_DIRECTORY.exists():
                return {}
            return {
                "plugins": [
                    AgentSkills(skills=str(_SKILLS_DIRECTORY))
                ]
            }
        common_hooks = [
            CitationGuardHook(),
            RetryOnToolErrorHook(settings.max_tool_retries),
        ]

        evidence_model = OpenAIResponsesModel(
            model_id=settings.evidence_model,
            client_args={"api_key": settings.openai_api_key},
            stateful=False,
        )
        reasoning_model = OpenAIResponsesModel(
            model_id=settings.reasoning_model,
            client_args={"api_key": settings.openai_api_key},
            stateful=False,
        )

        evidence_agent = Agent(
            model=evidence_model,
            system_prompt=(
                "You are SiteTrace's Evidence Agent. Normalize only events that "
                "are present in supplied TwelveLabs results. Preserve every "
                "evidence_clip_id and timestamp. Never invent an identity, "
                "action, location, or causal relation. Resolve cross-camera "
                "entities only when the supplied visual, temporal, and movement "
                "evidence supports it. Activate the construction-safety-review "
                "skill and return the requested Pydantic schema."
            ),
            hooks=common_hooks,
            session_manager=build_session_manager(
                session_id, settings, role="evidence"
            ),
            callback_handler=None,
            **plugin_args(),
        )
        compliance_agent = Agent(
            model=reasoning_model,
            system_prompt=(
                "You are SiteTrace's Compliance Agent. Compare the supplied JHA "
                "plan to deterministic Neo4j graph-diff results. Do not treat "
                "absence from video as proof of non-performance. Use only "
                "COMPLIANT, CONFIRMED_DEVIATION, "
                "REQUIRED_CONTROL_NOT_OBSERVED, or UNVERIFIABLE. Every factual "
                "finding requires evidence clip IDs and graph paths. Never infer "
                "organizational root cause from footage. Return the requested "
                "Pydantic schema."
            ),
            hooks=common_hooks,
            session_manager=build_session_manager(
                session_id, settings, role="compliance"
            ),
            callback_handler=None,
            **plugin_args(),
        )
        report_agent = Agent(
            model=reasoning_model,
            system_prompt=(
                "You are SiteTrace's Report Agent. Draft the incident or near-miss "
                "investigation report only from validated findings. Every factual "
                "report claim must cite one or more evidence_clip_ids. Clearly "
                "separate observations, graph-supported comparisons, limitations, "
                "and proposed corrective actions. Do not make release, blame, "
                "disciplinary, or root-cause decisions. The draft always requires "
                "human approval. Return the requested Pydantic schema."
            ),
            hooks=common_hooks,
            session_manager=build_session_manager(
                session_id, settings, role="report"
            ),
            callback_handler=None,
            **plugin_args(),
        )

        evidence_tool = _structured_agent_tool(
            function_name="evidence_specialist",
            agent=evidence_agent,
            output_model=EvidenceAgentOutput,
        )
        compliance_tool = _structured_agent_tool(
            function_name="compliance_specialist",
            agent=compliance_agent,
            output_model=ComplianceAgentOutput,
        )
        report_tool = _structured_agent_tool(
            function_name="report_specialist",
            agent=report_agent,
            output_model=ReportAgentOutput,
        )
        agent_tools = [
            evidence_tool,
            compliance_tool,
            report_tool,
            publish_investigation_report,
        ]
        orchestrator_agent = Agent(
            model=reasoning_model,
            system_prompt=(
                "You are the SiteTrace investigation orchestrator. The business "
                "workflow fixes execution order; your tools provide specialist "
                "reasoning inside those stages. Use Evidence before Compliance, "
                "Compliance before Report, and never publish until the human "
                "approval interrupt is answered affirmatively."
            ),
            tools=agent_tools,
            hooks=[
                CitationGuardHook(),
                RetryOnToolErrorHook(settings.max_tool_retries),
                ReportPublicationApprovalHook(),
            ],
            session_manager=build_session_manager(
                session_id, settings, role="orchestrator"
            ),
            callback_handler=None,
            **plugin_args(),
        )
        return StrandsAgentBundle(
            live=True,
            evidence_agent=evidence_agent,
            compliance_agent=compliance_agent,
            report_agent=report_agent,
            orchestrator_agent=orchestrator_agent,
            agent_tools=agent_tools,
        )
    except Exception as exc:
        logger.exception("Strands agent initialization failed")
        return StrandsAgentBundle(
            live=False,
            unavailable_reason=f"{type(exc).__name__}: {exc}",
        )


class SiteTraceWorkflow:
    """Deterministic workflow controller with retry, status, and approval gates."""

    def __init__(
        self,
        settings: OrchestrationSettings | None = None,
    ) -> None:
        self.settings = settings or OrchestrationSettings()
        self._runs: dict[str, WorkflowRun] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._lock = RLock()

    def create_run(
        self,
        *,
        case_id: str,
        request: Mapping[str, Any],
    ) -> WorkflowRun:
        run = WorkflowRun(
            run_id=f"run_{uuid4().hex}",
            case_id=case_id,
            stages=[StageRecord(stage=stage) for stage in WORKFLOW_ORDER],
        )
        with self._lock:
            self._runs[run.run_id] = run
            self._requests[run.run_id] = dict(request)
        return self.get_status(run.run_id)

    def get_status(self, run_id: str) -> WorkflowRun:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"Unknown SiteTrace workflow run: {run_id}")
            return WorkflowRun.model_validate(
                self._runs[run_id].model_dump(mode="python")
            )

    def start(
        self,
        run_id: str,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> WorkflowRun:
        """Execute deterministically until approval, completion, or failure."""

        with self._lock:
            run = self._runs[run_id]
            if run.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.CANCELLED,
            }:
                return self.get_status(run_id)
            run.status = WorkflowStatus.RUNNING
            run.failure = None
            run.updated_at = datetime.now(UTC)

        for stage in WORKFLOW_ORDER:
            if stage is WorkflowStage.HUMAN_APPROVAL:
                self._pause_for_approval(run_id)
                return self.get_status(run_id)
            if stage is WorkflowStage.PUBLISH_REPORT:
                # Publication is reached only through resume_after_approval.
                return self.get_status(run_id)
            record = self._record_for(run_id, stage)
            if record.status is StageStatus.COMPLETED:
                continue
            if not self._execute_stage(run_id, stage, handlers):
                return self.get_status(run_id)
        return self.get_status(run_id)

    async def start_async(
        self,
        run_id: str,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> WorkflowRun:
        """Async equivalent of :meth:`start` for FastAPI service handlers."""

        with self._lock:
            run = self._runs[run_id]
            if run.status in {
                WorkflowStatus.COMPLETED,
                WorkflowStatus.CANCELLED,
            }:
                return self.get_status(run_id)
            run.status = WorkflowStatus.RUNNING
            run.failure = None
            run.updated_at = datetime.now(UTC)

        for stage in WORKFLOW_ORDER:
            if stage is WorkflowStage.HUMAN_APPROVAL:
                self._pause_for_approval(run_id)
                return self.get_status(run_id)
            if stage is WorkflowStage.PUBLISH_REPORT:
                return self.get_status(run_id)
            record = self._record_for(run_id, stage)
            if record.status is StageStatus.COMPLETED:
                continue
            if not await self._execute_stage_async(run_id, stage, handlers):
                return self.get_status(run_id)
        return self.get_status(run_id)

    def resume_after_approval(
        self,
        run_id: str,
        *,
        approved: bool,
        approved_by: str,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> WorkflowRun:
        """Resolve the human gate and optionally execute publication."""

        with self._lock:
            run = self._runs[run_id]
            if run.status is not WorkflowStatus.AWAITING_APPROVAL:
                raise ValueError(
                    f"Workflow {run_id} is not awaiting approval"
                )
            approval_record = self._record_for_unlocked(
                run, WorkflowStage.HUMAN_APPROVAL
            )
            approval_record.completed_at = datetime.now(UTC)
            run.approved_by = approved_by
            run.approval_interrupt = {
                **(run.approval_interrupt or {}),
                "resolved": True,
                "approved": approved,
                "approved_by": approved_by,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            if not approved:
                approval_record.status = StageStatus.COMPLETED
                publish_record = self._record_for_unlocked(
                    run, WorkflowStage.PUBLISH_REPORT
                )
                publish_record.status = StageStatus.SKIPPED
                run.status = WorkflowStatus.CANCELLED
                run.updated_at = datetime.now(UTC)
                return self.get_status(run_id)
            approval_record.status = StageStatus.COMPLETED
            approval_record.output_key = WorkflowStage.HUMAN_APPROVAL.value
            run.artifacts[WorkflowStage.HUMAN_APPROVAL.value] = {
                "approved": True,
                "approved_by": approved_by,
            }
            run.status = WorkflowStatus.RUNNING
            run.updated_at = datetime.now(UTC)

        succeeded = self._execute_stage(
            run_id,
            WorkflowStage.PUBLISH_REPORT,
            handlers,
        )
        with self._lock:
            run = self._runs[run_id]
            if succeeded:
                run.status = WorkflowStatus.COMPLETED
                run.current_stage = None
            run.updated_at = datetime.now(UTC)
        return self.get_status(run_id)

    async def resume_after_approval_async(
        self,
        run_id: str,
        *,
        approved: bool,
        approved_by: str,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> WorkflowRun:
        """Async approval resume for an AgentCore/FastAPI request."""

        with self._lock:
            run = self._runs[run_id]
            if run.status is not WorkflowStatus.AWAITING_APPROVAL:
                raise ValueError(
                    f"Workflow {run_id} is not awaiting approval"
                )
            approval_record = self._record_for_unlocked(
                run, WorkflowStage.HUMAN_APPROVAL
            )
            approval_record.completed_at = datetime.now(UTC)
            run.approved_by = approved_by
            run.approval_interrupt = {
                **(run.approval_interrupt or {}),
                "resolved": True,
                "approved": approved,
                "approved_by": approved_by,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
            if not approved:
                approval_record.status = StageStatus.COMPLETED
                publish_record = self._record_for_unlocked(
                    run, WorkflowStage.PUBLISH_REPORT
                )
                publish_record.status = StageStatus.SKIPPED
                run.status = WorkflowStatus.CANCELLED
                run.updated_at = datetime.now(UTC)
                return self.get_status(run_id)
            approval_record.status = StageStatus.COMPLETED
            approval_record.output_key = WorkflowStage.HUMAN_APPROVAL.value
            run.artifacts[WorkflowStage.HUMAN_APPROVAL.value] = {
                "approved": True,
                "approved_by": approved_by,
            }
            run.status = WorkflowStatus.RUNNING
            run.updated_at = datetime.now(UTC)

        succeeded = await self._execute_stage_async(
            run_id,
            WorkflowStage.PUBLISH_REPORT,
            handlers,
        )
        with self._lock:
            run = self._runs[run_id]
            if succeeded:
                run.status = WorkflowStatus.COMPLETED
                run.current_stage = None
            run.updated_at = datetime.now(UTC)
        return self.get_status(run_id)

    def retry_failed(
        self,
        run_id: str,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> WorkflowRun:
        """Retry the failed stage, then continue to the approval boundary."""

        with self._lock:
            run = self._runs[run_id]
            failed = next(
                (
                    record
                    for record in run.stages
                    if record.status is StageStatus.FAILED
                ),
                None,
            )
            if failed is None:
                raise ValueError(f"Workflow {run_id} has no failed stage")
            failed.status = StageStatus.PENDING
            failed.error = None
            run.status = WorkflowStatus.RUNNING
            run.failure = None
        return self.start(run_id, handlers)

    def _pause_for_approval(self, run_id: str) -> None:
        with self._lock:
            run = self._runs[run_id]
            record = self._record_for_unlocked(
                run, WorkflowStage.HUMAN_APPROVAL
            )
            record.status = StageStatus.WAITING
            record.started_at = record.started_at or datetime.now(UTC)
            run.current_stage = WorkflowStage.HUMAN_APPROVAL
            run.status = WorkflowStatus.AWAITING_APPROVAL
            run.approval_interrupt = {
                "name": ReportPublicationApprovalHook.interrupt_name,
                "resolved": False,
                "reason": (
                    "Safety-manager approval is required before report publication"
                ),
            }
            run.updated_at = datetime.now(UTC)

    def _execute_stage(
        self,
        run_id: str,
        stage: WorkflowStage,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> bool:
        handler = handlers.get(stage) or handlers.get(stage.value)
        if handler is None:
            self._fail_run(
                run_id,
                stage,
                f"No business handler registered for {stage.value}",
            )
            return False

        for _ in range(self.settings.max_stage_retries + 1):
            with self._lock:
                run = self._runs[run_id]
                record = self._record_for_unlocked(run, stage)
                record.status = StageStatus.RUNNING
                record.started_at = record.started_at or datetime.now(UTC)
                record.attempts += 1
                record.error = None
                run.current_stage = stage
                run.updated_at = datetime.now(UTC)
                context = WorkflowExecutionContext(
                    run_id=run.run_id,
                    case_id=run.case_id,
                    stage=stage,
                    request=dict(self._requests[run_id]),
                    artifacts=dict(run.artifacts),
                    attempt=record.attempts,
                )
            try:
                with workflow_stage_span(context) as span:
                    output = handler(context)
                    if span is not None:
                        span.set_attribute("sitetrace.stage_status", "completed")
                if isinstance(output, BaseModel):
                    output = output.model_dump(mode="json")
                elif output is None:
                    output = {}
                with self._lock:
                    run = self._runs[run_id]
                    record = self._record_for_unlocked(run, stage)
                    record.status = StageStatus.COMPLETED
                    record.completed_at = datetime.now(UTC)
                    record.output_key = stage.value
                    run.artifacts[stage.value] = output
                    run.updated_at = datetime.now(UTC)
                return True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    record = self._record_for_unlocked(
                        self._runs[run_id], stage
                    )
                    record.error = error
                logger.warning(
                    "SiteTrace stage %s failed on attempt %s: %s",
                    stage.value,
                    context.attempt,
                    error,
                )
        self._fail_run(run_id, stage, record.error or "Unknown stage error")
        return False

    async def _execute_stage_async(
        self,
        run_id: str,
        stage: WorkflowStage,
        handlers: Mapping[WorkflowStage | str, StageHandler],
    ) -> bool:
        handler = handlers.get(stage) or handlers.get(stage.value)
        if handler is None:
            self._fail_run(
                run_id,
                stage,
                f"No business handler registered for {stage.value}",
            )
            return False

        last_error = "Unknown stage error"
        for _ in range(self.settings.max_stage_retries + 1):
            with self._lock:
                run = self._runs[run_id]
                record = self._record_for_unlocked(run, stage)
                record.status = StageStatus.RUNNING
                record.started_at = record.started_at or datetime.now(UTC)
                record.attempts += 1
                record.error = None
                run.current_stage = stage
                run.updated_at = datetime.now(UTC)
                context = WorkflowExecutionContext(
                    run_id=run.run_id,
                    case_id=run.case_id,
                    stage=stage,
                    request=dict(self._requests[run_id]),
                    artifacts=dict(run.artifacts),
                    attempt=record.attempts,
                )
            try:
                with workflow_stage_span(context) as span:
                    output = handler(context)
                    if inspect.isawaitable(output):
                        output = await output
                    if span is not None:
                        span.set_attribute("sitetrace.stage_status", "completed")
                if isinstance(output, BaseModel):
                    output = output.model_dump(mode="json")
                elif output is None:
                    output = {}
                with self._lock:
                    run = self._runs[run_id]
                    record = self._record_for_unlocked(run, stage)
                    record.status = StageStatus.COMPLETED
                    record.completed_at = datetime.now(UTC)
                    record.output_key = stage.value
                    run.artifacts[stage.value] = output
                    run.updated_at = datetime.now(UTC)
                return True
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    record = self._record_for_unlocked(
                        self._runs[run_id], stage
                    )
                    record.error = last_error
                logger.warning(
                    "SiteTrace async stage %s failed on attempt %s: %s",
                    stage.value,
                    context.attempt,
                    last_error,
                )
        self._fail_run(run_id, stage, last_error)
        return False

    def _fail_run(
        self,
        run_id: str,
        stage: WorkflowStage,
        error: str,
    ) -> None:
        with self._lock:
            run = self._runs[run_id]
            record = self._record_for_unlocked(run, stage)
            record.status = StageStatus.FAILED
            record.completed_at = datetime.now(UTC)
            record.error = error
            run.status = WorkflowStatus.FAILED
            run.current_stage = stage
            run.failure = error
            run.updated_at = datetime.now(UTC)

    def _record_for(self, run_id: str, stage: WorkflowStage) -> StageRecord:
        with self._lock:
            return self._record_for_unlocked(self._runs[run_id], stage)

    @staticmethod
    def _record_for_unlocked(
        run: WorkflowRun,
        stage: WorkflowStage,
    ) -> StageRecord:
        return next(record for record in run.stages if record.stage is stage)


class SiteTraceStrandsRuntime:
    """Facade used by the FastAPI business layer."""

    def __init__(
        self,
        *,
        session_id: str,
        settings: OrchestrationSettings | None = None,
    ) -> None:
        self.settings = settings or OrchestrationSettings()
        self.session_id = session_id
        self.agents = build_strands_agents(session_id, self.settings)
        self.workflow = SiteTraceWorkflow(self.settings)

    def health(self) -> dict[str, Any]:
        return {
            "strands_installed": STRANDS_AVAILABLE,
            "strands_agents_live": self.agents.live,
            "strands_unavailable_reason": self.agents.unavailable_reason,
            "session_backend": (
                "s3" if self.settings.session_bucket else "file"
            ),
            "telemetry": (
                "otlp"
                if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                else "global-or-disabled"
            ),
            "workflow_stages": [stage.value for stage in WORKFLOW_ORDER],
            "human_approval_required": True,
        }

    def invoke_specialist(
        self,
        role: str,
        payload: Mapping[str, Any],
    ) -> BaseModel:
        """Invoke a specialist with typed output, or return a safe empty result."""

        normalized = role.strip().lower()
        if normalized not in {"evidence", "compliance", "report"}:
            raise ValueError(f"Unknown SiteTrace specialist role: {role}")
        if not self.agents.live:
            if normalized == "evidence":
                return EvidenceAgentOutput(
                    coverage_gaps=[
                        self.agents.unavailable_reason
                        or "Evidence specialist is unavailable"
                    ]
                )
            if normalized == "compliance":
                return ComplianceAgentOutput(
                    escalation_required=True,
                    escalation_reason=(
                        self.agents.unavailable_reason
                        or "Compliance specialist is unavailable"
                    ),
                )
            return ReportAgentOutput(
                report_id=str(payload.get("report_id", "draft_unavailable")),
                title="Investigation report unavailable",
                limitations=[
                    self.agents.unavailable_reason
                    or "Report specialist is unavailable"
                ],
                human_approval_required=True,
            )

        agent = getattr(self.agents, f"{normalized}_agent")
        output_model: type[BaseModel]
        if normalized == "evidence":
            output_model = EvidenceAgentOutput
        elif normalized == "compliance":
            output_model = ComplianceAgentOutput
        else:
            output_model = ReportAgentOutput
        result = agent(
            json.dumps(payload, ensure_ascii=False, default=str),
            structured_output_model=output_model,
        )
        return result.structured_output


__all__ = [
    "CitationGuardHook",
    "ComplianceAgentOutput",
    "EvidenceAgentOutput",
    "FindingStatus",
    "InvestigationFinding",
    "OrchestrationSettings",
    "ReportAgentOutput",
    "ReportPublicationApprovalHook",
    "RetryOnToolErrorHook",
    "STRANDS_AVAILABLE",
    "SiteTraceStrandsRuntime",
    "SiteTraceWorkflow",
    "StageHandler",
    "StageStatus",
    "WorkflowExecutionContext",
    "WorkflowRun",
    "WorkflowStage",
    "WorkflowStatus",
    "build_session_manager",
    "build_strands_agents",
    "configure_strands_telemetry",
    "find_uncited_factual_paths",
    "publish_investigation_report",
    "workflow_stage_span",
]
