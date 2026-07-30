from datetime import UTC, datetime

from pypdf import PdfReader

from backend.app.schemas import (
    CitedNarrativeClaim,
    CorrectiveAction,
    DeviationFinding,
    EvidenceClip,
    FindingStatus,
    InvestigationPackage,
    ObservedEvent,
    PlannedStep,
)
from backend.app.services.report_service import ReportService


def sample_investigation() -> InvestigationPackage:
    clips = [
        EvidenceClip(
            evidence_clip_id="CLIP-001",
            item_id="ITEM-01",
            asset_id="ASSET-01",
            camera_id="CAM-01",
            source_filename="camera-one.mp4",
            start_sec=12.2,
            end_sec=21.8,
            summary="Material is placed across a marked pedestrian route.",
            visible_text="PEDESTRIAN WALKWAY",
            confidence=0.94,
        ),
        EvidenceClip(
            evidence_clip_id="CLIP-002",
            item_id="ITEM-02",
            asset_id="ASSET-02",
            camera_id="CAM-02",
            source_filename="camera-two.mp4",
            start_sec=39.0,
            end_sec=48.5,
            summary="A pedestrian changes route toward the equipment work area.",
            transcript="Stop!",
            visible_text="AUTHORIZED PERSONNEL ONLY",
            confidence=0.89,
        ),
    ]
    return InvestigationPackage(
        case_id="CASE-REPORT-01",
        title="Material Movement Near-Miss Investigation",
        incident_summary=(
            "Available footage records a blocked pedestrian route and a later "
            "near-miss sequence in an adjacent equipment work area."
        ),
        incident_overview=[
            CitedNarrativeClaim(
                claim_id="CLAIM-01",
                text=(
                    "The investigation was initiated after available footage "
                    "recorded material placed across a marked pedestrian route "
                    "during an active material-movement operation."
                ),
                evidence_clip_ids=["CLIP-001"],
                finding_ids=["FINDING-01"],
            ),
            CitedNarrativeClaim(
                claim_id="CLAIM-02",
                text=(
                    "A separate camera later recorded a pedestrian changing route "
                    "toward the equipment work area before an audible stop warning."
                ),
                evidence_clip_ids=["CLIP-002"],
                finding_ids=["FINDING-01"],
            ),
        ],
        event_timeline=[
            CitedNarrativeClaim(
                claim_id="TIMELINE-01",
                text=(
                    "At 00:12.2 on CAM-01, material entered the boundary of the "
                    "marked pedestrian route and remained visible through 00:21.8."
                ),
                evidence_clip_ids=["CLIP-001"],
            ),
            CitedNarrativeClaim(
                claim_id="TIMELINE-02",
                text=(
                    "At 00:39.0 on CAM-02, a pedestrian changed direction toward "
                    "the equipment area; an audible stop warning was retained in "
                    "the same evidence interval."
                ),
                evidence_clip_ids=["CLIP-002"],
            ),
        ],
        deviation_summary=[
            CitedNarrativeClaim(
                claim_id="DEVIATION-CLAIM-01",
                text=(
                    "The approved step required the pedestrian route to remain "
                    "open. CLIP-001 positively shows an incompatible condition, so "
                    "the graph comparison classified the matter as a confirmed "
                    "deviation rather than an absence-based inference."
                ),
                evidence_clip_ids=["CLIP-001"],
                finding_ids=["FINDING-01"],
            )
        ],
        planned_steps=[
            PlannedStep(
                step_id="JHA-S1",
                sequence=1,
                name="Stage material",
                description="Place material inside the approved storage area.",
                required_controls=["Keep the pedestrian walkway open"],
                must_avoid_zones=["Pedestrian Walkway"],
                required_roles=["Designated spotter"],
            )
        ],
        events=[
            ObservedEvent(
                event_id="EVENT-01",
                event_type="MATERIAL_PLACEMENT",
                summary="Material is placed across the marked pedestrian route.",
                camera_id="CAM-01",
                start_sec=12.2,
                end_sec=21.8,
                actor_ids=["Worker candidate A"],
                object_ids=["Material load P1"],
                zone_ids=["Pedestrian Walkway"],
                evidence_clip_ids=["CLIP-001"],
                confidence=0.94,
            ),
            ObservedEvent(
                event_id="EVENT-02",
                event_type="PEDESTRIAN_ROUTE_CHANGE",
                summary="A pedestrian changes route toward the equipment area.",
                camera_id="CAM-02",
                start_sec=39.0,
                end_sec=48.5,
                actor_ids=["Worker candidate C"],
                zone_ids=["Equipment Work Area"],
                evidence_clip_ids=["CLIP-002"],
                confidence=0.89,
            ),
        ],
        evidence_clips=clips,
        findings=[
            DeviationFinding(
                finding_id="FINDING-01",
                jha_step_id="JHA-S1",
                status=FindingStatus.CONFIRMED_DEVIATION,
                title="Pedestrian route not maintained",
                planned_control="Keep the pedestrian walkway open",
                observed_work=(
                    "Material is visibly positioned across the marked pedestrian "
                    "route during the cited interval."
                ),
                evidence_clip_ids=["CLIP-001"],
                graph_path_node_ids=["JHA-S1", "CONTROL-01", "EVENT-01", "ZONE-01"],
                graph_path_relationships=["REQUIRES", "CONTRADICTED_BY", "OCCURRED_IN"],
                confidence=0.93,
            )
        ],
        corrective_actions=[
            CorrectiveAction(
                action_id="ACTION-01",
                finding_id="FINDING-01",
                action_type="immediate",
                description=(
                    "Remove the material from the pedestrian route and document a "
                    "field inspection before affected movement resumes."
                ),
                owner_role="Site Safety Manager",
                due_date="Before work resumes",
            ),
            CorrectiveAction(
                action_id="ACTION-02",
                finding_id="FINDING-01",
                action_type="preventive",
                description=(
                    "Require photographic closeout evidence showing the pedestrian "
                    "route after every material-staging operation."
                ),
                owner_role="General Superintendent",
                due_date="Within 7 days",
            ),
        ],
        limitations=[
            (
                "The supplied clips do not cover the entire work shift, so the "
                "duration of the route obstruction cannot be established."
            ),
            (
                "Entity labels are operational candidates and were not reconciled "
                "against the project personnel roster."
            ),
        ],
        knowledge_store_id="KS-01",
        jockey_session_id="JOCKEY-SESSION-01",
        graph_metrics={"nodes": 58, "relationships": 112},
        sponsor_trace=[
            {
                "sponsor": "TwelveLabs",
                "features": ["Pegasus segmentation", "Marengo search", "clip citations"],
            },
            {
                "sponsor": "Neo4j",
                "features": ["AuraDB", "Cypher graph diff"],
            },
        ],
        generated_at=datetime(2026, 7, 30, 21, 0, tzinfo=UTC),
    )


def test_report_service_generates_detailed_controlled_english_pdf(tmp_path):
    output = ReportService().generate(
        sample_investigation(),
        output_directory=tmp_path,
    )

    assert output.exists()
    assert output.stat().st_size > 25_000
    reader = PdfReader(output)
    assert len(reader.pages) >= 8
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    for section in [
        "Investigation scope and methodology",
        "Detailed multi-camera chronology",
        "Approved JHA compared with work observed",
        "Evidence-supported contributing conditions",
        "Unknowns, evidence gaps, and investigation limitations",
        "Action rationale and effectiveness verification",
        "Review and approval",
        "Appendix A. Evidence register",
        "Appendix C. Context-graph and system provenance",
    ]:
        assert section in text
    assert "CLIP-001" in text
    assert "DRAFT UNTIL HUMAN APPROVAL" in text
    assert "root cause" in text

