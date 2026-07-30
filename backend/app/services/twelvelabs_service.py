"""TwelveLabs video-intelligence adapter.

The adapter intentionally uses the full sponsor stack:

* Assets + Knowledge Stores for the multi-camera corpus.
* Pegasus 1.5 asynchronous time-based metadata for typed event segments.
* Marengo 3.0 semantic search and text embeddings.
* Jockey Responses for cross-video entity and event resolution with citations.

All imports are lazy so the rest of SiteTrace remains usable in a credential-
free review environment.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

from ..config import settings
from ..schemas import EvidenceClip, ObservedEvent


logger = logging.getLogger(__name__)


SEGMENT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "material_movement",
        "description": (
            "A worker moves, carries, pushes, lifts, or transports construction "
            "equipment or material. Exclude people merely walking past material."
        ),
        "fields": [
            {
                "name": "actor_description",
                "type": "string",
                "description": "Visible non-biometric description of the person.",
            },
            {
                "name": "object_description",
                "type": "string",
                "description": "The material or equipment being moved.",
            },
            {
                "name": "origin_zone",
                "type": "string",
                "description": "Visible origin area or UNKNOWN.",
            },
            {
                "name": "destination_zone",
                "type": "string",
                "description": "Visible destination area or UNKNOWN.",
            },
            {
                "name": "visible_text",
                "type": "string",
                "description": "Relevant signs or labels visible in the segment.",
            },
        ],
    },
    {
        "id": "spotter_presence_change",
        "description": (
            "A person acting as a material-movement spotter enters, leaves, "
            "becomes distracted, or stops observing the active movement route."
        ),
        "fields": [
            {
                "name": "actor_description",
                "type": "string",
                "description": "Visible non-biometric description of the spotter candidate.",
            },
            {
                "name": "state",
                "type": "string",
                "enum": ["entered", "present", "left", "distracted", "unclear"],
                "description": "The observed spotter-presence state.",
            },
            {
                "name": "zone",
                "type": "string",
                "description": "The active movement zone or UNKNOWN.",
            },
        ],
    },
    {
        "id": "material_placement",
        "description": (
            "Material or equipment is set down, staged, parked, or left in a "
            "specific site zone."
        ),
        "fields": [
            {
                "name": "object_description",
                "type": "string",
                "description": "The material or equipment placed.",
            },
            {
                "name": "destination_zone",
                "type": "string",
                "description": "The visible placement zone or UNKNOWN.",
            },
            {
                "name": "inside_marked_storage",
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether placement is visibly within a marked storage area.",
            },
            {
                "name": "blocks_egress",
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether the object visibly obstructs an egress path.",
            },
            {
                "name": "visible_text",
                "type": "string",
                "description": "Relevant signs or labels visible near placement.",
            },
        ],
    },
    {
        "id": "egress_state_change",
        "description": (
            "The accessibility of a walkway, exit, door, or marked egress zone "
            "changes between clear, partially blocked, or blocked."
        ),
        "fields": [
            {
                "name": "zone_label",
                "type": "string",
                "description": "Visible exit or walkway label.",
            },
            {
                "name": "previous_state",
                "type": "string",
                "enum": ["clear", "partially_blocked", "blocked", "unknown"],
                "description": "State immediately before the change, if visible.",
            },
            {
                "name": "new_state",
                "type": "string",
                "enum": ["clear", "partially_blocked", "blocked", "unknown"],
                "description": "State after the observed change.",
            },
            {
                "name": "blocking_object",
                "type": "string",
                "description": "Visible obstruction or NONE.",
            },
        ],
    },
    {
        "id": "safety_instruction",
        "description": (
            "A spoken or written safety instruction refers to the work, route, "
            "spotter, storage zone, or egress condition."
        ),
        "fields": [
            {
                "name": "speaker_description",
                "type": "string",
                "description": "Visible speaker description or UNKNOWN.",
            },
            {
                "name": "instruction",
                "type": "string",
                "description": "Verbatim or near-verbatim spoken or written instruction.",
            },
            {
                "name": "referenced_zone",
                "type": "string",
                "description": "Referenced site zone or UNKNOWN.",
            },
        ],
    },
    {
        "id": "inspection_or_response",
        "description": (
            "A worker or supervisor visibly inspects, reports, corrects, or "
            "responds to a site condition."
        ),
        "fields": [
            {
                "name": "actor_description",
                "type": "string",
                "description": "Visible non-biometric description of the responder.",
            },
            {
                "name": "observed_condition",
                "type": "string",
                "description": "The condition being inspected or addressed.",
            },
            {
                "name": "response_type",
                "type": "string",
                "enum": ["inspection", "verbal_warning", "report", "correction", "unclear"],
                "description": "The observed response.",
            },
        ],
    },
    {
        "id": "mobile_equipment_movement",
        "description": (
            "A powered mobile machine such as a telehandler, forklift, loader, "
            "truck, or lift starts, stops, reverses, turns, or changes speed."
        ),
        "fields": [
            {
                "name": "equipment_description",
                "type": "string",
                "description": "Visible equipment type and non-unique markings.",
            },
            {
                "name": "movement",
                "type": "string",
                "enum": [
                    "starts",
                    "stops",
                    "reverses",
                    "turns",
                    "accelerates",
                    "unclear",
                ],
                "description": "The directly observed equipment movement.",
            },
            {
                "name": "spotter_visible",
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether a spotter is visible during this clip only.",
            },
            {
                "name": "clearance_observed",
                "type": "string",
                "enum": ["spoken", "radio", "gesture", "not_heard_or_seen", "unclear"],
                "description": (
                    "A directly observed movement clearance; not_heard_or_seen "
                    "does not prove that clearance did not occur."
                ),
            },
            {
                "name": "alarm_or_audio",
                "type": "string",
                "description": "Relevant reversing alarm, horn, radio, or warning audio.",
            },
            {
                "name": "zone",
                "type": "string",
                "description": "Visible work zone or UNKNOWN.",
            },
        ],
    },
    {
        "id": "pedestrian_route_change",
        "description": (
            "A pedestrian stops, detours, leaves a marked walkway, enters a "
            "different route, or changes direction around an obstruction."
        ),
        "fields": [
            {
                "name": "actor_description",
                "type": "string",
                "description": "Visible non-biometric description of the pedestrian.",
            },
            {
                "name": "origin_zone",
                "type": "string",
                "description": "The visible route before the change or UNKNOWN.",
            },
            {
                "name": "destination_zone",
                "type": "string",
                "description": "The visible route after the change or UNKNOWN.",
            },
            {
                "name": "visible_obstruction",
                "type": "string",
                "description": "The observed obstruction, if any; otherwise NONE.",
            },
            {
                "name": "visible_text",
                "type": "string",
                "description": "Relevant walkway, exclusion-zone, or access signage.",
            },
        ],
    },
    {
        "id": "restricted_or_blind_zone_entry",
        "description": (
            "A person or object enters or remains in a visibly marked restricted "
            "area, equipment operating envelope, exclusion zone, or blind-spot area."
        ),
        "fields": [
            {
                "name": "actor_description",
                "type": "string",
                "description": "Visible non-biometric person or object description.",
            },
            {
                "name": "zone",
                "type": "string",
                "description": "The marked or visually bounded zone.",
            },
            {
                "name": "equipment_description",
                "type": "string",
                "description": "Nearby operating equipment or NONE.",
            },
            {
                "name": "visible_text",
                "type": "string",
                "description": "Relevant restricted-access signage.",
            },
        ],
    },
    {
        "id": "warning_or_emergency_stop",
        "description": (
            "A person gives an urgent warning, equipment brakes or stops "
            "abruptly, or site personnel visibly intervene to prevent contact."
        ),
        "fields": [
            {
                "name": "warning_audio",
                "type": "string",
                "description": "The relevant spoken warning, alarm, horn, or radio audio.",
            },
            {
                "name": "equipment_description",
                "type": "string",
                "description": "Equipment responding to the warning or NONE.",
            },
            {
                "name": "response",
                "type": "string",
                "enum": [
                    "emergency_stop",
                    "controlled_stop",
                    "person_retreats",
                    "supervisor_intervenes",
                    "unclear",
                ],
                "description": "The directly observed response.",
            },
            {
                "name": "contact_observed",
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether physical contact is directly visible.",
            },
            {
                "name": "zone",
                "type": "string",
                "description": "Visible incident zone or UNKNOWN.",
            },
        ],
    },
    {
        "id": "near_miss_proximity",
        "description": (
            "A person, vehicle, equipment, or material comes into visibly close "
            "proximity and contact is avoided. Record observation only; do not "
            "infer the cause or an exact distance that is not measurable."
        ),
        "fields": [
            {
                "name": "person_description",
                "type": "string",
                "description": "Visible non-biometric description of the person.",
            },
            {
                "name": "equipment_or_object",
                "type": "string",
                "description": "The nearby equipment, vehicle, or material.",
            },
            {
                "name": "contact_observed",
                "type": "string",
                "enum": ["yes", "no", "unclear"],
                "description": "Whether physical contact is directly visible.",
            },
            {
                "name": "avoidance_action",
                "type": "string",
                "description": "Visible stop, retreat, turn, or other avoidance action.",
            },
            {
                "name": "zone",
                "type": "string",
                "description": "Visible incident zone or UNKNOWN.",
            },
        ],
    },
]


JOCKEY_EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "event_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "camera_id": {"type": "string"},
                    "start_sec": {"type": "number"},
                    "end_sec": {"type": "number"},
                    "actor_descriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "object_descriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "zone_ids": {"type": "array", "items": {"type": "string"}},
                    "source_item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confidence": {"type": "number"},
                    "connection_basis": {"type": "string"},
                },
                "required": [
                    "event_type",
                    "summary",
                    "camera_id",
                    "start_sec",
                    "end_sec",
                    "actor_descriptions",
                    "object_descriptions",
                    "zone_ids",
                    "source_item_ids",
                    "confidence",
                    "connection_basis",
                ],
            },
        },
        "unresolved_connections": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["events", "unresolved_connections"],
}


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _object_id(value: Any) -> str:
    for name in ("id", "_id", "asset_id", "item_id", "task_id"):
        result = getattr(value, name, None)
        if result:
            return str(result)
    dumped = _model_dump(value)
    if isinstance(dumped, dict):
        for name in ("id", "_id", "asset_id", "item_id", "task_id"):
            if dumped.get(name):
                return str(dumped[name])
    return ""


def _extract_json_text(response: Any) -> dict[str, Any]:
    dumped = _model_dump(response)
    outputs = dumped.get("output", []) if isinstance(dumped, dict) else []
    for output in outputs:
        output = _model_dump(output)
        if not isinstance(output, dict) or output.get("type") != "message":
            continue
        for content in output.get("content", []):
            content = _model_dump(content)
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                return json.loads(content["text"])
    raise RuntimeError("TwelveLabs Jockey returned no structured message")


class TwelveLabsService:
    def __init__(self) -> None:
        self._client: Any | None = None

    @property
    def available(self) -> bool:
        return settings.has_twelvelabs

    @property
    def client(self) -> Any:
        if not self.available:
            raise RuntimeError("TWELVE_LABS_API_KEY is not configured")
        if self._client is None:
            from twelvelabs import TwelveLabs

            self._client = TwelveLabs(api_key=settings.twelve_labs_api_key)
        return self._client

    def _wait_for_status(
        self,
        retrieve,
        identifier: str,
        *,
        success: set[str] | None = None,
        failure: set[str] | None = None,
    ) -> Any:
        success = success or {"ready"}
        failure = failure or {"failed"}
        deadline = time.monotonic() + settings.twelve_labs_poll_timeout_seconds
        while time.monotonic() < deadline:
            result = retrieve(identifier)
            status = str(getattr(result, "status", "")).lower()
            if status in success:
                return result
            if status in failure:
                raise RuntimeError(f"TwelveLabs resource {identifier} failed")
            time.sleep(settings.twelve_labs_poll_seconds)
        raise TimeoutError(f"TwelveLabs resource {identifier} did not become ready")

    def create_knowledge_store(self, case_id: str) -> str:
        if settings.twelve_labs_knowledge_store_id:
            return settings.twelve_labs_knowledge_store_id
        ontology = {
            "type": "object",
            "properties": {
                "site_zone": {
                    "type": "string",
                    "description": "The construction site zone visible in the shot.",
                },
                "work_activity": {
                    "type": "string",
                    "description": "The primary construction activity in the shot.",
                },
                "safety_relevance": {
                    "type": "string",
                    "enum": ["relevant", "not_relevant", "unclear"],
                    "description": "Whether the shot contains evidence relevant to a safety investigation.",
                },
            },
            "required": ["site_zone", "work_activity", "safety_relevance"],
        }
        store = self.client.knowledge_stores.create(
            name=f"{settings.twelve_labs_store_prefix}-{case_id}",
            description="Multi-camera construction incident evidence corpus",
            metadata={"case_id": case_id, "product": "SiteTrace"},
            ingestion_config={
                "enrichment_config": {
                    "type": "json_schema",
                    "json_schema": ontology,
                }
            },
        )
        store_id = _object_id(store)
        if not store_id:
            raise RuntimeError("TwelveLabs returned no knowledge store ID")
        return store_id

    def upload_asset(self, path: Path) -> str:
        if path.stat().st_size <= 200 * 1024 * 1024:
            with path.open("rb") as stream:
                asset = self.client.assets.create(method="direct", file=stream)
            asset_id = _object_id(asset)
        else:
            result = self.client.multipart_upload.upload_file(str(path))
            asset_id = str(getattr(result, "asset_id", "") or _object_id(result))
        if not asset_id:
            raise RuntimeError(f"TwelveLabs returned no asset ID for {path.name}")
        self._wait_for_status(
            lambda current_id: self.client.assets.retrieve(asset_id=current_id),
            asset_id,
        )
        return asset_id

    def add_asset_to_store(
        self,
        knowledge_store_id: str,
        asset_id: str,
        *,
        camera_id: str,
        filename: str,
    ) -> str:
        item = self.client.knowledge_store_items.create(
            knowledge_store_id=knowledge_store_id,
            asset_id=asset_id,
            metadata={"camera_id": camera_id, "filename": filename},
        )
        item_id = _object_id(item)
        if not item_id:
            raise RuntimeError("TwelveLabs returned no knowledge store item ID")
        self._wait_for_status(
            lambda current_id: self.client.knowledge_store_items.retrieve(
                knowledge_store_id=knowledge_store_id,
                item_id=current_id,
            ),
            item_id,
        )
        return item_id

    def segment_asset(
        self,
        asset_id: str,
        *,
        camera_id: str,
        filename: str,
        item_id: str | None = None,
    ) -> tuple[list[EvidenceClip], list[ObservedEvent]]:
        from twelvelabs.types import AsyncResponseFormat, VideoContext_AssetId

        task = self.client.analyze_async.tasks.create(
            video=VideoContext_AssetId(asset_id=asset_id),
            model_name=settings.twelve_labs_pegasus_model,
            analysis_mode="time_based_metadata",
            min_segment_duration=2.0,
            max_segment_duration=90.0,
            max_tokens=32768,
            response_format=AsyncResponseFormat(
                type="segment_definitions",
                segment_definitions=SEGMENT_DEFINITIONS,
            ),
        )
        task_id = str(getattr(task, "task_id", "") or _object_id(task))
        if not task_id:
            raise RuntimeError("Pegasus segmentation returned no task ID")
        ready = self._wait_for_status(
            lambda current_id: self.client.analyze_async.tasks.retrieve(current_id),
            task_id,
        )
        raw_data = getattr(getattr(ready, "result", None), "data", None)
        if raw_data is None:
            dumped = _model_dump(ready)
            raw_data = (
                ((dumped.get("result") or {}).get("data"))
                if isinstance(dumped, dict)
                else None
            )
        data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        if not isinstance(data, dict):
            raise RuntimeError("Pegasus segmentation returned invalid JSON")

        clips: list[EvidenceClip] = []
        events: list[ObservedEvent] = []
        sequence = 0
        for segment_type, entries in data.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                sequence += 1
                metadata = entry.get("metadata") or {}
                start = float(entry.get("start_time", 0))
                end = float(entry.get("end_time", start + 0.01))
                if end <= start:
                    continue
                evidence_id = f"{camera_id.replace(' ', '_')}-EV-{sequence:04d}"
                summary = self._segment_summary(segment_type, metadata)
                confidence = self._confidence(metadata)
                clip = EvidenceClip(
                    evidence_clip_id=evidence_id,
                    item_id=item_id,
                    asset_id=asset_id,
                    camera_id=camera_id,
                    source_filename=filename,
                    start_sec=start,
                    end_sec=end,
                    summary=summary,
                    transcript=str(
                        metadata.get("spoken_text")
                        or metadata.get("instruction")
                        or metadata.get("warning_audio")
                        or metadata.get("alarm_or_audio")
                        or ""
                    ),
                    visible_text=str(metadata.get("visible_text") or ""),
                    confidence=confidence,
                )
                event = ObservedEvent(
                    event_id=f"{camera_id.replace(' ', '_')}-EVENT-{sequence:04d}",
                    event_type=str(segment_type).upper(),
                    summary=summary,
                    camera_id=camera_id,
                    start_sec=start,
                    end_sec=end,
                    actor_ids=self._entity_values(
                        metadata,
                        (
                            "actor_description",
                            "speaker_description",
                            "person_description",
                        ),
                    ),
                    object_ids=self._entity_values(
                        metadata,
                        (
                            "object_description",
                            "blocking_object",
                            "equipment_description",
                            "equipment_or_object",
                            "visible_obstruction",
                        ),
                    ),
                    zone_ids=self._entity_values(
                        metadata,
                        (
                            "destination_zone",
                            "origin_zone",
                            "zone",
                            "zone_label",
                            "referenced_zone",
                        ),
                    ),
                    evidence_clip_ids=[evidence_id],
                    confidence=confidence,
                )
                clips.append(clip)
                events.append(event)
        return clips, events

    def search_knowledge_store(
        self,
        knowledge_store_id: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        result = self.client.knowledge_stores.search(
            knowledge_store_id=knowledge_store_id,
            query={"text": query},
            search_options={"video": {"modalities": ["visual", "audio"]}},
            page_size=min(limit, 50),
            include_metadata=True,
        )
        hits: list[dict[str, Any]] = []
        for hit in getattr(result, "data", []) or []:
            dumped = _model_dump(hit)
            if not isinstance(dumped, dict):
                continue
            for match in dumped.get("matches", []) or []:
                normalized = _model_dump(match)
                if isinstance(normalized, dict):
                    hits.append(
                        {
                            "item_id": dumped.get("item_id") or dumped.get("_id"),
                            "rank": dumped.get("rank"),
                            "start_sec": normalized.get("start_sec")
                            or normalized.get("start"),
                            "end_sec": normalized.get("end_sec")
                            or normalized.get("end"),
                            "transcription": normalized.get("transcription") or "",
                            "metadata": dumped.get("metadata") or {},
                        }
                    )
                if len(hits) >= limit:
                    return hits
        return hits

    def resolve_cross_video_events(
        self,
        knowledge_store_id: str,
    ) -> tuple[dict[str, Any], str | None, list[dict[str, Any]]]:
        from twelvelabs.types import TextParam, TextParamFormat_JsonSchema

        response = self.client.responses.create(
            knowledge_store_id=knowledge_store_id,
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "Across all cameras, reconstruct the construction incident "
                        "or near-miss event sequence. Include relevant material "
                        "placement, pedestrian route changes, spotter presence, "
                        "mobile-equipment movement, clearances, warnings, stops, "
                        "restricted-zone entries, and responses. Link the same object "
                        "or person candidate only when temporal, visual, motion, "
                        "audio, or text evidence supports the link. Every event must "
                        "cite the source knowledge-store item and exact start/end "
                        "seconds. Preserve observed sequence without asserting "
                        "causation or root cause. Report uncertain links as unresolved."
                    ),
                }
            ],
            instructions=(
                "You are the SiteTrace video evidence specialist. Distinguish direct "
                "observation from cross-camera inference and preserve traceability."
            ),
            include=["intermediate_outputs"],
            text=TextParam(
                format=TextParamFormat_JsonSchema(
                    name="sitetrace_cross_video_events",
                    schema_=JOCKEY_EVENT_SCHEMA,
                    strict=True,
                )
            ),
        )
        parsed = _extract_json_text(response)
        session_id = getattr(response, "session_id", None)
        dumped = _model_dump(response)
        intermediate: list[dict[str, Any]] = []
        if isinstance(dumped, dict):
            for item in dumped.get("output", []):
                item = _model_dump(item)
                if isinstance(item, dict) and item.get("type") != "message":
                    intermediate.append(item)
        return parsed, str(session_id) if session_id else None, intermediate

    def embed_text(self, text: str) -> list[float]:
        response = self.client.embed.create(
            model_name=settings.twelve_labs_marengo_model,
            text=text,
        )
        vector = self._find_vector(_model_dump(response))
        if not vector:
            raise RuntimeError("Marengo returned no text embedding")
        return vector

    @staticmethod
    def _segment_summary(segment_type: str, metadata: dict[str, Any]) -> str:
        useful = [
            f"{key.replace('_', ' ')}: {value}"
            for key, value in metadata.items()
            if value not in (None, "", [], "UNKNOWN", "unknown")
        ]
        return f"{segment_type.replace('_', ' ').title()}: " + "; ".join(useful)

    @staticmethod
    def _confidence(metadata: dict[str, Any]) -> float:
        value = metadata.get("confidence")
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.75

    @staticmethod
    def _entity_values(
        metadata: dict[str, Any],
        keys: Iterable[str],
    ) -> list[str]:
        values: list[str] = []
        for key in keys:
            raw = metadata.get(key)
            if raw and str(raw).lower() not in {"unknown", "none", "unclear"}:
                values.append(str(raw).strip())
        return list(dict.fromkeys(values))

    @classmethod
    def _find_vector(cls, value: Any) -> list[float] | None:
        value = _model_dump(value)
        if isinstance(value, list):
            if len(value) >= 8 and all(isinstance(item, (int, float)) for item in value):
                return [float(item) for item in value]
            for item in value:
                result = cls._find_vector(item)
                if result:
                    return result
        if isinstance(value, dict):
            for key in ("float_", "float", "embedding", "values", "vector"):
                if key in value:
                    result = cls._find_vector(value[key])
                    if result:
                        return result
            for item in value.values():
                result = cls._find_vector(item)
                if result:
                    return result
        return None


twelvelabs_service = TwelveLabsService()
