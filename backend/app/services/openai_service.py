"""Evidence-bounded OpenAI Responses API integration for SiteTrace.

The service keeps sponsor responsibilities deliberately narrow:

* Terra extracts planned JHA steps, maps observations to those steps, and
  drafts deviations and report prose.
* Luna normalizes high-volume video observations that already contain
  TwelveLabs evidence clip IDs.
* Sol is called only when the caller supplies genuinely conflicting evidence.

The OpenAI SDK is imported lazily.  Importing the backend therefore remains
safe in local review environments where the SDK or API key is unavailable.
All model responses use the Responses API structured-output parser with
Pydantic contracts and are checked again against caller-provided ID allowlists.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import Settings, settings
from ..schemas import (
    CorrectiveAction,
    DeviationFinding,
    FindingStatus,
    ObservedEvent,
    PlannedStep,
)


OutputT = TypeVar("OutputT", bound=BaseModel)


class OpenAIUnavailableError(RuntimeError):
    """The optional OpenAI integration is not configured or installed."""


class OpenAIResponseError(RuntimeError):
    """The Responses API completed without a usable structured result."""


class EvidencePolicyError(ValueError):
    """A model result crossed SiteTrace's evidence or causality boundary."""


class PlanDocument(BaseModel):
    """Text extracted from a JHA or supporting plan document."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    text: str = Field(min_length=1)


class PlannedStepBatch(BaseModel):
    """Structured Terra extraction from one or more plan documents."""

    model_config = ConfigDict(extra="forbid")

    planned_steps: list[PlannedStep] = Field(default_factory=list)
    unresolved_requirements: list[str] = Field(default_factory=list)


class ObservedEventBatch(BaseModel):
    """Structured Luna normalization of evidence-bearing observations."""

    model_config = ConfigDict(extra="forbid")

    events: list[ObservedEvent] = Field(default_factory=list)
    unverified_observations: list[str] = Field(default_factory=list)


class EventStepMapping(BaseModel):
    """A positively evidenced semantic link between observations and JHA."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    matched_step_ids: list[str] = Field(default_factory=list)
    satisfied_controls: list[str] = Field(default_factory=list)
    contradicted_controls: list[str] = Field(default_factory=list)
    evidence_clip_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    unresolved: bool = False


class EventStepMappingBatch(BaseModel):
    """Terra output used before Neo4j computes deterministic graph diffs."""

    model_config = ConfigDict(extra="forbid")

    mappings: list[EventStepMapping] = Field(default_factory=list)
    unresolved_event_ids: list[str] = Field(default_factory=list)


class EvidenceConflict(BaseModel):
    """A caller-declared contradiction that is eligible for Sol escalation."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_clip_ids: list[str] = Field(min_length=2)


class ConflictAssessment(BaseModel):
    """Sol's bounded assessment of one caller-declared evidence conflict."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1)
    status: Literal["CONSISTENT", "RESOLVED", "UNRESOLVED"]
    summary: str = Field(min_length=1)
    evidence_clip_ids: list[str] = Field(min_length=1)


class ConflictAssessmentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessments: list[ConflictAssessment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EvidenceBackedClaim(BaseModel):
    """A narrative claim that remains traceable to source video."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_clip_ids: list[str] = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)


class DeviationAnalysis(BaseModel):
    """Terra's evidence-backed plan-versus-observation draft."""

    model_config = ConfigDict(extra="forbid")

    findings: list[DeviationFinding] = Field(default_factory=list)
    observed_sequence: list[EvidenceBackedClaim] = Field(default_factory=list)
    conflict_assessments: list[ConflictAssessment] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ReportNarrative(BaseModel):
    """A pre-approval narrative; publication remains a Strands responsibility."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    incident_overview: list[EvidenceBackedClaim] = Field(default_factory=list)
    event_timeline: list[EvidenceBackedClaim] = Field(default_factory=list)
    deviation_summary: list[EvidenceBackedClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    corrective_actions: list[CorrectiveAction] = Field(default_factory=list)
    human_approval_required: Literal[True] = True


@dataclass(frozen=True, slots=True)
class OpenAIResult(Generic[OutputT]):
    """Structured data plus continuation and escalation audit metadata."""

    data: OutputT
    response_id: str
    model: str
    escalation_response_id: str | None = None
    escalation_model: str | None = None

    @property
    def escalated(self) -> bool:
        return self.escalation_response_id is not None


_BASE_INSTRUCTIONS = """\
You are an evidence-bounded reasoning component inside SiteTrace, a
construction incident investigation product.

Mandatory rules:
1. Evidence clip IDs are opaque citations. Copy only IDs supplied in the
   request. Never create, guess, repair, rename, or interpolate an evidence ID.
2. Missing video evidence means UNVERIFIABLE. It is never proof that a required
   control was not performed.
3. Separate directly observed facts, bounded inference, and unverified items.
4. Describe time-ordered contributing events as an observed sequence. Do not
   claim that temporal order proves causation.
5. Do not identify a root cause, organizational failure, intent, negligence, or
   a person's identity from video evidence.
6. Do not invent graph node IDs, relationship IDs, people, equipment, zones,
   plan requirements, timestamps, or source facts.
7. Return only the requested structured output.
"""

_PLAN_INSTRUCTIONS = """\
Extract the supplied JHA and plan text into ordered PlannedStep records.
Preserve explicit source step identifiers when present. Keep requirements
attached to their source step: controls in required_controls, prohibited areas
in must_avoid_zones, and mandatory roles in required_roles. Do not turn advice
or descriptive prose into a mandatory control. Put ambiguous or unreadable
requirements in unresolved_requirements instead of guessing.
"""

_NORMALIZATION_INSTRUCTIONS = """\
Normalize only the supplied raw video observations into ObservedEvent records.
Preserve source timestamps, camera IDs, and evidence clip IDs exactly. Merge
records only when the supplied data supports that they describe the same
event. Do not infer a control failure, root cause, identity, or continuous
absence from a gap between clips. Put unsupported candidates in
unverified_observations.
"""

_MAPPING_INSTRUCTIONS = """\
Map each supplied observed event to semantically relevant supplied JHA steps.
Only list a satisfied control when the event positively shows that control
being performed. Only list a contradicted control when the event positively
shows an incompatible condition; a missing matching event is not a
contradiction. Copy control strings and step IDs exactly from the request.
Mapping is not a compliance finding. Mark uncertain mappings unresolved.
"""

_CONFLICT_INSTRUCTIONS = """\
Review only the caller-declared conflicts. Reconcile differing clips when their
content supports reconciliation; otherwise mark the conflict UNRESOLVED.
Do not make compliance findings or draft report prose. Do not prefer one clip
without an evidence-based reason, and do not infer root cause or intent.
"""

_DEVIATION_INSTRUCTIONS = """\
Compare the supplied JHA steps, observed events, event-step mappings, and any
Sol conflict assessments. Draft CONFIRMED_DEVIATION only for a positively
observed incompatibility with a planned control. Draft COMPLIANT only for a
positively observed satisfied control. If evidence is missing, incomplete, or
conflicting, use UNVERIFIABLE with an evidence_gap_reason. Do not use
REQUIRED_CONTROL_NOT_OBSERVED; camera coverage and that status are computed
deterministically by Neo4j outside this model. Leave graph path fields empty
because graph IDs are not supplied here. Narrative claims must describe an
observed sequence, not a causal chain.
"""

_REPORT_INSTRUCTIONS = """\
Draft a detailed, professional English construction Incident or Near-Miss
Investigation Report narrative from the supplied validated steps, events, and
findings. This is not an executive-summary-only response. Write complete,
formal prose suitable for a safety manager's review and signature.

Use incident_overview for a thorough event statement and investigation scope.
Use event_timeline to reconstruct the multi-camera chronology in enough detail
that a reader can understand what was observed, when it was observed, and how
the cited clips relate across cameras. Use deviation_summary to explain each
planned JHA control, the corresponding observed work, the classification, and
the evidentiary basis. Prefer cohesive paragraphs over fragments or lists.
Preserve meaningful unknowns and camera-coverage limits in limitations.
Corrective actions must point to a supplied finding, remain proposals, identify
the action type and accountable role, and be specific enough to verify later.

Every factual narrative claim must cite one or more supplied evidence clip IDs.
Do not add a root cause, causal certainty, organizational blame, precise
distance, identity, or any other fact absent from the request. Describe
contributing events as an observed temporal sequence, not as proven causation.
human_approval_required must be true.
"""

_PROHIBITED_ROOT_CAUSE = re.compile(
    r"(?:\broot\s+cause\s+(?:is|was|has\s+been|identified|determined)"
    r"|\borganizational\s+(?:failure|fault|cause)"
    r"|\bmanagement\s+(?:failure|fault|negligence)"
    r"|근본\s*원인(?:은|이|으로)|조직(?:적)?\s*(?:실패|과실))",
    re.IGNORECASE,
)
_PROHIBITED_CAUSAL_CERTAINTY = re.compile(
    r"(?:\bdirectly\s+caused\b|\bwas\s+caused\s+by\b"
    r"|\bdefinitively\s+led\s+to\b|직접\s*원인|때문에\s*발생)",
    re.IGNORECASE,
)
_SAFE_ROOT_CAUSE_LIMITATION = re.compile(
    r"(?:\b(?:the\s+)?root\s+cause\s+"
    r"(?:cannot|could\s+not|is\s+not|was\s+not|has\s+not\s+been)"
    r"\s+(?:be\s+)?(?:determined|established|identified|verified)"
    r"|\bno\s+root\s+cause\s+(?:can|could|was)\s+"
    r"(?:be\s+)?(?:determined|established|identified|verified)"
    r"|근본\s*원인은?\s*(?:영상(?:만)?으로\s*)?"
    r"(?:확인|판단|규명)할\s*수\s*없)",
    re.IGNORECASE,
)


def _plain(value: Any) -> Any:
    """Convert supported domain values into JSON-compatible request data."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _unique_ids(values: Iterable[str], *, field_name: str) -> set[str]:
    result: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field_name} cannot contain an empty ID")
        result.add(normalized)
    return result


