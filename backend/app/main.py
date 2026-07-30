"""SiteTrace FastAPI surface for the web application and AgentCore container."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import aiofiles
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import settings
from .pipeline import InvestigationPipeline
from .schemas import (
    ApprovalRequest,
    CaseRecord,
    CaseStatus,
    HealthResponse,
    UploadedInput,
)
from .services.aws_service import AWSStorageService
from .services.neo4j_service import Neo4jService
from .services.openai_service import OpenAIService
from .services.twelvelabs_service import TwelveLabsService
from .store import CaseNotFoundError, case_store


neo4j_service = Neo4jService(
    uri=settings.neo4j_uri,
    username=settings.neo4j_username,
    password=settings.neo4j_password,
    database=settings.neo4j_database,
)
openai_service = OpenAIService()
twelvelabs_service = TwelveLabsService()
aws_service = AWSStorageService()
pipeline = InvestigationPipeline(
    neo4j=neo4j_service,
    openai=openai_service,
    twelvelabs=twelvelabs_service,
    aws_storage=aws_service,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if neo4j_service.configured:
        await neo4j_service.initialize_schema()
    yield
    await neo4j_service.close()


app = FastAPI(
    title="SiteTrace API",
    version="0.1.0",
    description=(
        "Multi-camera construction incident reconstruction with evidence-backed "
        "investigation reports."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe_filename(filename: str | None, fallback: str) -> str:
    value = Path(filename or fallback).name
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value).strip(" .")
    return value or fallback


async def _persist_upload(
    upload: UploadFile,
    *,
    case_id: str,
    role: str,
    index: int = 0,
) -> UploadedInput:
    filename = _safe_filename(upload.filename, f"{role}-{index}")
    directory = settings.upload_directory / case_id / role
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / filename
    size = 0
    async with aiofiles.open(destination, "wb") as stream:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                await stream.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"{filename} is too large")
            await stream.write(chunk)
    content_type = upload.content_type or "application/octet-stream"
    s3_uri = await asyncio.to_thread(
        aws_service.upload_file,
        destination,
        key=f"cases/{case_id}/{role}/{filename}",
        content_type=content_type,
    )
    return UploadedInput(
        filename=filename,
        media_type=content_type,
        size_bytes=size,
        local_path=str(destination),
        s3_uri=s3_uri,
    )


def _case_or_404(case_id: str) -> CaseRecord:
    try:
        return case_store.get(case_id)
    except CaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Case not found") from exc


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "Healthy", "service": "SiteTrace"}


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    neo4j = await neo4j_service.health()
    aws = await asyncio.to_thread(aws_service.health)
    return HealthResponse(
        status=(
            "ok"
            if all(
                (
                    openai_service.available,
                    twelvelabs_service.available,
                    neo4j.get("status") == "ok",
                )
            )
            else "degraded"
        ),
        service="SiteTrace",
        mode=settings.app_environment,
        sponsors={
            "openai": {
                "configured": openai_service.configured,
                "responses_api": openai_service.available,
                "models": {
                    "normalization": settings.openai_normalization_model,
                    "reasoning": settings.openai_reasoning_model,
                    "escalation": settings.openai_escalation_model,
                },
            },
            "twelvelabs": {
                "configured": twelvelabs_service.available,
                "pegasus": settings.twelve_labs_pegasus_model,
                "marengo": settings.twelve_labs_marengo_model,
                "jockey": True,
            },
            "neo4j": neo4j,
            "aws": aws,
            "strands": pipeline.runtime_for("health-check").health(),
        },
    )


@app.get("/cases", response_model=list[CaseRecord])
async def list_cases() -> list[CaseRecord]:
    return case_store.list()


@app.post("/cases", response_model=CaseRecord, status_code=201)
async def create_case(
    title: Annotated[str, Form(min_length=3)],
    jha: Annotated[UploadFile, File()],
    videos: Annotated[list[UploadFile], File(min_length=1)],
    supporting_documents: Annotated[list[UploadFile] | None, File()] = None,
    site_metadata: Annotated[UploadFile | None, File()] = None,
    site_map: Annotated[UploadFile | None, File()] = None,
) -> CaseRecord:
    case_id = f"ST-{uuid4().hex[:10].upper()}"
    jha_record = await _persist_upload(jha, case_id=case_id, role="jha")
    supporting = [
        await _persist_upload(
            upload,
            case_id=case_id,
            role="supporting-documents",
            index=index,
        )
        for index, upload in enumerate(supporting_documents or [])
    ]
    metadata_record = (
        await _persist_upload(
            site_metadata,
            case_id=case_id,
            role="metadata",
        )
        if site_metadata
        else None
    )
    map_record = (
        await _persist_upload(
            site_map,
            case_id=case_id,
            role="site-map",
        )
        if site_map
        else None
    )
    video_records = [
        await _persist_upload(
            upload,
            case_id=case_id,
            role="videos",
            index=index,
        )
        for index, upload in enumerate(videos)
    ]
    record = CaseRecord(
        case_id=case_id,
        title=title.strip(),
        status=CaseStatus.UPLOADED,
        jha=jha_record,
        supporting_documents=supporting,
        site_metadata=metadata_record,
        site_map=map_record,
        videos=video_records,
    )
    return case_store.save(record)


@app.get("/cases/{case_id}", response_model=CaseRecord)
async def get_case(case_id: str) -> CaseRecord:
    return _case_or_404(case_id)


@app.post("/cases/{case_id}/investigate", response_model=CaseRecord)
async def investigate(case_id: str) -> CaseRecord:
    _case_or_404(case_id)
    try:
        return await pipeline.start(case_id)
    except Exception as exc:
        record = _case_or_404(case_id)
        record.status = CaseStatus.FAILED
        record.error = f"{type(exc).__name__}: {exc}"
        case_store.save(record)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Investigation pipeline failed",
                "stage_error": record.error,
            },
        ) from exc


@app.post("/cases/{case_id}/approve", response_model=CaseRecord)
async def approve(case_id: str, request: ApprovalRequest) -> CaseRecord:
    _case_or_404(case_id)
    try:
        return await pipeline.approve(
            case_id,
            approved=request.approved,
            reviewer=request.reviewer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/cases/{case_id}/evidence/{evidence_clip_id}")
async def evidence_clip(case_id: str, evidence_clip_id: str) -> FileResponse:
    record = _case_or_404(case_id)
    if not record.investigation:
        raise HTTPException(status_code=404, detail="Evidence is not ready")
    clip = next(
        (
            candidate
            for candidate in record.investigation.evidence_clips
            if candidate.evidence_clip_id == evidence_clip_id
        ),
        None,
    )
    if clip is None:
        raise HTTPException(status_code=404, detail="Evidence clip not found")
    source = next(
        (
            video
            for video in record.videos
            if video.filename == clip.source_filename
        ),
        None,
    )
    if source is None or not source.local_path:
        raise HTTPException(status_code=404, detail="Source video not available")
    path = Path(source.local_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Source video not available")
    return FileResponse(
        path,
        media_type=source.media_type,
        filename=source.filename,
        content_disposition_type="inline",
    )


@app.get("/reports/{case_id}.pdf")
async def report(case_id: str) -> FileResponse:
    record = _case_or_404(case_id)
    if not record.report_path:
        raise HTTPException(
            status_code=409,
            detail="The report requires safety-manager approval",
        )
    path = Path(record.report_path)
    if not path.exists() and record.report_s3_uri:
        try:
            await asyncio.to_thread(
                aws_service.download_uri,
                record.report_s3_uri,
                path,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="Approved report could not be restored from S3",
            ) from exc
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report file not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{case_id}-investigation-report.pdf",
    )


@app.post("/invocations")
async def agentcore_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    """AgentCore-compatible JSON adapter for non-multipart workflow actions."""

    action = str(payload.get("action", "health"))
    if action == "health":
        return (await health()).model_dump(mode="json")
    case_id = str(payload.get("case_id", ""))
    if action == "get_case":
        return _case_or_404(case_id).model_dump(mode="json")
    if action == "investigate":
        return (await investigate(case_id)).model_dump(mode="json")
    if action == "approve":
        request = ApprovalRequest.model_validate(payload.get("approval") or {})
        return (await approve(case_id, request)).model_dump(mode="json")
    raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")
