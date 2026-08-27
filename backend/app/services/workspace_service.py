"""Workspace service for persistent folder hierarchy and AWS S3-backed files."""

from __future__ import annotations

import base64
import os
import uuid
import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException, UploadFile
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from supabase import Client

from app.core.config import settings

# Module-level cache for column detection — persists for the lifetime of the process.
# This avoids re-probing 18 columns on EVERY request (was the main performance bottleneck).
_COLUMNS_CACHE: Dict[str, Optional[bool]] = {}
_PARENT_COLUMN_CACHE: Optional[str] = None
_COLUMNS_DETECTED: bool = False
_BUCKET_REGION_CACHE: Dict[str, str] = {}

logger = logging.getLogger(__name__)


class WorkspaceService:
    """Handles folder and file operations using Supabase database + AWS S3 storage."""

    def __init__(self, supabase: Client):
        self.supabase = supabase

    # ------------------------------------------------------------------
    # Column-detection helpers — use module-level cache so probes only
    # happen ONCE per process lifetime (not on every request).
    # ------------------------------------------------------------------

    def _column_exists(self, table: str, column: str) -> bool:
        """Check whether a column exists by probing with a safe select."""
        try:
            self.supabase.table(table).select(f"id,{column}").limit(1).execute()
            return True
        except Exception:
            return False

    def _detect_files_columns(self) -> None:
        """Populate the module-level column cache (runs exactly once per process).

        A sample row can be older and omit newer optional columns such as
        parent_file_id even when the live files table supports them. We therefore
        treat the first row as a hint only and still probe each column directly before
        finalizing the capability cache.
        """
        global _COLUMNS_DETECTED, _COLUMNS_CACHE
        if _COLUMNS_DETECTED:
            return

        columns = [
            "storage_path", "storage_url", "file_url", "size_bytes", "mime_type",
            "original_filename", "file_type", "user_id", "folder_id", "name",
            "parent_file_id", "file_content", "raw_text", "summary",
            "content", "text", "last_accessed", "updated_at",
        ]

        row_keys: Optional[set[str]] = None
        try:
            res = self.supabase.table("files").select("*").limit(1).execute()
            if res.data:
                row_keys = set(res.data[0].keys())
        except Exception:
            row_keys = None

        def check_col(col: str) -> Tuple[str, bool]:
            if row_keys is not None and col in row_keys:
                return col, True
            return col, self._column_exists("files", col)

        # Execute sequentially to avoid concurrent HTTP client errors with Supabase
        for col in columns:
            _, exists = check_col(col)
            _COLUMNS_CACHE[col] = exists

        if any(_COLUMNS_CACHE.values()):
            _COLUMNS_DETECTED = True

    # Convenience properties that read from the module-level cache.
    @property
    def _files_has_storage_path(self) -> bool: return _COLUMNS_CACHE.get("storage_path", False)
    @property
    def _files_has_storage_url(self) -> bool: return _COLUMNS_CACHE.get("storage_url", False)
    @property
    def _files_has_file_url(self) -> bool: return _COLUMNS_CACHE.get("file_url", False)
    @property
    def _files_has_size_bytes(self) -> bool: return _COLUMNS_CACHE.get("size_bytes", False)
    @property
    def _files_has_mime_type(self) -> bool: return _COLUMNS_CACHE.get("mime_type", False)
    @property
    def _files_has_original_filename(self) -> bool: return _COLUMNS_CACHE.get("original_filename", False)
    @property
    def _files_has_file_type(self) -> bool: return _COLUMNS_CACHE.get("file_type", False)
    @property
    def _files_has_user_id(self) -> bool: return _COLUMNS_CACHE.get("user_id", False)
    @property
    def _files_has_folder_id(self) -> bool: return _COLUMNS_CACHE.get("folder_id", False)
    @property
    def _files_has_name(self) -> bool: return _COLUMNS_CACHE.get("name", False)
    @property
    def _files_has_parent_file_id(self) -> bool: return _COLUMNS_CACHE.get("parent_file_id", False)
    @property
    def _files_has_file_content(self) -> bool: return _COLUMNS_CACHE.get("file_content", False)
    @property
    def _files_has_raw_text(self) -> bool: return _COLUMNS_CACHE.get("raw_text", False)
    @property
    def _files_has_summary(self) -> bool: return _COLUMNS_CACHE.get("summary", False)
    @property
    def _files_has_content(self) -> bool: return _COLUMNS_CACHE.get("content", False)
    @property
    def _files_has_text(self) -> bool: return _COLUMNS_CACHE.get("text", False)
    @property
    def _files_has_last_accessed(self) -> bool: return _COLUMNS_CACHE.get("last_accessed", False)
    @property
    def _files_has_updated_at(self) -> bool: return _COLUMNS_CACHE.get("updated_at", False)

    def _extract_storage_key(self, file_row: Dict[str, Any]) -> Optional[str]:
        """Derive object key from known metadata fields."""
        _, storage_key = self._extract_storage_location(file_row)
        return storage_key

    @staticmethod
    def _extract_bucket_and_key_from_url(value: str) -> Tuple[Optional[str], Optional[str]]:
        parsed = urlparse(str(value))
        path = parsed.path or ""

        host = (parsed.netloc or "").split(":", 1)[0].lower()
        if host.endswith("amazonaws.com"):
            if ".s3." in host:
                bucket = host.split(".s3.", 1)[0]
                object_key = unquote(path.lstrip("/"))
                if bucket and object_key:
                    return bucket, object_key
            if host.startswith("s3.") or host == "s3.amazonaws.com":
                parts = unquote(path.lstrip("/")).split("/", 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    return parts[0], parts[1]

        for marker in (
            "/storage/v1/object/public/",
            "/storage/v1/object/sign/",
            "/storage/v1/object/authenticated/",
        ):
            if marker in path:
                object_path = unquote(path.split(marker, 1)[1].lstrip("/"))
                parts = object_path.split("/", 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    return parts[0], parts[1]
        return None, None

    def _normalize_storage_reference(self, value: Any) -> Tuple[Optional[str], Optional[str]]:
        """Normalize storage metadata into (bucket, object_key) when possible."""
        normalized = str(value or "").strip()
        if not normalized:
            return None, None

        if normalized.startswith("data:"):
            return None, normalized

        if normalized.startswith("s3://"):
            remainder = normalized[5:]
            parts = remainder.split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                return parts[0], parts[1]

        if normalized.startswith("http://") or normalized.startswith("https://"):
            bucket, object_key = self._extract_bucket_and_key_from_url(normalized)
            if object_key:
                return bucket, object_key

        path = normalized
        if "?" in path:
            path = path.split("?", 1)[0]
        if "#" in path:
            path = path.split("#", 1)[0]
        path = unquote(path).lstrip("/")

        if not path:
            return None, None

        if path.startswith("storage/v1/object/"):
            parts = path.split("/")
            if len(parts) >= 6:
                bucket = parts[4].strip()
                object_key = "/".join(parts[5:]).strip("/")
                if bucket and object_key:
                    return bucket, object_key

        for bucket in self._candidate_buckets(parsed_bucket=None):
            prefix = f"{bucket}/"
            if path.startswith(prefix):
                object_key = path[len(prefix):].strip("/")
                if object_key:
                    return bucket, object_key

        return None, path

    def _extract_storage_location(self, file_row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Derive (bucket, object_key) from common metadata patterns."""
        for key_field in ("storage_path", "file_url", "storage_url", "s3_key", "object_key", "path"):
            value = file_row.get(key_field)
            if not value:
                continue

            bucket, object_key = self._normalize_storage_reference(value)
            if object_key:
                return bucket, object_key

        storage_url = file_row.get("storage_url")
        if storage_url:
            bucket, object_key = self._normalize_storage_reference(storage_url)
            if object_key:
                return bucket, object_key

        return None, None

    def _candidate_buckets(self, parsed_bucket: Optional[str]) -> List[str]:
        buckets: List[str] = []
        for candidate in (
            parsed_bucket,
            self._workspace_bucket_name(),
        ):
            if candidate and candidate not in buckets:
                buckets.append(candidate)
        return buckets

    def _row_matches_folder(self, row: Dict[str, Any], folder_id: str) -> bool:
        """Best-effort folder match for schemas missing folder_id."""
        if str(row.get("folder_id") or "") == str(folder_id):
            return True

        storage_path = str(row.get("storage_path") or "")
        if storage_path and f"/{folder_id}/" in storage_path:
            return True

        return False

    def _workspace_bucket_name(self) -> str:
        bucket = str(settings.aws_s3_bucket or "").strip()
        if not bucket:
            raise HTTPException(status_code=500, detail="AWS S3 bucket is not configured")
        return bucket

    def _aws_storage_configured(self) -> bool:
        return bool(settings.aws_access_key_id and settings.aws_secret_access_key and settings.aws_s3_bucket)

    def _get_bucket_region(self, bucket: str) -> str:
        if bucket in _BUCKET_REGION_CACHE:
            return _BUCKET_REGION_CACHE[bucket]
        default_region = settings.aws_region or "eu-north-1"
        try:
            client = boto3.client(
                "s3",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=default_region,
            )
            response = client.get_bucket_location(Bucket=bucket)
            location = response.get("LocationConstraint")
            region = location if location else "us-east-1"
            _BUCKET_REGION_CACHE[bucket] = region
            return region
        except Exception:
            return default_region

    def _s3_client(self, bucket: Optional[str] = None):
        if not self._aws_storage_configured():
            raise HTTPException(status_code=500, detail="AWS S3 credentials are not configured")

        region = settings.aws_region
        if bucket:
            region = self._get_bucket_region(bucket)

        return boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=region,
        )

    @staticmethod
    def _s3_object_url(bucket: str, object_key: str) -> str:
        encoded_key = quote(str(object_key).lstrip("/"), safe="/")
        return f"https://{bucket}.s3.{settings.aws_region}.amazonaws.com/{encoded_key}"

    def _upload_to_s3(self, bucket: str, object_key: str, content: bytes, mime_type: str) -> str:
        try:
            self._s3_client(bucket).put_object(
                Bucket=bucket,
                Key=object_key,
                Body=content,
                ContentType=mime_type,
            )
        except ClientError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to upload file to S3: {exc}") from exc
        return self._s3_object_url(bucket, object_key)

    def _delete_from_s3(self, bucket: str, object_key: Optional[str]) -> None:
        if not object_key or (isinstance(object_key, str) and object_key.startswith("data:")):
            return
        try:
            self._s3_client(bucket).delete_object(Bucket=bucket, Key=str(object_key).lstrip("/"))
        except ClientError:
            # Continue DB cleanup even when the storage object is already gone.
            pass

    def _move_s3_object(self, bucket: str, source_key: str, destination_key: str) -> None:
        source_key = str(source_key or "").lstrip("/")
        destination_key = str(destination_key or "").lstrip("/")
        if not source_key or not destination_key or source_key == destination_key:
            return

        client = self._s3_client(bucket)
        client.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": source_key},
            Key=destination_key,
            MetadataDirective="COPY",
        )
        client.delete_object(Bucket=bucket, Key=source_key)

    def _signed_url_from_s3(self, bucket: str, object_key: str, expires_in: int) -> Optional[str]:
        try:
            normalized_key = str(object_key or "").strip().lstrip("/")
            if not normalized_key:
                return None

            return self._s3_client(bucket).generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": bucket, "Key": normalized_key},
                ExpiresIn=expires_in,
            )
        except ClientError:
            return None
        except Exception:
            return None

    def get_file_object_bytes(self, user_id: str, file_id: str) -> bytes:
        """Fetch the original file payload from S3 or the database fallback if present."""
        self._detect_files_columns()

        query = self.supabase.table("files").select("*").eq("id", file_id).limit(1)
        if self._files_has_user_id:
            query = query.eq("user_id", user_id)
        existing = query.execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="File not found")

        row = existing.data[0]
        mime = str(row.get("mime_type") or row.get("file_type") or "").lower()
        is_binary_doc = any(
            token in mime
            for token in (
                "pdf", "powerpoint", "presentation", "msword",
                "openxmlformats-officedocument", "octet-stream",
            )
        )

        # Resolve raw storage bytes from S3 (the original file payload).
        def _resolve_storage_bytes():
            storage_bucket, storage_key = self._extract_storage_location(row)
            if storage_key and not str(storage_key).startswith("data:"):
                for candidate_bucket in self._candidate_buckets(storage_bucket):
                    try:
                        obj = self._s3_client(candidate_bucket).get_object(
                            Bucket=candidate_bucket, Key=str(storage_key).lstrip("/")
                        )
                        body = obj.get("Body")
                        if body is not None:
                            return body.read()
                    except (ClientError, Exception):
                        continue
            return None

        def _resolve_text_columns() -> Optional[bytes]:
            for key in ("file_content", "raw_text", "summary", "content", "text"):
                value = row.get(key)
                if isinstance(value, bytes):
                    return value
                if isinstance(value, str):
                    return value.encode("utf-8")
            return None

        # Binary documents (PDF/PPTX/DOCX) MUST return the real file bytes — text
        # columns are extracted summaries / annotations, NOT valid PDF structure.
        # Drag-and-drop rebuilds PDFs from this endpoint, so returning DB text as
        # if it were the PDF is what caused quizzes about "1 0 obj" / "Type/Catalog"
        # / font names. Real bytes or a clean failure — never DB text for binaries.
        if is_binary_doc:
            storage_bytes = _resolve_storage_bytes()
            if storage_bytes is not None:
                return storage_bytes
            # Do NOT fall through to _resolve_text_columns() for binary documents.
            raise HTTPException(
                status_code=404,
                detail="The original file could not be loaded from storage. "
                       "Please upload it directly from your computer.",
            )

        # Non-binary docs (text notes / .txt) may legitimately live in DB text columns.
        text_bytes = _resolve_text_columns()
        if text_bytes is not None:
            return text_bytes
        storage_bytes = _resolve_storage_bytes()
        if storage_bytes is not None:
            return storage_bytes

        raise HTTPException(status_code=404, detail="File bytes could not be resolved from storage or database content")

    def get_file_content(self, user_id: str, file_id: str) -> Dict[str, Any]:
        """Fetch raw file bytes plus filename/mime metadata, proxied through the backend.

        This exists so the frontend never has to hit S3 directly from the browser
        (which requires the bucket to have CORS configured for every deployment
        origin, and breaks with a generic "Failed to fetch" when it isn't). Instead
        the browser calls this same-origin, authenticated endpoint and the backend
        does the S3 (or DB-fallback) read itself.
        """
        content = self.get_file_object_bytes(user_id=user_id, file_id=file_id)

        self._detect_files_columns()
        file_query = self.supabase.table("files").select("*").eq("id", file_id).limit(1)
        if self._files_has_user_id:
            file_query = file_query.eq("user_id", user_id)
        existing = file_query.execute()
        row = existing.data[0] if existing.data else {}

        filename = row.get("name") or row.get("original_filename") or file_id
        mime_type = row.get("mime_type") or "application/octet-stream"

        return {"content": content, "filename": filename, "mime_type": mime_type}

    @staticmethod
    def _build_tree(flat_folders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        lookup: Dict[str, Dict[str, Any]] = {}
        roots: List[Dict[str, Any]] = []

        for folder in flat_folders:
            node = dict(folder)
            node["children"] = []
            lookup[str(node["id"])] = node

        for node in lookup.values():
            parent_id = node.get("parent_folder_id") or node.get("parent_id")
            if parent_id:
                parent = lookup.get(str(parent_id))
                if parent:
                    parent["children"].append(node)
                else:
                    roots.append(node)
            else:
                roots.append(node)

        return roots

    def _detect_parent_column(self) -> Optional[str]:
        """Detect which parent folder column exists in the folders table."""
        global _PARENT_COLUMN_CACHE
        if _PARENT_COLUMN_CACHE is not None:
            return _PARENT_COLUMN_CACHE if _PARENT_COLUMN_CACHE != "__none__" else None

        try:
            res = self.supabase.table("folders").select("*").limit(1).execute()
            if res.data:
                keys = set(res.data[0].keys())
                if "parent_folder_id" in keys:
                    _PARENT_COLUMN_CACHE = "parent_folder_id"
                    return _PARENT_COLUMN_CACHE
                if "parent_id" in keys:
                    _PARENT_COLUMN_CACHE = "parent_id"
                    return _PARENT_COLUMN_CACHE
                _PARENT_COLUMN_CACHE = "__none__"
                return None
        except Exception:
            pass

        try:
            self.supabase.table("folders").select("id,parent_folder_id").limit(1).execute()
            _PARENT_COLUMN_CACHE = "parent_folder_id"
            return _PARENT_COLUMN_CACHE
        except Exception:
            pass

        try:
            self.supabase.table("folders").select("id,parent_id").limit(1).execute()
            _PARENT_COLUMN_CACHE = "parent_id"
            return _PARENT_COLUMN_CACHE
        except Exception:
            _PARENT_COLUMN_CACHE = "__none__"
            return None

    def list_folders(self, user_id: str) -> Dict[str, Any]:
        try:
            response = (
                self.supabase.table("folders")
                .select("*")
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .execute()
            )
            flat = response.data or []
            return {"folders": self._build_tree(flat), "flat_folders": flat}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to fetch folders: {exc}") from exc

    def create_folder(self, user_id: str, name: str, parent_folder_id: Optional[str] = None) -> Dict[str, Any]:
        if not name or not name.strip():
            raise HTTPException(status_code=400, detail="Folder name is required")

        parent_column = self._detect_parent_column()

        payload = {
            "name": name.strip(),
            "user_id": user_id,
        }
        if parent_column:
            payload[parent_column] = parent_folder_id
        elif parent_folder_id:
            raise HTTPException(
                status_code=400,
                detail="Nested folders are not enabled in your database schema (missing parent folder column).",
            )

        try:
            if parent_folder_id:
                parent = (
                    self.supabase.table("folders")
                    .select("id")
                    .eq("id", parent_folder_id)
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                )
                if not parent.data:
                    raise HTTPException(status_code=404, detail="Parent folder not found")

            created = self.supabase.table("folders").insert(payload).execute()
            if not created.data:
                raise HTTPException(status_code=500, detail="Folder creation returned no data")
            return created.data[0]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to create folder: {exc}") from exc

    def rename_folder(self, user_id: str, folder_id: str, new_name: str) -> Dict[str, Any]:
        if not new_name or not new_name.strip():
            raise HTTPException(status_code=400, detail="New folder name is required")

        try:
            updated = (
                self.supabase.table("folders")
                .update({"name": new_name.strip()})
                .eq("id", folder_id)
                .eq("user_id", user_id)
                .execute()
            )
            if not updated.data:
                raise HTTPException(status_code=404, detail="Folder not found")
            return updated.data[0]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to rename folder: {exc}") from exc

    def delete_folder(self, user_id: str, folder_id: str) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            folder = (
                self.supabase.table("folders")
                .select("id")
                .eq("id", folder_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if not folder.data:
                raise HTTPException(status_code=404, detail="Folder not found")

            all_folders = self.supabase.table("folders").select("*").eq("user_id", user_id).execute().data or []

            child_map: Dict[str, List[str]] = {}
            for item in all_folders:
                parent = item.get("parent_folder_id") or item.get("parent_id")
                if parent:
                    child_map.setdefault(str(parent), []).append(str(item["id"]))

            to_delete = [str(folder_id)]
            stack = [str(folder_id)]
            while stack:
                current = stack.pop()
                for child in child_map.get(current, []):
                    to_delete.append(child)
                    stack.append(child)

            files_query = self.supabase.table("files").select("*")
            if self._files_has_user_id:
                files_query = files_query.eq("user_id", user_id)
            if self._files_has_folder_id:
                files_query = files_query.in_("folder_id", to_delete)
            files = files_query.execute().data or []

            if not self._files_has_folder_id:
                files = [
                    row for row in files
                    if any(self._row_matches_folder(row, candidate) for candidate in to_delete)
                ]

            bucket = self._workspace_bucket_name()
            for file_row in files:
                parsed_bucket, storage_key = self._extract_storage_location(file_row)
                for candidate_bucket in self._candidate_buckets(parsed_bucket):
                    self._delete_from_s3(candidate_bucket, storage_key)

            # Never run an unscoped DELETE; delete by explicit IDs if available.
            file_ids_to_delete = [str(row.get("id")) for row in files if row.get("id")]
            if file_ids_to_delete:
                delete_files_query = self.supabase.table("files").delete().in_("id", file_ids_to_delete)
                if self._files_has_user_id:
                    delete_files_query = delete_files_query.eq("user_id", user_id)
                delete_files_query.execute()
            self.supabase.table("folders").delete().eq("user_id", user_id).in_("id", to_delete).execute()

            return {"deleted_folder_ids": to_delete, "deleted_files": len(files)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete folder: {exc}") from exc

    async def upload_file(self, user_id: str, folder_id: str, file: UploadFile) -> Dict[str, Any]:
        if not file.filename:
            raise HTTPException(status_code=400, detail="File name is required")

        try:
            folder = (
                self.supabase.table("folders")
                .select("id,name")
                .eq("id", folder_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if not folder.data:
                raise HTTPException(status_code=404, detail="Folder not found")

            uploaded_file_size = getattr(file, "size", None)
            if uploaded_file_size is None:
                try:
                    current_position = file.file.tell()
                    file.file.seek(0, os.SEEK_END)
                    uploaded_file_size = file.file.tell()
                    file.file.seek(current_position)
                except (AttributeError, OSError):
                    uploaded_file_size = None

            if uploaded_file_size is not None:
                storage_usage = self.get_storage_usage(user_id)
                storage_limit_bytes = int(storage_usage["storage_limit_bytes"])
                storage_used_bytes = int(storage_usage["storage_used_bytes"])
                uploaded_file_size = int(uploaded_file_size)

                if storage_used_bytes + uploaded_file_size > storage_limit_bytes:
                    plan_name = storage_usage.get("plan_name") or storage_usage.get("plan_code", "Free")
                    limit_mb = storage_usage.get("storage_limit_mb")
                    used_mb = storage_usage.get("storage_used_mb")
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Storage limit exceeded. Your {plan_name} plan allows "
                            f"{limit_mb:g} MB of total storage. You currently use {used_mb:g} MB."
                        ),
                    )

            content = await file.read()
            if not content:
                raise HTTPException(status_code=400, detail="Uploaded file is empty")

            ext = os.path.splitext(file.filename)[1].lower()
            file_key = f"workspace/{user_id}/{folder_id}/{uuid.uuid4().hex}{ext}"
            mime_type = file.content_type or "application/octet-stream"
            bucket = self._workspace_bucket_name()
            try:
                self._upload_to_s3(
                    bucket=bucket,
                    object_key=file_key,
                    content=content,
                    mime_type=mime_type,
                )
            except HTTPException:
                raise

            self._detect_files_columns()

            metadata = {}

            if self._files_has_user_id:
                metadata["user_id"] = user_id
            if self._files_has_folder_id:
                metadata["folder_id"] = folder_id
            if self._files_has_name:
                metadata["name"] = os.path.splitext(file.filename)[0]

            if self._files_has_original_filename:
                metadata["original_filename"] = file.filename
            if self._files_has_mime_type:
                metadata["mime_type"] = file.content_type
            if self._files_has_size_bytes:
                metadata["size_bytes"] = len(content)
            if self._files_has_file_type:
                metadata["file_type"] = ext.replace(".", "").upper() if ext else "FILE"
            if self._files_has_storage_path:
                metadata["storage_path"] = file_key
            if self._files_has_file_url:
                metadata["file_url"] = file_key
            if self._files_has_storage_url:
                metadata["storage_url"] = file_key
            now_iso = datetime.now(timezone.utc).isoformat()
            if self._files_has_last_accessed:
                metadata["last_accessed"] = now_iso
            if self._files_has_updated_at:
                metadata["updated_at"] = now_iso

            if not metadata:
                metadata["name"] = os.path.splitext(file.filename)[0]

            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"[FILES] Inserting file: {metadata.get('name')}")
            logger.info(f"[FILES] file_url: {metadata.get('file_url')}")

            saved = self.supabase.table("files").insert(metadata).execute()
            if not saved.data:
                self._delete_from_s3(bucket, file_key)
                raise HTTPException(status_code=500, detail="File metadata insert failed")
            inserted = saved.data[0]
            logger.info(f"[FILES] Created file id: {inserted.get('id')}")

            return inserted
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"File upload failed: {exc}") from exc

    def delete_file(self, user_id: str, file_id: str) -> Dict[str, Any]:
        try:
            self._detect_files_columns()
            
            file_query = self.supabase.table("files").select("*").eq("id", file_id).limit(1)
            if self._files_has_user_id:
                file_query = file_query.eq("user_id", user_id)
            existing = file_query.execute()
            if not existing.data:
                raise HTTPException(status_code=404, detail="File not found")
                
            file_row = existing.data[0]
            
            bucket, storage_key = self._extract_storage_location(file_row)
            if bucket and storage_key:
                self._delete_from_s3(bucket, storage_key)
                
            delete_query = self.supabase.table("files").delete().eq("id", file_id)
            if self._files_has_user_id:
                delete_query = delete_query.eq("user_id", user_id)
            delete_query.execute()
            
            return {"file_id": file_id}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}") from exc

    def list_files(self, user_id: str, folder_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            sort_column = "created_at"
            if self._files_has_last_accessed:
                sort_column = "last_accessed"
            elif self._files_has_updated_at:
                sort_column = "updated_at"

            columns_to_select = []
            skip_columns = {"file_content", "raw_text", "summary", "content", "text"}
            for col, exists in _COLUMNS_CACHE.items():
                if exists and col not in skip_columns:
                    columns_to_select.append(col)
            
            if "id" not in columns_to_select: columns_to_select.append("id")
            if "created_at" not in columns_to_select: columns_to_select.append("created_at")

            select_str = ",".join(set(columns_to_select))

            query = (
                self.supabase.table("files")
                .select(select_str)
                .order(sort_column, desc=True)
            )
            if self._files_has_user_id:
                query = query.eq("user_id", user_id)
            if folder_id:
                if self._files_has_folder_id:
                    query = query.eq("folder_id", folder_id)

            response = query.execute()
            rows = response.data or []

            if folder_id and not self._files_has_folder_id:
                rows = [row for row in rows if self._row_matches_folder(row, folder_id)]

            # Fetch notes from supabase notes table
            try:
                notes_query = self.supabase.table("notes").select("note_id, title, folder_id, created_at, updated_at, note_type")
                if folder_id:
                    notes_query = notes_query.eq("folder_id", folder_id)
                notes_query = notes_query.eq("user_id", user_id)
                notes_response = notes_query.execute()
                notes_rows = notes_response.data or []
                
                normalized_notes = []
                for n in notes_rows:
                    note_type = (n.get("note_type") or "note").upper()
                    normalized_notes.append({
                        "id": n.get("note_id"),
                        "name": n.get("title") or "Untitled Note",
                        "original_filename": n.get("title") or "Untitled Note",
                        "folder_id": n.get("folder_id"),
                        "parent_file_id": None,
                        "created_at": n.get("created_at"),
                        "updated_at": n.get("updated_at"),
                        "file_type": note_type,
                        "mime_type": "text/markdown",
                        "storage_url": "",
                        "storage_path": "",
                        "is_note": True,
                    })
                rows.extend(normalized_notes)
            except Exception as notes_err:
                logger.warning("Failed fetching notes in list_files: %s", notes_err)

            return {"files": rows}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to fetch files: {exc}") from exc

    def get_storage_usage(self, user_id: str) -> Dict[str, Any]:
        """Return the authenticated user's plan and current file storage usage."""
        plan_code = "free"
        plan_name = "Free"
        storage_limit_bytes = 0
        subscription = None

        try:
            free_plan_response = (
                self.supabase.table("plans")
                .select("code,name,storage_limit_bytes")
                .eq("code", "free")
                .limit(1)
                .execute()
            )
            free_plan = (free_plan_response.data or [None])[0]
            if free_plan:
                plan_code = free_plan.get("code") or plan_code
                plan_name = free_plan.get("name") or plan_name
                storage_limit_bytes = int(free_plan.get("storage_limit_bytes") or 0)

            now = datetime.now(timezone.utc).isoformat()
            subscription_response = (
                self.supabase.table("subscriptions")
                .select("plan_id,status,starts_at,expires_at,provider_reference")
                .eq("user_id", user_id)
                .eq("status", "active")
                .lte("starts_at", now)
                .gt("expires_at", now)
                .order("expires_at", desc=True)
                .limit(1)
                .execute()
            )
            subscription = (subscription_response.data or [None])[0]

            if subscription and subscription.get("plan_id"):
                plan_response = (
                    self.supabase.table("plans")
                    .select("code,name,storage_limit_bytes")
                    .eq("id", subscription["plan_id"])
                    .limit(1)
                    .execute()
                )
                plan = (plan_response.data or [None])[0]
                if plan:
                    plan_code = plan.get("code") or plan_code
                    plan_name = plan.get("name") or plan_name
                    storage_limit_bytes = int(plan.get("storage_limit_bytes") or storage_limit_bytes)
        except Exception:
            # Plan lookup must not prevent a user from seeing Free-plan usage,
            # but log it so missing plans/subscriptions tables are visible instead
            # of silently degrading every user to Free (root cause of the
            # "payment completes but plan stays free" bug).
            logger.exception("[PLANS] Plan/subscription lookup failed; defaulting to Free plan")

        try:
            self._detect_files_columns()
            if not self._files_has_size_bytes:
                storage_used_bytes = 0
            else:
                files_response = (
                    self.supabase.table("files")
                    .select("size_bytes")
                    .eq("user_id", user_id)
                    .execute()
                )
                storage_used_bytes = sum(
                    int(file_row.get("size_bytes") or 0)
                    for file_row in (files_response.data or [])
                )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to calculate storage usage: {exc}") from exc

        storage_remaining_bytes = max(storage_limit_bytes - storage_used_bytes, 0)
        bytes_per_mb = 1024 * 1024
        bytes_per_gb = 1024 * bytes_per_mb

        return {
            "is_pro": plan_code.lower() == "pro",
            "plan_code": plan_code,
            "plan_name": plan_name,
            "storage_limit_bytes": storage_limit_bytes,
            "storage_used_bytes": storage_used_bytes,
            "storage_remaining_bytes": storage_remaining_bytes,
            "subscription_status": "active" if subscription else "inactive",
            "subscription_starts_at": subscription.get("starts_at") if subscription else None,
            "subscription_expires_at": subscription.get("expires_at") if subscription else None,
            "provider_reference": subscription.get("provider_reference") if subscription else None,
            "storage_limit_mb": round(storage_limit_bytes / bytes_per_mb, 2),
            "storage_limit_gb": round(storage_limit_bytes / bytes_per_gb, 2),
            "storage_used_mb": round(storage_used_bytes / bytes_per_mb, 2),
            "storage_used_gb": round(storage_used_bytes / bytes_per_gb, 2),
            "storage_remaining_mb": round(storage_remaining_bytes / bytes_per_mb, 2),
            "storage_remaining_gb": round(storage_remaining_bytes / bytes_per_gb, 2),
        }

    def update_file_access(self, user_id: str, file_id: str) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            existing_query = self.supabase.table("files").select("*").eq("id", file_id).limit(1)
            if self._files_has_user_id:
                existing_query = existing_query.eq("user_id", user_id)
            existing = existing_query.execute()
            if not existing.data:
                raise HTTPException(status_code=404, detail="File not found")

            payload: Dict[str, Any] = {}
            now = datetime.now(timezone.utc).isoformat()
            if self._files_has_last_accessed:
                payload["last_accessed"] = now
            elif self._files_has_updated_at:
                payload["updated_at"] = now
            else:
                return existing.data[0]

            update_query = self.supabase.table("files").update(payload).eq("id", file_id)
            if self._files_has_user_id:
                update_query = update_query.eq("user_id", user_id)
            updated = update_query.execute()
            if not updated.data:
                raise HTTPException(status_code=404, detail="File not found")
            return updated.data[0]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to update file access: {exc}") from exc

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    @classmethod
    def _row_recent_timestamp(cls, row: Dict[str, Any]) -> datetime:
        for key in ("last_accessed", "updated_at", "created_at"):
            parsed = cls._parse_timestamp(row.get(key))
            if parsed:
                return parsed
        return datetime.fromtimestamp(0, tz=timezone.utc)

    @staticmethod
    def _build_folder_path(folder_lookup: Dict[str, Dict[str, Any]], folder_id: Optional[str]) -> Optional[str]:
        if not folder_id:
            return None

        parts: List[str] = []
        current = folder_lookup.get(str(folder_id))
        while current:
            name = current.get("name")
            if name:
                parts.append(str(name))
            parent_id = current.get("parent_folder_id") or current.get("parent_id")
            current = folder_lookup.get(str(parent_id)) if parent_id else None

        if not parts:
            return None

        return " → ".join(reversed(parts))

    def list_recent_files(self, user_id: str, limit: int = 5) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            columns_to_select = []
            skip_columns = {"file_content", "raw_text", "summary", "content", "text"}
            for col, exists in _COLUMNS_CACHE.items():
                if exists and col not in skip_columns:
                    columns_to_select.append(col)
            
            if "id" not in columns_to_select: columns_to_select.append("id")
            if "created_at" not in columns_to_select: columns_to_select.append("created_at")

            select_str = ",".join(set(columns_to_select))

            sort_column = "created_at"
            if self._files_has_last_accessed:
                sort_column = "last_accessed"
            elif self._files_has_updated_at:
                sort_column = "updated_at"

            safe_limit = max(1, min(int(limit or 5), 50))
            
            files_query = (
                self.supabase.table("files")
                .select(select_str)
                .order(sort_column, desc=True)
                .limit(safe_limit)
            )
            
            if self._files_has_user_id:
                files_query = files_query.eq("user_id", user_id)
            files = files_query.execute().data or []

            folders_query = self.supabase.table("folders").select("*").eq("user_id", user_id)
            folders = folders_query.execute().data or []
            folder_lookup = {str(folder["id"]): folder for folder in folders if folder.get("id")}
            bucket = self._workspace_bucket_name()

            # Since we sorted in DB, we can just use the results directly, but we sort locally again just in case the DB sort column didn't exactly match _row_recent_timestamp
            limited = sorted(files, key=self._row_recent_timestamp, reverse=True)
            recent_files = []
            for row in limited:
                folder_id = row.get("folder_id")
                folder = folder_lookup.get(str(folder_id)) if folder_id else None
                parent_id = folder.get("parent_folder_id") if folder else None
                _, storage_key = self._extract_storage_location(row)
                preview_available = bool(
                    row.get("storage_url")
                    or row.get("file_url")
                    or (isinstance(row.get("storage_path"), str) and str(row.get("storage_path")).startswith("data:"))
                    or storage_key
                    or self._files_has_file_content
                )
                recent_files.append({
                    **row,
                    "folder_name": folder.get("name") if folder else None,
                    "folder_path": self._build_folder_path(folder_lookup, folder_id),
                    "parent_folder_path": self._build_folder_path(folder_lookup, parent_id),
                    "recent_timestamp": self._row_recent_timestamp(row).isoformat(),
                    "preview_available": preview_available,
                })

            return {"files": recent_files}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to fetch recent files: {exc}") from exc

    def get_file_preview(self, user_id: str, file_id: str, expires_in: int = 3600) -> Dict[str, Any]:
        """Resolve a preview URL (or text content fallback) for a file."""
        try:
            self._detect_files_columns()

            file_query = self.supabase.table("files").select("*").eq("id", file_id).limit(1)
            if self._files_has_user_id:
                file_query = file_query.eq("user_id", user_id)
            existing = file_query.execute()
            if not existing.data:
                raise HTTPException(status_code=404, detail="File not found")

            row = existing.data[0]

            def _first_non_empty(*keys: str) -> Any:
                for key in keys:
                    value = row.get(key)
                    if isinstance(value, str):
                        if value.strip():
                            return value
                    elif value is not None:
                        return value
                return None

            parsed_bucket, storage_key = self._extract_storage_location(row)
            if storage_key and isinstance(storage_key, str) and storage_key.startswith("data:"):
                return {
                    "file_id": file_id,
                    "preview_url": storage_key,
                    "content": None,
                    "mime_type": row.get("mime_type"),
                    "source": "storage_path_data_url",
                }

            if storage_key:
                safe_expiry = max(60, min(int(expires_in or 3600), 86400))
                for candidate_bucket in self._candidate_buckets(parsed_bucket):
                    preview_url = self._signed_url_from_s3(candidate_bucket, storage_key, safe_expiry)
                    if not preview_url:
                        preview_url = self._s3_object_url(candidate_bucket, storage_key)

                    if preview_url:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.info(f"[PREVIEW] file_url: {storage_key}")
                        logger.info(f"[PREVIEW] Generated signed URL: {preview_url}")
                        return {
                            "file_id": file_id,
                            "preview_url": preview_url,
                            "content": None,
                            "mime_type": row.get("mime_type"),
                            "source": "aws_s3",
                        }

            storage_url = _first_non_empty("storage_url", "file_url", "url", "public_url")
            if storage_url:
                if isinstance(storage_url, str) and (storage_url.startswith("http://") or storage_url.startswith("https://") or storage_url.startswith("data:")):
                    return {
                        "file_id": file_id,
                        "preview_url": storage_url,
                        "content": None,
                        "mime_type": row.get("mime_type"),
                        "source": "storage_url",
                    }
                # If storage_url is a relative S3 key (e.g. "workspace/..."), generate a presigned S3 URL
                safe_expiry = max(60, min(int(expires_in or 3600), 86400))
                for candidate_bucket in self._candidate_buckets(None):
                    presigned = self._signed_url_from_s3(candidate_bucket, storage_url, safe_expiry)
                    if presigned:
                        import logging
                        logger = logging.getLogger(__name__)
                        logger.info(f"[PREVIEW] file_url: {storage_url}")
                        logger.info(f"[PREVIEW] Generated signed URL: {presigned}")
                        return {
                            "file_id": file_id,
                            "preview_url": presigned,
                            "content": None,
                            "mime_type": row.get("mime_type"),
                            "source": "aws_s3",
                        }

            text_content = _first_non_empty("file_content", "raw_text", "summary", "content", "text")
            if text_content is not None:
                content = text_content
                if isinstance(content, str) and content.startswith("data:"):
                    return {
                        "file_id": file_id,
                        "preview_url": content,
                        "content": None,
                        "mime_type": row.get("mime_type"),
                        "source": "database_data_url",
                    }
                return {
                    "file_id": file_id,
                    "preview_url": None,
                    "content": content,
                    "mime_type": row.get("mime_type"),
                    "source": "database",
                }

            # DEBUG LOGGING to diagnose why file has no content
            import logging
            logger = logging.getLogger(__name__)
            keys = ["file_content", "raw_text", "summary", "content", "text", "storage_url", "storage_path"]
            found = {k: bool(row.get(k)) for k in keys}
            logger.error(f"Preview 404 for file {file_id}. Name: {row.get('name')}. Found keys: {found}")

            raise HTTPException(
                status_code=404,
                detail="Preview not available: file has no readable content or storage reference.",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to resolve file preview: {exc}") from exc

    def move_file(self, user_id: str, file_id: str, folder_id: str) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            folder = (
                self.supabase.table("folders")
                .select("id")
                .eq("id", folder_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            if not folder.data:
                raise HTTPException(status_code=404, detail="Target folder not found")

            if not self._files_has_folder_id:
                raise HTTPException(
                    status_code=400,
                    detail="This database schema does not support moving files between folders (missing files.folder_id).",
                )

            existing_query = self.supabase.table("files").select("*").eq("id", file_id)
            if self._files_has_user_id:
                existing_query = existing_query.eq("user_id", user_id)
            existing = existing_query.limit(1).execute()
            if not existing.data:
                raise HTTPException(status_code=404, detail="File not found")

            update_payload: Dict[str, Any] = {"folder_id": folder_id}
            storage_bucket, storage_key = self._extract_storage_location(existing.data[0])
            if storage_key and not str(storage_key).startswith("data:"):
                bucket = self._workspace_bucket_name()
                current_name = os.path.basename(str(storage_key).rstrip("/")) or f"{file_id}"
                new_storage_key = f"workspace/{user_id}/{folder_id}/{current_name}"
                if new_storage_key != storage_key:
                    self._move_s3_object(storage_bucket or bucket, storage_key, new_storage_key)
                    if self._files_has_storage_path:
                        update_payload["storage_path"] = new_storage_key

            if self._files_has_user_id:
                updated = (
                    self.supabase.table("files")
                    .update(update_payload)
                    .eq("id", file_id)
                    .eq("user_id", user_id)
                    .execute()
                )
            else:
                updated = (
                    self.supabase.table("files")
                    .update(update_payload)
                    .eq("id", file_id)
                    .execute()
                )
            if not updated.data:
                raise HTTPException(status_code=404, detail="File not found")
            return updated.data[0]
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to move file: {exc}") from exc

    def delete_file(self, user_id: str, file_id: str) -> Dict[str, Any]:
        try:
            self._detect_files_columns()

            existing = (
                self.supabase.table("files")
                .select("*")
                .eq("id", file_id)
                .limit(1)
                .execute()
            )
            if self._files_has_user_id:
                existing = (
                    self.supabase.table("files")
                    .select("*")
                    .eq("id", file_id)
                    .eq("user_id", user_id)
                    .limit(1)
                    .execute()
                )
            if not existing.data:
                raise HTTPException(status_code=404, detail="File not found")

            all_files_query = self.supabase.table("files").select("*")
            if self._files_has_user_id:
                all_files_query = all_files_query.eq("user_id", user_id)
            all_files = all_files_query.execute().data or []

            child_map: Dict[str, List[str]] = {}
            if self._files_has_parent_file_id:
                for item in all_files:
                    parent_file_id = item.get("parent_file_id")
                    if parent_file_id:
                        child_map.setdefault(str(parent_file_id), []).append(str(item["id"]))

            to_delete = [str(file_id)]
            stack = [str(file_id)]
            while stack:
                current = stack.pop()
                for child_id in child_map.get(current, []):
                    to_delete.append(child_id)
                    stack.append(child_id)

            parsed_bucket, storage_key = self._extract_storage_location(existing.data[0])
            for candidate_bucket in self._candidate_buckets(parsed_bucket):
                self._delete_from_s3(candidate_bucket, storage_key)

            delete_query = self.supabase.table("files").delete().in_("id", to_delete)
            if self._files_has_user_id:
                delete_query = delete_query.eq("user_id", user_id)
            delete_query.execute()
            return {"deleted_file_id": file_id, "deleted_child_file_ids": [fid for fid in to_delete if fid != str(file_id)]}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {exc}") from exc

    @staticmethod
    def generate_pdf_document(title: str, content: str) -> bytes:
        """Produce a PDF document from the text payload while preserving real PDF bytes for preview."""
        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
        except ImportError as exc:
            raise HTTPException(
                status_code=500,
                detail="reportlab is required for PDF generation. Run: pip install reportlab"
            ) from exc

        buffer = BytesIO()
        styles = getSampleStyleSheet()
        title_text = str(title or "Document").strip() or "Document"
        body = [Paragraph(title_text, styles["Heading1"]), Spacer(1, 12)]

        safe_content = str(content or "").replace("\x00", "")
        paragraphs = [part.strip() for part in safe_content.splitlines() if part and part.strip()]
        if not paragraphs:
            paragraphs = ["No content available."]

        for paragraph in paragraphs[:2000]:
            body.append(Paragraph(paragraph, styles["BodyText"]))
            body.append(Spacer(1, 6))

        doc = SimpleDocTemplate(buffer, pagesize=letter, title=title_text)
        doc.build(body)
        return buffer.getvalue()

    def create_generated_pdf_file(
        self,
        user_id: str,
        folder_id: str,
        parent_file_id: str,
        name: str,
        content: str,
        original_filename: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a generated PDF (extracted text or summary) as a real S3-backed workspace file."""
        self._detect_files_columns()

        safe_content = str(content or "").replace("\x00", "")
        pdf_bytes = self.generate_pdf_document(title or name or "Document", safe_content)

        metadata: Dict[str, Any] = {}
        if self._files_has_user_id:
            metadata["user_id"] = user_id
        if self._files_has_folder_id:
            metadata["folder_id"] = folder_id
        if self._files_has_parent_file_id:
            metadata["parent_file_id"] = parent_file_id
        if self._files_has_name:
            metadata["name"] = name
        if self._files_has_original_filename and original_filename:
            metadata["original_filename"] = original_filename
        if self._files_has_file_type:
            metadata["file_type"] = "PDF"
        if self._files_has_mime_type:
            metadata["mime_type"] = "application/pdf"
        if self._files_has_size_bytes:
            metadata["size_bytes"] = len(pdf_bytes)

        safe_filename = (original_filename or name or "file").replace(" ", "_").replace("/", "_")
        if not safe_filename.lower().endswith(".pdf"):
            safe_filename = f"{safe_filename}.pdf"

        storage_key = f"workspace/{user_id}/{folder_id}/{uuid.uuid4().hex}_{safe_filename}"
        bucket = self._workspace_bucket_name()

        try:
            self._upload_to_s3(
                bucket=bucket,
                object_key=storage_key,
                content=pdf_bytes,
                mime_type="application/pdf",
            )
        except HTTPException:
            raise

        if self._files_has_storage_path:
            metadata["storage_path"] = storage_key
        if self._files_has_file_url:
            metadata["file_url"] = storage_key
        if self._files_has_storage_url:
            metadata["storage_url"] = storage_key

        if not metadata:
            raise HTTPException(status_code=500, detail="No writable file metadata columns found")

        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"[FILES] Detected columns: {list(k for k, v in _COLUMNS_CACHE.items() if v)}")
        logger.info(f"[FILES] Inserting file: {name}")
        logger.info(f"[FILES] file_url: {metadata.get('file_url')}")

        try:
            saved = self.supabase.table("files").insert(metadata).execute()
            if not saved.data:
                self._delete_from_s3(bucket, storage_key)
                raise HTTPException(status_code=500, detail="Generated PDF insert failed")
            
            inserted = saved.data[0]
            logger.info(f"[FILES] Created file id: {inserted.get('id')}")
            return inserted
        except HTTPException:
            self._delete_from_s3(bucket, storage_key)
            raise
        except Exception as exc:
            self._delete_from_s3(bucket, storage_key)
            raise HTTPException(status_code=500, detail=f"Failed to save generated PDF: {exc}") from exc

    def create_generated_text_file(
        self,
        user_id: str,
        folder_id: str,
        parent_file_id: str,
        name: str,
        content: str,
        file_type: str = "TXT",
        original_filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist an extracted text or summary file as a child file row.
        
        Files are stored in the database for metadata and in AWS S3 for durable object storage.
        """
        self._detect_files_columns()

        safe_content = str(content or "").replace("\x00", "")
        content_bytes = safe_content.encode("utf-8")

        metadata: Dict[str, Any] = {}
        if self._files_has_user_id:
            metadata["user_id"] = user_id
        if self._files_has_folder_id:
            metadata["folder_id"] = folder_id
        if self._files_has_parent_file_id:
            metadata["parent_file_id"] = parent_file_id
        if self._files_has_name:
            metadata["name"] = name
        if self._files_has_original_filename and original_filename:
            metadata["original_filename"] = original_filename
        if self._files_has_file_type:
            metadata["file_type"] = file_type
        if self._files_has_mime_type:
            metadata["mime_type"] = "text/plain"
        if self._files_has_size_bytes:
            metadata["size_bytes"] = len(content_bytes)


        # Upload the generated text asset to AWS S3 so the object survives across sessions.
        safe_filename = (original_filename or name or "file").replace(" ", "_").replace("/", "_")
        storage_key = f"workspace/{user_id}/{folder_id}/{uuid.uuid4().hex}_{safe_filename}"
        bucket = self._workspace_bucket_name()

        try:
            self._upload_to_s3(
                bucket=bucket,
                object_key=storage_key,
                content=content_bytes,
                mime_type="text/plain",
            )
        except HTTPException:
            raise

        if self._files_has_storage_path:
            metadata["storage_path"] = storage_key
        if self._files_has_file_url:
            metadata["file_url"] = storage_key
        if self._files_has_storage_url:
            metadata["storage_url"] = storage_key

        if not metadata:
            raise HTTPException(status_code=500, detail="No writable file metadata columns found")

        try:
            saved = self.supabase.table("files").insert(metadata).execute()
            if not saved.data:
                self._delete_from_s3(bucket, storage_key)
                raise HTTPException(status_code=500, detail="Generated file insert failed")
            return saved.data[0]
        except HTTPException:
            self._delete_from_s3(bucket, storage_key)
            raise
        except Exception as exc:
            self._delete_from_s3(bucket, storage_key)
            raise HTTPException(status_code=500, detail=f"Failed to save generated file: {exc}") from exc


    def search_files(
        self, user_id: str, query: str = "", file_type: Optional[str] = None, limit: int = 20
    ) -> Dict[str, Any]:
        """Search for files by name or type for cross-module access."""
        try:
            self._detect_files_columns()

            # Build base query
            q = self.supabase.table("files").select("*").eq("user_id", user_id).limit(limit)

            # Apply filters
            if query and query.strip():
                # Search by name using ilike for case-insensitive partial matching
                q = q.ilike("name", f"%{query}%")

            if file_type and file_type.strip():
                # Filter by file type
                q = q.ilike("file_type", file_type.strip())

            result = q.execute()
            return {"files": result.data or [], "count": len(result.data or [])}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Search failed: {exc}") from exc

    def export_file_to_module(
        self,
        user_id: str,
        file_id: str,
        module_name: str,
        module_ref_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a reference to a workspace file in another module without copying it."""
        try:
            self._detect_files_columns()

            # Verify file exists and belongs to user
            file_query = self.supabase.table("files").select("*").eq("id", file_id)
            if self._files_has_user_id:
                file_query = file_query.eq("user_id", user_id)
            
            file_result = file_query.limit(1).execute()
            if not file_result.data:
                raise HTTPException(status_code=404, detail="File not found")

            file_data = file_result.data[0]

            # Create module reference table entry if it doesn't exist
            try:
                ref_metadata = {
                    "workspace_file_id": file_id,
                    "user_id": user_id,
                    "module_name": module_name,
                    "module_ref_id": module_ref_id,
                    "file_name": file_data.get("name", ""),
                    "file_type": file_data.get("file_type", ""),
                    "storage_url": file_data.get("storage_url"),
                    "storage_path": file_data.get("storage_path"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                # Try to insert into module_file_references table
                # This table should be created by each module or shared infrastructure
                ref_result = self.supabase.table("module_file_references").insert(ref_metadata).execute()
                
                if ref_result.data:
                    return {
                        "status": "success",
                        "message": f"File exported to {module_name}",
                        "reference": ref_result.data[0],
                    }
            except Exception as ref_exc:
                # If module_file_references table doesn't exist, return the file data directly
                # Modules can access files directly via the files endpoint
                pass

            return {
                "status": "success",
                "message": f"File can be accessed by {module_name}",
                "file": {
                    "id": file_data.get("id"),
                    "name": file_data.get("name"),
                    "file_type": file_data.get("file_type"),
                    "storage_url": file_data.get("storage_url"),
                    "storage_path": file_data.get("storage_path"),
                    "created_at": file_data.get("created_at"),
                },
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Export failed: {exc}") from exc