def _assert_subset(
    referenced: Iterable[str],
    allowed: set[str],
    *,
    field_name: str,
) -> None:
    unknown = sorted(_unique_ids(referenced, field_name=field_name) - allowed)
    if unknown:
        raise EvidencePolicyError(
            f"{field_name} contains IDs that were not supplied: {unknown}"
        )


def _validate_narrative_language(values: Iterable[str]) -> None:
    for value in values:
        without_safe_limitation = _SAFE_ROOT_CAUSE_LIMITATION.sub(
            "",
            value,
        )
        if _PROHIBITED_ROOT_CAUSE.search(without_safe_limitation):
            raise EvidencePolicyError(
                "Model output asserted an unsupported root or organizational cause"
            )
        if _PROHIBITED_CAUSAL_CERTAINTY.search(value):
            raise EvidencePolicyError(
                "Model output converted temporal evidence into causal certainty"
            )


def _coerce_documents(
    documents: str
    | Mapping[str, str]
    | Sequence[PlanDocument | Mapping[str, Any]],
) -> list[PlanDocument]:
    if isinstance(documents, str):
        return [PlanDocument(filename="plan-document", text=documents)]
    if isinstance(documents, Mapping):
        result = [
            PlanDocument(filename=str(filename), text=str(text))
            for filename, text in documents.items()
        ]
        if not result:
            raise ValueError("At least one plan document is required")
        return result
    result = [
        item if isinstance(item, PlanDocument) else PlanDocument.model_validate(item)
        for item in documents
    ]
    if not result:
        raise ValueError("At least one plan document is required")
    return result


