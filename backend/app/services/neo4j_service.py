"""Production-minded Neo4j AuraDB core for SiteTrace.

The service deliberately keeps all graph writes behind fixed, parameterized
Cypher templates.  Neo4j MCP is a read-only development aid and is not part of
the runtime write path.

The data model separates:

* work as planned (JHA steps, controls, permitted/prohibited zones);
* work as observed (time-coded events and cross-camera entities);
* evidence (source-preserving clips and camera coverage windows); and
* findings (deterministic graph differences with evidence links).

No database credentials are needed to import or test this module.  With
``dry_run=True`` it records the exact query specifications that would run.
An ``InMemoryQueryExecutor`` can supply deterministic query responses to tests
and local demos without pretending to be a graph database.
"""

from __future__ import annotations

import copy
import inspect
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence


MARENGO_3_DIMENSIONS = 512
SEGMENT_VECTOR_INDEX = "sitetrace_segment_marengo_v3"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class CypherQuery:
    """A named, parameterized query safe to record or execute."""

    name: str
    text: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    readonly: bool = False


class InMemoryQueryExecutor:
    """Small deterministic executor for credential-free tests and previews.

    Responses are keyed by ``CypherQuery.name``. Values may be a static list or
    a callable receiving the query. Every query is deep-copied into ``calls``.
    """

    def __init__(
        self,
        responses: Mapping[
            str,
            Sequence[Mapping[str, Any]]
            | Callable[[CypherQuery], Sequence[Mapping[str, Any]]],
        ]
        | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.calls: list[CypherQuery] = []

    async def __call__(self, query: CypherQuery) -> list[dict[str, Any]]:
        self.calls.append(
            CypherQuery(
                name=query.name,
                text=query.text,
                parameters=copy.deepcopy(dict(query.parameters)),
                readonly=query.readonly,
            )
        )
        response = self.responses.get(query.name, [])
        if callable(response):
            response = response(query)
        return copy.deepcopy([dict(row) for row in response])


QueryExecutor = Callable[
    [CypherQuery],
    Awaitable[Sequence[Mapping[str, Any]]] | Sequence[Mapping[str, Any]],
]


def _required(value: Mapping[str, Any], field_name: str, context: str) -> Any:
    result = value.get(field_name)
    if result is None or result == "":
        raise ValueError(f"{context}.{field_name} is required")
    return result


def _normalized_name(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _as_rows(values: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in (values or [])]


def _serialize_graph_value(value: Any) -> Any:
    """Convert Neo4j graph/native values into API-safe plain Python values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _serialize_graph_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_graph_value(item) for item in value]
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "nodes": [_serialize_graph_value(node) for node in value.nodes],
            "relationships": [
                _serialize_graph_value(relationship)
                for relationship in value.relationships
            ],
        }
    if hasattr(value, "labels") and hasattr(value, "element_id"):
        return {
            "element_id": value.element_id,
            "labels": sorted(value.labels),
            "properties": {
                str(key): _serialize_graph_value(item)
                for key, item in dict(value).items()
            },
        }
    if (
        hasattr(value, "type")
        and hasattr(value, "start_node")
        and hasattr(value, "end_node")
    ):
        return {
            "element_id": getattr(value, "element_id", None),
            "type": value.type,
            "start_node_element_id": value.start_node.element_id,
            "end_node_element_id": value.end_node.element_id,
            "properties": {
                str(key): _serialize_graph_value(item)
                for key, item in dict(value).items()
            },
        }
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class Neo4jService:
    """AuraDB service with deterministic plan-versus-observation operations."""

    PLAN_UPSERT = """
    MERGE (investigation:InvestigationCase {id: $case_id})
    ON CREATE SET investigation.created_at = datetime()
    SET investigation.title = $case_title,
        investigation.status = coalesce(
          $case_status, investigation.status, 'DRAFT'
        ),
        investigation.updated_at = datetime()
    MERGE (jha:JHA {id: $jha.id})
    ON CREATE SET jha.created_at = datetime()
    SET jha.case_id = $case_id,
        jha.title = $jha.title,
        jha.version = $jha.version,
        jha.source_uri = $jha.source_uri,
        jha.updated_at = datetime()
    MERGE (investigation)-[:GOVERNED_BY]->(jha)
    WITH jha
    UNWIND $steps AS row
      MERGE (step:JHAStep {id: row.id})
      ON CREATE SET step.created_at = datetime()
      SET step.jha_id = $jha.id,
          step.step_order = row.step_order,
          step.title = row.title,
          step.description = row.description,
          step.updated_at = datetime()
      MERGE (jha)-[:HAS_STEP]->(step)
      FOREACH (control IN row.required_controls |
        MERGE (c:SafetyControl {id: control.id})
        ON CREATE SET c.created_at = datetime()
        SET c.code = control.code,
            c.title = control.title,
            c.description = control.description,
            c.required_role = control.required_role,
            c.updated_at = datetime()
        MERGE (step)-[:REQUIRES]->(c))
      FOREACH (zone IN row.prohibited_zones |
        MERGE (z:Zone {id: zone.id})
        ON CREATE SET z.created_at = datetime()
        SET z.name = zone.name,
            z.normalized_name = zone.normalized_name,
            z.kind = zone.kind,
            z.updated_at = datetime()
        MERGE (step)-[:MUST_AVOID]->(z))
      FOREACH (zone IN row.approved_zones |
        MERGE (z:Zone {id: zone.id})
        ON CREATE SET z.created_at = datetime()
        SET z.name = zone.name,
            z.normalized_name = zone.normalized_name,
            z.kind = zone.kind,
            z.updated_at = datetime()
        MERGE (step)-[:APPROVED_DESTINATION]->(z))
    RETURN $jha.id AS jha_id, count(*) AS steps_upserted
    """

    PLAN_SEQUENCE_UPSERT = """
    UNWIND $pairs AS pair
      MATCH (first:JHAStep {id: pair.first_id})
      MATCH (second:JHAStep {id: pair.second_id})
      MERGE (first)-[:FOLLOWED_BY]->(second)
    RETURN count(*) AS relationships_upserted
    """

    VIDEO_SEGMENT_UPSERT = """
    MATCH (investigation:InvestigationCase {id: $case_id})
    MERGE (video:Video {id: $video.id})
    ON CREATE SET video.created_at = datetime()
    SET video.case_id = $case_id,
        video.camera_id = $video.camera_id,
        video.title = $video.title,
        video.recording_started_at = $video.recording_started_at,
        video.duration_sec = $video.duration_sec,
        video.storage_uri = $video.storage_uri,
        video.sha256 = $video.sha256,
        video.twelvelabs_asset_id = $video.twelvelabs_asset_id,
        video.updated_at = datetime()
    MERGE (investigation)-[:HAS_VIDEO]->(video)
    WITH video
    UNWIND $segments AS row
      MERGE (segment:Segment {id: row.id})
      ON CREATE SET segment.created_at = datetime()
      SET segment.case_id = $case_id,
          segment.video_id = $video.id,
          segment.segment_index = row.segment_index,
          segment.start_sec = row.start_sec,
          segment.end_sec = row.end_sec,
          segment.global_start_ms = row.global_start_ms,
          segment.global_end_ms = row.global_end_ms,
          segment.summary = row.summary,
          segment.transcript = row.transcript,
          segment.on_screen_text = row.on_screen_text,
          segment.embedding_model = row.embedding_model,
          segment.embedding = row.embedding,
          segment.updated_at = datetime()
      MERGE (video)-[:CONTAINS]->(segment)
      MERGE (clip:EvidenceClip {id: row.evidence.id})
      ON CREATE SET clip.created_at = datetime()
      SET clip.case_id = $case_id,
          clip.video_id = $video.id,
          clip.camera_id = $video.camera_id,
          clip.start_sec = row.evidence.start_sec,
          clip.end_sec = row.evidence.end_sec,
          clip.source_uri = row.evidence.source_uri,
          clip.item_reference = row.evidence.item_reference,
          clip.citation_label = row.evidence.citation_label,
          clip.updated_at = datetime()
      MERGE (segment)-[:HAS_EVIDENCE]->(clip)
    RETURN $video.id AS video_id, count(*) AS segments_upserted
    """

    SEGMENT_SEQUENCE_UPSERT = """
    UNWIND $pairs AS pair
      MATCH (first:Segment {id: pair.first_id})
      MATCH (second:Segment {id: pair.second_id})
      MERGE (first)-[:NEXT]->(second)
    RETURN count(*) AS relationships_upserted
    """

    EVENT_UPSERT = """
    MATCH (investigation:InvestigationCase {id: $case_id})
    UNWIND $events AS row
      MERGE (event:ObservedEvent {id: row.id})
      ON CREATE SET event.created_at = datetime()
      SET event.case_id = $case_id,
          event.event_type = row.event_type,
          event.summary = row.summary,
          event.observation_status = row.observation_status,
          event.confidence = row.confidence,
          event.global_start_ms = row.global_start_ms,
          event.global_end_ms = row.global_end_ms,
          event.updated_at = datetime()
      MERGE (investigation)-[:HAS_EVENT]->(event)
      FOREACH (clip_id IN row.evidence_clip_ids |
        MERGE (clip:EvidenceClip {id: clip_id})
        MERGE (event)-[:SUPPORTED_BY]->(clip))
      FOREACH (segment_id IN row.segment_ids |
        MERGE (segment:Segment {id: segment_id})
        MERGE (segment)-[:EVIDENCE_FOR]->(event))
      FOREACH (step_id IN row.matched_step_ids |
        MERGE (step:JHAStep {id: step_id})
        MERGE (event)-[:MATCHES_STEP]->(step))
      FOREACH (control_id IN row.satisfied_control_ids |
        MERGE (control:SafetyControl {id: control_id})
        MERGE (event)-[:SATISFIES]->(control))
      FOREACH (zone_id IN row.zone_ids |
        MERGE (zone:Zone {id: zone_id})
        MERGE (event)-[:OCCURRED_IN]->(zone))
      FOREACH (zone_id IN row.blocked_zone_ids |
        MERGE (zone:Zone {id: zone_id})
        MERGE (event)-[:BLOCKS]->(zone))
      FOREACH (entity IN row.entities |
        MERGE (node:Entity {id: entity.id})
        ON CREATE SET node.created_at = datetime()
        SET node.name = entity.name,
            node.normalized_name = entity.normalized_name,
            node.kind = entity.kind,
            node.description = entity.description,
            node.updated_at = datetime()
        MERGE (event)-[:INVOLVES]->(node))
    RETURN count(*) AS events_upserted
    """

    EVENT_SEQUENCE_UPSERT = """
    UNWIND $pairs AS pair
      MATCH (first:ObservedEvent {id: pair.first_id})
      MATCH (second:ObservedEvent {id: pair.second_id})
      MERGE (first)-[r:PRECEDES]->(second)
      SET r.basis = pair.basis,
          r.confidence = pair.confidence,
          r.evidence_clip_ids = pair.evidence_clip_ids,
          r.updated_at = datetime()
    RETURN count(*) AS relationships_upserted
    """

    COVERAGE_UPSERT = """
    MATCH (investigation:InvestigationCase {id: $case_id})
    UNWIND $coverage AS row
      MERGE (window:CoverageWindow {id: row.id})
      ON CREATE SET window.created_at = datetime()
      SET window.case_id = $case_id,
          window.camera_id = row.camera_id,
          window.global_start_ms = row.global_start_ms,
          window.global_end_ms = row.global_end_ms,
          window.sufficient = row.sufficient,
          window.reason = row.reason,
          window.assessed_by = row.assessed_by,
          window.updated_at = datetime()
      MERGE (investigation)-[:HAS_COVERAGE]->(window)
      FOREACH (step_id IN row.step_ids |
        MERGE (step:JHAStep {id: step_id})
        MERGE (window)-[:COVERS_STEP]->(step))
      FOREACH (video_id IN row.video_ids |
        MERGE (video:Video {id: video_id})
        MERGE (window)-[:FROM_VIDEO]->(video))
      FOREACH (clip_id IN row.evidence_clip_ids |
        MERGE (clip:EvidenceClip {id: clip_id})
        MERGE (window)-[:SUPPORTED_BY]->(clip))
    RETURN count(*) AS coverage_windows_upserted
    """

    REQUIRED_CONTROL_DIFF = """
    MATCH
      (investigation:InvestigationCase {id: $case_id})
        -[:GOVERNED_BY]->(jha:JHA)
    MATCH (jha)-[:HAS_STEP]->(step:JHAStep)-[:REQUIRES]->(control:SafetyControl)
    OPTIONAL MATCH (satisfying:ObservedEvent)-[:MATCHES_STEP]->(step)
    WHERE satisfying.case_id = $case_id
      AND satisfying.observation_status = 'OBSERVED'
      AND EXISTS { MATCH (satisfying)-[:SATISFIES]->(control) }
    WITH investigation, jha, step, control,
         count(DISTINCT satisfying) AS satisfied_count
    WHERE satisfied_count = 0
    OPTIONAL MATCH (coverage:CoverageWindow)-[:COVERS_STEP]->(step)
    WHERE coverage.case_id = $case_id
    OPTIONAL MATCH (coverage)-[:SUPPORTED_BY]->(coverage_clip:EvidenceClip)
    WITH investigation, jha, step, control,
         collect(DISTINCT coverage) AS coverage_windows,
         collect(DISTINCT coverage_clip.id) AS coverage_clip_ids
    WITH investigation, jha, step, control,
         [item IN coverage_windows | item.id] AS coverage_ids,
         coverage_clip_ids,
         any(item IN coverage_windows
             WHERE coalesce(item.sufficient, false)) AS sufficient_coverage
    RETURN
      'REQUIRED_CONTROL' AS finding_type,
      CASE WHEN sufficient_coverage
           THEN 'REQUIRED_CONTROL_NOT_OBSERVED'
           ELSE 'UNVERIFIABLE' END AS status,
      investigation.id AS case_id,
      jha.id AS jha_id,
      step.id AS step_id,
      step.step_order AS step_order,
      control.id AS control_id,
      control.title AS planned_control,
      null AS event_id,
      null AS zone_id,
      coverage_ids,
      CASE WHEN sufficient_coverage
           THEN coverage_clip_ids
           ELSE [] END AS evidence_clip_ids,
      CASE WHEN sufficient_coverage
           THEN 'Required control was not observed in sufficient camera coverage.'
           ELSE 'Available footage is insufficient to verify the required control.'
      END AS rationale
    ORDER BY step.step_order, control.id
    """

    PROHIBITED_ZONE_DIFF = """
    MATCH
      (investigation:InvestigationCase {id: $case_id})
        -[:GOVERNED_BY]->(jha:JHA)
    MATCH (jha)-[:HAS_STEP]->(step:JHAStep)-[:MUST_AVOID]->(zone:Zone)
    MATCH (event:ObservedEvent)-[:MATCHES_STEP]->(step)
    WHERE event.case_id = $case_id
      AND event.observation_status = 'OBSERVED'
      AND (
        EXISTS { MATCH (event)-[:OCCURRED_IN]->(zone) }
        OR EXISTS { MATCH (event)-[:BLOCKS]->(zone) }
      )
    OPTIONAL MATCH (event)-[:SUPPORTED_BY]->(clip:EvidenceClip)
    WITH investigation, jha, step, zone, event,
         collect(DISTINCT clip.id) AS evidence_clip_ids
    RETURN
      'PROHIBITED_ZONE' AS finding_type,
      'CONFIRMED_DEVIATION' AS status,
      investigation.id AS case_id,
      jha.id AS jha_id,
      step.id AS step_id,
      step.step_order AS step_order,
      null AS control_id,
      'Avoid ' + zone.name AS planned_control,
      event.id AS event_id,
      event.global_start_ms AS event_start_ms,
      zone.id AS zone_id,
      [] AS coverage_ids,
      evidence_clip_ids,
      'Observed event occurred in or blocked a prohibited zone.' AS rationale
    ORDER BY step_order, event_start_ms
    """

    SEQUENCE_REVERSAL_DIFF = """
    MATCH
      (investigation:InvestigationCase {id: $case_id})
        -[:GOVERNED_BY]->(jha:JHA)
    MATCH (jha)-[:HAS_STEP]->(first:JHAStep)-[:FOLLOWED_BY]->(second:JHAStep)
    MATCH (first_event:ObservedEvent)-[:MATCHES_STEP]->(first)
    MATCH (second_event:ObservedEvent)-[:MATCHES_STEP]->(second)
    WHERE first_event.case_id = $case_id
      AND second_event.case_id = $case_id
      AND first_event.observation_status = 'OBSERVED'
      AND second_event.observation_status = 'OBSERVED'
      AND second_event.global_start_ms < first_event.global_start_ms
    OPTIONAL MATCH (first_event)-[:SUPPORTED_BY]->(first_clip:EvidenceClip)
    OPTIONAL MATCH (second_event)-[:SUPPORTED_BY]->(second_clip:EvidenceClip)
    WITH investigation, jha, first, second, first_event, second_event,
         collect(DISTINCT first_clip.id) +
         collect(DISTINCT second_clip.id) AS clip_ids
    RETURN
      'SEQUENCE_REVERSAL' AS finding_type,
      'CONFIRMED_DEVIATION' AS status,
      investigation.id AS case_id,
      jha.id AS jha_id,
      first.id AS step_id,
      first.step_order AS step_order,
      null AS control_id,
      first.title + ' before ' + second.title AS planned_control,
      second_event.id AS event_id,
      null AS zone_id,
      [] AS coverage_ids,
      [item IN clip_ids WHERE item IS NOT NULL] AS evidence_clip_ids,
      'Observed event order contradicts the approved JHA sequence.' AS rationale
    ORDER BY first.step_order, second_event.global_start_ms
    """

    FINDING_UPSERT = """
    MATCH (investigation:InvestigationCase {id: $case_id})
    UNWIND $findings AS row
      MERGE (finding:Deviation {id: row.id})
      ON CREATE SET finding.created_at = datetime()
      SET finding.case_id = $case_id,
          finding.finding_type = row.finding_type,
          finding.status = row.status,
          finding.rationale = row.rationale,
          finding.confidence = row.confidence,
          finding.human_confirmed = row.human_confirmed,
          finding.updated_at = datetime()
      MERGE (investigation)-[:HAS_FINDING]->(finding)
      FOREACH (step_id IN row.step_ids |
        MERGE (step:JHAStep {id: step_id})
        MERGE (finding)-[:CONCERNS_STEP]->(step))
      FOREACH (control_id IN row.control_ids |
        MERGE (control:SafetyControl {id: control_id})
        MERGE (finding)-[:CONTRADICTS]->(control))
      FOREACH (event_id IN row.event_ids |
        MERGE (event:ObservedEvent {id: event_id})
        MERGE (finding)-[:SUPPORTED_BY_EVENT]->(event))
      FOREACH (zone_id IN row.zone_ids |
        MERGE (zone:Zone {id: zone_id})
        MERGE (finding)-[:CONCERNS_ZONE]->(zone))
      FOREACH (clip_id IN row.evidence_clip_ids |
        MERGE (clip:EvidenceClip {id: clip_id})
        MERGE (finding)-[:SUPPORTED_BY]->(clip))
    RETURN count(*) AS findings_upserted
    """

    EVIDENCE_BUNDLE = """
    MATCH (finding:Deviation {id: $deviation_id})
    OPTIONAL MATCH (finding)-[:CONCERNS_STEP]->(step:JHAStep)
    OPTIONAL MATCH (step)-[:REQUIRES]->(control:SafetyControl)
    OPTIONAL MATCH (finding)-[:SUPPORTED_BY_EVENT]->(event:ObservedEvent)
    OPTIONAL MATCH (finding)-[:CONCERNS_ZONE]->(zone:Zone)
    OPTIONAL MATCH (finding)-[:SUPPORTED_BY]->(direct_clip:EvidenceClip)
    OPTIONAL MATCH (event)-[:SUPPORTED_BY]->(event_clip:EvidenceClip)
    WITH finding,
         collect(DISTINCT step {
           .id, .step_order, .title, .description
         }) AS steps,
         collect(DISTINCT control {
           .id, .code, .title, .description
         }) AS controls,
         collect(DISTINCT event {
           .id, .event_type, .summary, .global_start_ms,
           .global_end_ms, .confidence, .observation_status
         }) AS events,
         collect(DISTINCT zone {
           .id, .name, .kind
         }) AS zones,
         collect(DISTINCT direct_clip) +
         collect(DISTINCT event_clip) AS raw_clips
    WITH finding, steps, controls, events, zones,
         [clip IN raw_clips WHERE clip IS NOT NULL] AS clips
    RETURN finding {
             .id, .finding_type, .status, .rationale,
             .confidence, .human_confirmed
           } AS finding,
           steps,
           controls,
           events,
           zones,
           [clip IN clips | clip {
             .id, .video_id, .camera_id, .start_sec, .end_sec,
             .source_uri, .item_reference, .citation_label
           }] AS evidence_clips
    """

    EVIDENCE_PATH = """
    MATCH (finding:Deviation {id: $deviation_id})
    OPTIONAL MATCH path_a =
      (finding)-[:CONCERNS_STEP|CONTRADICTS|CONCERNS_ZONE]->(planned)
    OPTIONAL MATCH path_b =
      (finding)-[:SUPPORTED_BY_EVENT]->(event:ObservedEvent)
        -[:SUPPORTED_BY]->(clip:EvidenceClip)
    RETURN finding.id AS deviation_id,
           [path IN collect(DISTINCT path_a) WHERE path IS NOT NULL] AS planned_paths,
           [path IN collect(DISTINCT path_b) WHERE path IS NOT NULL] AS evidence_paths
    """

    CASE_EVIDENCE_SUBGRAPH = """
    MATCH (investigation:InvestigationCase {id: $case_id})
    OPTIONAL MATCH planned_path =
      (investigation)-[:GOVERNED_BY]->(:JHA)-[:HAS_STEP]->(:JHAStep)
        -[:REQUIRES|MUST_AVOID|APPROVED_DESTINATION]->()
    OPTIONAL MATCH event_path =
      (investigation)-[:HAS_EVENT]->(:ObservedEvent)
        -[:MATCHES_STEP|SATISFIES|OCCURRED_IN|BLOCKS|INVOLVES|SUPPORTED_BY]->()
    OPTIONAL MATCH finding_path =
      (investigation)-[:HAS_FINDING]->(:Deviation)
        -[:CONCERNS_STEP|CONTRADICTS|SUPPORTED_BY_EVENT|CONCERNS_ZONE|SUPPORTED_BY]->()
    RETURN investigation.id AS case_id,
           [path IN collect(DISTINCT planned_path)
            WHERE path IS NOT NULL] AS planned_paths,
           [path IN collect(DISTINCT event_path)
            WHERE path IS NOT NULL] AS observed_paths,
           [path IN collect(DISTINCT finding_path)
            WHERE path IS NOT NULL] AS finding_paths
    """

    SCHEMA_LABELS = """
    CALL db.labels() YIELD label
    RETURN collect(label) AS labels
    """

    SCHEMA_RELATIONSHIPS = """
    CALL db.relationshipTypes() YIELD relationshipType
    RETURN collect(relationshipType) AS relationship_types
    """

    SCHEMA_CONSTRAINTS = """
    SHOW CONSTRAINTS
    YIELD name, type, entityType, labelsOrTypes, properties
    RETURN name, type, entityType, labelsOrTypes, properties
    ORDER BY name
    """

    SCHEMA_INDEXES = """
    SHOW INDEXES
    YIELD name, type, entityType, labelsOrTypes, properties, state,
          populationPercent, indexProvider
    RETURN name, type, entityType, labelsOrTypes, properties, state,
           populationPercent, indexProvider
    ORDER BY name
    """

    VECTOR_SEARCH = f"""
    MATCH (segment:Segment)
      SEARCH segment IN (
        VECTOR INDEX {SEGMENT_VECTOR_INDEX}
        FOR $embedding
        LIMIT $top_k
      ) SCORE AS score
    WHERE segment.case_id = $case_id
    OPTIONAL MATCH (video:Video)-[:CONTAINS]->(segment)
    OPTIONAL MATCH (segment)-[:EVIDENCE_FOR]->(event:ObservedEvent)
    OPTIONAL MATCH (segment)-[:HAS_EVIDENCE]->(clip:EvidenceClip)
    OPTIONAL MATCH (event)-[:INVOLVES]->(entity:Entity)
    OPTIONAL MATCH (event)-[:OCCURRED_IN|BLOCKS]->(zone:Zone)
    RETURN segment {{
             .id, .video_id, .start_sec, .end_sec, .summary,
             .transcript, .on_screen_text
           }} AS segment,
           score,
           video {{ .id, .camera_id, .title }} AS video,
           collect(DISTINCT event {{
             .id, .event_type, .summary, .global_start_ms
           }}) AS events,
           collect(DISTINCT entity {{ .id, .name, .kind }}) AS entities,
           collect(DISTINCT zone {{ .id, .name, .kind }}) AS zones,
           collect(DISTINCT clip {{
             .id, .camera_id, .start_sec, .end_sec,
             .source_uri, .item_reference
           }}) AS evidence_clips
    ORDER BY score DESC
    """

    LEGACY_VECTOR_SEARCH = """
    CALL db.index.vector.queryNodes($index_name, $top_k, $embedding)
    YIELD node AS segment, score
    WHERE segment.case_id = $case_id
    OPTIONAL MATCH (video:Video)-[:CONTAINS]->(segment)
    OPTIONAL MATCH (segment)-[:EVIDENCE_FOR]->(event:ObservedEvent)
    OPTIONAL MATCH (segment)-[:HAS_EVIDENCE]->(clip:EvidenceClip)
    OPTIONAL MATCH (event)-[:INVOLVES]->(entity:Entity)
    OPTIONAL MATCH (event)-[:OCCURRED_IN|BLOCKS]->(zone:Zone)
    RETURN segment {
             .id, .video_id, .start_sec, .end_sec, .summary,
             .transcript, .on_screen_text
           } AS segment,
           score,
           video { .id, .camera_id, .title } AS video,
           collect(DISTINCT event {
             .id, .event_type, .summary, .global_start_ms
           }) AS events,
           collect(DISTINCT entity { .id, .name, .kind }) AS entities,
           collect(DISTINCT zone { .id, .name, .kind }) AS zones,
           collect(DISTINCT clip {
             .id, .camera_id, .start_sec, .end_sec,
             .source_uri, .item_reference
           }) AS evidence_clips
    ORDER BY score DESC
    """

    def __init__(
        self,
        *,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
        driver: Any | None = None,
        executor: QueryExecutor | None = None,
        dry_run: bool | None = None,
        vector_dimensions: int = MARENGO_3_DIMENSIONS,
        allow_legacy_vector_fallback: bool = True,
    ) -> None:
        self.uri = uri or os.getenv("NEO4J_URI", "")
        self.username = username or os.getenv("NEO4J_USERNAME", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "")
        self.database = database or os.getenv("NEO4J_DATABASE", "neo4j")
        self._driver = driver
        self._owns_driver = driver is None
        self._executor = executor
        if dry_run is None:
            configured = os.getenv("NEO4J_DRY_RUN", "").casefold() in {
                "1",
                "true",
                "yes",
            }
            dry_run = configured or (
                executor is None
                and driver is None
                and not (self.uri and self.username and self.password)
            )
        self.dry_run = bool(dry_run)
        self.query_log: list[CypherQuery] = []
        self.vector_dimensions = int(vector_dimensions)
        if not 1 <= self.vector_dimensions <= 4096:
            raise ValueError("vector_dimensions must be between 1 and 4096")
        self.allow_legacy_vector_fallback = allow_legacy_vector_fallback

    @property
    def configured(self) -> bool:
        """Whether runtime Aura credentials (or an injected driver) exist."""

        return self._driver is not None or bool(
            self.uri and self.username and self.password
        )

    async def open(self) -> None:
        """Open and verify the official Neo4j async driver lazily."""

        if self.dry_run or self._executor is not None or self._driver is not None:
            return
        if not self.configured:
            raise RuntimeError(
                "Neo4j is not configured. Set NEO4J_URI, NEO4J_USERNAME, "
                "and NEO4J_PASSWORD, or enable dry_run."
            )
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(
                "Install the official Neo4j Python driver: pip install neo4j"
            ) from exc
        self._driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.username, self.password),
            max_connection_lifetime=55 * 60,
            connection_timeout=10,
        )
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        if self._driver is not None and self._owns_driver:
            await self._driver.close()
            self._driver = None

    async def __aenter__(self) -> "Neo4jService":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _execute(self, query: CypherQuery) -> list[dict[str, Any]]:
        """Execute a known query or record it in safe dry-run mode."""

        self.query_log.append(query)
        if self._executor is not None:
            value = self._executor(query)
            if inspect.isawaitable(value):
                value = await value
            return [dict(record) for record in value]
        if self.dry_run:
            return []
        await self.open()
        if self._driver is None:  # defensive: open() must set it
            raise RuntimeError("Neo4j driver is unavailable")
        records, _summary, _keys = await self._driver.execute_query(
            query.text,
            parameters_=dict(query.parameters),
            database_=self.database,
            routing_="r" if query.readonly else "w",
        )
        return [
            _serialize_graph_value(
                record.data() if hasattr(record, "data") else dict(record)
            )
            for record in records
        ]

    @staticmethod
    def _write(name: str, text: str, parameters: Mapping[str, Any]) -> CypherQuery:
        return CypherQuery(name=name, text=text, parameters=parameters, readonly=False)

    @staticmethod
    def _read(name: str, text: str, parameters: Mapping[str, Any]) -> CypherQuery:
        return CypherQuery(name=name, text=text, parameters=parameters, readonly=True)

    @staticmethod
    def _schema_statements(path: Path | None = None) -> list[str]:
        schema_path = path or Path(__file__).resolve().parents[2] / "cypher" / "schema.cypher"
        body = schema_path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("//")
        )
        return [statement.strip() for statement in code.split(";") if statement.strip()]

    async def ensure_schema(self, path: Path | None = None) -> list[dict[str, Any]]:
        """Apply idempotent constraints and indexes from ``schema.cypher``."""

        results: list[dict[str, Any]] = []
        for index, statement in enumerate(self._schema_statements(path), start=1):
            rows = await self._execute(
                self._write(
                    f"schema_{index}",
                    statement,
                    {},
                )
            )
            results.extend(rows)
        return results

    async def initialize_schema(
        self, path: Path | None = None
    ) -> list[dict[str, Any]]:
        """API-facing alias for applying idempotent constraints and indexes."""

        return await self.ensure_schema(path)

    @classmethod
    def build_plan_queries(
        cls,
        *,
        case_id: str,
        jha: Mapping[str, Any],
        case_title: str = "",
        case_status: str = "DRAFT",
    ) -> tuple[CypherQuery, CypherQuery]:
        """Build idempotent planned-work graph upserts."""

        _required({"case_id": case_id}, "case_id", "case")
        jha_id = str(_required(jha, "id", "jha"))
        raw_steps = _as_rows(jha.get("steps"))
        if not raw_steps:
            raise ValueError("jha.steps must contain at least one step")
        steps: list[dict[str, Any]] = []
        for offset, source in enumerate(raw_steps, start=1):
            step_id = str(_required(source, "id", f"jha.steps[{offset - 1}]"))
            controls: list[dict[str, Any]] = []
            for control in _as_rows(source.get("required_controls")):
                control_id = str(_required(control, "id", f"step {step_id} control"))
                controls.append(
                    {
                        "id": control_id,
                        "code": str(control.get("code", "")),
                        "title": str(control.get("title", "")),
                        "description": str(control.get("description", "")),
                        "required_role": str(control.get("required_role", "")),
                    }
                )

            def zones(key: str) -> list[dict[str, Any]]:
                output: list[dict[str, Any]] = []
                for zone in _as_rows(source.get(key)):
                    zone_id = str(_required(zone, "id", f"step {step_id} zone"))
                    name = str(zone.get("name", zone_id))
                    output.append(
                        {
                            "id": zone_id,
                            "name": name,
                            "normalized_name": _normalized_name(name),
                            "kind": str(zone.get("kind", "work_zone")),
                        }
                    )
                return output

            steps.append(
                {
                    "id": step_id,
                    "step_order": int(source.get("step_order", offset)),
                    "title": str(source.get("title", "")),
                    "description": str(source.get("description", "")),
                    "required_controls": controls,
                    "prohibited_zones": zones("prohibited_zones"),
                    "approved_zones": zones("approved_zones"),
                }
            )
        steps.sort(key=lambda item: (item["step_order"], item["id"]))
        pairs = [
            {"first_id": left["id"], "second_id": right["id"]}
            for left, right in zip(steps, steps[1:])
        ]
        jha_row = {
            "id": jha_id,
            "title": str(jha.get("title", "")),
            "version": str(jha.get("version", "")),
            "source_uri": str(jha.get("source_uri", "")),
        }
        return (
            cls._write(
                "upsert_planned_graph",
                cls.PLAN_UPSERT,
                {
                    "case_id": case_id,
                    "case_title": case_title,
                    "case_status": case_status,
                    "jha": jha_row,
                    "steps": steps,
                },
            ),
            cls._write(
                "upsert_planned_sequence",
                cls.PLAN_SEQUENCE_UPSERT,
                {"pairs": pairs},
            ),
        )

    async def upsert_planned_graph(
        self,
        *,
        case_id: str,
        jha: Mapping[str, Any],
        case_title: str = "",
        case_status: str = "DRAFT",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query in self.build_plan_queries(
            case_id=case_id,
            jha=jha,
            case_title=case_title,
            case_status=case_status,
        ):
            rows.extend(await self._execute(query))
        return rows

    @classmethod
    def build_video_queries(
        cls,
        *,
        case_id: str,
        video: Mapping[str, Any],
        segments: Iterable[Mapping[str, Any]],
    ) -> tuple[CypherQuery, CypherQuery]:
        """Build idempotent Video/Segment/EvidenceClip upserts."""

        video_id = str(_required(video, "id", "video"))
        video_row = {
            "id": video_id,
            "camera_id": str(_required(video, "camera_id", "video")),
            "title": str(video.get("title", "")),
            "recording_started_at": video.get("recording_started_at"),
            "duration_sec": video.get("duration_sec"),
            "storage_uri": str(video.get("storage_uri", "")),
            "sha256": str(video.get("sha256", "")),
            "twelvelabs_asset_id": str(video.get("twelvelabs_asset_id", "")),
        }
        rows: list[dict[str, Any]] = []
        for offset, source in enumerate(_as_rows(segments)):
            segment_id = str(_required(source, "id", f"segments[{offset}]"))
            embedding = source.get("embedding")
            if embedding is not None:
                embedding = [float(value) for value in embedding]
                if len(embedding) != MARENGO_3_DIMENSIONS:
                    raise ValueError(
                        f"segment {segment_id} embedding must contain "
                        f"{MARENGO_3_DIMENSIONS} values for Marengo 3.0"
                    )
            evidence = dict(source.get("evidence") or {})
            clip_id = str(evidence.get("id") or f"{segment_id}:clip")
            start_sec = float(source.get("start_sec", 0.0))
            end_sec = float(source.get("end_sec", start_sec))
            if end_sec < start_sec:
                raise ValueError(f"segment {segment_id} ends before it starts")
            rows.append(
                {
                    "id": segment_id,
                    "segment_index": int(source.get("segment_index", offset)),
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "global_start_ms": source.get("global_start_ms"),
                    "global_end_ms": source.get("global_end_ms"),
                    "summary": str(source.get("summary", "")),
                    "transcript": str(source.get("transcript", "")),
                    "on_screen_text": str(source.get("on_screen_text", "")),
                    "embedding_model": (
                        str(source.get("embedding_model", "marengo3.0"))
                        if embedding is not None
                        else ""
                    ),
                    "embedding": embedding,
                    "evidence": {
                        "id": clip_id,
                        "start_sec": float(evidence.get("start_sec", start_sec)),
                        "end_sec": float(evidence.get("end_sec", end_sec)),
                        "source_uri": str(
                            evidence.get("source_uri", video_row["storage_uri"])
                        ),
                        "item_reference": str(evidence.get("item_reference", "")),
                        "citation_label": str(
                            evidence.get(
                                "citation_label",
                                f"{video_row['camera_id']} "
                                f"{start_sec:.2f}-{end_sec:.2f}s",
                            )
                        ),
                    },
                }
            )
        rows.sort(key=lambda row: (row["segment_index"], row["id"]))
        pairs = [
            {"first_id": left["id"], "second_id": right["id"]}
            for left, right in zip(rows, rows[1:])
        ]
        return (
            cls._write(
                "upsert_video_segments",
                cls.VIDEO_SEGMENT_UPSERT,
                {
                    "case_id": case_id,
                    "video": video_row,
                    "segments": rows,
                },
            ),
            cls._write(
                "upsert_segment_sequence",
                cls.SEGMENT_SEQUENCE_UPSERT,
                {"pairs": pairs},
            ),
        )

    async def upsert_video_segments(
        self,
        *,
        case_id: str,
        video: Mapping[str, Any],
        segments: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query in self.build_video_queries(
            case_id=case_id,
            video=video,
            segments=segments,
        ):
            rows.extend(await self._execute(query))
        return rows

    @classmethod
    def build_observation_queries(
        cls,
        *,
        case_id: str,
        events: Iterable[Mapping[str, Any]],
        coverage: Iterable[Mapping[str, Any]] | None = None,
    ) -> tuple[CypherQuery, CypherQuery, CypherQuery]:
        """Build idempotent event/entity/evidence and coverage upserts."""

        event_rows: list[dict[str, Any]] = []
        explicit_pairs: list[dict[str, Any]] = []
        for source in _as_rows(events):
            event_id = str(_required(source, "id", "event"))
            status = str(source.get("observation_status", "OBSERVED")).upper()
            if status not in {"OBSERVED", "INFERRED"}:
                raise ValueError(
                    f"event {event_id} observation_status must be OBSERVED or INFERRED"
                )
            evidence_clip_ids = list(source.get("evidence_clip_ids") or [])
            if status == "OBSERVED" and not evidence_clip_ids:
                raise ValueError(
                    f"observed event {event_id} requires evidence_clip_ids"
                )
            entity_rows: list[dict[str, Any]] = []
            for entity in _as_rows(source.get("entities")):
                entity_id = str(_required(entity, "id", f"event {event_id} entity"))
                name = str(entity.get("name", entity_id))
                entity_rows.append(
                    {
                        "id": entity_id,
                        "name": name,
                        "normalized_name": _normalized_name(name),
                        "kind": str(entity.get("kind", "unknown")),
                        "description": str(entity.get("description", "")),
                    }
                )
            event_rows.append(
                {
                    "id": event_id,
                    "event_type": str(_required(source, "event_type", "event")),
                    "summary": str(source.get("summary", "")),
                    "observation_status": status,
                    "confidence": float(source.get("confidence", 0.0)),
                    "global_start_ms": source.get("global_start_ms"),
                    "global_end_ms": source.get("global_end_ms"),
                    "evidence_clip_ids": evidence_clip_ids,
                    "segment_ids": list(source.get("segment_ids") or []),
                    "matched_step_ids": list(source.get("matched_step_ids") or []),
                    "satisfied_control_ids": list(
                        source.get("satisfied_control_ids") or []
                    ),
                    "zone_ids": list(source.get("zone_ids") or []),
                    "blocked_zone_ids": list(source.get("blocked_zone_ids") or []),
                    "entities": entity_rows,
                }
            )
            for target in source.get("precedes_event_ids") or []:
                explicit_pairs.append(
                    {
                        "first_id": event_id,
                        "second_id": str(target),
                        "basis": str(source.get("sequence_basis", "timestamp")),
                        "confidence": float(source.get("sequence_confidence", 1.0)),
                        "evidence_clip_ids": list(
                            source.get("evidence_clip_ids") or []
                        ),
                    }
                )
        coverage_rows: list[dict[str, Any]] = []
        for source in _as_rows(coverage):
            sufficient = bool(source.get("sufficient", False))
            evidence_clip_ids = list(source.get("evidence_clip_ids") or [])
            if sufficient and not evidence_clip_ids:
                raise ValueError(
                    "sufficient coverage requires evidence_clip_ids so a "
                    "not-observed finding remains auditable"
                )
            coverage_rows.append(
                {
                    "id": str(_required(source, "id", "coverage")),
                    "camera_id": str(source.get("camera_id", "")),
                    "global_start_ms": source.get("global_start_ms"),
                    "global_end_ms": source.get("global_end_ms"),
                    "sufficient": sufficient,
                    "reason": str(source.get("reason", "")),
                    "assessed_by": str(
                        source.get("assessed_by", "coverage-validator")
                    ),
                    "step_ids": list(source.get("step_ids") or []),
                    "video_ids": list(source.get("video_ids") or []),
                    "evidence_clip_ids": evidence_clip_ids,
                }
            )
        return (
            cls._write(
                "upsert_observed_events",
                cls.EVENT_UPSERT,
                {"case_id": case_id, "events": event_rows},
            ),
            cls._write(
                "upsert_event_sequence",
                cls.EVENT_SEQUENCE_UPSERT,
                {"pairs": explicit_pairs},
            ),
            cls._write(
                "upsert_coverage_windows",
                cls.COVERAGE_UPSERT,
                {"case_id": case_id, "coverage": coverage_rows},
            ),
        )

    async def upsert_observed_graph(
        self,
        *,
        case_id: str,
        events: Iterable[Mapping[str, Any]],
        coverage: Iterable[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for query in self.build_observation_queries(
            case_id=case_id,
            events=events,
            coverage=coverage,
        ):
            rows.extend(await self._execute(query))
        return rows

    async def upsert_case_graph(
        self,
        *,
        case_id: str,
        jha: Mapping[str, Any],
        case_title: str = "",
        case_status: str = "DRAFT",
        videos: Iterable[Mapping[str, Any]] | None = None,
        events: Iterable[Mapping[str, Any]] | None = None,
        coverage: Iterable[Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Upsert a complete case graph through the fixed runtime templates.

        Each entry in ``videos`` must be ``{"video": {...}, "segments": [...]}``.
        The method is intentionally orchestration-friendly for the FastAPI and
        Strands workflow layers while retaining the smaller specialized methods.
        """

        results = await self.upsert_planned_graph(
            case_id=case_id,
            jha=jha,
            case_title=case_title,
            case_status=case_status,
        )
        for bundle in _as_rows(videos):
            video = bundle.get("video")
            if not isinstance(video, Mapping):
                raise ValueError("each videos entry requires a video mapping")
            results.extend(
                await self.upsert_video_segments(
                    case_id=case_id,
                    video=video,
                    segments=bundle.get("segments") or [],
                )
            )
        if events or coverage:
            results.extend(
                await self.upsert_observed_graph(
                    case_id=case_id,
                    events=events or [],
                    coverage=coverage,
                )
            )
        return results

    @classmethod
    def build_graph_diff_queries(cls, case_id: str) -> tuple[CypherQuery, ...]:
        """Return deterministic read-only plan-versus-observation queries."""

        parameters = {"case_id": case_id}
        return (
            cls._read(
                "diff_required_controls",
                cls.REQUIRED_CONTROL_DIFF,
                parameters,
            ),
            cls._read(
                "diff_prohibited_zones",
                cls.PROHIBITED_ZONE_DIFF,
                parameters,
            ),
            cls._read(
                "diff_sequence_reversals",
                cls.SEQUENCE_REVERSAL_DIFF,
                parameters,
            ),
        )

    async def graph_diff(self, case_id: str) -> list[dict[str, Any]]:
        """Compute deterministic differences; never infer root cause."""

        findings: list[dict[str, Any]] = []
        for query in self.build_graph_diff_queries(case_id):
            findings.extend(await self._execute(query))
        return findings

    async def run_graph_diff(self, case_id: str) -> list[dict[str, Any]]:
        """API-facing alias for the deterministic Cypher graph diff."""

        return await self.graph_diff(case_id)

    @classmethod
    def build_findings_query(
        cls,
        *,
        case_id: str,
        findings: Iterable[Mapping[str, Any]],
    ) -> CypherQuery:
        rows: list[dict[str, Any]] = []
        for source in _as_rows(findings):
            finding_id = str(_required(source, "id", "finding"))
            evidence_ids = list(source.get("evidence_clip_ids") or [])
            status = str(_required(source, "status", f"finding {finding_id}"))
            if status != "UNVERIFIABLE" and not evidence_ids:
                raise ValueError(
                    f"finding {finding_id} must include evidence_clip_ids; "
                    "use UNVERIFIABLE when evidence is insufficient"
                )
            rows.append(
                {
                    "id": finding_id,
                    "finding_type": str(source.get("finding_type", "")),
                    "status": status,
                    "rationale": str(source.get("rationale", "")),
                    "confidence": float(source.get("confidence", 0.0)),
                    "human_confirmed": bool(
                        source.get("human_confirmed", False)
                    ),
                    "step_ids": list(source.get("step_ids") or []),
                    "control_ids": list(source.get("control_ids") or []),
                    "event_ids": list(source.get("event_ids") or []),
                    "zone_ids": list(source.get("zone_ids") or []),
                    "evidence_clip_ids": evidence_ids,
                }
            )
        return cls._write(
            "upsert_findings",
            cls.FINDING_UPSERT,
            {"case_id": case_id, "findings": rows},
        )

    async def upsert_findings(
        self,
        *,
        case_id: str,
        findings: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        return await self._execute(
            self.build_findings_query(case_id=case_id, findings=findings)
        )

    async def evidence_bundle(self, deviation_id: str) -> dict[str, Any]:
        rows = await self._execute(
            self._read(
                "evidence_bundle",
                self.EVIDENCE_BUNDLE,
                {"deviation_id": deviation_id},
            )
        )
        return rows[0] if rows else {}

    async def evidence_paths(self, deviation_id: str) -> dict[str, Any]:
        rows = await self._execute(
            self._read(
                "evidence_paths",
                self.EVIDENCE_PATH,
                {"deviation_id": deviation_id},
            )
        )
        return rows[0] if rows else {
            "deviation_id": deviation_id,
            "planned_paths": [],
            "evidence_paths": [],
        }

    async def get_evidence_subgraph(self, case_id: str) -> dict[str, Any]:
        """Return the bounded plan/observation/finding paths for one case."""

        rows = await self._execute(
            self._read(
                "case_evidence_subgraph",
                self.CASE_EVIDENCE_SUBGRAPH,
                {"case_id": case_id},
            )
        )
        return rows[0] if rows else {
            "case_id": case_id,
            "planned_paths": [],
            "observed_paths": [],
            "finding_paths": [],
        }

    async def case_metrics(self, case_id: str) -> dict[str, int]:
        """Count the bounded context graph reachable from one investigation."""

        rows = await self._execute(
            self._read(
                "case_graph_metrics",
                """
                MATCH (investigation:InvestigationCase {id: $case_id})
                CALL {
                  WITH investigation
                  OPTIONAL MATCH (investigation)-[*0..5]->(node)
                  RETURN count(DISTINCT node) AS node_count
                }
                CALL {
                  WITH investigation
                  OPTIONAL MATCH path=(investigation)-[*1..5]->()
                  UNWIND relationships(path) AS relationship
                  RETURN count(DISTINCT relationship) AS relationship_count
                }
                MATCH (investigation)-[:HAS_VIDEO]->(video:Video)
                OPTIONAL MATCH (video)-[:CONTAINS]->(segment:Segment)
                OPTIONAL MATCH (investigation)-[:HAS_FINDING]->(finding:Deviation)
                RETURN node_count,
                       relationship_count,
                       count(DISTINCT video) AS video_count,
                       count(DISTINCT segment) AS segment_count,
                       count(DISTINCT finding) AS finding_count
                """,
                {"case_id": case_id},
            )
        )
        if not rows:
            return {
                "nodes": 0,
                "relationships": 0,
                "videos": 0,
                "segments": 0,
                "findings": 0,
            }
        row = rows[0]
        return {
            "nodes": int(row.get("node_count", 0)),
            "relationships": int(row.get("relationship_count", 0)),
            "videos": int(row.get("video_count", 0)),
            "segments": int(row.get("segment_count", 0)),
            "findings": int(row.get("finding_count", 0)),
        }

    async def semantic_evidence_search(
        self,
        *,
        case_id: str,
        embedding: Sequence[float],
        top_k: int = 8,
    ) -> list[dict[str, Any]]:
        vector = [float(value) for value in embedding]
        if len(vector) != self.vector_dimensions:
            raise ValueError(
                f"query embedding has {len(vector)} dimensions; expected "
                f"{self.vector_dimensions}"
            )
        top_k = max(1, min(int(top_k), 50))
        modern = self._read(
            "semantic_evidence_search",
            self.VECTOR_SEARCH,
            {
                "case_id": case_id,
                "embedding": vector,
                "top_k": top_k,
            },
        )
        try:
            return await self._execute(modern)
        except Exception:
            if not self.allow_legacy_vector_fallback:
                raise
        # Compatibility fallback for Neo4j 5.26 Aura instances. The preferred
        # Neo4j 2026 path above uses SEARCH; queryNodes is deprecated in 2026.04.
        return await self._execute(
            self._read(
                "semantic_evidence_search_legacy",
                self.LEGACY_VECTOR_SEARCH,
                {
                    "case_id": case_id,
                    "embedding": vector,
                    "top_k": top_k,
                    "index_name": SEGMENT_VECTOR_INDEX,
                },
            )
        )

    async def health(self) -> dict[str, Any]:
        """Return connectivity and vector-index readiness without secrets."""

        if self.dry_run:
            return {
                "status": "dry_run",
                "configured": self.configured,
                "database": self.database,
                "vector_index": SEGMENT_VECTOR_INDEX,
                "vector_dimensions": self.vector_dimensions,
            }
        try:
            await self.open()
            rows = await self._execute(
                self._read(
                    "health",
                    """
                    SHOW VECTOR INDEXES
                    YIELD name, state, populationPercent
                    WHERE name = $index_name
                    RETURN name, state, populationPercent
                    """,
                    {"index_name": SEGMENT_VECTOR_INDEX},
                )
            )
            index = rows[0] if rows else None
            return {
                "status": (
                    "ok" if index and index.get("state") == "ONLINE" else "degraded"
                ),
                "configured": True,
                "database": self.database,
                "vector_index": index,
                "vector_dimensions": self.vector_dimensions,
            }
        except Exception as exc:
            return {
                "status": "error",
                "configured": self.configured,
                "database": self.database,
                "error": type(exc).__name__,
            }

    async def inspect_schema(self) -> dict[str, Any]:
        """Inspect live labels, relationships, constraints, and indexes."""

        labels = await self._execute(
            self._read("schema_labels", self.SCHEMA_LABELS, {})
        )
        relationships = await self._execute(
            self._read(
                "schema_relationship_types",
                self.SCHEMA_RELATIONSHIPS,
                {},
            )
        )
        constraints = await self._execute(
            self._read("schema_constraints", self.SCHEMA_CONSTRAINTS, {})
        )
        indexes = await self._execute(
            self._read("schema_indexes", self.SCHEMA_INDEXES, {})
        )
        return {
            "labels": labels[0].get("labels", []) if labels else [],
            "relationship_types": (
                relationships[0].get("relationship_types", [])
                if relationships
                else []
            ),
            "constraints": constraints,
            "indexes": indexes,
        }


__all__ = [
    "CypherQuery",
    "InMemoryQueryExecutor",
    "MARENGO_3_DIMENSIONS",
    "Neo4jService",
    "SEGMENT_VECTOR_INDEX",
]
