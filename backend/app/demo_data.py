"""Deterministic incident package used only when sponsor services are unavailable."""

from __future__ import annotations

from .schemas import (
    CorrectiveAction,
    DeviationFinding,
    EvidenceClip,
    FindingStatus,
    InvestigationPackage,
    ObservedEvent,
    PlannedStep,
)


def build_demo_investigation(case_id: str, title: str) -> InvestigationPackage:
    planned_steps = [
        PlannedStep(
            step_id="JHA-MAT-014-S1",
            sequence=1,
            name="Inspect the movement route",
            required_controls=["Confirm all egress paths are clear"],
            must_avoid_zones=["EXIT_E2"],
        ),
        PlannedStep(
            step_id="JHA-MAT-014-S2",
            sequence=2,
            name="Move the pallet",
            required_controls=["Named spotter remains present throughout movement"],
            required_roles=["SPOTTER"],
        ),
        PlannedStep(
            step_id="JHA-MAT-014-S3",
            sequence=3,
            name="Stage the pallet",
            required_controls=["Place materials fully inside Storage Zone B"],
            must_avoid_zones=["EXIT_E2"],
        ),
        PlannedStep(
            step_id="JHA-MAT-014-S4",
            sequence=4,
            name="Close out the work",
            required_controls=["Conduct a final egress inspection"],
        ),
    ]
    clips = [
        EvidenceClip(
            evidence_clip_id="EV-014",
            item_id="CAM01-DEMO",
            camera_id="CAM 01",
            source_filename="cam-01.mp4",
            start_sec=132.4,
            end_sec=141.8,
            summary="A worker begins moving Pallet A through the east corridor.",
            confidence=0.94,
        ),
        EvidenceClip(
            evidence_clip_id="EV-019",
            item_id="CAM01-DEMO",
            camera_id="CAM 01",
            source_filename="cam-01.mp4",
            start_sec=142.0,
            end_sec=150.0,
            summary="The spotter leaves while pallet movement continues.",
            confidence=0.86,
        ),
        EvidenceClip(
            evidence_clip_id="EV-027",
            item_id="CAM02-DEMO",
            camera_id="CAM 02",
            source_filename="cam-02.mp4",
            start_sec=148.1,
            end_sec=157.0,
            summary="The same pallet is placed next to the marked Exit E2.",
            visible_text="EXIT E2",
            confidence=0.88,
        ),
        EvidenceClip(
            evidence_clip_id="EV-041",
            item_id="CAM03-DEMO",
            camera_id="CAM 03",
            source_filename="cam-03.mp4",
            start_sec=380.0,
            end_sec=391.0,
            summary="Exit E2 is first observed with its egress path obstructed.",
            visible_text="EXIT E2",
            confidence=0.96,
        ),
    ]
    events = [
        ObservedEvent(
            event_id="EVENT-MOVE-01",
            event_type="MATERIAL_MOVEMENT",
            summary=clips[0].summary,
            camera_id="CAM 01",
            start_sec=clips[0].start_sec,
            end_sec=clips[0].end_sec,
            actor_ids=["WORKER_ORANGE_VEST"],
            object_ids=["PALLET_A"],
            zone_ids=["EAST_CORRIDOR"],
            evidence_clip_ids=["EV-014"],
            confidence=0.94,
        ),
        ObservedEvent(
            event_id="EVENT-SPOTTER-LEAVE-01",
            event_type="SPOTTER_PRESENCE_CHANGE",
            summary=clips[1].summary,
            camera_id="CAM 01",
            start_sec=clips[1].start_sec,
            end_sec=clips[1].end_sec,
            actor_ids=["SPOTTER_CANDIDATE"],
            zone_ids=["EAST_CORRIDOR"],
            evidence_clip_ids=["EV-019"],
            confidence=0.86,
        ),
        ObservedEvent(
            event_id="EVENT-PLACE-01",
            event_type="MATERIAL_PLACEMENT",
            summary=clips[2].summary,
            camera_id="CAM 02",
            start_sec=clips[2].start_sec,
            end_sec=clips[2].end_sec,
            object_ids=["PALLET_A"],
            zone_ids=["EXIT_E2"],
            evidence_clip_ids=["EV-027"],
            confidence=0.88,
        ),
        ObservedEvent(
            event_id="EVENT-BLOCKED-01",
            event_type="EGRESS_STATE_CHANGE",
            summary=clips[3].summary,
            camera_id="CAM 03",
            start_sec=clips[3].start_sec,
            end_sec=clips[3].end_sec,
            object_ids=["PALLET_A"],
            zone_ids=["EXIT_E2"],
            evidence_clip_ids=["EV-041"],
            confidence=0.96,
        ),
    ]
    findings = [
        DeviationFinding(
            finding_id="DEV-001",
            jha_step_id="JHA-MAT-014-S2",
            status=FindingStatus.CONFIRMED_DEVIATION,
            title="Spotter control was not maintained through the movement window",
            planned_control="Named spotter remains present throughout movement",
            observed_work="The spotter left before the pallet reached its destination.",
            evidence_clip_ids=["EV-014", "EV-019"],
            graph_path_node_ids=[
                "JHA-MAT-014-S2",
                "SPOTTER_CONTROL",
                "EVENT-SPOTTER-LEAVE-01",
                "EVENT-MOVE-01",
            ],
            graph_path_relationships=[
                "REQUIRES",
                "CONTRADICTED_BY",
                "PRECEDES",
            ],
            confidence=0.86,
        ),
        DeviationFinding(
            finding_id="DEV-002",
            jha_step_id="JHA-MAT-014-S3",
            status=FindingStatus.CONFIRMED_DEVIATION,
            title="Material was staged in a prohibited egress zone",
            planned_control="Place materials fully inside Storage Zone B",
            observed_work="Pallet A was placed next to Exit E2 and the path was later observed blocked.",
            evidence_clip_ids=["EV-027", "EV-041"],
            graph_path_node_ids=[
                "JHA-MAT-014-S3",
                "PALLET_A",
                "EVENT-PLACE-01",
                "EXIT_E2",
                "EVENT-BLOCKED-01",
            ],
            graph_path_relationships=[
                "MUST_AVOID",
                "INVOLVES",
                "OCCURRED_IN",
                "STATE_CHANGED_TO",
            ],
            confidence=0.92,
        ),
        DeviationFinding(
            finding_id="DEV-003",
            jha_step_id="JHA-MAT-014-S4",
            status=FindingStatus.UNVERIFIABLE,
            title="Final egress inspection could not be verified",
            planned_control="Conduct a final egress inspection",
            observed_work=None,
            evidence_gap_reason="Camera coverage ended before work closeout.",
            confidence=0.41,
        ),
    ]
    actions = [
        CorrectiveAction(
            action_id="ACT-001",
            finding_id="DEV-002",
            action_type="immediate",
            description="Remove Pallet A and inspect the full Exit E2 egress route.",
            owner_role="Site Safety Manager",
            due_date="Immediately",
        ),
        CorrectiveAction(
            action_id="ACT-002",
            finding_id="DEV-001",
            action_type="corrective",
            description="Assign a named spotter for the complete material-movement window.",
            owner_role="General Foreman",
            due_date="Before the next material movement",
        ),
        CorrectiveAction(
            action_id="ACT-003",
            finding_id="DEV-003",
            action_type="preventive",
            description="Require recorded visual confirmation of the final egress inspection.",
            owner_role="Safety Program Manager",
            due_date="Within 7 days",
        ),
    ]
    return InvestigationPackage(
        case_id=case_id,
        title=title,
        incident_summary=(
            "Multi-camera evidence shows Pallet A moving through the east corridor, "
            "the spotter leaving before placement, and Exit E2 subsequently being "
            "observed obstructed. The sequence is supported by four cited clips. "
            "Organizational root cause remains outside the scope of video evidence."
        ),
        planned_steps=planned_steps,
        events=events,
        evidence_clips=clips,
        findings=findings,
        corrective_actions=actions,
        limitations=[
            "Video evidence establishes observed sequence, not organizational root cause.",
            "The final closeout inspection is unverifiable because camera coverage ends.",
            "Person labels describe visible characteristics and are not biometric identities.",
        ],
        knowledge_store_id="demo-knowledge-store",
        jockey_session_id="demo-session",
        graph_metrics={
            "planned_steps": 4,
            "events": 4,
            "entities": 7,
            "relationships": 19,
            "findings": 3,
        },
        sponsor_trace=[
            {
                "sponsor": "TwelveLabs",
                "feature": "Pegasus 1.5 segmentation",
                "status": "demo",
                "detail": "Four timestamped evidence clips",
            },
            {
                "sponsor": "TwelveLabs",
                "feature": "Jockey cross-video reasoning",
                "status": "demo",
                "detail": "Pallet A linked across three cameras",
            },
            {
                "sponsor": "OpenAI",
                "feature": "Structured Outputs",
                "status": "demo",
                "detail": "JHA steps and findings validated",
            },
            {
                "sponsor": "Neo4j",
                "feature": "Deterministic graph diff",
                "status": "demo",
                "detail": "Planned and observed relationships compared",
            },
            {
                "sponsor": "AWS Strands",
                "feature": "Workflow and human approval",
                "status": "demo",
                "detail": "Report is awaiting reviewer approval",
            },
        ],
    )

