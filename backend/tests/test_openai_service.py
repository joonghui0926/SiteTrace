from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from backend.app.schemas import (
    CorrectiveAction,
    DeviationFinding,
    FindingStatus,
    ObservedEvent,
    PlannedStep,
)
from backend.app.services.openai_service import (
    ConflictAssessment,
    ConflictAssessmentBatch,
    DeviationAnalysis,
    EvidenceBackedClaim,
    EvidenceConflict,
    EvidencePolicyError,
    EventStepMapping,
    EventStepMappingBatch,
    ObservedEventBatch,
    OpenAIService,
    OpenAIUnavailableError,
    PlannedStepBatch,
    ReportNarrative,
)


def run(coroutine):
    return asyncio.run(coroutine)


class FakeResponses:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("Unexpected Responses API call")
        output = self.outputs.pop(0)
        return SimpleNamespace(
            id=f"resp-{len(self.calls)}",
            model=kwargs["model"],
            output_parsed=output,
        )


class FakeClient:
    def __init__(self, outputs: list[Any]) -> None:
        self.responses = FakeResponses(outputs)


def configured_settings(*, key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=key,
        openai_normalization_model="gpt-5.6-luna",
        openai_reasoning_model="gpt-5.6-terra",
        openai_escalation_model="gpt-5.6-sol",
    )


def planned_step() -> PlannedStep:
    return PlannedStep(
        step_id="JHA-07:S2",
        sequence=2,
        name="Keep walkway clear",
        required_controls=["Walkway A remains open"],
        required_roles=["Spotter"],
    )


def observed_event(*, clip_id: str = "CLIP-01") -> ObservedEvent:
    return ObservedEvent(
        event_id="EVENT-01",
        event_type="WALKWAY_BLOCKED",
        summary="A pallet is visibly positioned across Walkway A.",
        camera_id="CAM-01",
        start_sec=12.5,
        end_sec=18.0,
        object_ids=["PALLET-P1"],
        zone_ids=["WALKWAY-A"],
        evidence_clip_ids=[clip_id],
        confidence=0.94,
    )


def event_mapping() -> EventStepMappingBatch:
    return EventStepMappingBatch(
        mappings=[
            EventStepMapping(
                event_id="EVENT-01",
                matched_step_ids=["JHA-07:S2"],
                contradicted_controls=["Walkway A remains open"],
                evidence_clip_ids=["CLIP-01"],
                confidence=0.93,
                rationale="The cited clip positively shows an obstruction.",
            )
        ]
    )


def confirmed_finding() -> DeviationFinding:
    return DeviationFinding(
        finding_id="FINDING-01",
        jha_step_id="JHA-07:S2",
        status=FindingStatus.CONFIRMED_DEVIATION,
        title="Walkway obstruction",
        planned_control="Walkway A remains open",
        observed_work="A pallet is visibly positioned across Walkway A.",
        evidence_clip_ids=["CLIP-01"],
        confidence=0.93,
        requires_human_review=True,
    )


def test_unconfigured_service_imports_and_reports_unavailable_cleanly():
    service = OpenAIService(configured_settings=configured_settings(key=""))

    assert service.available is False
    assert service.unavailable_reason == "OPENAI_API_KEY is not configured"
    with pytest.raises(OpenAIUnavailableError, match="OPENAI_API_KEY"):
        _ = service.client


def test_plan_extraction_uses_terra_structured_output_cache_and_continuation():
    output = PlannedStepBatch(planned_steps=[planned_step()])
    client = FakeClient([output])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.parse_plan_documents(
            {"JHA-07.pdf": "Step 2: Keep the walkway open."},
            previous_response_id="resp-prior",
        )
    )

    assert result.data.planned_steps == [planned_step()]
    assert result.response_id == "resp-1"
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["text_format"] is PlannedStepBatch
    assert call["prompt_cache_key"] == "sitetrace-plan-extraction-v1"
    assert call["previous_response_id"] == "resp-prior"
    assert call["store"] is True
    payload = json.loads(call["input"])
    assert payload["documents"][0]["filename"] == "JHA-07.pdf"


def test_luna_normalization_preserves_only_allowed_evidence_ids():
    client = FakeClient([ObservedEventBatch(events=[observed_event()])])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.normalize_observed_events(
            [
                {
                    "segment_id": "SEG-01",
                    "camera_id": "CAM-01",
                    "evidence_clip_ids": ["CLIP-01"],
                }
            ],
            allowed_evidence_clip_ids=["CLIP-01"],
        )
    )

    assert result.data.events[0].evidence_clip_ids == ["CLIP-01"]
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-luna"
    assert call["reasoning"] == {"effort": "low"}
    assert call["text_format"] is ObservedEventBatch

    invented_client = FakeClient(
        [ObservedEventBatch(events=[observed_event(clip_id="MADE-UP-CLIP")])]
    )
    invented_service = OpenAIService(
        configured_settings=configured_settings(),
        client=invented_client,
    )
    with pytest.raises(EvidencePolicyError, match="not supplied"):
        run(
            invented_service.normalize_observed_events(
                [{"segment_id": "SEG-01"}],
                allowed_evidence_clip_ids=["CLIP-01"],
            )
        )


def test_event_step_mapping_is_typed_and_validates_input_ids():
    output = event_mapping()
    client = FakeClient([output])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.match_events_to_steps(
            [planned_step()],
            [observed_event()],
            allowed_evidence_clip_ids=["CLIP-01"],
            previous_response_id="resp-normalized",
        )
    )

    mapping = result.data.mappings[0]
    assert mapping.event_id == "EVENT-01"
    assert mapping.matched_step_ids == ["JHA-07:S2"]
    assert mapping.contradicted_controls == ["Walkway A remains open"]
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["previous_response_id"] == "resp-normalized"
    assert call["text_format"] is EventStepMappingBatch

    invalid_output = event_mapping().model_copy(deep=True)
    invalid_output.mappings[0].matched_step_ids = ["JHA-MADE-UP"]
    invalid_client = FakeClient([invalid_output])
    invalid_service = OpenAIService(
        configured_settings=configured_settings(),
        client=invalid_client,
    )
    with pytest.raises(EvidencePolicyError, match="matched_step_ids"):
        run(
            invalid_service.match_events_to_steps(
                [planned_step()],
                [observed_event()],
            )
        )


