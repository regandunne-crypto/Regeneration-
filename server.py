#!/usr/bin/env python3
"""WebSocket game server for multi-subject quiz platform with optional Supabase-backed test bank.

Key ideas:
- Students still join by subject.
- Host now selects a subject and then a saved test for that subject.
- Tests can be stored durably in Supabase when environment variables are configured.
- Without Supabase, the app still works using in-memory fallback storage (not durable).
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import string
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# ──────────────────────────────────────────────────────────────────────────────
# Subject catalogue and built-in legacy question sets
# ──────────────────────────────────────────────────────────────────────────────
SUBJECTS = {
    '1EM105B': {'code': '1EM105B', 'name': 'Mechanics', 'questions': []},
    'DYN317B': {'code': 'DYN317B', 'name': 'Dynamics', 'questions': []},
    'MEC105B': {'code': 'MEC105B', 'name': 'Mechanics', 'questions': []},
}

BUILTIN_SUBJECT_CODES = set(SUBJECTS.keys())
SUBJECT_CODE_PATTERN = re.compile(r"^[A-Z0-9]{3,10}$")

TIME_PER_Q = 30
MAX_POINTS = 1000
MIN_POINTS = 200
REQUEST_TIMEOUT = 20
REQUIRE_SUPABASE = os.environ.get("REQUIRE_SUPABASE", "").strip().lower() in {"1", "true", "yes", "on"}
_local_store_env = os.environ.get("LOCAL_STORE_PATH", "").strip()
LOCAL_STORE_PATH = Path(_local_store_env).expanduser() if _local_store_env else (Path(__file__).resolve().parent / "local_store.json")
LOCAL_STORE_VERSION = 1

limiter = Limiter(key_func=get_remote_address)


class SupabaseUnavailable(RuntimeError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Test bank models + storage
# ──────────────────────────────────────────────────────────────────────────────
SESSION_COOKIE_NAME = "lecturer_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def _session_secret() -> str:
    return (
        os.environ.get("APP_SESSION_SECRET", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or "engineering-quiz-dev-secret"
    )


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    rounds = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds_str, salt, digest = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        rounds = int(rounds_str)
    except Exception:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds).hex()
    return hmac.compare_digest(candidate, digest)


def create_session_token(lecturer_id: str) -> str:
    expires = int(time.time()) + SESSION_MAX_AGE
    payload = f"{lecturer_id}.{expires}"
    signature = hmac.new(_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(f"{payload}.{signature.hex()}".encode("utf-8")).decode("utf-8")


def parse_session_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        lecturer_id, expires_str, signature = decoded.split(".", 2)
        payload = f"{lecturer_id}.{expires_str}"
        expected = hmac.new(_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_str) < int(time.time()):
            return None
        return lecturer_id
    except Exception:
        return None


class QuestionPayload(BaseModel):
    q: str = Field(min_length=1, max_length=600)
    options: list[str]
    correct: int = Field(ge=0, le=3)
    explanation: str = Field(default="", max_length=2000)

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str]) -> list[str]:
        if len(value) != 4:
            raise ValueError("Each question must have exactly 4 options.")
        cleaned = []
        for item in value:
            text = (item or "").strip()
            if not text:
                raise ValueError("Answer options cannot be blank.")
            cleaned.append(text[:240])
        return cleaned

    @field_validator("q")
    @classmethod
    def validate_question_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Question text cannot be blank.")
        return value

    @field_validator("explanation")
    @classmethod
    def normalize_explanation(cls, value: str) -> str:
        return (value or "").strip()


class TestPayload(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    chapter: str = Field(default="", max_length=140)
    description: str = Field(default="", max_length=600)
    questions: list[QuestionPayload]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Test title cannot be blank.")
        return value

    @field_validator("chapter")
    @classmethod
    def normalize_chapter(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, value: list[QuestionPayload]) -> list[QuestionPayload]:
        if not value:
            raise ValueError("A test must include at least one question.")
        return value


class SubjectPayload(BaseModel):
    code: str = Field(min_length=3, max_length=10)
    name: str = Field(min_length=2, max_length=60)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        cleaned = (value or "").strip().upper()
        if not SUBJECT_CODE_PATTERN.match(cleaned):
            raise ValueError("Subject code must be 3-10 letters or numbers (no spaces).")
        return cleaned

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < 2 or len(cleaned) > 60:
            raise ValueError("Subject name must be 2-60 characters.")
        return cleaned


class LecturerSignupPayload(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=240)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 2:
            raise ValueError("Name must be at least 2 characters.")
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Please enter a valid email address.")
        return value


class LecturerLoginPayload(BaseModel):
    email: str = Field(min_length=5, max_length=240)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return (value or "").strip().lower()


class DraftQuestionPayload(BaseModel):
    q: str = Field(default="", max_length=600)
    options: list[str] = Field(default_factory=lambda: ["", "", "", ""])
    correct: int = Field(default=0, ge=0, le=3)
    explanation: str = Field(default="", max_length=2000)

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str]) -> list[str]:
        value = list(value or [])[:4]
        while len(value) < 4:
            value.append("")
        return [(item or "").strip()[:240] for item in value]

    @field_validator("q")
    @classmethod
    def normalize_question_text(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("explanation")
    @classmethod
    def normalize_explanation(cls, value: str) -> str:
        return (value or "").strip()


class DraftPayload(BaseModel):
    title: str = Field(default="", max_length=140)
    chapter: str = Field(default="", max_length=140)
    description: str = Field(default="", max_length=600)
    questions: list[DraftQuestionPayload] = Field(default_factory=list)
    editingTestId: str | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("chapter")
    @classmethod
    def normalize_chapter(cls, value: str) -> str:
        return (value or "").strip()

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return (value or "").strip()


class SupabaseStore:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self._client = httpx.AsyncClient(
            headers={
                "apikey": self.service_role_key,
                "Authorization": f"Bearer {self.service_role_key}",
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        self.quiz_tests_base = f"{self.base_url}/rest/v1/quiz_tests"
        self.lecturers_base = f"{self.base_url}/rest/v1/quiz_lecturers"
        self.drafts_base = f"{self.base_url}/rest/v1/quiz_test_drafts"
        self.subjects_base = f"{self.base_url}/rest/v1/quiz_subjects"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _check_response(self, resp: httpx.Response) -> None:
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Supabase request failed: {detail}")

    async def _request(self, method: str, url: str, *, params=None, body=None, prefer: str | None = None) -> list[dict[str, Any]]:
        headers = dict(self._client.headers)
        if prefer:
            headers["Prefer"] = prefer
        resp = await self._client.request(method, url, params=params, headers=headers, json=body)
        self._check_response(resp)
        if not resp.text:
            return []
        try:
            return resp.json()
        except Exception:
            return []

    async def get_lecturer_by_email(self, email: str) -> dict[str, Any] | None:
        rows = await self._request("GET", self.lecturers_base, params={
            "select": "id,name,email,password_hash,created_at,updated_at",
            "email": f"eq.{email.lower()}",
            "limit": "1",
        })
        return rows[0] if rows else None

    async def get_lecturer_by_id(self, lecturer_id: str) -> dict[str, Any] | None:
        rows = await self._request("GET", self.lecturers_base, params={
            "select": "id,name,email,created_at,updated_at",
            "id": f"eq.{lecturer_id}",
            "limit": "1",
        })
        return rows[0] if rows else None

    async def create_lecturer(self, name: str, email: str, password_hash: str) -> dict[str, Any]:
        rows = await self._request("POST", self.lecturers_base, body={
            "name": name,
            "email": email.lower(),
            "password_hash": password_hash,
        }, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the created lecturer.")
        return rows[0]

    async def list_subjects(self) -> list[dict[str, Any]]:
        rows = await self._request("GET", self.subjects_base, params={
            "select": "code,name,created_by,created_at",
            "order": "name.asc",
        })
        for row in rows:
            if row.get("code"):
                row["code"] = str(row["code"]).strip().upper()
            if row.get("name"):
                row["name"] = str(row["name"]).strip()
        return rows

    async def get_subject(self, code: str) -> dict[str, Any] | None:
        rows = await self._request("GET", self.subjects_base, params={
            "select": "code,name,created_by,created_at",
            "code": f"ilike.{code}",
            "limit": "1",
        })
        if not rows:
            return None
        row = rows[0]
        row["code"] = str(row.get("code") or "").strip().upper()
        row["name"] = str(row.get("name") or "").strip()
        return row

    async def create_subject(self, code: str, name: str, lecturer_id: str) -> dict[str, Any]:
        rows = await self._request("POST", self.subjects_base, body={
            "code": code,
            "name": name,
            "created_by": lecturer_id,
        }, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the created subject.")
        row = rows[0]
        row["code"] = str(row.get("code") or "").strip().upper()
        row["name"] = str(row.get("name") or "").strip()
        return row

    async def delete_subject(self, code: str, lecturer_id: str) -> None:
        await self._request("DELETE", self.subjects_base, params={
            "code": f"ilike.{code}",
            "created_by": f"eq.{lecturer_id}",
        })

    async def subject_has_tests(self, subject_code: str) -> bool:
        rows = await self._request("GET", self.quiz_tests_base, params={
            "select": "id",
            "subject_code": f"eq.{subject_code}",
            "limit": "1",
        })
        return bool(rows)

    async def list_tests_by_creator(self, lecturer_id: str) -> list[dict[str, Any]]:
        rows = await self._request("GET", self.quiz_tests_base, params={
            "select": "id,subject_code,title,chapter,description,questions,question_count,created_at,updated_at,created_by,owner_name",
            "created_by": f"eq.{lecturer_id}",
            "order": "subject_code.asc,updated_at.desc",
        })
        for row in rows:
            row["source"] = "supabase"
            row.setdefault("question_count", len(row.get("questions") or []))
        return rows

    async def list_tests(self, subject_code: str, lecturer_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self._request("GET", self.quiz_tests_base, params={
            "select": "id,subject_code,title,chapter,description,question_count,created_at,updated_at,created_by,owner_name",
            "subject_code": f"eq.{subject_code}",
            "order": "updated_at.desc",
        })
        for row in rows:
            row["source"] = "supabase"
            row["can_edit"] = bool(lecturer_id and row.get("created_by") == lecturer_id)
        return rows

    async def get_test(self, subject_code: str, test_id: str, lecturer_id: str | None = None) -> dict[str, Any] | None:
        rows = await self._request("GET", self.quiz_tests_base, params={
            "select": "id,subject_code,title,chapter,description,questions,question_count,created_at,updated_at,created_by,owner_name",
            "subject_code": f"eq.{subject_code}",
            "id": f"eq.{test_id}",
            "limit": "1",
        })
        if not rows:
            return None
        row = rows[0]
        row["source"] = "supabase"
        row["can_edit"] = bool(lecturer_id and row.get("created_by") == lecturer_id)
        return row

    async def create_test(self, subject_code: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        rows = await self._request("POST", self.quiz_tests_base, body={
            "subject_code": subject_code,
            "title": payload.title,
            "chapter": payload.chapter or None,
            "description": payload.description or None,
            "question_count": len(payload.questions),
            "questions": [q.model_dump() for q in payload.questions],
            "created_by": lecturer["id"],
            "updated_by": lecturer["id"],
            "owner_name": lecturer.get("name") or lecturer.get("email") or "Lecturer",
        }, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the created test.")
        row = rows[0]
        row["source"] = "supabase"
        row["can_edit"] = True
        return row

    async def delete_test(self, subject_code: str, test_id: str) -> None:
        await self._request("DELETE", self.quiz_tests_base, params={
            "subject_code": f"eq.{subject_code}",
            "id": f"eq.{test_id}",
        })

    async def update_test(self, subject_code: str, test_id: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        existing = await self.get_test(subject_code, test_id, lecturer["id"])
        if not existing:
            raise KeyError("Test not found")
        if existing.get("created_by") and existing.get("created_by") != lecturer["id"]:
            raise PermissionError("Only the lecturer who created this test can edit it.")
        rows = await self._request("PATCH", self.quiz_tests_base, params={
            "subject_code": f"eq.{subject_code}",
            "id": f"eq.{test_id}",
        }, body={
            "title": payload.title,
            "chapter": payload.chapter or None,
            "description": payload.description or None,
            "question_count": len(payload.questions),
            "questions": [q.model_dump() for q in payload.questions],
            "updated_by": lecturer["id"],
            "owner_name": existing.get("owner_name") or lecturer.get("name") or lecturer.get("email") or "Lecturer",
            "updated_at": datetime.utcnow().isoformat(),
        }, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the updated test.")
        row = rows[0]
        row["source"] = "supabase"
        row["can_edit"] = True
        return row

    async def get_draft(self, subject_code: str, lecturer_id: str) -> dict[str, Any] | None:
        rows = await self._request("GET", self.drafts_base, params={
            "select": "id,lecturer_id,subject_code,title,chapter,description,questions,question_count,editing_test_id,updated_at",
            "lecturer_id": f"eq.{lecturer_id}",
            "subject_code": f"eq.{subject_code}",
            "limit": "1",
        })
        return rows[0] if rows else None

    async def save_draft(self, subject_code: str, lecturer: dict[str, Any], payload: DraftPayload) -> dict[str, Any]:
        existing = await self.get_draft(subject_code, lecturer["id"])
        body = {
            "lecturer_id": lecturer["id"],
            "subject_code": subject_code,
            "title": payload.title,
            "chapter": payload.chapter or None,
            "description": payload.description or None,
            "question_count": len(payload.questions),
            "questions": [q.model_dump() for q in payload.questions],
            "editing_test_id": payload.editingTestId,
            "owner_name": lecturer.get("name") or lecturer.get("email") or "Lecturer",
            "updated_at": datetime.utcnow().isoformat(),
        }
        if existing:
            rows = await self._request("PATCH", self.drafts_base, params={
                "id": f"eq.{existing['id']}",
                "lecturer_id": f"eq.{lecturer['id']}",
            }, body=body, prefer="return=representation")
        else:
            rows = await self._request("POST", self.drafts_base, body=body, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the saved draft.")
        return rows[0]

    async def clear_draft(self, subject_code: str, lecturer_id: str) -> None:
        await self._request("DELETE", self.drafts_base, params={
            "lecturer_id": f"eq.{lecturer_id}",
            "subject_code": f"eq.{subject_code}",
        })


class HybridTestRepository:
    def __init__(self, subjects: dict[str, Any]):
        self.subjects = subjects
        self.builtin_tests: dict[str, dict[str, dict[str, Any]]] = {}
        self.local_custom_tests: dict[str, dict[str, dict[str, Any]]] = {}
        self.local_drafts: dict[tuple[str, str], dict[str, Any]] = {}
        self.local_lecturers: dict[str, dict[str, Any]] = {}
        self.local_subjects: dict[str, dict[str, Any]] = {}
        self.local_store_path = LOCAL_STORE_PATH
        self.local_store_enabled = True
        self.local_store_error: str | None = None
        self.require_supabase = REQUIRE_SUPABASE
        self.supabase_configured = False
        self.supabase_error: str | None = None
        self._seed_builtin_tests()
        self._load_local_store()

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        self.supabase_configured = bool(url and key)
        self.remote = SupabaseStore(url, key) if self.supabase_configured else None
        self._set_storage_mode()

    def _set_storage_mode(self) -> None:
        if self.remote is not None:
            self.storage_mode = "supabase"
        elif self.local_store_enabled:
            self.storage_mode = "local-file"
        else:
            self.storage_mode = "in-memory"

    def supabase_unavailable(self) -> bool:
        return self.supabase_configured and self.remote is None and bool(self.supabase_error)

    def _ensure_supabase_for_write(self) -> None:
        if self.require_supabase and self.supabase_configured and self.remote is None:
            raise SupabaseUnavailable("Supabase is unavailable. Writes are disabled while REQUIRE_SUPABASE is enabled.")

    def _draft_key(self, lecturer_id: str, subject_code: str) -> str:
        return f"{lecturer_id}::{subject_code}"

    def _parse_draft_key(self, key: str) -> tuple[str, str] | None:
        if not isinstance(key, str) or "::" not in key:
            return None
        parts = key.split("::", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        return parts[0], parts[1]

    def _register_subject(self, code: str, name: str) -> None:
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if not code or not name or code in BUILTIN_SUBJECT_CODES:
            return
        entry = self.subjects.get(code)
        if entry:
            entry["name"] = name
            entry.setdefault("questions", [])
        else:
            self.subjects[code] = {"code": code, "name": name, "questions": []}

    def _load_local_store(self) -> None:
        if not self.local_store_path.exists():
            return
        try:
            with self.local_store_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            self.local_store_error = f"Failed to load local store: {exc}"
            return
        if not isinstance(data, dict):
            return
        subjects = data.get("local_subjects", {})
        if isinstance(subjects, dict):
            for raw_code, row in subjects.items():
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or raw_code or "").strip().upper()
                name = str(row.get("name") or "").strip()
                if not code or not name or code in BUILTIN_SUBJECT_CODES:
                    continue
                cleaned = {
                    "code": code,
                    "name": name,
                    "created_by": row.get("created_by"),
                    "created_at": row.get("created_at"),
                }
                self.local_subjects[code] = cleaned
                self._register_subject(code, name)
        tests = data.get("local_custom_tests", {})
        if isinstance(tests, dict):
            for code, items in tests.items():
                if code not in self.subjects or not isinstance(items, dict):
                    continue
                cleaned: dict[str, dict[str, Any]] = {}
                for test_id, row in items.items():
                    if not isinstance(row, dict):
                        continue
                    row.setdefault("subject_code", code)
                    row["source"] = row.get("source") or "local-file"
                    cleaned[test_id] = row
                if cleaned:
                    self.local_custom_tests[code] = cleaned
        drafts = data.get("local_drafts", {})
        if isinstance(drafts, dict):
            for key, row in drafts.items():
                parsed = self._parse_draft_key(key)
                if not parsed or not isinstance(row, dict):
                    continue
                self.local_drafts[parsed] = row
        lecturers = data.get("local_lecturers", {})
        if isinstance(lecturers, dict):
            self.local_lecturers = lecturers

    def _persist_local_store(self) -> None:
        if not self.local_store_enabled:
            return
        payload = {
            "version": LOCAL_STORE_VERSION,
            "local_subjects": self.local_subjects,
            "local_custom_tests": self.local_custom_tests,
            "local_drafts": {
                self._draft_key(lecturer_id, subject_code): row
                for (lecturer_id, subject_code), row in self.local_drafts.items()
            },
            "local_lecturers": self.local_lecturers,
        }
        tmp_path = self.local_store_path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            tmp_path.replace(self.local_store_path)
        except Exception as exc:
            self.local_store_error = f"Failed to save local store: {exc}"
            self.local_store_enabled = False
            self._set_storage_mode()

    def _cache_lecturer_row(self, row: dict[str, Any] | None) -> None:
        if not row or "password_hash" not in row:
            return
        email = (row.get("email") or "").strip().lower()
        if not email:
            return
        existing = self.local_lecturers.get(email, {})
        merged = dict(existing)
        merged.update(row)
        self.local_lecturers[email] = merged
        self._persist_local_store()

    def _handle_supabase_error(self, exc: RuntimeError) -> bool:
        message = str(exc)
        message_lower = message.lower()
        if (
            "pgrst205" in message_lower
            or "schema cache" in message_lower
            or ("could not find the table" in message_lower)
            or ("relation" in message_lower and "does not exist" in message_lower)
        ):
            self.supabase_error = message
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return True
        return False

    async def _call_remote(self, awaitable, fallback):
        if self.remote is None or awaitable is None:
            return fallback()
        try:
            return await awaitable
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return fallback()
            raise
        except httpx.RequestError as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return fallback()

    def _seed_builtin_tests(self) -> None:
        for code in self.subjects:
            self.builtin_tests[code] = {}
            self.local_custom_tests[code] = {}

    def get_storage_status(self) -> dict[str, Any]:
        if self.remote is None:
            if self.local_store_enabled:
                if self.supabase_configured and self.supabase_error:
                    note = "Supabase configured but schema is missing. Using local file storage."
                else:
                    note = "Local file storage is active on this server. Data resets on redeploy unless a persistent disk is used."
            else:
                if self.supabase_configured and self.supabase_error:
                    note = "Supabase configured but schema is missing. Running in-memory until the schema is applied."
                else:
                    note = "In-memory storage resets on redeploy/restart."
        else:
            note = "Supabase storage is active."
        return {
            "mode": self.storage_mode,
            "supabaseConfigured": self.supabase_configured,
            "note": note,
            "supabaseError": self.supabase_error,
        }

    def _summary(self, row: dict[str, Any], lecturer_id: str | None = None) -> dict[str, Any]:
        created_by = row.get("created_by")
        source = row.get("source", "supabase")
        can_edit = False
        if source not in {"built-in"}:
            can_edit = bool(lecturer_id and created_by and created_by == lecturer_id)
        return {
            "id": row["id"],
            "subject_code": row["subject_code"],
            "title": row.get("title", "Untitled Test"),
            "chapter": row.get("chapter") or "",
            "description": row.get("description") or "",
            "questionCount": row.get("question_count") or len(row.get("questions") or []),
            "source": source,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "ownerName": row.get("owner_name") or "System",
            "createdBy": created_by,
            "canEdit": can_edit,
        }

    async def get_lecturer_by_email(self, email: str) -> dict[str, Any] | None:
        email = email.strip().lower()
        def _local_lookup():
            return self.local_lecturers.get(email)
        if self.remote is None:
            return _local_lookup()
        try:
            row = await self.remote.get_lecturer_by_email(email)
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return _local_lookup()
            raise
        except httpx.RequestError as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return _local_lookup()
        if row:
            self._cache_lecturer_row(row)
            return row
        return _local_lookup()

    async def get_lecturer_by_id(self, lecturer_id: str) -> dict[str, Any] | None:
        def _local_lookup():
            for row in self.local_lecturers.values():
                if row["id"] == lecturer_id:
                    return {k: v for k, v in row.items() if k != "password_hash"}
            return None
        if self.remote is None:
            return _local_lookup()
        try:
            row = await self.remote.get_lecturer_by_id(lecturer_id)
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return _local_lookup()
            raise
        except httpx.RequestError as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return _local_lookup()
        return row or _local_lookup()

    async def create_lecturer(self, payload: LecturerSignupPayload) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        if await self.get_lecturer_by_email(payload.email):
            raise ValueError("An account with that email already exists.")
        password_hash = hash_password(payload.password)
        def _local_create():
            row = {
                "id": str(uuid.uuid4()),
                "name": payload.name,
                "email": payload.email,
                "password_hash": password_hash,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            self.local_lecturers[payload.email] = row
            self._persist_local_store()
            return {k: v for k, v in row.items() if k != "password_hash"}
        result = await self._call_remote(
            self.remote.create_lecturer(payload.name, payload.email, password_hash) if self.remote else None,
            _local_create
        )
        self._cache_lecturer_row(result)
        return result

    async def list_subjects(self) -> list[dict[str, Any]]:
        remote_rows = await self._call_remote(
            self.remote.list_subjects() if self.remote else None,
            lambda: list(self.local_subjects.values())
        )
        combined: dict[str, dict[str, Any]] = {}
        for row in list(self.local_subjects.values()):
            code = (row.get("code") or "").strip().upper()
            name = (row.get("name") or "").strip()
            if not code or not name or code in BUILTIN_SUBJECT_CODES:
                continue
            combined[code] = {
                "code": code,
                "name": name,
                "created_by": row.get("created_by"),
                "created_at": row.get("created_at"),
            }
            self._register_subject(code, name)
        for row in remote_rows or []:
            code = (row.get("code") or "").strip().upper()
            name = (row.get("name") or "").strip()
            if not code or not name or code in BUILTIN_SUBJECT_CODES:
                continue
            combined[code] = {
                "code": code,
                "name": name,
                "created_by": row.get("created_by"),
                "created_at": row.get("created_at"),
            }
            self._register_subject(code, name)
        if combined:
            self.local_subjects.update(combined)
            self._persist_local_store()
        return list(combined.values())

    async def create_subject(self, code: str, name: str, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if code in BUILTIN_SUBJECT_CODES or code in self.subjects:
            raise ValueError("Subject code already exists.")
        if self.remote is not None:
            existing = await self._call_remote(self.remote.get_subject(code), lambda: None)
            if existing:
                raise ValueError("Subject code already exists.")
        def _local_create():
            row = {
                "code": code,
                "name": name,
                "created_by": lecturer.get("id"),
                "created_at": datetime.utcnow().isoformat(),
            }
            self.local_subjects[code] = row
            self._register_subject(code, name)
            self._persist_local_store()
            return row
        async def _remote_create():
            try:
                return await self.remote.create_subject(code, name, lecturer["id"])
            except RuntimeError as exc:
                if "duplicate" in str(exc).lower():
                    raise ValueError("Subject code already exists.")
                raise
        row = await self._call_remote(_remote_create() if self.remote else None, _local_create)
        if row:
            self.local_subjects[code] = {
                "code": code,
                "name": row.get("name") or name,
                "created_by": row.get("created_by") or lecturer.get("id"),
                "created_at": row.get("created_at"),
            }
            self._register_subject(code, row.get("name") or name)
            self._persist_local_store()
        return row

    async def delete_subject(self, code: str, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        code = (code or "").strip().upper()
        if code in BUILTIN_SUBJECT_CODES:
            raise PermissionError("Built-in subjects cannot be deleted.")

        def _local_delete():
            row = self.local_subjects.get(code)
            if not row:
                raise KeyError("Subject not found")
            if row.get("created_by") != lecturer.get("id"):
                raise PermissionError("Only the lecturer who created this subject can delete it.")
            if self.local_custom_tests.get(code):
                raise ValueError("Cannot delete a subject with saved tests.")
            self.local_subjects.pop(code, None)
            if code in self.subjects and code not in BUILTIN_SUBJECT_CODES:
                self.subjects.pop(code, None)
            self.local_custom_tests.pop(code, None)
            self._persist_local_store()
            return row

        async def _remote_delete():
            row = await self.remote.get_subject(code)
            if not row:
                raise KeyError("Subject not found")
            if row.get("created_by") != lecturer.get("id"):
                raise PermissionError("Only the lecturer who created this subject can delete it.")
            if await self.remote.subject_has_tests(code) or self.local_custom_tests.get(code):
                raise ValueError("Cannot delete a subject with saved tests.")
            await self.remote.delete_subject(code, lecturer.get("id"))
            return row

        row = await self._call_remote(_remote_delete() if self.remote else None, _local_delete)
        self.local_subjects.pop(code, None)
        if code in self.subjects and code not in BUILTIN_SUBJECT_CODES:
            self.subjects.pop(code, None)
        self.local_custom_tests.pop(code, None)
        self._persist_local_store()
        return row

    async def list_tests_by_creator(self, lecturer_id: str) -> list[dict[str, Any]]:
        remote_rows = await self._call_remote(
            self.remote.list_tests_by_creator(lecturer_id) if self.remote else None,
            lambda: []
        )
        local_rows: list[dict[str, Any]] = []
        for items in self.local_custom_tests.values():
            for row in items.values():
                if row.get("created_by") == lecturer_id:
                    local_rows.append(row)
        return list(remote_rows or []) + local_rows

    async def list_tests(self, subject_code: str, lecturer_id: str | None = None) -> list[dict[str, Any]]:
        if subject_code not in self.subjects:
            raise KeyError(subject_code)

        tests: list[dict[str, Any]] = []
        remote_rows = await self._call_remote(
            self.remote.list_tests(subject_code, lecturer_id) if self.remote else None,
            lambda: []
        )
        local_rows = list(self.local_custom_tests.get(subject_code, {}).values())

        tests.extend(self._summary(row, lecturer_id) for row in remote_rows)
        tests.extend(self._summary(row, lecturer_id) for row in local_rows)
        return tests

    async def get_test(self, subject_code: str, test_id: str, lecturer_id: str | None = None) -> dict[str, Any] | None:
        if test_id in self.builtin_tests.get(subject_code, {}):
            row = self.builtin_tests[subject_code][test_id]
            row["can_edit"] = False
            return row
        if test_id in self.local_custom_tests.get(subject_code, {}):
            row = self.local_custom_tests[subject_code][test_id]
            row["can_edit"] = bool(lecturer_id and row.get("created_by") == lecturer_id)
            return row
        return await self._call_remote(
            self.remote.get_test(subject_code, test_id, lecturer_id) if self.remote else None,
            lambda: None
        )

    async def create_test(self, subject_code: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        if subject_code not in self.subjects:
            raise KeyError(subject_code)
        def _local_create():
            test_id = f"local:{uuid.uuid4()}"
            row = {
                "id": test_id,
                "subject_code": subject_code,
                "title": payload.title,
                "chapter": payload.chapter,
                "description": payload.description,
                "question_count": len(payload.questions),
                "questions": [q.model_dump() for q in payload.questions],
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
                "source": "local-file" if self.local_store_enabled else "in-memory",
                "created_by": lecturer["id"],
                "owner_name": lecturer.get("name") or lecturer.get("email") or "Lecturer",
            }
            self.local_custom_tests.setdefault(subject_code, {})[test_id] = row
            self._persist_local_store()
            return row
        async def _remote_create():
            row = await self.remote.create_test(subject_code, payload, lecturer)
            row.setdefault("question_count", len(payload.questions))
            row.setdefault("questions", [q.model_dump() for q in payload.questions])
            row.setdefault("owner_name", lecturer.get("name") or lecturer.get("email") or "Lecturer")
            row.setdefault("created_by", lecturer["id"])
            return row
        return await self._call_remote(_remote_create() if self.remote else None, _local_create)

    async def update_test(self, subject_code: str, test_id: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        if test_id in self.builtin_tests.get(subject_code, {}):
            raise PermissionError("Built-in tests cannot be edited.")
        def _local_update():
            row = self.local_custom_tests.get(subject_code, {}).get(test_id)
            if not row:
                raise KeyError("Test not found")
            if row.get("created_by") and row.get("created_by") != lecturer["id"]:
                raise PermissionError("Only the lecturer who created this test can edit it.")
            row.update({
                "title": payload.title,
                "chapter": payload.chapter,
                "description": payload.description,
                "question_count": len(payload.questions),
                "questions": [q.model_dump() for q in payload.questions],
                "updated_at": datetime.utcnow().isoformat(),
            })
            self._persist_local_store()
            return row
        async def _remote_update():
            row = await self.remote.update_test(subject_code, test_id, payload, lecturer)
            row.setdefault("question_count", len(payload.questions))
            row.setdefault("questions", [q.model_dump() for q in payload.questions])
            row.setdefault("owner_name", lecturer.get("name") or lecturer.get("email") or "Lecturer")
            row.setdefault("created_by", lecturer["id"])
            return row
        return await self._call_remote(_remote_update() if self.remote else None, _local_update)

    async def delete_test(self, subject_code: str, test_id: str, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        if test_id in self.builtin_tests.get(subject_code, {}):
            raise PermissionError("Built-in tests cannot be deleted.")

        def _local_delete():
            row = self.local_custom_tests.get(subject_code, {}).get(test_id)
            if not row:
                raise KeyError("Test not found")
            if row.get("created_by") != lecturer["id"]:
                raise PermissionError("Only the lecturer who created this test can delete it.")
            self.local_custom_tests.get(subject_code, {}).pop(test_id, None)
            self._persist_local_store()
            return row

        async def _remote_delete():
            existing = await self.remote.get_test(subject_code, test_id, lecturer.get("id"))
            if not existing:
                raise KeyError("Test not found")
            if existing.get("created_by") != lecturer["id"]:
                raise PermissionError("Only the lecturer who created this test can delete it.")
            await self.remote.delete_test(subject_code, test_id)
            return existing

        return await self._call_remote(_remote_delete() if self.remote else None, _local_delete)

    async def get_draft(self, subject_code: str, lecturer: dict[str, Any]) -> dict[str, Any] | None:
        # Do NOT use _call_remote here — a failure on the quiz_test_drafts table must
        # never disable self.remote (which would break all subsequent test reads/writes).
        if self.remote is not None:
            try:
                return await self.remote.get_draft(subject_code, lecturer["id"])
            except Exception:
                pass  # Fall back to local silently
        return self.local_drafts.get((lecturer["id"], subject_code))

    async def save_draft(self, subject_code: str, lecturer: dict[str, Any], payload: DraftPayload) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        def _local_save():
            row = {
                "id": self.local_drafts.get((lecturer["id"], subject_code), {}).get("id", f"draft:{uuid.uuid4()}"),
                "lecturer_id": lecturer["id"],
                "subject_code": subject_code,
                "title": payload.title,
                "chapter": payload.chapter,
                "description": payload.description,
                "question_count": len(payload.questions),
                "questions": [q.model_dump() for q in payload.questions],
                "editing_test_id": payload.editingTestId,
                "updated_at": datetime.utcnow().isoformat(),
                "owner_name": lecturer.get("name") or lecturer.get("email") or "Lecturer",
            }
            self.local_drafts[(lecturer["id"], subject_code)] = row
            self._persist_local_store()
            return row
        # Do NOT use _call_remote here — a failure on the quiz_test_drafts table must
        # never disable self.remote (which would break all subsequent test reads/writes).
        if self.remote is not None:
            try:
                return await self.remote.save_draft(subject_code, lecturer, payload)
            except Exception:
                pass  # Fall back to local silently; draft failure is non-critical
        return _local_save()

    async def clear_draft(self, subject_code: str, lecturer: dict[str, Any]) -> None:
        # Always clear the local draft copy first.
        self.local_drafts.pop((lecturer["id"], subject_code), None)
        self._persist_local_store()
        # Do NOT use _call_remote here — a failure on the quiz_test_drafts table must
        # never disable self.remote (which would break all subsequent test reads/writes).
        if self.remote is not None:
            try:
                await self.remote.clear_draft(subject_code, lecturer["id"])
            except Exception:
                pass  # Non-critical; the test itself was already saved successfully


repo = HybridTestRepository(SUBJECTS)


# ──────────────────────────────────────────────────────────────────────────────
# Per-room live game state
# ──────────────────────────────────────────────────────────────────────────────
class GameRoom:
    def __init__(self, subject_code: str):
        self.subject_code = subject_code
        self.subject_name = SUBJECTS[subject_code]["name"]
        self.last_game_stats = None
        self.active_test_id = None
        self.active_test_title = ""
        self.active_test_chapter = ""
        self.questions: list[dict[str, Any]] = []
        self.total_q = 0
        self.session_name = ""
        self.current_token = ""
        self.game_code = ""
        self.game_code_enabled = False
        self.reset_runtime_state(clear_players=True)

    def set_active_test(self, test_data: dict[str, Any] | None) -> None:
        self.active_test_id = test_data.get("id") if test_data else None
        self.active_test_title = test_data.get("title", "") if test_data else ""
        self.active_test_chapter = test_data.get("chapter", "") if test_data else ""
        self.questions = list(test_data.get("questions", [])) if test_data else []
        self.total_q = len(self.questions)

    def reset_runtime_state(self, *, clear_players: bool) -> None:
        self.phase = "lobby"
        self.current_q = 0
        self.question_start_time = 0
        self.game_code = ""
        self.game_code_enabled = False
        if clear_players:
            self.players = {}
            self.current_token = ""
        self.host_ws = None
        self.host_visitor = None
        self.answers_this_round = {}
        self.question_timer_task = None
        self.paused = False

    def archive_stats(self) -> None:
        if not self.players:
            return
        self.last_game_stats = {
            "subject_code": self.subject_code,
            "subject_name": self.subject_name,
            "test_id": self.active_test_id,
            "test_title": self.active_test_title,
            "test_chapter": self.active_test_chapter,
            "session_name": self.session_name,
            "timestamp": datetime.now().isoformat(),
            "questions": self.questions,
            "players": {}
        }
        for vid, p in self.players.items():
            self.last_game_stats["players"][vid] = {
                "name": p["name"],
                "student_number": p.get("student_number", ""),
                "score": p["score"],
                "answers": p["answers"]
            }


rooms: dict[str, GameRoom] = {code: GameRoom(code) for code in SUBJECTS}
session_tokens: dict[str, dict[str, Any]] = {}
SESSION_TOKEN_TTL = 60 * 90
SESSION_TOKEN_LENGTH = 6
SESSION_TOKEN_ALPHABET = string.ascii_uppercase + string.digits


def consume_session_token(token: str) -> None:
    normalized = (token or "").strip().upper()
    if not normalized:
        return
    session_tokens.pop(normalized, None)


def generate_session_token(subject_code: str) -> str:
    normalized = (subject_code or "").strip().upper()
    to_remove = [token for token, entry in list(session_tokens.items()) if entry.get("subject_code") == normalized]
    for token in to_remove:
        consume_session_token(token)
    token = ""
    while not token or token in session_tokens:
        token = "".join(secrets.choice(SESSION_TOKEN_ALPHABET) for _ in range(SESSION_TOKEN_LENGTH))
    session_tokens[token] = {
        "subject_code": normalized,
        "expires_at": time.time() + SESSION_TOKEN_TTL
    }
    return token


def lookup_session_token(token: str) -> str | None:
    normalized = (token or "").strip().upper()
    if not normalized:
        return None
    entry = session_tokens.get(normalized)
    if not entry:
        return None
    if time.time() > float(entry.get("expires_at") or 0):
        consume_session_token(normalized)
        return None
    subject_code = (entry.get("subject_code") or "").strip().upper()
    return subject_code or None


def get_room_active_token(room: GameRoom | None) -> str:
    if room is None:
        return ""
    token = (room.current_token or "").strip().upper()
    if not token:
        return ""
    if lookup_session_token(token) == room.subject_code:
        return token
    return ""


def generate_game_code() -> str:
    """Generate a random 4-digit numeric code."""
    return f"{secrets.randbelow(9000) + 1000}"


async def _cleanup_expired_tokens() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [token for token, entry in list(session_tokens.items()) if now > float(entry.get("expires_at") or 0)]
        for token in expired:
            consume_session_token(token)


def register_subject_in_catalog(code: str, name: str) -> None:
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not code or not name or code in BUILTIN_SUBJECT_CODES:
        return
    entry = SUBJECTS.get(code)
    if entry:
        entry["name"] = name
        entry.setdefault("questions", [])
    else:
        SUBJECTS[code] = {"code": code, "name": name, "questions": []}
    room = rooms.get(code)
    if room:
        room.subject_name = SUBJECTS[code]["name"]
    else:
        rooms[code] = GameRoom(code)


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        subjects = await repo.list_subjects()
        for row in subjects:
            register_subject_in_catalog(row.get("code"), row.get("name"))
    except Exception as exc:
        print(f"Failed to load subjects from Supabase: {exc}")
    cleanup_task = asyncio.create_task(_cleanup_expired_tokens())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if repo.remote is not None:
            try:
                await repo.remote.aclose()
            except Exception:
                pass

app = FastAPI(lifespan=lifespan)
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").strip()
allow_origins = ["*"] if ALLOWED_ORIGINS == "*" else [origin.strip() for origin in ALLOWED_ORIGINS.split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allow_origins, allow_methods=["*"], allow_headers=["*"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/style.css")
def style_css():
    return FileResponse(BASE_DIR / "style.css", media_type="text/css")


@app.get("/app.js")
def app_js():
    return FileResponse(BASE_DIR / "app.js", media_type="application/javascript")


def public_lecturer_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "Lecturer",
        "email": row.get("email"),
    }


def set_session_cookie(response: JSONResponse, lecturer_id: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_token(lecturer_id),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


async def current_lecturer_from_request(request: Request) -> dict[str, Any] | None:
    lecturer_id = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if not lecturer_id:
        return None
    return await repo.get_lecturer_by_id(lecturer_id)


async def require_lecturer(request: Request) -> dict[str, Any]:
    lecturer = await current_lecturer_from_request(request)
    if not lecturer:
        raise HTTPException(status_code=401, detail="Lecturer sign-in required")
    return lecturer


async def current_lecturer_from_websocket(websocket: WebSocket) -> dict[str, Any] | None:
    lecturer_id = parse_session_token(websocket.cookies.get(SESSION_COOKIE_NAME))
    if not lecturer_id:
        return None
    return await repo.get_lecturer_by_id(lecturer_id)


@app.get("/api/health")
def health():
    return {"ok": True, "storage": repo.get_storage_status()}


@app.get("/api/storage-status")
def storage_status():
    return repo.get_storage_status()


@app.get("/api/lecturer/session")
async def lecturer_session(request: Request):
    lecturer = await current_lecturer_from_request(request)
    return {"authenticated": bool(lecturer), "lecturer": public_lecturer_view(lecturer) if lecturer else None}


@app.post("/api/lecturer/signup")
@limiter.limit("5/minute")
async def lecturer_signup(payload: dict[str, Any], request: Request):
    try:
        validated = LecturerSignupPayload.model_validate(payload)
        lecturer = await repo.create_lecturer(validated)
        response = JSONResponse({"ok": True, "lecturer": public_lecturer_view(lecturer)})
        set_session_cookie(response, lecturer["id"], request)
        return response
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/lecturer/login")
@limiter.limit("10/minute")
async def lecturer_login(payload: dict[str, Any], request: Request):
    try:
        validated = LecturerLoginPayload.model_validate(payload)
        lecturer = await repo.get_lecturer_by_email(validated.email)
        if not lecturer:
            if repo.supabase_unavailable():
                raise HTTPException(status_code=503, detail="Supabase is unavailable. Please try again once it is restored.")
            raise HTTPException(status_code=401, detail="Incorrect email or password")
        if not verify_password(validated.password, lecturer.get("password_hash", "")):
            raise HTTPException(status_code=401, detail="Incorrect email or password")
        response = JSONResponse({"ok": True, "lecturer": public_lecturer_view(lecturer)})
        set_session_cookie(response, lecturer["id"], request)
        return response
    except HTTPException:
        raise
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/lecturer/logout")
def lecturer_logout():
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@app.get("/api/subjects")
async def get_subjects():
    result = []
    for code, info in SUBJECTS.items():
        tests = await repo.list_tests(code)
        builtin_questions = len(info.get("questions", []))
        total_questions = sum(t.get("questionCount", 0) for t in tests) or builtin_questions
        result.append({
            "code": code,
            "name": info["name"],
            "questionCount": total_questions,
            "testCount": len(tests)
        })
    result.sort(key=lambda item: item["name"].lower())
    return result


@app.post("/api/session-token/{subject_code}")
async def create_session_token_endpoint(subject_code: str, request: Request):
    await require_lecturer(request)
    normalized = (subject_code or "").strip().upper()
    if normalized not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    token = generate_session_token(normalized)
    room = rooms.get(normalized)
    if room:
        room.current_token = token
    return {"ok": True, "token": token, "subject_code": normalized}


@app.get("/api/session-token/{token}/validate")
async def validate_session_token_endpoint(token: str):
    subject_code = lookup_session_token(token)
    if not subject_code:
        raise HTTPException(status_code=404, detail="This session link has expired or is invalid. Ask your lecturer for the current QR code.")
    subject = SUBJECTS.get(subject_code)
    return {
        "valid": True,
        "subject_code": subject_code,
        "subject_name": subject["name"] if subject else subject_code
    }


@app.post("/api/subjects")
async def create_subject(payload: dict[str, Any], request: Request):
    lecturer = await require_lecturer(request)
    try:
        validated = SubjectPayload.model_validate(payload)
        code = validated.code
        if code in SUBJECTS:
            raise HTTPException(status_code=409, detail="Subject code already exists.")
        created = await repo.create_subject(code, validated.name, lecturer)
        register_subject_in_catalog(code, created.get("name") or validated.name)
        return {"ok": True, "subject": {"code": code, "name": created.get("name") or validated.name}}
    except HTTPException:
        raise
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/subjects/{code}")
async def delete_subject(code: str, request: Request):
    lecturer = await require_lecturer(request)
    normalized = (code or "").strip().upper()
    if normalized in BUILTIN_SUBJECT_CODES:
        raise HTTPException(status_code=403, detail="Built-in subjects cannot be deleted.")
    if normalized not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    try:
        await repo.delete_subject(normalized, lecturer)
        for token, entry in list(session_tokens.items()):
            if (entry.get("subject_code") or "").strip().upper() == normalized:
                consume_session_token(token)
        SUBJECTS.pop(normalized, None)
        rooms.pop(normalized, None)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Subject not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tests/{subject_code}")
async def get_tests(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        return await repo.list_tests(subject_code, lecturer.get("id"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tests/{subject_code}/{test_id}")
async def get_test_detail(subject_code: str, test_id: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        row = await repo.get_test(subject_code, test_id, lecturer.get("id"))
        if not row:
            raise HTTPException(status_code=404, detail="Test not found")
        return {
            "id": row["id"],
            "subject_code": row["subject_code"],
            "title": row.get("title") or "",
            "chapter": row.get("chapter") or "",
            "description": row.get("description") or "",
            "questions": row.get("questions") or [],
            "questionCount": row.get("question_count") or len(row.get("questions") or []),
            "source": row.get("source", "supabase"),
            "ownerName": row.get("owner_name") or "System",
            "canEdit": bool(row.get("can_edit") or (row.get("created_by") == lecturer.get("id"))),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/tests/{subject_code}")
async def create_test(subject_code: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        validated = TestPayload.model_validate(payload)
        created = await repo.create_test(subject_code, validated, lecturer)
        try:
            await repo.clear_draft(subject_code, lecturer)
        except Exception:
            pass  # Draft clear is non-critical — never let it mask a successful test save
        return {"ok": True, "test": repo._summary(created, lecturer.get("id"))}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/tests/{subject_code}/{test_id}")
async def update_test(subject_code: str, test_id: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        validated = TestPayload.model_validate(payload)
        updated = await repo.update_test(subject_code, test_id, validated, lecturer)
        try:
            await repo.clear_draft(subject_code, lecturer)
        except Exception:
            pass  # Draft clear is non-critical — never let it mask a successful test update
        return {"ok": True, "test": repo._summary(updated, lecturer.get("id"))}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except KeyError:
        raise HTTPException(status_code=404, detail="Test not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/tests/{subject_code}/{test_id}")
async def delete_test(subject_code: str, test_id: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        await repo.delete_test(subject_code, test_id, lecturer)
        return {"ok": True}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Test not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/drafts/{subject_code}")
async def get_test_draft(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        draft = await repo.get_draft(subject_code, lecturer)
        return {"draft": draft}
    except Exception:
        # If the draft table doesn't exist yet, return no draft rather than erroring.
        return {"draft": None}


@app.post("/api/drafts/{subject_code}")
async def save_test_draft(subject_code: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        validated = DraftPayload.model_validate(payload)
        draft = await repo.save_draft(subject_code, lecturer, validated)
        return {"ok": True, "draft": draft}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except Exception:
        # Draft save failure is non-critical. Return a soft ok so autosave
        # errors never break the editor UI or cascade into test operations.
        return {"ok": False, "draft": None, "error": "Draft could not be saved to storage (non-critical)."}


@app.delete("/api/drafts/{subject_code}")
async def clear_test_draft(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = await require_lecturer(request)
    try:
        await repo.clear_draft(subject_code, lecturer)
        return {"ok": True}
    except Exception:
        # Always return ok — draft clearing is never worth surfacing as an error.
        return {"ok": True}


@app.get("/api/export/tests")
async def export_tests(request: Request):
    lecturer = await require_lecturer(request)
    try:
        tests = await repo.list_tests_by_creator(lecturer["id"])
        stamp = datetime.utcnow().strftime("%Y%m%d")
        filename = f"quiz_backup_{stamp}.json"
        payload = json.dumps(tests, ensure_ascii=False, indent=2)
        headers = {"Content-Disposition": f"attachment; filename={filename}"}
        return Response(content=payload, media_type="application/json", headers=headers)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/stats/{subject_code}")
def download_stats(subject_code: str):
    if subject_code not in rooms:
        raise HTTPException(status_code=404, detail="Subject not found")

    room = rooms[subject_code]
    stats = room.last_game_stats
    if not stats or not stats["players"]:
        raise HTTPException(status_code=404, detail="No game data available. Play a game first.")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed")

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Student Results"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="2E4057", end_color="2E4057", fill_type="solid")
    correct_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    wrong_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    no_answer_fill = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
    thin_border = Border(left=Side(style="thin"), right=Side(style="thin"), top=Side(style="thin"), bottom=Side(style="thin"))

    questions = stats["questions"]
    num_q = len(questions)
    headers = ["Rank", "Student Name", "Student Number", "Total Score"]
    for i in range(num_q):
        headers.append(f"Q{i+1}")
        headers.append(f"Q{i+1} Time (s)")
    headers.extend(["Questions Correct", "Accuracy %"])

    for col, heading in enumerate(headers, 1):
        cell = ws1.cell(row=1, column=col, value=heading)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = thin_border

    sorted_players = sorted(stats["players"].values(), key=lambda p: -p["score"])
    for rank, player in enumerate(sorted_players, 1):
        row = rank + 1
        ws1.cell(row=row, column=1, value=rank).border = thin_border
        ws1.cell(row=row, column=2, value=player["name"]).border = thin_border
        ws1.cell(row=row, column=3, value=player.get("student_number", "")).border = thin_border
        ws1.cell(row=row, column=4, value=player["score"]).border = thin_border
        correct_count = 0
        col_offset = 5
        for qi in range(num_q):
            result_col = col_offset + qi * 2
            time_col = result_col + 1
            if qi < len(player["answers"]):
                ans = player["answers"][qi]
                is_correct = ans.get("correct", False)
                points = ans.get("points", 0)
                time_taken = round(ans.get("time", 0), 1) if ans.get("time") else "-"
                if is_correct:
                    correct_count += 1
                result_cell = ws1.cell(row=row, column=result_col)
                if ans.get("choice", -1) == -1:
                    result_cell.value = "No answer"
                    result_cell.fill = no_answer_fill
                elif is_correct:
                    result_cell.value = f"✓ (+{points})"
                    result_cell.fill = correct_fill
                else:
                    chosen = ans.get("choice", -1)
                    if 0 <= chosen < len(questions[qi]["options"]):
                        result_cell.value = f"✗ ({questions[qi]['options'][chosen][:20]})"
                    else:
                        result_cell.value = "✗"
                    result_cell.fill = wrong_fill
                result_cell.border = thin_border
                result_cell.alignment = Alignment(horizontal="center")
                time_cell = ws1.cell(row=row, column=time_col, value=time_taken)
                time_cell.border = thin_border
                time_cell.alignment = Alignment(horizontal="center")
            else:
                ws1.cell(row=row, column=result_col, value="-").border = thin_border
                ws1.cell(row=row, column=time_col, value="-").border = thin_border
        summary_col = col_offset + num_q * 2
        ws1.cell(row=row, column=summary_col, value=f"{correct_count}/{num_q}").border = thin_border
        accuracy = round((correct_count / num_q) * 100, 1) if num_q else 0
        ws1.cell(row=row, column=summary_col + 1, value=f"{accuracy}%").border = thin_border

    for col in ws1.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws1.column_dimensions[col_letter].width = min(max_len + 3, 28)

    ws2 = wb.create_sheet("Question Analysis")
    q_headers = ["Question #", "Question Text", "Correct Answer", "# Correct", "# Wrong", "# No Answer", "% Correct", "Avg Time (s)"]
    for col, heading in enumerate(q_headers, 1):
        cell = ws2.cell(row=1, column=col, value=heading)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = thin_border

    for qi, question in enumerate(questions):
        row = qi + 2
        correct_count = wrong_count = no_answer_count = 0
        total_time = 0
        time_count = 0
        for player in stats["players"].values():
            if qi < len(player["answers"]):
                ans = player["answers"][qi]
                if ans.get("choice", -1) == -1:
                    no_answer_count += 1
                elif ans.get("correct"):
                    correct_count += 1
                    if ans.get("time"):
                        total_time += ans["time"]
                        time_count += 1
                else:
                    wrong_count += 1
                    if ans.get("time"):
                        total_time += ans["time"]
                        time_count += 1
        total_answered = correct_count + wrong_count + no_answer_count
        pct_correct = round((correct_count / total_answered) * 100, 1) if total_answered else 0
        avg_time = round(total_time / time_count, 1) if time_count else "-"
        ws2.cell(row=row, column=1, value=qi + 1).border = thin_border
        ws2.cell(row=row, column=2, value=question["q"][:100]).border = thin_border
        ws2.cell(row=row, column=3, value=question["options"][question["correct"]]).border = thin_border
        ws2.cell(row=row, column=4, value=correct_count).border = thin_border
        ws2.cell(row=row, column=5, value=wrong_count).border = thin_border
        ws2.cell(row=row, column=6, value=no_answer_count).border = thin_border
        pct_cell = ws2.cell(row=row, column=7, value=f"{pct_correct}%")
        pct_cell.border = thin_border
        if pct_correct < 50:
            pct_cell.fill = wrong_fill
        elif pct_correct >= 80:
            pct_cell.fill = correct_fill
        ws2.cell(row=row, column=8, value=avg_time).border = thin_border

    for col in ws2.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws2.column_dimensions[col_letter].width = min(max_len + 3, 32)

    summary = wb.create_sheet("Game Summary")
    summary["A1"] = "Subject"
    summary["B1"] = stats["subject_name"]
    summary["A2"] = "Subject Code"
    summary["B2"] = stats["subject_code"]
    summary["A3"] = "Test Title"
    summary["B3"] = stats.get("test_title") or ""
    summary["A4"] = "Chapter"
    summary["B4"] = stats.get("test_chapter") or ""
    summary["A5"] = "Played At"
    summary["B5"] = stats["timestamp"]
    summary["A6"] = "Players"
    summary["B6"] = len(stats["players"])
    summary["A7"] = "Questions"
    summary["B7"] = len(stats["questions"])

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    def _safe_filename_part(value: str) -> str:
        """Strip non-ASCII and filesystem-unsafe characters, replace spaces with underscores."""
        import unicodedata
        # Normalise accented characters to their ASCII base where possible
        value = unicodedata.normalize("NFKD", value)
        # Keep only printable ASCII, replace spaces, drop the rest
        result = []
        for ch in value:
            if ch == " ":
                result.append("_")
            elif ch in r'/\:*?"<>|':
                result.append("-")
            elif 0x20 <= ord(ch) <= 0x7E:
                result.append(ch)
            # Non-ASCII characters (e.g. em dash —) are silently dropped
        return "".join(result)[:60].strip("_-") or "Stats"

    safe_title = _safe_filename_part(stats.get("test_title") or subject_code)
    safe_session = _safe_filename_part(room.last_game_stats.get("session_name") or safe_title)
    filename = f"Stats_{subject_code}_{safe_session}.xlsx"
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket helpers
# ──────────────────────────────────────────────────────────────────────────────
def get_active_test_meta(room: GameRoom) -> dict[str, Any] | None:
    if not room.active_test_id:
        return None
    return {
        "id": room.active_test_id,
        "title": room.active_test_title,
        "chapter": room.active_test_chapter,
        "questionCount": room.total_q,
    }


def is_participating_player(room: GameRoom, player: dict[str, Any] | None) -> bool:
    if not room.game_code_enabled:
        return True
    return bool(player and player.get("game_code_verified"))


def find_existing_player(
    room: GameRoom,
    *,
    visitor_id: str,
    name: str,
    student_number: str
) -> tuple[str | None, dict[str, Any] | None]:
    name_lower = name.lower()
    match_by_student = bool(student_number)
    for vid, player in room.players.items():
        if vid == visitor_id:
            continue
        if match_by_student:
            if player.get("student_number") == student_number:
                return vid, player
        elif player.get("name", "").lower() == name_lower:
            return vid, player
    return None, None


def build_joined_payload(room: GameRoom, visitor_id: str) -> dict[str, Any]:
    joined_payload = {
        "type": "joined",
        "phase": room.phase,
        "playerId": visitor_id,
        "playerCount": len(room.players),
        "activeTest": get_active_test_meta(room)
    }
    if room.phase == "question":
        q = room.questions[room.current_q]
        elapsed = time.time() - room.question_start_time
        remaining = max(0, TIME_PER_Q - elapsed)
        joined_payload["currentQuestion"] = {
            "question": q["q"],
            "options": q["options"],
            "qNum": room.current_q + 1,
            "totalQ": room.total_q,
            "timeLimit": TIME_PER_Q,
            "remaining": round(remaining, 2)
        }
        joined_payload["alreadyAnswered"] = visitor_id in room.answers_this_round
    elif room.phase in ("reveal", "get_ready", "final"):
        joined_payload["phase"] = room.phase
    return joined_payload


def get_player_list(room: GameRoom, *, participant_only: bool = False) -> list[dict[str, Any]]:
    players = []
    for vid, p in room.players.items():
        if participant_only and not is_participating_player(room, p):
            continue
        players.append({
            "id": vid,
            "name": p["name"],
            "score": p["score"],
            "connected": p.get("ws") is not None
        })
    players.sort(key=lambda x: -x["score"])
    return players


def get_leaderboard(room: GameRoom, *, participant_only: bool = False) -> list[dict[str, Any]]:
    players = get_player_list(room, participant_only=participant_only)
    for i, p in enumerate(players):
        p["rank"] = i + 1
    return players


async def broadcast_to_players(room: GameRoom, msg: dict[str, Any], *, participant_only: bool = False) -> None:
    async def _safe_send(ws: WebSocket):
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass

    tasks = []
    for _, player in list(room.players.items()):
        if participant_only and not is_participating_player(room, player):
            continue
        ws = player.get("ws")
        if ws is not None:
            tasks.append(_safe_send(ws))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def broadcast_to_selected_players(room: GameRoom, msg: dict[str, Any], player_ids: set[str]) -> None:
    async def _safe_send(ws: WebSocket):
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass

    tasks = []
    for vid, player in list(room.players.items()):
        if vid not in player_ids:
            continue
        ws = player.get("ws")
        if ws is not None:
            tasks.append(_safe_send(ws))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def send_to_host(room: GameRoom, msg: dict[str, Any]) -> None:
    if room.host_ws is None:
        return
    try:
        await room.host_ws.send_text(json.dumps(msg))
    except Exception:
        room.host_ws = None


async def push_room_update(room: GameRoom) -> None:
    payload = {
        "type": "player_update",
        "players": get_player_list(room),
        "activeTest": get_active_test_meta(room)
    }
    await send_to_host(room, payload)
    await broadcast_to_players(room, payload)


def mark_unanswered_players(room: GameRoom) -> None:
    for vid in room.players:
        if not is_participating_player(room, room.players.get(vid)):
            continue
        if vid not in room.answers_this_round:
            room.players[vid]["streak"] = 0
            room.players[vid]["answers"].append({
                "q": room.current_q,
                "choice": -1,
                "correct": False,
                "points": 0,
                "time": 0
            })


async def sync_answer_count(room: GameRoom) -> None:
    if room.phase != "question":
        return
    answered_count = len(room.answers_this_round)
    total_connected = sum(
        1
        for _, player in room.players.items()
        if player.get("ws") is not None and is_participating_player(room, player)
    )
    await send_to_host(room, {
        "type": "answer_count",
        "answered": answered_count,
        "total": total_connected
    })


async def maybe_finish_question_early(room: GameRoom) -> None:
    if room.phase != "question":
        return
    answered_count = len(room.answers_this_round)
    total_connected = sum(
        1
        for _, player in room.players.items()
        if player.get("ws") is not None and is_participating_player(room, player)
    )
    if answered_count >= total_connected and total_connected > 0:
        if room.question_timer_task and not room.question_timer_task.done():
            room.question_timer_task.cancel()
        mark_unanswered_players(room)
        await auto_reveal(room)


async def kick_player_from_room(room: GameRoom, player_id: str, *, message: str) -> bool:
    player = room.players.pop(player_id, None)
    if not player:
        return False
    room.answers_this_round.pop(player_id, None)
    ws = player.get("ws")
    if ws is not None:
        try:
            await ws.send_text(json.dumps({
                "type": "kicked",
                "message": message
            }))
        except Exception:
            pass
        try:
            await ws.close(code=4002)
        except Exception:
            pass
    await push_room_update(room)
    await sync_answer_count(room)
    await maybe_finish_question_early(room)
    return True


async def cancel_question_timer(room: GameRoom) -> None:
    if room.question_timer_task and not room.question_timer_task.done():
        room.question_timer_task.cancel()
        try:
            await room.question_timer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    room.question_timer_task = None


async def return_room_to_lobby(room: GameRoom, *, keep_players: bool) -> None:
    # Generate a fresh session token (this also consumes the previous one)
    new_token = generate_session_token(room.subject_code)
    room.current_token = new_token
    room.game_code = ""
    room.game_code_enabled = False
    await cancel_question_timer(room)
    room.phase = "lobby"
    room.current_q = 0
    room.question_start_time = 0
    room.answers_this_round = {}
    room.paused = False

    if keep_players:
        for player in room.players.values():
            player["score"] = 0
            player["streak"] = 0
            player["answers"] = []
        await broadcast_to_players(room, {
            "type": "reset",
            "phase": "lobby",
            "playerCount": len(room.players),
            "activeTest": get_active_test_meta(room)
        })
    else:
        await broadcast_to_players(room, {
            "type": "reset",
            "phase": "lobby",
            "playerCount": 0,
            "activeTest": get_active_test_meta(room)
        })
        room.players = {}

    await send_to_host(room, {
        "type": "host_joined",
        "phase": "lobby",
        "players": get_player_list(room),
        "currentQ": 0,
        "totalQ": room.total_q,
        "subjectCode": room.subject_code,
        "subjectName": room.subject_name,
        "selectedTest": get_active_test_meta(room),
        "gameCode": room.game_code,
        "gameCodeEnabled": room.game_code_enabled,
        "hasQuestions": room.total_q > 0,
        "hasStats": room.last_game_stats is not None,
        "sessionToken": new_token
    })
    await push_room_update(room)


async def force_end_game(room: GameRoom) -> None:
    lb = get_leaderboard(room, participant_only=True)
    participant_ids = {
        vid for vid, player in room.players.items()
        if is_participating_player(room, player)
    }
    non_participant_ids = set(room.players.keys()) - participant_ids
    active_token = get_room_active_token(room)
    if active_token:
        consume_session_token(active_token)
    room.game_code = ""
    room.game_code_enabled = False
    await cancel_question_timer(room)
    room.phase = "final"
    room.archive_stats()
    await broadcast_to_selected_players(room, {"type": "final", "leaderboard": lb}, participant_ids)
    await broadcast_to_selected_players(room, {"type": "game_ended"}, non_participant_ids)
    await send_to_host(room, {"type": "final", "leaderboard": lb, "hasStats": True})
    room.players = {}


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    visitor_id = (
        websocket.headers.get("x-visitor-id")
        or websocket.query_params.get("visitorId")
        or str(uuid.uuid4())
    )
    role = None
    room = None

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "host_join":
                lecturer = await current_lecturer_from_websocket(websocket)
                if not lecturer:
                    await websocket.send_text(json.dumps({"type": "auth_required", "message": "Lecturer sign-in required"}))
                    continue
                subject_code = msg.get("subject")
                test_id = msg.get("testId")
                host_token = (msg.get("token") or "").strip().upper()
                if subject_code not in rooms:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Invalid subject"}))
                    continue
                test_data = await repo.get_test(subject_code, test_id) if test_id else None
                if not test_data:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Test not found"}))
                    continue

                role = "host"
                room = rooms[subject_code]
                requested_new_test = bool(test_id and test_id != room.active_test_id)
                if room.phase == "lobby" or (room.phase == "final" and requested_new_test):
                    room.set_active_test(test_data)
                room.session_name = msg.get("sessionName", "").strip()[:80] or room.active_test_title
                if host_token and lookup_session_token(host_token) == subject_code:
                    room.current_token = host_token
                elif room.phase == "lobby":
                    room.current_token = ""
                room.host_ws = websocket
                room.host_visitor = visitor_id

                if room.phase == "final" and requested_new_test:
                    await return_room_to_lobby(room, keep_players=True)
                    continue

                await websocket.send_text(json.dumps({
                    "type": "host_joined",
                    "phase": room.phase,
                    "players": get_player_list(room),
                    "currentQ": room.current_q,
                    "totalQ": room.total_q,
                    "subjectCode": room.subject_code,
                    "subjectName": room.subject_name,
                    "selectedTest": get_active_test_meta(room),
                    "gameCode": room.game_code,
                    "gameCodeEnabled": room.game_code_enabled,
                    "hasQuestions": room.total_q > 0,
                    "hasStats": room.last_game_stats is not None
                }))
                await push_room_update(room)

            elif action == "start_game":
                if role != "host" or room is None:
                    continue
                if room.total_q == 0:
                    await websocket.send_text(json.dumps({"type": "error", "message": "No questions loaded for this test."}))
                    continue
                use_code = msg.get("useCode", False)
                if use_code:
                    room.game_code = generate_game_code()
                    room.game_code_enabled = True
                    for player in room.players.values():
                        player["game_code_verified"] = False
                else:
                    room.game_code = ""
                    room.game_code_enabled = False
                    for player in room.players.values():
                        player["game_code_verified"] = True
                if room.game_code_enabled:
                    await send_to_host(room, {
                        "type": "game_code_display",
                        "code": room.game_code,
                        "countdown": 20
                    })
                    await broadcast_to_players(room, {
                        "type": "game_code_required",
                        "countdown": 20
                    })
                    await asyncio.sleep(20)
                room.paused = False
                import random
                if msg.get("shuffle"):
                    random.shuffle(room.questions)
                room.phase = "get_ready"
                room.current_q = 0
                for vid in room.players:
                    room.players[vid]["score"] = 0
                    room.players[vid]["streak"] = 0
                    room.players[vid]["answers"] = []
                await broadcast_to_players(room, {"type": "get_ready", "qNum": 1, "totalQ": room.total_q}, participant_only=True)
                await send_to_host(room, {"type": "get_ready", "qNum": 1, "totalQ": room.total_q})
                await asyncio.sleep(3)
                await send_question(room)

            elif action == "next_question":
                if role == "host" and room is not None:
                    await advance_to_next(room)

            elif action == "host_pause":
                if role != "host" or room is None:
                    continue
                room.paused = not room.paused
                await send_to_host(room, {"type": "pause_state", "paused": room.paused})
                await broadcast_to_players(room, {
                    "type": "pause_state",
                    "paused": room.paused
                })

            elif action == "reset_game":
                if role == "host" and room is not None:
                    room.archive_stats()
                    await return_room_to_lobby(room, keep_players=True)

            elif action == "cancel_game":
                if role == "host" and room is not None:
                    await return_room_to_lobby(room, keep_players=True)

            elif action == "end_game":
                if role == "host" and room is not None:
                    await force_end_game(room)

            elif action == "kick_player":
                if role != "host" or room is None:
                    continue
                player_id = (msg.get("playerId") or "").strip()
                if not player_id:
                    continue
                await kick_player_from_room(
                    room,
                    player_id,
                    message="The lecturer removed you from this session."
                )

            elif action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif action == "player_join":
                token = (msg.get("token") or "").strip().upper()
                subject_code_from_token = lookup_session_token(token) if token else None
                subject_code = subject_code_from_token or (msg.get("subject") or "").strip().upper()
                if subject_code not in rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "This session link has expired or is invalid. Ask your lecturer for the current QR code."
                    }))
                    continue
                room = rooms[subject_code]
                name = msg.get("name", "Anonymous").strip()[:20]
                student_number = msg.get("studentNumber", "").strip()[:20]
                provided_code = (msg.get("gameCode") or "").strip()
                existing_vid, existing_player = find_existing_player(
                    room,
                    visitor_id=visitor_id,
                    name=name,
                    student_number=student_number
                )
                required_room_token = (room.current_token or "").strip().upper()
                is_known_player = visitor_id in room.players or existing_player is not None
                # Reject on expired/invalid token only for players not already in the room.
                # Known players (already mid-game) must always be allowed to reconnect.
                if token and not subject_code_from_token and not is_known_player:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "This session link has expired. Ask your lecturer for the current QR code."
                    }))
                    continue
                if required_room_token and not subject_code_from_token and not is_known_player:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "This session link has expired or is invalid. Ask your lecturer for the current QR code."
                    }))
                    continue
                current_player = room.players.get(visitor_id)
                can_bypass_game_code = (
                    is_participating_player(room, existing_player)
                    or is_participating_player(room, current_player)
                )
                if room.game_code_enabled and not can_bypass_game_code and provided_code != room.game_code:
                    await websocket.send_text(json.dumps({
                        "type": "error_game_code",
                        "message": "Enter the 4-digit code shown on the lecturer screen to continue."
                    }))
                    continue
                if room.phase != "lobby" and not is_known_player:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "The game is already in progress. You cannot join as a new player at this stage."
                    }))
                    continue
                if subject_code_from_token and room.current_token != token:
                    room.current_token = token
                role = "player"
                if existing_player:
                    old_ws = existing_player.get("ws")
                    if old_ws is not None:
                        try:
                            await old_ws.close(code=4001)
                        except Exception:
                            pass
                    room.players.pop(existing_vid, None)
                    existing_player["name"] = name
                    existing_player["student_number"] = student_number
                    existing_player["ws"] = websocket
                    existing_player["game_code_verified"] = (
                        is_participating_player(room, existing_player)
                        or not room.game_code_enabled
                        or provided_code == room.game_code
                    )
                    room.players[visitor_id] = existing_player
                elif visitor_id in room.players:
                    room.players[visitor_id]["ws"] = websocket
                    room.players[visitor_id]["name"] = name
                    room.players[visitor_id]["student_number"] = student_number
                    room.players[visitor_id]["game_code_verified"] = (
                        is_participating_player(room, room.players[visitor_id])
                        or not room.game_code_enabled
                        or provided_code == room.game_code
                    )
                else:
                    room.players[visitor_id] = {
                        "name": name,
                        "student_number": student_number,
                        "score": 0,
                        "streak": 0,
                        "answers": [],
                        "ws": websocket,
                        "game_code_verified": (not room.game_code_enabled or provided_code == room.game_code)
                    }
                await websocket.send_text(json.dumps(build_joined_payload(room, visitor_id)))
                await push_room_update(room)
                await sync_answer_count(room)

            elif action == "verify_game_code":
                if role != "player" or room is None or visitor_id not in room.players:
                    continue
                if not room.game_code_enabled:
                    await websocket.send_text(json.dumps(build_joined_payload(room, visitor_id)))
                    continue
                provided_code = (msg.get("gameCode") or "").strip()
                if provided_code != room.game_code:
                    await websocket.send_text(json.dumps({
                        "type": "error_game_code",
                        "message": "Enter the 4-digit code shown on the lecturer screen to continue."
                    }))
                    continue
                room.players[visitor_id]["game_code_verified"] = True
                await websocket.send_text(json.dumps(build_joined_payload(room, visitor_id)))
                await push_room_update(room)
                await sync_answer_count(room)

            elif action == "player_leave":
                if role != "player" or room is None:
                    continue
                room.players.pop(visitor_id, None)
                await websocket.send_text(json.dumps({"type": "left"}))
                await push_room_update(room)
                await sync_answer_count(room)
                await maybe_finish_question_early(room)
                await websocket.close()
                break

            elif action == "answer":
                if role != "player" or room is None or room.phase != "question":
                    continue
                if not is_participating_player(room, room.players.get(visitor_id)):
                    await websocket.send_text(json.dumps({
                        "type": "error_game_code",
                        "message": "Enter the 4-digit code shown on the lecturer screen to continue."
                    }))
                    continue
                if visitor_id in room.answers_this_round:
                    continue
                choice = msg.get("choice", -1)
                answer_time = time.time() - room.question_start_time
                room.answers_this_round[visitor_id] = {"choice": choice, "time": answer_time}

                q = room.questions[room.current_q]
                is_correct = choice == q["correct"]
                points = 0
                if is_correct:
                    time_fraction = min(answer_time / TIME_PER_Q, 1.0)
                    points = round(MAX_POINTS - (MAX_POINTS - MIN_POINTS) * time_fraction)
                    room.players[visitor_id]["streak"] += 1
                    if room.players[visitor_id]["streak"] >= 3:
                        points = round(points * 1.2)
                else:
                    room.players[visitor_id]["streak"] = 0
                room.players[visitor_id]["score"] += points
                room.players[visitor_id]["answers"].append({
                    "q": room.current_q,
                    "choice": choice,
                    "correct": is_correct,
                    "points": points,
                    "time": answer_time
                })
                await websocket.send_text(json.dumps({
                    "type": "answer_result",
                    "correct": is_correct,
                    "points": points,
                    "totalScore": room.players[visitor_id]["score"],
                    "streak": room.players[visitor_id]["streak"],
                    "correctAnswer": q["correct"],
                    "explanation": q["explanation"]
                }))
                await sync_answer_count(room)
                await maybe_finish_question_early(room)

    except WebSocketDisconnect:
        if role == "host" and room:
            room.host_ws = None
        elif role == "player" and room and visitor_id in room.players:
            if room.phase == "lobby":
                room.players.pop(visitor_id, None)
            else:
                room.players[visitor_id]["ws"] = None
            await push_room_update(room)
            await sync_answer_count(room)
            await maybe_finish_question_early(room)
    except Exception:
        if role == "host" and room:
            room.host_ws = None
        elif role == "player" and room and visitor_id in room.players:
            if room.phase == "lobby":
                room.players.pop(visitor_id, None)
            else:
                room.players[visitor_id]["ws"] = None
            await push_room_update(room)
            await sync_answer_count(room)
            await maybe_finish_question_early(room)


async def send_question(room: GameRoom) -> None:
    q = room.questions[room.current_q]
    q_index = room.current_q
    room.phase = "question"
    server_ts = time.time()
    room.question_start_time = server_ts
    room.answers_this_round = {}
    await broadcast_to_players(room, {
        "type": "question",
        "qNum": room.current_q + 1,
        "totalQ": room.total_q,
        "question": q["q"],
        "options": q["options"],
        "timeLimit": TIME_PER_Q,
        "serverTimestamp": server_ts
    }, participant_only=True)
    await send_to_host(room, {
        "type": "question",
        "qNum": room.current_q + 1,
        "totalQ": room.total_q,
        "question": q["q"],
        "options": q["options"],
        "correctAnswer": q["correct"],
        "timeLimit": TIME_PER_Q,
        "serverTimestamp": server_ts
    })
    await sync_answer_count(room)

    async def _timer():
        # Count elapsed time in 0.5s ticks, freezing while room.paused is True
        elapsed = 0.0
        while elapsed < TIME_PER_Q:
            await asyncio.sleep(0.5)
            if room.phase != "question" or room.current_q != q_index:
                return  # Question already advanced (e.g. all answered early)
            if not room.paused:
                elapsed += 0.5
        if room.phase == "question" and room.current_q == q_index:
            mark_unanswered_players(room)
            await auto_reveal(room)

    room.question_timer_task = asyncio.create_task(_timer())


async def auto_reveal(room: GameRoom) -> None:
    q = room.questions[room.current_q]
    room.phase = "reveal"
    lb = get_leaderboard(room, participant_only=True)
    for vid, player in room.players.items():
        if not is_participating_player(room, player):
            continue
        if vid not in room.answers_this_round:
            ws = player.get("ws")
            if ws:
                try:
                    await ws.send_text(json.dumps({
                        "type": "answer_result",
                        "correct": False,
                        "points": 0,
                        "totalScore": player["score"],
                        "correctAnswer": q["correct"],
                        "explanation": q["explanation"],
                        "timedOut": True
                    }))
                except Exception:
                    pass
    await broadcast_to_players(room, {"type": "leaderboard", "leaderboard": lb}, participant_only=True)
    await send_to_host(room, {
        "type": "reveal",
        "correctAnswer": q["correct"],
        "explanation": q["explanation"],
        "leaderboard": lb,
        "isLast": room.current_q >= room.total_q - 1
    })
    waited = 0.0
    while True:
        await asyncio.sleep(0.5)
        if room.paused:
            continue
        waited += 0.5
        if waited >= 5.0:
            break
    if room.phase == "reveal":
        await advance_to_next(room)


async def advance_to_next(room: GameRoom) -> None:
    room.current_q += 1
    if room.current_q >= room.total_q:
        await force_end_game(room)
    else:
        room.phase = "get_ready"
        await broadcast_to_players(room, {"type": "get_ready", "qNum": room.current_q + 1, "totalQ": room.total_q}, participant_only=True)
        await send_to_host(room, {"type": "get_ready", "qNum": room.current_q + 1, "totalQ": room.total_q})
        await asyncio.sleep(3)
        await send_question(room)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
