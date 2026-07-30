# SiteTrace

**SiteTrace reconstructs a construction incident from hours of multi-camera
footage and produces the evidence-backed investigation report a safety manager
would normally write by hand.**

SiteTrace is an upload-first web application for post-incident, near-miss, and
hazard investigations. A reviewer supplies the approved JHA, supporting plans,
camera metadata, and all relevant footage. The system reconstructs only
observable events, compares work-as-done with work-as-planned, preserves every
source-video citation, and pauses for a qualified human before publishing the
final PDF.

It does **not** infer root cause from video alone, and absence from footage is
never treated as proof that a control was not performed.

## Sponsor architecture

| Platform | Essential SiteTrace responsibility |
|---|---|
| TwelveLabs | Pegasus 1.5 extracts timestamped actions, audio, and on-screen text. Marengo 3.0 performs evidence search and produces 512-dimensional segment embeddings. Jockey reasons across each case's multi-camera Knowledge Store and returns clip citations. |
| OpenAI | Responses API Structured Outputs parse the JHA, normalize observed events, map them to planned controls, distinguish observation from inference, and draft evidence-bounded report language. Luna handles normalization, Terra handles safety reasoning, and Sol is called only for explicit evidence conflicts. |
| Neo4j | AuraDB stores the planned-work and observed-work property graphs, Marengo embeddings, evidence provenance, and findings. Deterministic Cypher computes missing or contradicted relationships; vector retrieval is followed by graph traversal. |
| AWS Strands | A bounded Workflow controls parsing, parallel camera analysis, graph writing, graph diff, evidence verification, drafting, the human approval interrupt, and publication. Specialist agents run as tools with hooks, skills, persisted sessions, retries, and OpenTelemetry. |
| AWS | The temporary hackathon deployment uses AgentCore Runtime, private S3 evidence/session storage, Secrets Manager, CloudWatch, and an AWS-region Neo4j AuraDB instance. |

```text
JHA + plans + metadata + multi-camera video
  -> TwelveLabs evidence extraction and cross-video citations
  -> OpenAI safety-language normalization and JHA matching
  -> Neo4j planned/observed graph + deterministic Graph Diff
  -> Strands evidence gate + human approval interrupt
  -> evidence-backed Incident Investigation Report PDF
```

## Investigation workflow

1. Securely store the uploaded files.
2. Parse the JHA and supporting plans into typed `PlannedStep` records.
3. Analyze camera files in parallel with Pegasus.
4. Connect entities and events across the Jockey Knowledge Store.
5. Normalize the event sequence and match it to JHA controls.
6. Write videos, segments, events, entities, zones, controls, and provenance to
   AuraDB.
7. Run parameterized Cypher Graph Diff and verify every factual finding against
   an allowed `EvidenceClip`.
8. Draft the investigation package and stop at the Strands human-review gate.
9. Record the reviewer and generate the final PDF and evidence annex.

No event, finding, graph path, or report sentence is seeded from a demo case.

## Web experience

The responsive web UI uses a white, low-chrome visual system and the SiteTrace
green `#73C580`. It provides:

- required JHA and multi-video upload;
- optional supporting PDFs, camera start-time metadata, and site map;
- a nine-stage sponsor workflow with status and failure visibility;
- cited multi-camera event timeline and source-video playback;
- JHA-versus-observed findings and Neo4j evidence paths;
- a report preview, reviewer attestation, approval, and final PDF link.

## Local setup

Prerequisites: Node.js 22.13+, Python 3.11+, and credentials for OpenAI,
TwelveLabs, and Neo4j AuraDB.

```powershell
Copy-Item .env.example .env
npm install

py -3.11 -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -e "backend[test]"
```

Populate the ignored `.env` file:

```dotenv
NEXT_PUBLIC_SITETRACE_API_URL=http://localhost:8000
OPENAI_API_KEY=
TWELVE_LABS_API_KEY=
NEO4J_URI=neo4j+s://...
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=
NEO4J_DATABASE=neo4j
AWS_REGION=us-east-1
SITETRACE_S3_BUCKET=
```

Run the API and web app in separate terminals:

```powershell
backend\.venv\Scripts\python.exe -m uvicorn app.main:app `
  --app-dir backend --host 127.0.0.1 --port 8000

npm run dev
```

Open `http://localhost:3000`. Health and sponsor capability status are available
at `http://localhost:8000/health`.

## Upload contract

`POST /cases` accepts multipart form data:

- `title`: investigation title;
- `jha`: one approved JHA PDF;
- `videos`: one or more original camera videos;
- `supporting_documents`: optional PDFs;
- `site_metadata`: optional JSON with camera IDs and ISO-8601 recording start
  times;
- `site_map`: optional PNG, JPEG, or WebP.

Example metadata:

```json
{
  "cameras": [
    {
      "filename": "CAM-01.mp4",
      "camera_id": "CAM-01",
      "recording_started_at": "2026-07-30T10:14:00-07:00"
    }
  ]
}
```

The remaining API surface is:

```text
GET  /health
GET  /cases/{case_id}
POST /cases/{case_id}/investigate
POST /cases/{case_id}/approve
GET  /cases/{case_id}/evidence/{evidence_clip_id}
GET  /reports/{case_id}.pdf
POST /invocations
```

## Validation

```powershell
npm test
npm run lint
backend\.venv\Scripts\python.exe -m pytest backend\tests -q
```

The suite verifies server rendering, the real multipart contract, absence of
hardcoded demo facts, evidence-ID enforcement, model routing and schema
allowlists, parameterized Cypher, graph-diff semantics, and the Strands
workflow boundary.

## Temporary AWS deployment

The repository includes safe plan/apply/cleanup scripts for the workshop
account:

```powershell
.\infra\aws\deploy-agentcore.ps1 -Mode Plan
.\infra\aws\deploy-agentcore.ps1 -Mode Apply
.\infra\aws\deploy-frontend.ps1 -Mode Plan
.\infra\aws\deploy-frontend.ps1 -Mode Apply
```

They default to the `sitetrace-workshop` AWS CLI profile and `us-east-1`.
Credentials remain in Secrets Manager and are never baked into the image.
See [infra/aws/README.md](infra/aws/README.md) for runtime permissions,
verification, and cleanup.

This deployment is intentionally temporary: Workshop Studio credentials and
resources expire after the event.

## Security

- `.env`, uploads, generated reports, sessions, and deployment output are
  ignored by Git.
- The health endpoint returns capability booleans and model names, never
  secrets.
- File names are normalized and upload size is bounded.
- Neo4j write queries are parameterized; the investigation graph diff uses
  reviewed Cypher templates.
- Only evidence IDs returned by the current TwelveLabs analysis may support a
  factual finding.
- Report publication requires an explicit named reviewer.