class OpenAIService:
    """Direct, typed Responses API adapter with evidence-policy enforcement."""

    def __init__(
        self,
        *,
        configured_settings: Settings | Any | None = None,
        client: Any | None = None,
    ) -> None:
        self._settings = configured_settings or settings
        self._client = client
        self._client_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(getattr(self._settings, "openai_api_key", ""))

    @property
    def available(self) -> bool:
        if self._client is not None:
            return True
        return self.configured and self._sdk_installed()

    @property
    def unavailable_reason(self) -> str | None:
        if self._client is not None:
            return None
        if not self.configured:
            return "OPENAI_API_KEY is not configured"
        if not self._sdk_installed():
            return "The optional openai Python package is not installed"
        return self._client_error

    @staticmethod
    def _sdk_installed() -> bool:
        try:
            return importlib.util.find_spec("openai") is not None
        except (ImportError, ValueError):
            return False

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.configured:
            raise OpenAIUnavailableError("OPENAI_API_KEY is not configured")
        try:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=getattr(self._settings, "openai_api_key")
            )
        except Exception as exc:
            self._client_error = f"{type(exc).__name__}: {exc}"
            raise OpenAIUnavailableError(
                f"OpenAI client is unavailable: {self._client_error}"
            ) from exc
        return self._client

    @property
    def normalization_model(self) -> str:
        return str(
            getattr(
                self._settings,
                "openai_normalization_model",
                "gpt-5.6-luna",
            )
        )

    @property
    def reasoning_model(self) -> str:
        return str(
            getattr(
                self._settings,
                "openai_reasoning_model",
                "gpt-5.6-terra",
            )
        )

    @property
    def escalation_model(self) -> str:
        return str(
            getattr(
                self._settings,
                "openai_escalation_model",
                "gpt-5.6-sol",
            )
        )

    async def _structured_response(
        self,
        *,
        model: str,
        output_model: type[OutputT],
        stage: str,
        stage_instructions: str,
        payload: Mapping[str, Any],
        previous_response_id: str | None,
        reasoning_effort: Literal["low", "medium", "high"],
    ) -> OpenAIResult[OutputT]:
        request: dict[str, Any] = {
            "model": model,
            "instructions": f"{_BASE_INSTRUCTIONS}\n{stage_instructions}",
            "input": json.dumps(
                _plain(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "text_format": output_model,
            "prompt_cache_key": f"sitetrace-{stage}-v1",
            "reasoning": {"effort": reasoning_effort},
            "store": True,
            "metadata": {"product": "sitetrace", "stage": stage},
        }
        if previous_response_id:
            request["previous_response_id"] = previous_response_id

        response = self.client.responses.parse(**request)
        if inspect.isawaitable(response):
            response = await response

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise OpenAIResponseError(
                f"OpenAI returned no structured output for stage {stage}"
            )
        if not isinstance(parsed, output_model):
            parsed = output_model.model_validate(parsed)

        response_id = str(getattr(response, "id", "") or "")
        if not response_id:
            raise OpenAIResponseError(
                f"OpenAI returned no response ID for stage {stage}"
            )
        response_model = str(getattr(response, "model", "") or model)
        return OpenAIResult(
            data=parsed,
            response_id=response_id,
            model=response_model,
        )

    async def parse_plan_documents(
        self,
        documents: str
        | Mapping[str, str]
        | Sequence[PlanDocument | Mapping[str, Any]],
        *,
        previous_response_id: str | None = None,
    ) -> OpenAIResult[PlannedStepBatch]:
        """Use Terra to turn extracted JHA/plan text into ``PlannedStep``."""

        source_documents = _coerce_documents(documents)
        result = await self._structured_response(
            model=self.reasoning_model,
            output_model=PlannedStepBatch,
            stage="plan-extraction",
            stage_instructions=_PLAN_INSTRUCTIONS,
            payload={"documents": source_documents},
            previous_response_id=previous_response_id,
            reasoning_effort="medium",
        )
        step_ids = [step.step_id for step in result.data.planned_steps]
        if len(step_ids) != len(set(step_ids)):
            raise OpenAIResponseError("Terra returned duplicate planned step IDs")
        sequences = [step.sequence for step in result.data.planned_steps]
        if len(sequences) != len(set(sequences)):
            raise OpenAIResponseError("Terra returned duplicate planned sequences")
        _validate_narrative_language(
            [
                step.name
                for step in result.data.planned_steps
            ]
            + [
                step.description
                for step in result.data.planned_steps
            ]
            + result.data.unresolved_requirements
        )
        return result

    async def parse_planned_steps(
        self,
        documents: str
        | Mapping[str, str]
        | Sequence[PlanDocument | Mapping[str, Any]],
        *,
        previous_response_id: str | None = None,
    ) -> OpenAIResult[PlannedStepBatch]:
        """Compatibility alias with a domain-oriented method name."""

        return await self.parse_plan_documents(
            documents,
            previous_response_id=previous_response_id,
        )

    async def normalize_observed_events(
        self,
        raw_observations: Sequence[Mapping[str, Any] | BaseModel],
        *,
        allowed_evidence_clip_ids: Iterable[str],
        previous_response_id: str | None = None,
    ) -> OpenAIResult[ObservedEventBatch]:
        """Use Luna to normalize observations without manufacturing evidence."""

        allowed = _unique_ids(
            allowed_evidence_clip_ids,
            field_name="allowed_evidence_clip_ids",
        )
        result = await self._structured_response(
            model=self.normalization_model,
            output_model=ObservedEventBatch,
            stage="event-normalization",
            stage_instructions=_NORMALIZATION_INSTRUCTIONS,
            payload={
                "allowed_evidence_clip_ids": sorted(allowed),
                "raw_observations": list(raw_observations),
            },
            previous_response_id=previous_response_id,
            reasoning_effort="low",
        )
        event_ids = [event.event_id for event in result.data.events]
        if len(event_ids) != len(set(event_ids)):
            raise OpenAIResponseError("Luna returned duplicate observed event IDs")
        for event in result.data.events:
            _assert_subset(
                event.evidence_clip_ids,
                allowed,
                field_name=f"event {event.event_id} evidence_clip_ids",
            )
        _validate_narrative_language(
            [event.summary for event in result.data.events]
            + result.data.unverified_observations
        )
        return result

    async def normalize_events(
        self,
        raw_observations: Sequence[Mapping[str, Any] | BaseModel],
        *,
        allowed_evidence_clip_ids: Iterable[str],
        previous_response_id: str | None = None,
    ) -> OpenAIResult[ObservedEventBatch]:
        """Short compatibility alias for ``normalize_observed_events``."""

        return await self.normalize_observed_events(
            raw_observations,
            allowed_evidence_clip_ids=allowed_evidence_clip_ids,
            previous_response_id=previous_response_id,
        )

    async def match_events_to_steps(
        self,
        planned_steps: Sequence[PlannedStep | Mapping[str, Any]],
        observed_events: Sequence[ObservedEvent | Mapping[str, Any]],
        *,
        allowed_evidence_clip_ids: Iterable[str] | None = None,
        previous_response_id: str | None = None,
    ) -> OpenAIResult[EventStepMappingBatch]:
        """Use Terra to produce pre-graph event↔step/control mappings."""

        steps = [
            step
            if isinstance(step, PlannedStep)
            else PlannedStep.model_validate(step)
            for step in planned_steps
        ]
        events = [
            event
            if isinstance(event, ObservedEvent)
            else ObservedEvent.model_validate(event)
            for event in observed_events
        ]
        step_ids = _unique_ids(
            (step.step_id for step in steps),
            field_name="planned step IDs",
        )
        event_ids = _unique_ids(
            (event.event_id for event in events),
            field_name="observed event IDs",
        )
        event_evidence = {
            event.event_id: set(event.evidence_clip_ids) for event in events
        }
        derived_evidence = {
            clip_id for event in events for clip_id in event.evidence_clip_ids
        }
        allowed = (
            _unique_ids(
                allowed_evidence_clip_ids,
                field_name="allowed_evidence_clip_ids",
            )
            if allowed_evidence_clip_ids is not None
            else derived_evidence
        )
        _assert_subset(
            derived_evidence,
            allowed,
            field_name="observed event evidence_clip_ids",
        )
        valid_controls = {
            control for step in steps for control in step.required_controls
        }

        result = await self._structured_response(
            model=self.reasoning_model,
            output_model=EventStepMappingBatch,
            stage="event-step-mapping",
            stage_instructions=_MAPPING_INSTRUCTIONS,
            payload={
                "planned_steps": steps,
                "observed_events": events,
                "allowed_evidence_clip_ids": sorted(allowed),
            },
            previous_response_id=previous_response_id,
            reasoning_effort="medium",
        )

        mapped_event_ids: list[str] = []
        for mapping in result.data.mappings:
            if mapping.event_id not in event_ids:
                raise EvidencePolicyError(
                    f"Mapping references unknown event ID {mapping.event_id}"
                )
            mapped_event_ids.append(mapping.event_id)
            _assert_subset(
                mapping.matched_step_ids,
                step_ids,
                field_name=f"mapping {mapping.event_id} matched_step_ids",
            )
            _assert_subset(
                mapping.satisfied_controls,
                valid_controls,
                field_name=f"mapping {mapping.event_id} satisfied_controls",
            )
            _assert_subset(
                mapping.contradicted_controls,
                valid_controls,
                field_name=f"mapping {mapping.event_id} contradicted_controls",
            )
            _assert_subset(
                mapping.evidence_clip_ids,
                allowed,
                field_name=f"mapping {mapping.event_id} evidence_clip_ids",
            )
            _assert_subset(
                mapping.evidence_clip_ids,
                event_evidence[mapping.event_id],
                field_name=(
                    f"mapping {mapping.event_id} event-specific evidence_clip_ids"
                ),
            )
            if (
                not mapping.matched_step_ids
                and not mapping.unresolved
            ):
                raise EvidencePolicyError(
                    f"Mapping {mapping.event_id} has no step and must be unresolved"
                )
        if len(mapped_event_ids) != len(set(mapped_event_ids)):
            raise OpenAIResponseError("Terra returned duplicate event mappings")
        _assert_subset(
            result.data.unresolved_event_ids,
            event_ids,
            field_name="unresolved_event_ids",
        )
        accounted_for = set(mapped_event_ids) | set(
            result.data.unresolved_event_ids
        )
        omitted = sorted(event_ids - accounted_for)
        if omitted:
            raise OpenAIResponseError(
                f"Terra omitted observed events from mapping output: {omitted}"
            )
        for mapping in result.data.mappings:
            matched_controls = {
                control
                for step in steps
                if step.step_id in mapping.matched_step_ids
                for control in step.required_controls
            }
            _assert_subset(
                mapping.satisfied_controls,
                matched_controls,
                field_name=(
                    f"mapping {mapping.event_id} step-specific "
                    "satisfied_controls"
                ),
            )
            _assert_subset(
                mapping.contradicted_controls,
                matched_controls,
                field_name=(
                    f"mapping {mapping.event_id} step-specific "
                    "contradicted_controls"
                ),
            )
            overlap = sorted(
                set(mapping.satisfied_controls)
                & set(mapping.contradicted_controls)
            )
            if overlap:
                raise EvidencePolicyError(
                    f"Mapping {mapping.event_id} both satisfies and "
                    f"contradicts controls: {overlap}"
                )
        _validate_narrative_language(
            [mapping.rationale for mapping in result.data.mappings]
        )
        return result

    async def resolve_conflicting_evidence(
        self,
        conflicts: Sequence[EvidenceConflict | Mapping[str, Any]],
        *,
        allowed_evidence_clip_ids: Iterable[str],
        previous_response_id: str | None = None,
    ) -> OpenAIResult[ConflictAssessmentBatch]:
        """Escalate only explicit evidence conflicts to Sol."""

        conflict_models = [
            item
            if isinstance(item, EvidenceConflict)
            else EvidenceConflict.model_validate(item)
            for item in conflicts
        ]
        if not conflict_models:
            raise ValueError("At least one evidence conflict is required")
        allowed = _unique_ids(
            allowed_evidence_clip_ids,
            field_name="allowed_evidence_clip_ids",
        )
        conflict_ids = _unique_ids(
            (conflict.conflict_id for conflict in conflict_models),
            field_name="conflict IDs",
        )
        conflict_evidence = {
            conflict.conflict_id: set(conflict.evidence_clip_ids)
            for conflict in conflict_models
        }
        for conflict in conflict_models:
            if len(conflict_evidence[conflict.conflict_id]) < 2:
                raise ValueError(
                    f"Conflict {conflict.conflict_id} requires two distinct "
                    "evidence clip IDs"
                )
            _assert_subset(
                conflict.evidence_clip_ids,
                allowed,
                field_name=f"conflict {conflict.conflict_id} evidence_clip_ids",
            )

        result = await self._structured_response(
            model=self.escalation_model,
            output_model=ConflictAssessmentBatch,
            stage="evidence-conflict-resolution",
            stage_instructions=_CONFLICT_INSTRUCTIONS,
            payload={
                "conflicts": conflict_models,
                "allowed_evidence_clip_ids": sorted(allowed),
            },
            previous_response_id=previous_response_id,
            reasoning_effort="high",
        )
        assessed_ids: list[str] = []
        for assessment in result.data.assessments:
            if assessment.conflict_id not in conflict_ids:
                raise EvidencePolicyError(
                    "Sol assessment references unknown conflict ID "
                    f"{assessment.conflict_id}"
                )
            assessed_ids.append(assessment.conflict_id)
            _assert_subset(
                assessment.evidence_clip_ids,
                conflict_evidence[assessment.conflict_id],
                field_name=(
                    f"conflict {assessment.conflict_id} assessment evidence_clip_ids"
                ),
            )
        if len(assessed_ids) != len(set(assessed_ids)):
            raise OpenAIResponseError("Sol returned duplicate conflict assessments")
        _validate_narrative_language(
            [assessment.summary for assessment in result.data.assessments]
            + result.data.limitations
        )
        return result

    async def analyze_deviations(
        self,
        planned_steps: Sequence[PlannedStep | Mapping[str, Any]],
        observed_events: Sequence[ObservedEvent | Mapping[str, Any]],
        mappings: EventStepMappingBatch | Mapping[str, Any],
        *,
        allowed_evidence_clip_ids: Iterable[str] | None = None,
        conflicts: Sequence[EvidenceConflict | Mapping[str, Any]] | None = None,
        previous_response_id: str | None = None,
    ) -> OpenAIResult[DeviationAnalysis]:
        """Draft deviations with Terra, using Sol only for supplied conflicts."""

        steps = [
            step
            if isinstance(step, PlannedStep)
            else PlannedStep.model_validate(step)
            for step in planned_steps
        ]
        events = [
            event
            if isinstance(event, ObservedEvent)
            else ObservedEvent.model_validate(event)
            for event in observed_events
        ]
        mapping_batch = (
            mappings
            if isinstance(mappings, EventStepMappingBatch)
            else EventStepMappingBatch.model_validate(mappings)
        )
        step_ids = {step.step_id for step in steps}
        event_ids = {event.event_id for event in events}
        derived_evidence = {
            clip_id for event in events for clip_id in event.evidence_clip_ids
        }
        allowed = (
            _unique_ids(
                allowed_evidence_clip_ids,
                field_name="allowed_evidence_clip_ids",
            )
            if allowed_evidence_clip_ids is not None
            else derived_evidence
        )
        _assert_subset(
            derived_evidence,
            allowed,
            field_name="observed event evidence_clip_ids",
        )
        for mapping in mapping_batch.mappings:
            if mapping.event_id not in event_ids:
                raise EvidencePolicyError(
                    f"Input mapping references unknown event ID {mapping.event_id}"
                )
            _assert_subset(
                mapping.matched_step_ids,
                step_ids,
                field_name=f"mapping {mapping.event_id} matched_step_ids",
            )
            _assert_subset(
                mapping.evidence_clip_ids,
                allowed,
                field_name=f"mapping {mapping.event_id} evidence_clip_ids",
            )
            matched_controls = {
                control
                for step in steps
                if step.step_id in mapping.matched_step_ids
                for control in step.required_controls
            }
            _assert_subset(
                mapping.satisfied_controls,
                matched_controls,
                field_name=(
                    f"mapping {mapping.event_id} step-specific "
                    "satisfied_controls"
                ),
            )
            _assert_subset(
                mapping.contradicted_controls,
                matched_controls,
                field_name=(
                    f"mapping {mapping.event_id} step-specific "
                    "contradicted_controls"
                ),
            )

        conflict_result: OpenAIResult[ConflictAssessmentBatch] | None = None
        if conflicts:
            conflict_result = await self.resolve_conflicting_evidence(
                conflicts,
                allowed_evidence_clip_ids=allowed,
            )
        conflict_assessments = (
            conflict_result.data.assessments if conflict_result else []
        )

        result = await self._structured_response(
            model=self.reasoning_model,
            output_model=DeviationAnalysis,
            stage="deviation-analysis",
            stage_instructions=_DEVIATION_INSTRUCTIONS,
            payload={
                "planned_steps": steps,
                "observed_events": events,
                "event_step_mappings": mapping_batch,
                "conflict_assessments": conflict_assessments,
                "allowed_evidence_clip_ids": sorted(allowed),
            },
            previous_response_id=previous_response_id,
            reasoning_effort="medium",
        )
        step_evidence: dict[str, set[str]] = {}
        for mapping in mapping_batch.mappings:
            for step_id in mapping.matched_step_ids:
                step_evidence.setdefault(step_id, set()).update(
                    mapping.evidence_clip_ids
                )
        self._validate_deviation_analysis(
            result.data,
            step_ids=step_ids,
            allowed_evidence=allowed,
            conflict_assessments=conflict_assessments,
            step_evidence=step_evidence,
        )
        if conflict_result is None:
            return result
        return OpenAIResult(
            data=result.data,
            response_id=result.response_id,
            model=result.model,
            escalation_response_id=conflict_result.response_id,
            escalation_model=conflict_result.model,
        )

    @staticmethod
    def _validate_deviation_analysis(
        analysis: DeviationAnalysis,
        *,
        step_ids: set[str],
        allowed_evidence: set[str],
        conflict_assessments: Sequence[ConflictAssessment],
        step_evidence: Mapping[str, set[str]],
    ) -> None:
        finding_ids: set[str] = set()
        language: list[str] = list(analysis.limitations)
        for finding in analysis.findings:
            if finding.finding_id in finding_ids:
                raise OpenAIResponseError(
                    f"Terra returned duplicate finding ID {finding.finding_id}"
                )
            finding_ids.add(finding.finding_id)
            if finding.jha_step_id not in step_ids:
                raise EvidencePolicyError(
                    f"Finding references unknown JHA step {finding.jha_step_id}"
                )
            if finding.status == FindingStatus.REQUIRED_CONTROL_NOT_OBSERVED:
                raise EvidencePolicyError(
                    "OpenAI may not infer REQUIRED_CONTROL_NOT_OBSERVED; "
                    "Neo4j computes that status from explicit coverage"
                )
            _assert_subset(
                finding.evidence_clip_ids,
                allowed_evidence,
                field_name=f"finding {finding.finding_id} evidence_clip_ids",
            )
            if finding.status != FindingStatus.UNVERIFIABLE:
                _assert_subset(
                    finding.evidence_clip_ids,
                    step_evidence.get(finding.jha_step_id, set()),
                    field_name=(
                        f"finding {finding.finding_id} step-mapped "
                        "evidence_clip_ids"
                    ),
                )
            if finding.graph_path_node_ids or finding.graph_path_relationships:
                raise EvidencePolicyError(
                    "OpenAI may not invent Neo4j graph path identifiers"
                )
            language.extend(
                [
                    finding.title,
                    finding.planned_control,
                    finding.observed_work or "",
                    finding.evidence_gap_reason or "",
                ]
            )
        for claim in analysis.observed_sequence:
            _assert_subset(
                claim.evidence_clip_ids,
                allowed_evidence,
                field_name=f"claim {claim.claim_id} evidence_clip_ids",
            )
            _assert_subset(
                claim.finding_ids,
                finding_ids,
                field_name=f"claim {claim.claim_id} finding_ids",
            )
            language.append(claim.text)

        supplied_assessments = {
            assessment.conflict_id: assessment for assessment in conflict_assessments
        }
        for assessment in analysis.conflict_assessments:
            source = supplied_assessments.get(assessment.conflict_id)
            if source is None or assessment != source:
                raise EvidencePolicyError(
                    "Terra may copy but not alter Sol conflict assessments"
                )
            language.append(assessment.summary)
        _validate_narrative_language(language)

    async def draft_report(
        self,
        *,
        case_id: str,
        title: str,
        planned_steps: Sequence[PlannedStep | Mapping[str, Any]],
        observed_events: Sequence[ObservedEvent | Mapping[str, Any]],
        findings: Sequence[DeviationFinding | Mapping[str, Any]],
        limitations: Sequence[str] | None = None,
        allowed_evidence_clip_ids: Iterable[str] | None = None,
        previous_response_id: str | None = None,
    ) -> OpenAIResult[ReportNarrative]:
        """Use Terra to draft an evidence-cited, pre-approval report."""

        if not case_id.strip():
            raise ValueError("case_id is required")
        if not title.strip():
            raise ValueError("title is required")
        steps = [
            step
            if isinstance(step, PlannedStep)
            else PlannedStep.model_validate(step)
            for step in planned_steps
        ]
        events = [
            event
            if isinstance(event, ObservedEvent)
            else ObservedEvent.model_validate(event)
            for event in observed_events
        ]
        finding_models = [
            finding
            if isinstance(finding, DeviationFinding)
            else DeviationFinding.model_validate(finding)
            for finding in findings
        ]
        finding_ids = {finding.finding_id for finding in finding_models}
        derived_evidence = {
            clip_id for event in events for clip_id in event.evidence_clip_ids
        }
        for finding in finding_models:
            derived_evidence.update(finding.evidence_clip_ids)
        allowed = (
            _unique_ids(
                allowed_evidence_clip_ids,
                field_name="allowed_evidence_clip_ids",
            )
            if allowed_evidence_clip_ids is not None
            else derived_evidence
        )
        _assert_subset(
            derived_evidence,
            allowed,
            field_name="report input evidence_clip_ids",
        )
        result = await self._structured_response(
            model=self.reasoning_model,
            output_model=ReportNarrative,
            stage="report-draft",
            stage_instructions=_REPORT_INSTRUCTIONS,
            payload={
                "case_id": case_id,
                "requested_title": title,
                "planned_steps": steps,
                "observed_events": events,
                "findings": finding_models,
                "known_limitations": list(limitations or []),
                "allowed_evidence_clip_ids": sorted(allowed),
            },
            previous_response_id=previous_response_id,
            reasoning_effort="medium",
        )
        self._validate_report(
            result.data,
            allowed_evidence=allowed,
            finding_ids=finding_ids,
            finding_evidence={
                finding.finding_id: set(finding.evidence_clip_ids)
                for finding in finding_models
            },
        )
        return result

    @staticmethod
    def _validate_report(
        report: ReportNarrative,
        *,
        allowed_evidence: set[str],
        finding_ids: set[str],
        finding_evidence: Mapping[str, set[str]],
    ) -> None:
        claims = (
            report.incident_overview
            + report.event_timeline
            + report.deviation_summary
        )
        claim_ids: set[str] = set()
        language = [report.title, *report.limitations]
        for claim in claims:
            if claim.claim_id in claim_ids:
                raise OpenAIResponseError(
                    f"Terra returned duplicate report claim ID {claim.claim_id}"
                )
            claim_ids.add(claim.claim_id)
            _assert_subset(
                claim.evidence_clip_ids,
                allowed_evidence,
                field_name=f"report claim {claim.claim_id} evidence_clip_ids",
            )
            _assert_subset(
                claim.finding_ids,
                finding_ids,
                field_name=f"report claim {claim.claim_id} finding_ids",
            )
            if claim.finding_ids:
                cited_finding_evidence = {
                    clip_id
                    for finding_id in claim.finding_ids
                    for clip_id in finding_evidence[finding_id]
                }
                _assert_subset(
                    claim.evidence_clip_ids,
                    cited_finding_evidence,
                    field_name=(
                        f"report claim {claim.claim_id} finding-linked "
                        "evidence_clip_ids"
                    ),
                )
            language.append(claim.text)
        for action in report.corrective_actions:
            if action.finding_id not in finding_ids:
                raise EvidencePolicyError(
                    f"Corrective action references unknown finding {action.finding_id}"
                )
            language.append(action.description)
        _validate_narrative_language(language)


__all__ = [
    "ConflictAssessment",
    "ConflictAssessmentBatch",
    "DeviationAnalysis",
    "EvidenceBackedClaim",
    "EvidenceConflict",
    "EvidencePolicyError",
    "EventStepMapping",
    "EventStepMappingBatch",
    "ObservedEventBatch",
    "OpenAIResponseError",
    "OpenAIResult",
    "OpenAIService",
    "OpenAIUnavailableError",
    "PlanDocument",
    "PlannedStepBatch",
    "ReportNarrative",
]
