"""Typed contracts shared by the API and sponsor integrations."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


UTC = timezone.utc


class CaseStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class FindingStatus(StrEnum):
    COMPLIANT = "COMPLIANT"
    CONFIRMED_DEVIATION = "CONFIRMED_DEVIATION"
    REQUIRED_CONTROL_NOT_OBSERVED = "REQUIRED_CONTROL_NOT_OBSERVED"
    UNVERIFIABLE = "UNVERIFIABLE"


class UploadedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str
    media_type: str
    size_bytes: int = Field(ge=0)
    local_path: str | None = None
    s3_uri: str | None = None
    camera_id: str | None = None
    asset_id: str | None = None
    knowledge_store_item_id: str | None = None


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    sequence: int = Field(ge=1)
    name: str
    description: str = ""
    required_controls: list[str] = Field(default_factory=list)
    must_avoid_zones: list[str] = Field(default_factory=list)
    required_roles: list[str] = Field(default_factory=list)


class EvidenceClip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_clip_id: str
    item_id: str | None = None
    asset_id: str | None = None
    camera_id: str
    source_filename: str
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    summary: str
    transcript: str = ""
    visible_text: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)
    source_url: str | None = None
    embedding: list[float] | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_interval(self) -> "EvidenceClip":
        if self.end_sec <= self.start_sec:
            raise ValueError("end_sec must be greater than start_sec")
        return self


class ObservedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    summary: str
    camera_id: str
    start_sec: float = Field(ge=0)
    end_sec: float = Field(gt=0)
    actor_ids: list[str] = Field(default_factory=list)
    object_ids: list[str] = Field(default_factory=list)
    zone_ids: list[str] = Field(default_factory=list)
    evidence_clip_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    observation_type: str = Field(
        default="observed",
        pattern="^(observed|inferred|human_confirmed)$",
    )


class DeviationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    jha_step_id: str
    status: FindingStatus
    title: str
    planned_control: str
    observed_work: str | None = None
    evidence_clip_ids: list[str] = Field(default_factory=list)
    graph_path_node_ids: list[str] = Field(default_factory=list)
    graph_path_relationships: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence_gap_reason: str | None = None
    requires_human_review: bool = True

    @model_validator(mode="after")
    def enforce_evidence_policy(self) -> "DeviationFinding":
        if self.status == FindingStatus.UNVERIFIABLE:
            if not self.evidence_gap_reason:
                raise ValueError("UNVERIFIABLE requires an evidence_gap_reason")
            return self
        if not self.evidence_clip_ids:
            raise ValueError("Factual findings require at least one evidence clip")
        return self


class CorrectiveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str
    finding_id: str
    action_type: str = Field(pattern="^(immediate|corrective|preventive)$")
    description: str
    owner_role: str
    due_date: str | None = None
    status: str = "DRAFT"


class CitedNarrativeClaim(BaseModel):
    """One report paragraph whose factual content stays source-traceable."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    text: str
    evidence_clip_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)


class InvestigationPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    title: str
    incident_summary: str
    incident_overview: list[CitedNarrativeClaim] = Field(default_factory=list)
    event_timeline: list[CitedNarrativeClaim] = Field(default_factory=list)
    deviation_summary: list[CitedNarrativeClaim] = Field(default_factory=list)
    planned_steps: list[PlannedStep] = Field(default_factory=list)
    events: list[ObservedEvent] = Field(default_factory=list)
    evidence_clips: list[EvidenceClip] = Field(default_factory=list)
    findings: list[DeviationFinding] = Field(default_factory=list)
    corrective_actions: list[CorrectiveAction] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    knowledge_store_id: str | None = None
    jockey_session_id: str | None = None
    graph_metrics: dict[str, int] = Field(default_factory=dict)
    sponsor_trace: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    title: str
    status: CaseStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    jha: UploadedInput
    supporting_documents: list[UploadedInput] = Field(default_factory=list)
    site_metadata: UploadedInput | None = None
    site_map: UploadedInput | None = None
    videos: list[UploadedInput] = Field(min_length=1)
    investigation: InvestigationPackage | None = None
    workflow_run_id: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    report_path: str | None = None
    report_s3_uri: str | None = None
    error: str | None = None


class ApprovalRequest(BaseModel):
    reviewer: str = Field(default="Safety Manager", min_length=1)
    approved: bool


class HealthResponse(BaseModel):
    status: str
    service: str
    mode: str
    sponsors: dict[str, dict[str, Any]]
