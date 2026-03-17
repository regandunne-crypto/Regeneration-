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
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
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
SUBJECTS = {'1EM105B': {'code': '1EM105B',
             'name': 'Mechanics',
             'questions': [{'correct': 0,
                            'explanation': 'These are the SI base units for length, mass, and time. The chapter summary identifies meter, kilogram, and second as the basic SI units used to build many other units.',
                            'options': ['meter, kilogram, second',
                                        'centimeter, gram, second',
                                        'foot, slug, second',
                                        'meter, gram, minute'],
                            'q': 'Which set contains the SI base units for length, mass, and time?'},
                           {'correct': 1,
                            'explanation': 'A derived unit comes from combining base units. For example, speed uses meters and seconds, so it is not a base unit by itself.',
                            'options': ['a unit used only in laboratories',
                                        'a unit formed by combining base units',
                                        'a unit with no dimensions',
                                        'a unit used only in the British system'],
                            'q': 'A derived unit is best described as'},
                           {'correct': 1,
                            'explanation': 'Writing units through each step lets you see whether the conversion is arranged properly. If the unwanted units cancel and the wanted units remain, the setup is correct.',
                            'options': ['It changes the physical quantity into a new one',
                                        'It helps units cancel algebraically and shows whether the setup is correct',
                                        'It makes the answer more accurate automatically',
                                        'It removes the need for formulas'],
                            'q': 'Why is it useful to write units explicitly during a conversion?'},
                           {'correct': 1,
                            'explanation': 'Multiplying a vector by -1 flips its direction but does not change its size. That is exactly how negative vectors are handled in the chapter.',
                            'options': ['Its magnitude doubles and its direction stays the same',
                                        'Its magnitude stays the same and its direction reverses',
                                        'Its magnitude becomes zero',
                                        'Its x-component changes, but not its y-component'],
                            'q': 'When a vector is multiplied by (-1), what happens?'},
                           {'correct': 1,
                            'explanation': 'That is the rule for the tail-to-head method of vector addition.',
                            'options': ['head of the first vector to the tail of the last vector',
                                        'tail of the first vector to the head of the last vector',
                                        'midpoint of the first vector to the midpoint of the last vector',
                                        'longest vector to the shortest vector'],
                            'q': 'In the tail-to-head method, the resultant vector is drawn from the'},
                           {'correct': 2,
                            'explanation': 'If vectors form a closed polygon, you end where you started. That means the overall or resultant vector is zero.',
                            'options': ['equal to the longest vector',
                                        'one unit vector',
                                        'zero',
                                        'twice the shortest vector'],
                            'q': 'If several vectors form a closed polygon when placed head to tail, the resultant is'},
                           {'correct': 3,
                            'explanation': 'Two vectors are equal only if they have the same size and point in the same direction. Having just one of those is not enough.',
                            'options': ['units and components only',
                                        'direction only',
                                        'magnitude only',
                                        'magnitude and direction'],
                            'q': 'Two vectors are equal only when they have the same'},
                           {'correct': 1,
                            'explanation': 'A vector straight down along the -y axis has no horizontal part, so Ax = 0, and its vertical part is negative.',
                            'options': ['a positive x-component and zero y-component',
                                        'zero x-component and a negative y-component',
                                        'a negative x-component and zero y-component',
                                        'zero x-component and a positive y-component'],
                            'q': 'A vector pointing straight in the (-y) direction has'},
                           {'correct': 0,
                            'explanation': 'If both Ax and Ay double, the vector gets twice as large, but the ratio Ay/Ax stays the same, so the angle does not change.',
                            'options': ['magnitude doubles and its direction stays the same',
                                        'magnitude stays the same and its direction changes',
                                        'magnitude doubles and its direction reverses',
                                        'magnitude becomes four times larger'],
                            'q': "If both (Ax) and (Ay) become twice as large, the vector's"},
                           {'correct': 3,
                            'explanation': 'Once Rx and Ry are known, they form a right triangle with the resultant R. The magnitude is therefore found using the Pythagorean theorem.',
                            'options': ['law of sines',
                                        'inverse tangent function',
                                        'unit conversion method',
                                        'Pythagorean theorem'],
                            'q': 'After finding (Rx) and (Ry), the magnitude of the resultant vector is found using the'}]},
 'DYN317B': {'code': 'DYN317B', 'name': 'Dynamics', 'questions': []},
 'MEC105B': {'code': 'MEC105B',
             'name': 'Mechanics',
             'questions': [{'correct': 1,
                            'explanation': 'Statics deals with the equilibrium of bodies at rest '
                                           'or moving with constant velocity (zero acceleration).',
                            'options': ['Dynamics', 'Statics', 'Kinematics', 'Thermodynamics'],
                            'q': 'What is the study of bodies at rest or moving with constant '
                                 'velocity called?'},
                           {'correct': 2,
                            'explanation': "Newton's Third Law: forces always occur in equal, "
                                           'opposite, and collinear pairs between interacting '
                                           'bodies.',
                            'options': ['B exerts an equal force in the same direction',
                                        'B exerts half the force in the opposite direction',
                                        'B exerts an equal and opposite force on A',
                                        'No reaction force exists'],
                            'q': "According to Newton's Third Law, if body A exerts a force on "
                                 'body B, what is true about the reaction?'},
                           {'correct': 1,
                            'explanation': 'A force is fully described by its magnitude, its '
                                           'direction (line of action and sense), and its point of '
                                           'application.',
                            'options': ['Mass, length, and time',
                                        'Magnitude, direction, and point of application',
                                        'Speed, acceleration, and position',
                                        'Weight, volume, and density'],
                            'q': 'A force vector is characterised by which three properties?'},
                           {'correct': 2,
                            'explanation': 'Transmissibility allows a force to slide along its '
                                           'line of action on a rigid body without altering the '
                                           'external effects.',
                            'options': ['A force can be replaced by a couple',
                                        'A force can be moved to any point on the body',
                                        'A force may be applied anywhere along its line of action '
                                        'without changing the external effects on a rigid body',
                                        'Forces always cancel in pairs'],
                            'q': 'What does the Principle of Transmissibility state?'},
                           {'correct': 1,
                            'explanation': 'The parallelogram law constructs the resultant as the '
                                           'diagonal of a parallelogram formed by the two force '
                                           'vectors.',
                            'options': ['One of the sides of the parallelogram',
                                        'The diagonal of the parallelogram',
                                        'The perimeter of the parallelogram',
                                        'The area of the parallelogram'],
                            'q': 'When adding two concurrent forces using the parallelogram law, '
                                 'the resultant is represented by:'},
                           {'correct': 1,
                            'explanation': 'A moment measures the tendency of a force to cause '
                                           'rotation about a specified point or axis (M = F × d).',
                            'options': ['The force multiplied by the mass',
                                        'The tendency of the force to cause rotation about that '
                                        'point',
                                        'The component of the force along the line to the point',
                                        'The acceleration produced at that point'],
                            'q': 'What is the moment of a force about a point?'},
                           {'correct': 2,
                            'explanation': 'A couple consists of two equal, opposite, '
                                           'non-collinear forces. They cannot be combined into a '
                                           'single force — their only effect is a pure rotational '
                                           'tendency.',
                            'options': ['Equal, parallel, and in the same direction',
                                        'Unequal and perpendicular',
                                        'Equal, opposite, and non-collinear (parallel)',
                                        'Equal, opposite, and collinear'],
                            'q': 'A couple is defined as two forces that are:'},
                           {'correct': 2,
                            'explanation': 'The moment of a couple (M = Fd) is the same about all '
                                           'points — it is a free vector that can be moved '
                                           'anywhere without changing its effect.',
                            'options': ['It depends on the point about which you take the moment',
                                        'It is always zero',
                                        'It is the same about every point — it is a free vector',
                                        'It only acts in the horizontal plane'],
                            'q': 'What is unique about the moment produced by a couple?'},
                           {'correct': 2,
                            'explanation': 'The textbook emphasises: "The free-body diagram is the '
                                           'most important single step in the solution of problems '
                                           'in mechanics."',
                            'options': ['Choosing the coordinate system',
                                        'Calculating the resultant force',
                                        'Drawing a correct and complete free-body diagram',
                                        'Summing moments about the origin'],
                            'q': 'What is the single most important step in solving statics '
                                 'problems?'},
                           {'correct': 2,
                            'explanation': 'When isolating a body, every removed contact or '
                                           'support must be replaced by the reactive forces it '
                                           'would have exerted on the body.',
                            'options': ['Nothing — the contact is simply removed',
                                        'A displacement vector',
                                        'The appropriate reaction force(s)',
                                        'A fixed boundary condition'],
                            'q': 'On a free-body diagram, what replaces a surface of contact that '
                                 'has been removed during isolation?'},
                           {'correct': 2,
                            'explanation': 'For 2D equilibrium: ΣFx = 0, ΣFy = 0, and ΣMO = 0 — '
                                           'three independent equations.',
                            'options': ['1', '2', '3', '6'],
                            'q': 'How many independent scalar equilibrium equations are available '
                                 'for a general coplanar (2D) force system?'},
                           {'correct': 1,
                            'explanation': 'A roller (or rocker or ball) support can only exert a '
                                           'compressive force normal to the supporting surface — '
                                           'it cannot resist tangential forces.',
                            'options': ['A force in any direction plus a couple',
                                        'A force only normal (perpendicular) to the supporting '
                                        'surface',
                                        'A horizontal and vertical force',
                                        'A couple only'],
                            'q': 'A roller support on a flat surface provides what type of '
                                 'reaction?'},
                           {'correct': 0,
                            'explanation': 'A pin free to rotate supports a force in any direction '
                                           '(resolved into Rx and Ry) but cannot resist rotation, '
                                           'so no couple.',
                            'options': ['A force in any direction in the plane (2 components) but '
                                        'no couple',
                                        'Only a vertical force',
                                        'A force and a couple (moment)',
                                        'Only a horizontal force'],
                            'q': 'A pin connection that is free to turn can support which of the '
                                 'following?'},
                           {'correct': 2,
                            'explanation': 'When unknowns exceed the number of independent '
                                           'equilibrium equations, the body is statically '
                                           'indeterminate — additional equations from deformation '
                                           'are needed.',
                            'options': ['Statically determinate',
                                        'In unstable equilibrium',
                                        'Statically indeterminate',
                                        'A two-force member'],
                            'q': 'If a body has more unknown support reactions than available '
                                 'independent equilibrium equations, it is called:'},
                           {'correct': 1,
                            'explanation': 'A two-force member in equilibrium requires the forces '
                                           'to be equal, opposite, and collinear — regardless of '
                                           "the member's shape.",
                            'options': ['Perpendicular to each other',
                                        'Equal in magnitude, opposite in direction, and collinear',
                                        'Unequal but parallel',
                                        'Applied at the same point'],
                           'q': 'For a two-force member in equilibrium, the two forces must '
                                 'be:'}]}}

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
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json",
        })
        self.quiz_tests_base = f"{self.base_url}/rest/v1/quiz_tests"
        self.lecturers_base = f"{self.base_url}/rest/v1/quiz_lecturers"
        self.drafts_base = f"{self.base_url}/rest/v1/quiz_test_drafts"
        self.subjects_base = f"{self.base_url}/rest/v1/quiz_subjects"

    def _check_response(self, resp: requests.Response) -> None:
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Supabase request failed: {detail}")

    def _request(self, method: str, url: str, *, params=None, body=None, prefer: str | None = None) -> list[dict[str, Any]]:
        headers = dict(self.session.headers)
        if prefer:
            headers["Prefer"] = prefer
        resp = self.session.request(method, url, params=params, headers=headers, data=json.dumps(body) if body is not None else None, timeout=REQUEST_TIMEOUT)
        self._check_response(resp)
        if not resp.text:
            return []
        try:
            return resp.json()
        except Exception:
            return []

    def get_lecturer_by_email(self, email: str) -> dict[str, Any] | None:
        rows = self._request("GET", self.lecturers_base, params={
            "select": "id,name,email,password_hash,created_at,updated_at",
            "email": f"eq.{email.lower()}",
            "limit": "1",
        })
        return rows[0] if rows else None

    def get_lecturer_by_id(self, lecturer_id: str) -> dict[str, Any] | None:
        rows = self._request("GET", self.lecturers_base, params={
            "select": "id,name,email,created_at,updated_at",
            "id": f"eq.{lecturer_id}",
            "limit": "1",
        })
        return rows[0] if rows else None

    def create_lecturer(self, name: str, email: str, password_hash: str) -> dict[str, Any]:
        rows = self._request("POST", self.lecturers_base, body={
            "name": name,
            "email": email.lower(),
            "password_hash": password_hash,
        }, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the created lecturer.")
        return rows[0]

    def list_subjects(self) -> list[dict[str, Any]]:
        rows = self._request("GET", self.subjects_base, params={
            "select": "code,name,created_by,created_at",
            "order": "name.asc",
        })
        for row in rows:
            if row.get("code"):
                row["code"] = str(row["code"]).strip().upper()
            if row.get("name"):
                row["name"] = str(row["name"]).strip()
        return rows

    def get_subject(self, code: str) -> dict[str, Any] | None:
        rows = self._request("GET", self.subjects_base, params={
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

    def create_subject(self, code: str, name: str, lecturer_id: str) -> dict[str, Any]:
        rows = self._request("POST", self.subjects_base, body={
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

    def delete_subject(self, code: str, lecturer_id: str) -> None:
        self._request("DELETE", self.subjects_base, params={
            "code": f"ilike.{code}",
            "created_by": f"eq.{lecturer_id}",
        })

    def subject_has_tests(self, subject_code: str) -> bool:
        rows = self._request("GET", self.quiz_tests_base, params={
            "select": "id",
            "subject_code": f"eq.{subject_code}",
            "limit": "1",
        })
        return bool(rows)

    def list_tests_by_creator(self, lecturer_id: str) -> list[dict[str, Any]]:
        rows = self._request("GET", self.quiz_tests_base, params={
            "select": "id,subject_code,title,chapter,description,questions,question_count,created_at,updated_at,created_by,owner_name",
            "created_by": f"eq.{lecturer_id}",
            "order": "subject_code.asc,updated_at.desc",
        })
        for row in rows:
            row["source"] = "supabase"
            row.setdefault("question_count", len(row.get("questions") or []))
        return rows

    def list_tests(self, subject_code: str, lecturer_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._request("GET", self.quiz_tests_base, params={
            "select": "id,subject_code,title,chapter,description,question_count,created_at,updated_at,created_by,owner_name",
            "subject_code": f"eq.{subject_code}",
            "order": "updated_at.desc",
        })
        for row in rows:
            row["source"] = "supabase"
            row["can_edit"] = bool(lecturer_id and row.get("created_by") == lecturer_id)
        return rows

    def get_test(self, subject_code: str, test_id: str, lecturer_id: str | None = None) -> dict[str, Any] | None:
        rows = self._request("GET", self.quiz_tests_base, params={
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

    def create_test(self, subject_code: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        rows = self._request("POST", self.quiz_tests_base, body={
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

    def delete_test(self, subject_code: str, test_id: str) -> None:
        self._request("DELETE", self.quiz_tests_base, params={
            "subject_code": f"eq.{subject_code}",
            "id": f"eq.{test_id}",
        })

    def update_test(self, subject_code: str, test_id: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
        existing = self.get_test(subject_code, test_id, lecturer["id"])
        if not existing:
            raise KeyError("Test not found")
        if existing.get("created_by") and existing.get("created_by") != lecturer["id"]:
            raise PermissionError("Only the lecturer who created this test can edit it.")
        rows = self._request("PATCH", self.quiz_tests_base, params={
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

    def get_draft(self, subject_code: str, lecturer_id: str) -> dict[str, Any] | None:
        rows = self._request("GET", self.drafts_base, params={
            "select": "id,lecturer_id,subject_code,title,chapter,description,questions,question_count,editing_test_id,updated_at",
            "lecturer_id": f"eq.{lecturer_id}",
            "subject_code": f"eq.{subject_code}",
            "limit": "1",
        })
        return rows[0] if rows else None

    def save_draft(self, subject_code: str, lecturer: dict[str, Any], payload: DraftPayload) -> dict[str, Any]:
        existing = self.get_draft(subject_code, lecturer["id"])
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
            rows = self._request("PATCH", self.drafts_base, params={
                "id": f"eq.{existing['id']}",
                "lecturer_id": f"eq.{lecturer['id']}",
            }, body=body, prefer="return=representation")
        else:
            rows = self._request("POST", self.drafts_base, body=body, prefer="return=representation")
        if not rows:
            raise RuntimeError("Supabase did not return the saved draft.")
        return rows[0]

    def clear_draft(self, subject_code: str, lecturer_id: str) -> None:
        self._request("DELETE", self.drafts_base, params={
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

    def _call_remote(self, fn, fallback):
        if self.remote is None:
            return fallback()
        try:
            return fn()
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return fallback()
            raise
        except requests.RequestException as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return fallback()

    def _seed_builtin_tests(self) -> None:
        for code, info in self.subjects.items():
            questions = info.get("questions", []) or []
            self.builtin_tests[code] = {}
            self.local_custom_tests[code] = {}
            if questions:
                test_id = f"builtin:{code}:default"
                self.builtin_tests[code][test_id] = {
                    "id": test_id,
                    "subject_code": code,
                    "title": f"{info['name']} Core Quiz",
                    "chapter": "Built-in starter quiz",
                    "description": "Legacy built-in quiz packaged with the app.",
                    "question_count": len(questions),
                    "questions": questions,
                    "created_at": None,
                    "updated_at": None,
                    "source": "built-in",
                    "created_by": None,
                    "owner_name": "System",
                }

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

    def get_lecturer_by_email(self, email: str) -> dict[str, Any] | None:
        email = email.strip().lower()
        def _local_lookup():
            return self.local_lecturers.get(email)
        if self.remote is None:
            return _local_lookup()
        try:
            row = self.remote.get_lecturer_by_email(email)
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return _local_lookup()
            raise
        except requests.RequestException as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return _local_lookup()
        if row:
            self._cache_lecturer_row(row)
            return row
        return _local_lookup()

    def get_lecturer_by_id(self, lecturer_id: str) -> dict[str, Any] | None:
        def _local_lookup():
            for row in self.local_lecturers.values():
                if row["id"] == lecturer_id:
                    return {k: v for k, v in row.items() if k != "password_hash"}
            return None
        if self.remote is None:
            return _local_lookup()
        try:
            row = self.remote.get_lecturer_by_id(lecturer_id)
        except RuntimeError as exc:
            if self._handle_supabase_error(exc):
                return _local_lookup()
            raise
        except requests.RequestException as exc:
            self.supabase_error = str(exc)
            self.remote = None
            self.local_store_enabled = True
            self._set_storage_mode()
            return _local_lookup()
        return row or _local_lookup()

    def create_lecturer(self, payload: LecturerSignupPayload) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        if self.get_lecturer_by_email(payload.email):
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
        result = self._call_remote(
            lambda: self.remote.create_lecturer(payload.name, payload.email, password_hash),
            _local_create
        )
        self._cache_lecturer_row(result)
        return result

    def list_subjects(self) -> list[dict[str, Any]]:
        remote_rows = self._call_remote(
            lambda: self.remote.list_subjects(),
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

    def create_subject(self, code: str, name: str, lecturer: dict[str, Any]) -> dict[str, Any]:
        self._ensure_supabase_for_write()
        code = (code or "").strip().upper()
        name = (name or "").strip()
        if code in BUILTIN_SUBJECT_CODES or code in self.subjects:
            raise ValueError("Subject code already exists.")
        if self.remote is not None:
            existing = self._call_remote(lambda: self.remote.get_subject(code), lambda: None)
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
        def _remote_create():
            try:
                return self.remote.create_subject(code, name, lecturer["id"])
            except RuntimeError as exc:
                if "duplicate" in str(exc).lower():
                    raise ValueError("Subject code already exists.")
                raise
        row = self._call_remote(_remote_create, _local_create)
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

    def delete_subject(self, code: str, lecturer: dict[str, Any]) -> dict[str, Any]:
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

        def _remote_delete():
            row = self.remote.get_subject(code)
            if not row:
                raise KeyError("Subject not found")
            if row.get("created_by") != lecturer.get("id"):
                raise PermissionError("Only the lecturer who created this subject can delete it.")
            if self.remote.subject_has_tests(code) or self.local_custom_tests.get(code):
                raise ValueError("Cannot delete a subject with saved tests.")
            self.remote.delete_subject(code, lecturer.get("id"))
            return row

        row = self._call_remote(_remote_delete, _local_delete)
        self.local_subjects.pop(code, None)
        if code in self.subjects and code not in BUILTIN_SUBJECT_CODES:
            self.subjects.pop(code, None)
        self.local_custom_tests.pop(code, None)
        self._persist_local_store()
        return row

    def list_tests_by_creator(self, lecturer_id: str) -> list[dict[str, Any]]:
        remote_rows = self._call_remote(
            lambda: self.remote.list_tests_by_creator(lecturer_id),
            lambda: []
        )
        local_rows: list[dict[str, Any]] = []
        for items in self.local_custom_tests.values():
            for row in items.values():
                if row.get("created_by") == lecturer_id:
                    local_rows.append(row)
        return list(remote_rows or []) + local_rows

    def list_tests(self, subject_code: str, lecturer_id: str | None = None) -> list[dict[str, Any]]:
        if subject_code not in self.subjects:
            raise KeyError(subject_code)

        tests: list[dict[str, Any]] = []
        builtin_rows = list(self.builtin_tests.get(subject_code, {}).values())
        tests.extend(self._summary(row, lecturer_id) for row in builtin_rows)

        remote_rows = self._call_remote(
            lambda: self.remote.list_tests(subject_code, lecturer_id),
            lambda: []
        )
        local_rows = list(self.local_custom_tests.get(subject_code, {}).values())

        tests.extend(self._summary(row, lecturer_id) for row in remote_rows)
        tests.extend(self._summary(row, lecturer_id) for row in local_rows)
        return tests

    def get_test(self, subject_code: str, test_id: str, lecturer_id: str | None = None) -> dict[str, Any] | None:
        if test_id in self.builtin_tests.get(subject_code, {}):
            row = self.builtin_tests[subject_code][test_id]
            row["can_edit"] = False
            return row
        if test_id in self.local_custom_tests.get(subject_code, {}):
            row = self.local_custom_tests[subject_code][test_id]
            row["can_edit"] = bool(lecturer_id and row.get("created_by") == lecturer_id)
            return row
        return self._call_remote(
            lambda: self.remote.get_test(subject_code, test_id, lecturer_id),
            lambda: None
        )

    def create_test(self, subject_code: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
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
        def _remote_create():
            row = self.remote.create_test(subject_code, payload, lecturer)
            row.setdefault("question_count", len(payload.questions))
            row.setdefault("questions", [q.model_dump() for q in payload.questions])
            row.setdefault("owner_name", lecturer.get("name") or lecturer.get("email") or "Lecturer")
            row.setdefault("created_by", lecturer["id"])
            return row
        return self._call_remote(_remote_create, _local_create)

    def update_test(self, subject_code: str, test_id: str, payload: TestPayload, lecturer: dict[str, Any]) -> dict[str, Any]:
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
        def _remote_update():
            row = self.remote.update_test(subject_code, test_id, payload, lecturer)
            row.setdefault("question_count", len(payload.questions))
            row.setdefault("questions", [q.model_dump() for q in payload.questions])
            row.setdefault("owner_name", lecturer.get("name") or lecturer.get("email") or "Lecturer")
            row.setdefault("created_by", lecturer["id"])
            return row
        return self._call_remote(_remote_update, _local_update)

    def delete_test(self, subject_code: str, test_id: str, lecturer: dict[str, Any]) -> dict[str, Any]:
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

        def _remote_delete():
            existing = self.remote.get_test(subject_code, test_id, lecturer.get("id"))
            if not existing:
                raise KeyError("Test not found")
            if existing.get("created_by") != lecturer["id"]:
                raise PermissionError("Only the lecturer who created this test can delete it.")
            self.remote.delete_test(subject_code, test_id)
            return existing

        return self._call_remote(_remote_delete, _local_delete)

    def get_draft(self, subject_code: str, lecturer: dict[str, Any]) -> dict[str, Any] | None:
        return self._call_remote(
            lambda: self.remote.get_draft(subject_code, lecturer["id"]),
            lambda: self.local_drafts.get((lecturer["id"], subject_code))
        )

    def save_draft(self, subject_code: str, lecturer: dict[str, Any], payload: DraftPayload) -> dict[str, Any]:
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
        return self._call_remote(
            lambda: self.remote.save_draft(subject_code, lecturer, payload),
            _local_save
        )

    def clear_draft(self, subject_code: str, lecturer: dict[str, Any]) -> None:
        self._ensure_supabase_for_write()
        def _local_clear():
            self.local_drafts.pop((lecturer["id"], subject_code), None)
            self._persist_local_store()
        return self._call_remote(
            lambda: self.remote.clear_draft(subject_code, lecturer["id"]),
            _local_clear
        )


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
        if clear_players:
            self.players = {}
        self.host_ws = None
        self.host_visitor = None
        self.answers_this_round = {}
        self.question_timer_task = None

    def archive_stats(self) -> None:
        if not self.players:
            return
        self.last_game_stats = {
            "subject_code": self.subject_code,
            "subject_name": self.subject_name,
            "test_id": self.active_test_id,
            "test_title": self.active_test_title,
            "test_chapter": self.active_test_chapter,
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
        subjects = repo.list_subjects()
        for row in subjects:
            register_subject_in_catalog(row.get("code"), row.get("name"))
    except Exception as exc:
        print(f"Failed to load subjects from Supabase: {exc}")
    yield

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


def current_lecturer_from_request(request: Request) -> dict[str, Any] | None:
    lecturer_id = parse_session_token(request.cookies.get(SESSION_COOKIE_NAME))
    if not lecturer_id:
        return None
    return repo.get_lecturer_by_id(lecturer_id)


def require_lecturer(request: Request) -> dict[str, Any]:
    lecturer = current_lecturer_from_request(request)
    if not lecturer:
        raise HTTPException(status_code=401, detail="Lecturer sign-in required")
    return lecturer


def current_lecturer_from_websocket(websocket: WebSocket) -> dict[str, Any] | None:
    lecturer_id = parse_session_token(websocket.cookies.get(SESSION_COOKIE_NAME))
    if not lecturer_id:
        return None
    return repo.get_lecturer_by_id(lecturer_id)


@app.get("/api/health")
def health():
    return {"ok": True, "storage": repo.get_storage_status()}


@app.get("/api/storage-status")
def storage_status():
    return repo.get_storage_status()


@app.get("/api/lecturer/session")
def lecturer_session(request: Request):
    lecturer = current_lecturer_from_request(request)
    return {"authenticated": bool(lecturer), "lecturer": public_lecturer_view(lecturer) if lecturer else None}


@app.post("/api/lecturer/signup")
@limiter.limit("5/minute")
def lecturer_signup(payload: dict[str, Any], request: Request):
    try:
        validated = LecturerSignupPayload.model_validate(payload)
        lecturer = repo.create_lecturer(validated)
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
def lecturer_login(payload: dict[str, Any], request: Request):
    try:
        validated = LecturerLoginPayload.model_validate(payload)
        lecturer = repo.get_lecturer_by_email(validated.email)
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
def get_subjects():
    result = []
    for code, info in SUBJECTS.items():
        tests = repo.list_tests(code)
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


@app.post("/api/subjects")
def create_subject(payload: dict[str, Any], request: Request):
    lecturer = require_lecturer(request)
    try:
        validated = SubjectPayload.model_validate(payload)
        code = validated.code
        if code in SUBJECTS:
            raise HTTPException(status_code=409, detail="Subject code already exists.")
        created = repo.create_subject(code, validated.name, lecturer)
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
def delete_subject(code: str, request: Request):
    lecturer = require_lecturer(request)
    normalized = (code or "").strip().upper()
    if normalized in BUILTIN_SUBJECT_CODES:
        raise HTTPException(status_code=403, detail="Built-in subjects cannot be deleted.")
    if normalized not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    try:
        repo.delete_subject(normalized, lecturer)
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
def get_tests(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        return repo.list_tests(subject_code, lecturer.get("id"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tests/{subject_code}/{test_id}")
def get_test_detail(subject_code: str, test_id: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        row = repo.get_test(subject_code, test_id, lecturer.get("id"))
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
def create_test(subject_code: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        validated = TestPayload.model_validate(payload)
        created = repo.create_test(subject_code, validated, lecturer)
        repo.clear_draft(subject_code, lecturer)
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
def update_test(subject_code: str, test_id: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        validated = TestPayload.model_validate(payload)
        updated = repo.update_test(subject_code, test_id, validated, lecturer)
        repo.clear_draft(subject_code, lecturer)
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
def delete_test(subject_code: str, test_id: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        repo.delete_test(subject_code, test_id, lecturer)
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
def get_test_draft(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        draft = repo.get_draft(subject_code, lecturer)
        return {"draft": draft}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/drafts/{subject_code}")
def save_test_draft(subject_code: str, payload: dict[str, Any], request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        validated = DraftPayload.model_validate(payload)
        draft = repo.save_draft(subject_code, lecturer, validated)
        return {"ok": True, "draft": draft}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/drafts/{subject_code}")
def clear_test_draft(subject_code: str, request: Request):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    lecturer = require_lecturer(request)
    try:
        repo.clear_draft(subject_code, lecturer)
        return {"ok": True}
    except SupabaseUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/export/tests")
def export_tests(request: Request):
    lecturer = require_lecturer(request)
    try:
        tests = repo.list_tests_by_creator(lecturer["id"])
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
    safe_title = (stats.get("test_title") or subject_code).replace(" ", "_").replace("/", "-")[:60]
    filename = f"Stats_{subject_code}_{safe_title}.xlsx"
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


def get_player_list(room: GameRoom) -> list[dict[str, Any]]:
    players = []
    for vid, p in room.players.items():
        players.append({
            "id": vid,
            "name": p["name"],
            "score": p["score"],
            "connected": p.get("ws") is not None
        })
    players.sort(key=lambda x: -x["score"])
    return players


def get_leaderboard(room: GameRoom) -> list[dict[str, Any]]:
    players = get_player_list(room)
    for i, p in enumerate(players):
        p["rank"] = i + 1
    return players


async def broadcast_to_players(room: GameRoom, msg: dict[str, Any]) -> None:
    async def _safe_send(ws: WebSocket):
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            pass

    tasks = []
    for _, player in list(room.players.items()):
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
    await cancel_question_timer(room)
    room.phase = "lobby"
    room.current_q = 0
    room.question_start_time = 0
    room.answers_this_round = {}

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
        "hasQuestions": room.total_q > 0,
        "hasStats": room.last_game_stats is not None
    })
    await push_room_update(room)


async def force_end_game(room: GameRoom) -> None:
    await cancel_question_timer(room)
    room.phase = "final"
    room.archive_stats()
    lb = get_leaderboard(room)
    await broadcast_to_players(room, {"type": "final", "leaderboard": lb})
    await send_to_host(room, {"type": "final", "leaderboard": lb, "hasStats": True})


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
                lecturer = current_lecturer_from_websocket(websocket)
                if not lecturer:
                    await websocket.send_text(json.dumps({"type": "auth_required", "message": "Lecturer sign-in required"}))
                    continue
                subject_code = msg.get("subject")
                test_id = msg.get("testId")
                if subject_code not in rooms:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Invalid subject"}))
                    continue
                test_data = repo.get_test(subject_code, test_id) if test_id else None
                if not test_data:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Test not found"}))
                    continue

                role = "host"
                room = rooms[subject_code]
                if room.phase == "lobby":
                    room.set_active_test(test_data)
                room.host_ws = websocket
                room.host_visitor = visitor_id

                await websocket.send_text(json.dumps({
                    "type": "host_joined",
                    "phase": room.phase,
                    "players": get_player_list(room),
                    "currentQ": room.current_q,
                    "totalQ": room.total_q,
                    "subjectCode": room.subject_code,
                    "subjectName": room.subject_name,
                    "selectedTest": get_active_test_meta(room),
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
                room.phase = "get_ready"
                room.current_q = 0
                for vid in room.players:
                    room.players[vid]["score"] = 0
                    room.players[vid]["streak"] = 0
                    room.players[vid]["answers"] = []
                await broadcast_to_players(room, {"type": "get_ready", "qNum": 1, "totalQ": room.total_q})
                await send_to_host(room, {"type": "get_ready", "qNum": 1, "totalQ": room.total_q})
                await asyncio.sleep(3)
                await send_question(room)

            elif action == "next_question":
                if role == "host" and room is not None:
                    await advance_to_next(room)

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

            elif action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif action == "player_join":
                subject_code = msg.get("subject")
                if subject_code not in rooms:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Invalid subject"}))
                    continue
                role = "player"
                room = rooms[subject_code]
                name = msg.get("name", "Anonymous").strip()[:20]
                student_number = msg.get("studentNumber", "").strip()[:20]
                name_lower = name.lower()
                existing_vid = None
                existing_player = None
                match_by_student = bool(student_number)
                for vid, player in room.players.items():
                    if vid == visitor_id:
                        continue
                    if match_by_student:
                        if player.get("student_number") == student_number:
                            existing_vid = vid
                            existing_player = player
                            break
                    else:
                        if player.get("name", "").lower() == name_lower:
                            existing_vid = vid
                            existing_player = player
                            break
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
                    room.players[visitor_id] = existing_player
                elif visitor_id in room.players:
                    room.players[visitor_id]["ws"] = websocket
                    room.players[visitor_id]["name"] = name
                    room.players[visitor_id]["student_number"] = student_number
                else:
                    room.players[visitor_id] = {
                        "name": name,
                        "student_number": student_number,
                        "score": 0,
                        "streak": 0,
                        "answers": [],
                        "ws": websocket
                    }
                await websocket.send_text(json.dumps({
                    "type": "joined",
                    "phase": room.phase,
                    "playerId": visitor_id,
                    "playerCount": len(room.players),
                    "activeTest": get_active_test_meta(room)
                }))
                await push_room_update(room)

            elif action == "player_leave":
                if role != "player" or room is None:
                    continue
                room.players.pop(visitor_id, None)
                await websocket.send_text(json.dumps({"type": "left"}))
                await push_room_update(room)
                await websocket.close()
                break

            elif action == "answer":
                if role != "player" or room is None or room.phase != "question":
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
                    "correctAnswer": q["correct"],
                    "explanation": q["explanation"]
                }))
                answered_count = len(room.answers_this_round)
                total_connected = sum(1 for _, player in room.players.items() if player.get("ws") is not None)
                await send_to_host(room, {
                    "type": "answer_count",
                    "answered": answered_count,
                    "total": total_connected
                })
                if answered_count >= total_connected and total_connected > 0:
                    if room.question_timer_task and not room.question_timer_task.done():
                        room.question_timer_task.cancel()
                    for vid2 in room.players:
                        if vid2 not in room.answers_this_round:
                            room.players[vid2]["streak"] = 0
                            room.players[vid2]["answers"].append({
                                "q": room.current_q,
                                "choice": -1,
                                "correct": False,
                                "points": 0,
                                "time": 0
                            })
                    await auto_reveal(room)

    except WebSocketDisconnect:
        if role == "host" and room:
            room.host_ws = None
        elif role == "player" and room and visitor_id in room.players:
            if room.phase == "lobby":
                room.players.pop(visitor_id, None)
            else:
                room.players[visitor_id]["ws"] = None
            await push_room_update(room)
    except Exception:
        if role == "host" and room:
            room.host_ws = None
        elif role == "player" and room and visitor_id in room.players:
            if room.phase == "lobby":
                room.players.pop(visitor_id, None)
            else:
                room.players[visitor_id]["ws"] = None


async def send_question(room: GameRoom) -> None:
    q = room.questions[room.current_q]
    q_index = room.current_q
    room.phase = "question"
    room.question_start_time = time.time()
    room.answers_this_round = {}
    await broadcast_to_players(room, {
        "type": "question",
        "qNum": room.current_q + 1,
        "totalQ": room.total_q,
        "question": q["q"],
        "options": q["options"],
        "timeLimit": TIME_PER_Q
    })
    await send_to_host(room, {
        "type": "question",
        "qNum": room.current_q + 1,
        "totalQ": room.total_q,
        "question": q["q"],
        "options": q["options"],
        "correctAnswer": q["correct"],
        "timeLimit": TIME_PER_Q
    })

    async def _timer():
        await asyncio.sleep(TIME_PER_Q)
        if room.phase == "question" and room.current_q == q_index:
            for vid in room.players:
                if vid not in room.answers_this_round:
                    room.players[vid]["streak"] = 0
                    room.players[vid]["answers"].append({
                        "q": room.current_q,
                        "choice": -1,
                        "correct": False,
                        "points": 0,
                        "time": 0
                    })
            await auto_reveal(room)

    room.question_timer_task = asyncio.create_task(_timer())


async def auto_reveal(room: GameRoom) -> None:
    q = room.questions[room.current_q]
    room.phase = "reveal"
    lb = get_leaderboard(room)
    for vid, player in room.players.items():
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
    await broadcast_to_players(room, {"type": "leaderboard", "leaderboard": lb})
    await send_to_host(room, {
        "type": "reveal",
        "correctAnswer": q["correct"],
        "explanation": q["explanation"],
        "leaderboard": lb,
        "isLast": room.current_q >= room.total_q - 1
    })
    await asyncio.sleep(5)
    if room.phase == "reveal":
        await advance_to_next(room)


async def advance_to_next(room: GameRoom) -> None:
    room.current_q += 1
    if room.current_q >= room.total_q:
        await force_end_game(room)
    else:
        room.phase = "get_ready"
        await broadcast_to_players(room, {"type": "get_ready", "qNum": room.current_q + 1, "totalQ": room.total_q})
        await send_to_host(room, {"type": "get_ready", "qNum": room.current_q + 1, "totalQ": room.total_q})
        await asyncio.sleep(3)
        await send_question(room)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
