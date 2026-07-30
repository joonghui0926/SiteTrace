"""Minimal AWS adapter for temporary hackathon infrastructure.

The backend keeps working locally when no bucket is configured.  In the
Workshop account, uploaded evidence and approved reports are copied to S3 while
Strands continues to own the investigation workflow and approval boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import settings


class AWSStorageService:
    def __init__(self) -> None:
        self._s3: Any | None = None
        self._sts: Any | None = None

    @property
    def available(self) -> bool:
        return settings.has_aws_storage

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3", region_name=settings.aws_region)
        return self._s3

    @property
    def sts(self) -> Any:
        if self._sts is None:
            import boto3

            self._sts = boto3.client("sts", region_name=settings.aws_region)
        return self._sts

    def upload_file(
        self,
        path: Path,
        *,
        key: str,
        content_type: str | None = None,
    ) -> str | None:
        if not self.available:
            return None
        extra_args = {"ContentType": content_type} if content_type else None
        kwargs: dict[str, Any] = {
            "Filename": str(path),
            "Bucket": settings.sitetrace_s3_bucket,
            "Key": key,
        }
        if extra_args:
            kwargs["ExtraArgs"] = extra_args
        self.s3.upload_file(**kwargs)
        return f"s3://{settings.sitetrace_s3_bucket}/{key}"

    def download_uri(self, uri: str, destination: Path) -> Path:
        if not self.available:
            raise RuntimeError("S3 storage is not configured")
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
            raise ValueError("Expected an s3://bucket/key URI")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.s3.download_file(
            Bucket=parsed.netloc,
            Key=parsed.path.lstrip("/"),
            Filename=str(destination),
        )
        return destination

    def health(self) -> dict[str, Any]:
        if not self.available:
            return {
                "configured": False,
                "bucket": False,
                "identity": False,
            }
        try:
            identity = self.sts.get_caller_identity()
            self.s3.head_bucket(Bucket=settings.sitetrace_s3_bucket)
            return {
                "configured": True,
                "bucket": True,
                "identity": bool(identity.get("Account")),
                "region": settings.aws_region,
            }
        except Exception as exc:
            return {
                "configured": True,
                "bucket": False,
                "identity": False,
                "region": settings.aws_region,
                "error": type(exc).__name__,
            }
