from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "services"
    / "neo4j_service.py"
)
SPEC = importlib.util.spec_from_file_location("sitetrace_neo4j_service", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

CypherQuery = MODULE.CypherQuery
InMemoryQueryExecutor = MODULE.InMemoryQueryExecutor
Neo4jService = MODULE.Neo4jService
SEGMENT_VECTOR_INDEX = MODULE.SEGMENT_VECTOR_INDEX


def run(coro):
    return asyncio.run(coro)


def sample_jha():
    return {
        "id": "JHA-MAT-014",
        "title": "Material movement",
        "version": "3",
        "source_uri": "s3://docs/JHA-MAT-014.pdf",
        "steps": [
            {
                "id": "JHA-MAT-014:S1",
                "step_order": 1,
                "title": "Prepare route",
                "required_controls": [
                    {
                        "id": "CTRL-SPOTTER",
                        "code": "SPOTTER",
                        "title": "Named spotter remains present",
                    }
                ],
                "prohibited_zones": [
                    {"id": "ZONE-EXIT-E2", "name": "Exit E2", "kind": "egress"}
                ],
            },
            {
                "id": "JHA-MAT-014:S2",
                "step_order": 2,
                "title": "Place material",
                "approved_zones": [
                    {
                        "id": "ZONE-STORAGE-B",
                        "name": "Storage Zone B",
                        "kind": "storage",
                    }
                ],
            },
        ],
    }


def test_plan_query_is_parameterized_and_idempotent():
    upsert, sequence = Neo4jService.build_plan_queries(
        case_id="CASE-1",
        case_title="Exit E2 near miss",
        jha=sample_jha(),
    )
    assert "MERGE (jha:JHA {id: $jha.id})" in upsert.text
    assert "JHA-MAT-014" not in upsert.text
    assert upsert.parameters["jha"]["id"] == "JHA-MAT-014"
    assert upsert.parameters["steps"][0]["required_controls"][0]["id"] == (
        "CTRL-SPOTTER"
    )
    assert sequence.parameters["pairs"] == [
        {
            "first_id": "JHA-MAT-014:S1",
            "second_id": "JHA-MAT-014:S2",
        }
    ]


def test_video_query_validates_marengo_dimensions_and_builds_citations():
    vector = [0.0] * 512
    upsert, sequence = Neo4jService.build_video_queries(
        case_id="CASE-1",
        video={
            "id": "VID-1",
            "camera_id": "CAM-02",
            "storage_uri": "s3://footage/cam-02.mp4",
        },
        segments=[
            {
                "id": "SEG-1",
                "start_sec": 148.1,
                "end_sec": 157.0,
                "embedding": vector,
                "summary": "Pallet placed next to Exit E2.",
            },
            {
                "id": "SEG-2",
                "start_sec": 157.0,
                "end_sec": 163.0,
                "summary": "Exit remains obstructed.",
            },
        ],
    )
    row = upsert.parameters["segments"][0]
    assert row["embedding_model"] == "marengo3.0"
    assert row["evidence"]["id"] == "SEG-1:clip"
    assert row["evidence"]["citation_label"] == "CAM-02 148.10-157.00s"
    assert sequence.parameters["pairs"][0]["first_id"] == "SEG-1"

    with pytest.raises(ValueError, match="512"):
        Neo4jService.build_video_queries(
            case_id="CASE-1",
            video={"id": "VID-1", "camera_id": "CAM-02"},
            segments=[{"id": "BAD", "embedding": [0.0] * 10}],
        )


def test_observation_query_preserves_observed_vs_inferred_and_coverage():
    events, sequence, coverage = Neo4jService.build_observation_queries(
        case_id="CASE-1",
        events=[
            {
                "id": "EV-1",
                "event_type": "SPOTTER_DEPARTURE",
                "observation_status": "OBSERVED",
                "evidence_clip_ids": ["CLIP-1"],
                "matched_step_ids": ["JHA-MAT-014:S1"],
                "precedes_event_ids": ["EV-2"],
                "entities": [
                    {
                        "id": "PERSON-CANDIDATE-1",
                        "name": "Worker in orange vest",
                        "kind": "person_candidate",
                    }
                ],
            }
        ],
        coverage=[
            {
                "id": "COVERAGE-1",
                "camera_id": "CAM-01",
                "sufficient": False,
                "reason": "Camera view ended before closeout.",
                "step_ids": ["JHA-MAT-014:S1"],
                "video_ids": ["VID-1"],
            }
        ],
    )
    assert events.parameters["events"][0]["observation_status"] == "OBSERVED"
    assert events.parameters["events"][0]["entities"][0]["normalized_name"] == (
        "worker in orange vest"
    )
    assert sequence.parameters["pairs"][0]["second_id"] == "EV-2"
    assert coverage.parameters["coverage"][0]["sufficient"] is False


def test_observed_events_and_sufficient_coverage_require_real_citations():
    with pytest.raises(ValueError, match="observed event"):
        Neo4jService.build_observation_queries(
            case_id="CASE-1",
            events=[
                {
                    "id": "EV-NO-CITE",
                    "event_type": "MATERIAL_MOVEMENT",
                    "observation_status": "OBSERVED",
                }
            ],
        )
    with pytest.raises(ValueError, match="sufficient coverage"):
        Neo4jService.build_observation_queries(
            case_id="CASE-1",
            events=[],
            coverage=[
                {
                    "id": "COVERAGE-NO-CITE",
                    "sufficient": True,
                    "step_ids": ["JHA-MAT-014:S1"],
                }
            ],
        )


def test_graph_diff_is_deterministic_and_never_equates_absence_to_failure():
    queries = Neo4jService.build_graph_diff_queries("CASE-1")
    assert [query.name for query in queries] == [
        "diff_required_controls",
        "diff_prohibited_zones",
        "diff_sequence_reversals",
    ]
    required = queries[0].text
    assert "REQUIRED_CONTROL_NOT_OBSERVED" in required
    assert "UNVERIFIABLE" in required
    assert "sufficient_coverage" in required
    assert "coverage_clip_ids" in required
    assert "NOT PERFORMED" not in required.upper()
    assert "MUST_AVOID" in queries[1].text
    assert "second_event.global_start_ms < first_event.global_start_ms" in (
        queries[2].text
    )
    assert all(query.readonly for query in queries)


def test_findings_require_evidence_except_unverifiable():
    with pytest.raises(ValueError, match="evidence_clip_ids"):
        Neo4jService.build_findings_query(
            case_id="CASE-1",
            findings=[
                {
                    "id": "DEV-1",
                    "status": "CONFIRMED_DEVIATION",
                    "finding_type": "PROHIBITED_ZONE",
                }
            ],
        )
    query = Neo4jService.build_findings_query(
        case_id="CASE-1",
        findings=[
            {
                "id": "DEV-2",
                "status": "UNVERIFIABLE",
                "finding_type": "REQUIRED_CONTROL",
            }
        ],
    )
    assert query.parameters["findings"][0]["evidence_clip_ids"] == []


def test_in_memory_executor_runs_complete_diff_without_credentials():
    executor = InMemoryQueryExecutor(
        {
            "diff_required_controls": [
                {
                    "finding_type": "REQUIRED_CONTROL",
                    "status": "UNVERIFIABLE",
                }
            ],
            "diff_prohibited_zones": [
                {
                    "finding_type": "PROHIBITED_ZONE",
                    "status": "CONFIRMED_DEVIATION",
                    "evidence_clip_ids": ["CLIP-2"],
                }
            ],
        }
    )
    service = Neo4jService(executor=executor)
    findings = run(service.graph_diff("CASE-1"))
    assert len(findings) == 2
    assert all(call.readonly for call in executor.calls)
    assert executor.calls[0].parameters == {"case_id": "CASE-1"}


def test_dry_run_records_queries_and_reports_non_secret_health():
    service = Neo4jService(dry_run=True)
    run(
        service.upsert_planned_graph(
            case_id="CASE-1",
            jha=sample_jha(),
        )
    )
    assert service.query_log
    assert all(isinstance(query, CypherQuery) for query in service.query_log)
    health = run(service.health())
    assert health["status"] == "dry_run"
    assert health["vector_index"] == SEGMENT_VECTOR_INDEX
    assert "password" not in health


def test_api_facing_aliases_are_orchestration_friendly():
    executor = InMemoryQueryExecutor(
        {
            "diff_required_controls": [{"status": "UNVERIFIABLE"}],
            "case_evidence_subgraph": [{"case_id": "CASE-1", "planned_paths": []}],
        }
    )
    service = Neo4jService(executor=executor)
    run(service.initialize_schema())
    run(
        service.upsert_case_graph(
            case_id="CASE-1",
            jha=sample_jha(),
            videos=[],
            events=[],
        )
    )
    assert run(service.run_graph_diff("CASE-1")) == [{"status": "UNVERIFIABLE"}]
    assert run(service.get_evidence_subgraph("CASE-1"))["case_id"] == "CASE-1"


def test_vector_search_prefers_modern_search_clause():
    executor = InMemoryQueryExecutor(
        {"semantic_evidence_search": [{"score": 0.93, "segment": {"id": "SEG-1"}}]}
    )
    service = Neo4jService(executor=executor)
    rows = run(
        service.semantic_evidence_search(
            case_id="CASE-1",
            embedding=[0.0] * 512,
            top_k=5,
        )
    )
    assert rows[0]["score"] == 0.93
    query = executor.calls[0]
    assert f"VECTOR INDEX {SEGMENT_VECTOR_INDEX}" in query.text
    assert "SEARCH segment IN" in query.text
    assert query.parameters["top_k"] == 5


def test_schema_contains_core_constraints_and_vector_index():
    schema_path = Path(__file__).resolve().parents[1] / "cypher" / "schema.cypher"
    schema = schema_path.read_text(encoding="utf-8")
    for label in (
        "InvestigationCase",
        "JHA",
        "JHAStep",
        "SafetyControl",
        "ObservedEvent",
        "EvidenceClip",
        "CoverageWindow",
        "Deviation",
    ):
        assert f"(n:{label})" in schema
    assert "CREATE VECTOR INDEX sitetrace_segment_marengo_v3" in schema
    assert "`vector.dimensions`: 512" in schema
