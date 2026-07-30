from pathlib import Path

from app.pipeline import InvestigationPipeline, _jockey_observations
from app.schemas import (
    CaseRecord,
    CaseStatus,
    EvidenceClip,
    FindingStatus,
    ObservedEvent,
    PlannedStep,
    UploadedInput,
)
from app.services.openai_service import EventStepMapping, EventStepMappingBatch


def sample_record() -> CaseRecord:
    return CaseRecord(
        case_id="CASE-1",
        title="Uploaded investigation",
        status=CaseStatus.UPLOADED,
        jha=UploadedInput(
            filename="JHA-01.pdf",
            media_type="application/pdf",
            size_bytes=100,
            local_path=str(Path("JHA-01.pdf")),
        ),
        videos=[
            UploadedInput(
                filename="camera.mp4",
                media_type="video/mp4",
                size_bytes=100,
                local_path=str(Path("camera.mp4")),
            )
        ],
    )


def sample_clip() -> EvidenceClip:
    return EvidenceClip(
        evidence_clip_id="CLIP-1",
        item_id="ITEM-1",
        asset_id="ASSET-1",
        camera_id="CAM-01",
        source_filename="camera.mp4",
        start_sec=12,
        end_sec=18,
        summary="Material was placed beside the marked walkway.",
        visible_text="PEDESTRIAN WALKWAY",
        confidence=0.91,
    )


def test_jockey_results_are_cited_to_existing_twelvelabs_clips():
    observations = _jockey_observations(
        {
            "events": [
                {
                    "event_type": "PLACEMENT",
                    "summary": "The same pallet appears beside the walkway.",
                    "camera_id": "CAM-01",
                    "start_sec": 13,
                    "end_sec": 17,
                    "confidence": 0.8,
                    "connection_basis": "visual and temporal agreement",
                }
            ]
        },
        [sample_clip()],
    )
    assert observations[0]["evidence_clip_ids"] == ["CLIP-1"]
    assert observations[0]["observation_type"] == "inferred"


def test_planned_and_observed_graphs_use_matching_zone_and_control_ids():
    step = PlannedStep(
        step_id="STEP-1",
        sequence=1,
        name="Stage material",
        required_controls=["Keep the walkway clear"],
        must_avoid_zones=["Walkway A"],
    )
    plan, control_lookup = InvestigationPipeline._neo4j_plan(
        sample_record(),
        [step],
    )
    assert plan["steps"][0]["prohibited_zones"][0]["id"] == "ZONE:WALKWAY_A"
    event = ObservedEvent(
        event_id="EVENT-1",
        event_type="EGRESS_STATE_CHANGE",
        summary="Walkway A is visibly blocked.",
        camera_id="CAM-01",
        start_sec=12,
        end_sec=18,
        object_ids=["Pallet P1"],
        zone_ids=["Walkway A"],
        evidence_clip_ids=["CLIP-1"],
        confidence=0.91,
    )
    mappings = EventStepMappingBatch(
        mappings=[
            EventStepMapping(
                event_id="EVENT-1",
                matched_step_ids=["STEP-1"],
                satisfied_controls=[],
                contradicted_controls=["Keep the walkway clear"],
                evidence_clip_ids=["CLIP-1"],
                confidence=0.9,
                rationale="The cited clip shows the marked walkway obstructed.",
            )
        ]
    )
    graph_event = InvestigationPipeline._neo4j_events(
        [event],
        mappings,
        control_lookup,
    )[0]
    assert graph_event["matched_step_ids"] == ["STEP-1"]
    assert graph_event["zone_ids"] == ["ZONE:WALKWAY_A"]
    assert graph_event["blocked_zone_ids"] == ["ZONE:WALKWAY_A"]


def test_graph_diff_conversion_never_labels_an_uncited_fact():
    findings = InvestigationPipeline._findings_from_graph_rows(
        "CASE-1",
        [
            {
                "finding_type": "REQUIRED_CONTROL",
                "status": "UNVERIFIABLE",
                "step_id": "STEP-1",
                "planned_control": "Spotter remains present",
                "rationale": "Available footage is insufficient.",
                "evidence_clip_ids": [],
            },
            {
                "finding_type": "PROHIBITED_ZONE",
                "status": "CONFIRMED_DEVIATION",
                "step_id": "STEP-2",
                "planned_control": "Avoid Walkway A",
                "event_id": "EVENT-1",
                "zone_id": "ZONE:WALKWAY_A",
                "evidence_clip_ids": ["CLIP-1"],
            },
        ],
        [
            ObservedEvent(
                event_id="EVENT-1",
                event_type="PLACEMENT",
                summary="Material is visibly inside Walkway A.",
                camera_id="CAM-01",
                start_sec=12,
                end_sec=18,
                evidence_clip_ids=["CLIP-1"],
                confidence=0.9,
            )
        ],
    )
    assert findings[0].status == FindingStatus.UNVERIFIABLE
    assert findings[0].evidence_gap_reason
    assert findings[1].status == FindingStatus.CONFIRMED_DEVIATION
    assert findings[1].evidence_clip_ids == ["CLIP-1"]
