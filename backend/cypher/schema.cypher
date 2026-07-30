// SiteTrace evidence context graph schema.
//
// Runtime writes are performed by parameterized Cypher templates in
// backend/app/services/neo4j_service.py. Neo4j MCP is intentionally limited
// to read-only schema inspection and exploratory development queries.
//
// Marengo 3.0 emits 512-dimensional embeddings. Embeddings from Marengo 2.7
// are not compatible and must never be mixed into this index.

// ---- Investigation and plan identity ---------------------------------------
CREATE CONSTRAINT sitetrace_case_id IF NOT EXISTS
FOR (n:InvestigationCase) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_jha_id IF NOT EXISTS
FOR (n:JHA) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_jha_step_id IF NOT EXISTS
FOR (n:JHAStep) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_control_id IF NOT EXISTS
FOR (n:SafetyControl) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_zone_id IF NOT EXISTS
FOR (n:Zone) REQUIRE n.id IS UNIQUE;

// ---- Video, observation, and evidence identity -----------------------------
CREATE CONSTRAINT sitetrace_video_id IF NOT EXISTS
FOR (n:Video) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_segment_id IF NOT EXISTS
FOR (n:Segment) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_event_id IF NOT EXISTS
FOR (n:ObservedEvent) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_entity_id IF NOT EXISTS
FOR (n:Entity) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_clip_id IF NOT EXISTS
FOR (n:EvidenceClip) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_coverage_id IF NOT EXISTS
FOR (n:CoverageWindow) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_deviation_id IF NOT EXISTS
FOR (n:Deviation) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT sitetrace_report_id IF NOT EXISTS
FOR (n:InvestigationReport) REQUIRE n.id IS UNIQUE;

// ---- Operational lookup indexes -------------------------------------------
CREATE INDEX sitetrace_case_status IF NOT EXISTS
FOR (n:InvestigationCase) ON (n.status);

CREATE INDEX sitetrace_step_jha_order IF NOT EXISTS
FOR (n:JHAStep) ON (n.jha_id, n.step_order);

CREATE INDEX sitetrace_control_code IF NOT EXISTS
FOR (n:SafetyControl) ON (n.code);

CREATE INDEX sitetrace_zone_name IF NOT EXISTS
FOR (n:Zone) ON (n.normalized_name);

CREATE INDEX sitetrace_video_case_camera IF NOT EXISTS
FOR (n:Video) ON (n.case_id, n.camera_id);

CREATE INDEX sitetrace_segment_video_time IF NOT EXISTS
FOR (n:Segment) ON (n.video_id, n.start_sec);

CREATE INDEX sitetrace_segment_case IF NOT EXISTS
FOR (n:Segment) ON (n.case_id);

CREATE INDEX sitetrace_event_case_time IF NOT EXISTS
FOR (n:ObservedEvent) ON (n.case_id, n.global_start_ms);

CREATE INDEX sitetrace_event_type IF NOT EXISTS
FOR (n:ObservedEvent) ON (n.event_type);

CREATE INDEX sitetrace_clip_case IF NOT EXISTS
FOR (n:EvidenceClip) ON (n.case_id);

CREATE INDEX sitetrace_deviation_case_status IF NOT EXISTS
FOR (n:Deviation) ON (n.case_id, n.status);

// Keyword fallback complements Marengo vector retrieval.
CREATE FULLTEXT INDEX sitetrace_segment_fulltext IF NOT EXISTS
FOR (n:Segment) ON EACH [n.summary, n.transcript, n.on_screen_text];

CREATE FULLTEXT INDEX sitetrace_plan_fulltext IF NOT EXISTS
FOR (n:JHAStep|SafetyControl) ON EACH [n.title, n.description];

// Semantic retrieval over TwelveLabs Marengo 3.0 fused segment embeddings.
CREATE VECTOR INDEX sitetrace_segment_marengo_v3 IF NOT EXISTS
FOR (n:Segment) ON (n.embedding)
OPTIONS { indexConfig: {
  `vector.dimensions`: 512,
  `vector.similarity_function`: 'cosine'
} };
