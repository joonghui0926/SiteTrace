# SiteTrace on the hackathon AWS account

SiteTrace uses the temporary Workshop Studio account in `us-east-1`. It is not
a permanent deployment. The deployed shape is:

```text
Amplify web
  -> AgentCore custom runtime (FastAPI + Strands)
       -> OpenAI Responses API
       -> TwelveLabs API
       -> Neo4j AuraDB on AWS
       -> S3 evidence and Strands sessions
       -> CloudWatch / OpenTelemetry
```

The application remains usable locally when AWS or Strands credentials are
absent. Live investigation stages report their unavailable integration instead
of fabricating evidence.

## 1. Local AWS CLI session

In Workshop Studio, choose **Get AWS CLI credentials** and copy the generated
temporary environment-variable commands into the local VS Code terminal. Do
not place them in `.env` or commit them.

Verify the session:

```powershell
$env:AWS_REGION = "us-east-1"
aws sts get-caller-identity
```

The workshop credentials include a session token and expire when the event
ends. The two workshop short links provide learning environments; opening a
link does not configure the local AWS CLI automatically.

## 2. Runtime secrets

Store runtime credentials in AWS Secrets Manager, never in the image:

```text
sitetrace/openai
sitetrace/twelvelabs
sitetrace/neo4j
```

Expected environment variables after secret injection:

```text
OPENAI_API_KEY
TWELVE_LABS_API_KEY
NEO4J_URI
NEO4J_USERNAME
NEO4J_PASSWORD
SITETRACE_S3_BUCKET
SITETRACE_SESSION_PREFIX=sitetrace/sessions/
AWS_REGION=us-east-1
```

Rotate any credential that has appeared in chat, screenshots, logs, or shell
history before storing it.

## 3. S3 and IAM

Create one private, encrypted bucket for:

```text
uploads/          original footage
evidence/         clipped evidence and manifests
reports/          approved PDF reports
sessions/         Strands S3SessionManager state
```

The AgentCore execution role needs only:

- `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` on this bucket’s objects
- `s3:ListBucket` on this bucket
- `secretsmanager:GetSecretValue` for the three SiteTrace secrets
- CloudWatch log and OpenTelemetry permissions required by AgentCore
- ECR image pull permissions

Block public access. Use presigned URLs for short-lived browser uploads and
evidence playback.

## 4. AgentCore runtime contract

The FastAPI app implemented by the business API must expose:

```text
GET  /ping
POST /invocations
```

AgentCore custom containers must:

- target `linux/arm64`
- listen on port `8080`
- be pushed to Amazon ECR
- return a healthy response from `/ping`
- accept AgentCore invocation payloads at `/invocations`

`backend/Dockerfile` implements the architecture and port requirements. It
expects the ASGI application at `app.main:app`.

## 5. Build and smoke-test

From the repository root:

```powershell
docker buildx create --use
docker buildx build --platform linux/arm64 `
  -f backend/Dockerfile `
  -t sitetrace-agent:arm64 `
  --load backend

docker run --platform linux/arm64 --rm -p 8080:8080 `
  --env-file backend/.env `
  sitetrace-agent:arm64

Invoke-WebRequest http://localhost:8080/ping
Invoke-RestMethod -Method Post `
  -Uri http://localhost:8080/invocations `
  -ContentType "application/json" `
  -Body '{"input":{"action":"health"}}'
```

Never use the real `.env` in CI or commit it.

## 6. Push the ARM64 image

Replace placeholders after `aws sts get-caller-identity`:

```powershell
$awsAccountId = "<account-id>"
$awsRegion = "us-east-1"
$ecrRepository = "sitetrace-agent"
$ecrHost = "$awsAccountId.dkr.ecr.$awsRegion.amazonaws.com"

aws ecr create-repository `
  --repository-name $ecrRepository `
  --region $awsRegion

aws ecr get-login-password --region $awsRegion |
  docker login --username AWS --password-stdin $ecrHost

docker buildx build --platform linux/arm64 `
  -f backend/Dockerfile `
  -t "$ecrHost/${ecrRepository}:hackathon" `
  --push backend
```

Create the AgentCore Runtime with the event-provided role and the pushed image
using the current `@aws/agentcore` CLI:

```powershell
npm install -g @aws/agentcore
agentcore create
agentcore deploy
agentcore invoke
```

The CLI wizard should select **Strands Agents**, the existing custom FastAPI
service, `us-east-1`, and the ECR image above. Workshop IAM restrictions may
prevent creation of some resources; do not broaden policies without the event
operator.

## 7. Sessions, approval, and observability

- Local development uses `FileSessionManager`.
- Setting `SITETRACE_S3_BUCKET` switches agents to `S3SessionManager`.
- The Strands report-publication tool raises a human interrupt before it runs.
  The API must return the interrupt to the browser and resume with an
  `interruptResponse`; it must not auto-approve it.
- `OTEL_EXPORTER_OTLP_ENDPOINT` enables the Strands OTLP exporter.
- In AgentCore, AWS OpenTelemetry auto-instrumentation sends agent/model/tool
  spans to CloudWatch GenAI Observability. Pass the investigation session ID as
  OpenTelemetry baggage key `session.id` where the API layer creates a request
  span.

## 8. Cleanup

Before the Workshop Studio event expires, download or push source code. Delete
created AgentCore runtimes, ECR images/repositories, Amplify apps, Secrets
Manager secrets, and S3 objects if the temporary account does not clean them
automatically. The final README and demo should not claim a permanent URL.

## Official references

- [Strands AgentCore Python deployment](https://strandsagents.com/docs/user-guide/deploy/deploy_to_bedrock_agentcore/python/)
- [Strands workflows](https://strandsagents.com/docs/user-guide/concepts/multi-agent/workflow/)
- [Strands interrupts](https://strandsagents.com/docs/user-guide/concepts/interrupts/)
- [Strands session management](https://strandsagents.com/docs/user-guide/concepts/agents/session-management/)
- [Strands traces](https://strandsagents.com/docs/user-guide/observability-evaluation/traces/)