def test_deviations_use_terra_without_unnecessary_sol_escalation():
    analysis = DeviationAnalysis(
        findings=[confirmed_finding()],
        observed_sequence=[
            EvidenceBackedClaim(
                claim_id="CLAIM-01",
                text="The pallet is observed in Walkway A.",
                evidence_clip_ids=["CLIP-01"],
                finding_ids=["FINDING-01"],
            )
        ],
    )
    client = FakeClient([analysis])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.analyze_deviations(
            [planned_step()],
            [observed_event()],
            event_mapping(),
            previous_response_id="resp-mapping",
        )
    )

    assert result.escalated is False
    assert [call["model"] for call in client.responses.calls] == [
        "gpt-5.6-terra"
    ]
    assert client.responses.calls[0]["previous_response_id"] == "resp-mapping"
    assert result.data.findings[0].finding_id == "FINDING-01"


def test_declared_conflict_calls_sol_then_terra_and_records_both_responses():
    sol_assessment = ConflictAssessment(
        conflict_id="CONFLICT-01",
        status="UNRESOLVED",
        summary="The two cited views do not establish a single state.",
        evidence_clip_ids=["CLIP-01", "CLIP-02"],
    )
    sol_output = ConflictAssessmentBatch(assessments=[sol_assessment])
    terra_output = DeviationAnalysis(
        findings=[
            DeviationFinding(
                finding_id="FINDING-02",
                jha_step_id="JHA-07:S2",
                status=FindingStatus.UNVERIFIABLE,
                title="Walkway state unresolved",
                planned_control="Walkway A remains open",
                evidence_clip_ids=["CLIP-01", "CLIP-02"],
                confidence=0.4,
                evidence_gap_reason="The cited camera views conflict.",
            )
        ],
        conflict_assessments=[sol_assessment],
        limitations=["The walkway state remains unresolved."],
    )
    client = FakeClient([sol_output, terra_output])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.analyze_deviations(
            [planned_step()],
            [observed_event()],
            event_mapping(),
            allowed_evidence_clip_ids=["CLIP-01", "CLIP-02"],
            conflicts=[
                EvidenceConflict(
                    conflict_id="CONFLICT-01",
                    description="Camera views disagree about walkway state.",
                    evidence_clip_ids=["CLIP-01", "CLIP-02"],
                )
            ],
        )
    )

    assert [call["model"] for call in client.responses.calls] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert result.escalated is True
    assert result.escalation_response_id == "resp-1"
    assert result.response_id == "resp-2"
    assert result.escalation_model == "gpt-5.6-sol"


def test_model_cannot_turn_missing_control_evidence_into_nonperformance():
    unsupported = DeviationAnalysis(
        findings=[
            DeviationFinding(
                finding_id="FINDING-ABSENCE",
                jha_step_id="JHA-07:S2",
                status=FindingStatus.REQUIRED_CONTROL_NOT_OBSERVED,
                title="Spotter not observed",
                planned_control="Spotter remains present",
                evidence_clip_ids=["CLIP-01"],
                confidence=0.6,
            )
        ]
    )
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=FakeClient([unsupported]),
    )

    with pytest.raises(
        EvidencePolicyError,
        match="REQUIRED_CONTROL_NOT_OBSERVED",
    ):
        run(
            service.analyze_deviations(
                [planned_step()],
                [observed_event()],
                event_mapping(),
            )
        )


def test_report_uses_terra_requires_citations_and_rejects_root_cause_claims():
    report = ReportNarrative(
        title="Lift Zone C Near-Miss Investigation",
        incident_overview=[
            EvidenceBackedClaim(
                claim_id="REPORT-CLAIM-01",
                text="A pallet is visible across Walkway A.",
                evidence_clip_ids=["CLIP-01"],
                finding_ids=["FINDING-01"],
            )
        ],
        corrective_actions=[
            CorrectiveAction(
                action_id="ACTION-01",
                finding_id="FINDING-01",
                action_type="immediate",
                description="Remove the pallet and verify the walkway is clear.",
                owner_role="Site Supervisor",
            )
        ],
    )
    client = FakeClient([report])
    service = OpenAIService(
        configured_settings=configured_settings(),
        client=client,
    )

    result = run(
        service.draft_report(
            case_id="CASE-01",
            title="Lift Zone C Near-Miss Investigation",
            planned_steps=[planned_step()],
            observed_events=[observed_event()],
            findings=[confirmed_finding()],
            previous_response_id="resp-findings",
        )
    )

    assert result.data.human_approval_required is True
    call = client.responses.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["previous_response_id"] == "resp-findings"
    assert call["text_format"] is ReportNarrative

    unsafe_report = report.model_copy(deep=True)
    unsafe_report.incident_overview[0].text = (
        "The root cause was inadequate management training."
    )
    unsafe_service = OpenAIService(
        configured_settings=configured_settings(),
        client=FakeClient([unsafe_report]),
    )
    with pytest.raises(EvidencePolicyError, match="root"):
        run(
            unsafe_service.draft_report(
                case_id="CASE-01",
                title="Unsafe report",
                planned_steps=[planned_step()],
                observed_events=[observed_event()],
                findings=[confirmed_finding()],
            )
        )
