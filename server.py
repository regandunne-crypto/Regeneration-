#!/usr/bin/env python3
"""WebSocket game server for multi-subject quiz platform with optional Supabase-backed test bank.

Key ideas:
- Students still join by subject.
- Host now selects a subject and then a saved test for that subject.
- Tests can be stored durably in Supabase when environment variables are configured.
- Without Supabase, the app still works using in-memory fallback storage (not durable).
"""

import asyncio
import io
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

# ──────────────────────────────────────────────────────────────────────────────
# Subject catalogue and built-in legacy question sets
# ──────────────────────────────────────────────────────────────────────────────
SUBJECTS = {'1EM105B': {'code': '1EM105B', 'name': 'Mechanics', 'questions': []},
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

TIME_PER_Q = 30
MAX_POINTS = 1000
MIN_POINTS = 200
REQUEST_TIMEOUT = 20


# ──────────────────────────────────────────────────────────────────────────────
# Test bank models + storage
# ──────────────────────────────────────────────────────────────────────────────
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


class SupabaseTestStore:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.rest_base = f"{self.base_url}/rest/v1/quiz_tests"
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Content-Type": "application/json"
        })

    def _check_response(self, resp: requests.Response) -> None:
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Supabase request failed: {detail}")

    def list_tests(self, subject_code: str) -> list[dict[str, Any]]:
        params = {
            "select": "id,subject_code,title,chapter,description,question_count,created_at,updated_at",
            "subject_code": f"eq.{subject_code}",
            "order": "updated_at.desc"
        }
        resp = self.session.get(self.rest_base, params=params, timeout=REQUEST_TIMEOUT)
        self._check_response(resp)
        rows = resp.json()
        for row in rows:
            row["source"] = "supabase"
        return rows

    def get_test(self, subject_code: str, test_id: str) -> dict[str, Any] | None:
        params = {
            "select": "id,subject_code,title,chapter,description,questions,question_count,created_at,updated_at",
            "subject_code": f"eq.{subject_code}",
            "id": f"eq.{test_id}",
            "limit": "1"
        }
        resp = self.session.get(self.rest_base, params=params, timeout=REQUEST_TIMEOUT)
        self._check_response(resp)
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        row["source"] = "supabase"
        return row

    def create_test(self, subject_code: str, payload: TestPayload) -> dict[str, Any]:
        body = {
            "subject_code": subject_code,
            "title": payload.title,
            "chapter": payload.chapter or None,
            "description": payload.description or None,
            "question_count": len(payload.questions),
            "questions": [q.model_dump() for q in payload.questions],
        }
        headers = dict(self.session.headers)
        headers["Prefer"] = "return=representation"
        resp = self.session.post(self.rest_base, headers=headers, data=json.dumps(body), timeout=REQUEST_TIMEOUT)
        self._check_response(resp)
        rows = resp.json()
        if not rows:
            raise RuntimeError("Supabase did not return the created test.")
        row = rows[0]
        row["source"] = "supabase"
        return row


class HybridTestRepository:
    def __init__(self, subjects: dict[str, Any]):
        self.subjects = subjects
        self.builtin_tests: dict[str, dict[str, dict[str, Any]]] = {}
        self.local_custom_tests: dict[str, dict[str, dict[str, Any]]] = {}
        self._seed_builtin_tests()

        url = os.environ.get("SUPABASE_URL", "").strip()
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        self.remote = SupabaseTestStore(url, key) if url and key else None
        self.storage_mode = "supabase" if self.remote else "in-memory"

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
                }

    def get_storage_status(self) -> dict[str, Any]:
        return {
            "mode": self.storage_mode,
            "supabaseConfigured": self.remote is not None,
            "note": "Supabase storage is durable. In-memory storage resets on redeploy/restart." if self.remote is None else "Supabase storage is active."
        }

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "subject_code": row["subject_code"],
            "title": row.get("title", "Untitled Test"),
            "chapter": row.get("chapter") or "",
            "description": row.get("description") or "",
            "questionCount": row.get("question_count") or len(row.get("questions") or []),
            "source": row.get("source", "supabase"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def list_tests(self, subject_code: str) -> list[dict[str, Any]]:
        if subject_code not in self.subjects:
            raise KeyError(subject_code)

        tests: list[dict[str, Any]] = []

        # Built-in first so the existing default mechanics quiz remains available.
        builtin_rows = list(self.builtin_tests.get(subject_code, {}).values())
        tests.extend(self._summary(row) for row in builtin_rows)

        remote_rows: list[dict[str, Any]] = []
        if self.remote is not None:
            remote_rows = self.remote.list_tests(subject_code)
        local_rows = list(self.local_custom_tests.get(subject_code, {}).values())

        tests.extend(self._summary(row) for row in remote_rows)
        tests.extend(self._summary(row) for row in local_rows)
        return tests

    def get_test(self, subject_code: str, test_id: str) -> dict[str, Any] | None:
        if test_id in self.builtin_tests.get(subject_code, {}):
            return self.builtin_tests[subject_code][test_id]
        if test_id in self.local_custom_tests.get(subject_code, {}):
            return self.local_custom_tests[subject_code][test_id]
        if self.remote is not None:
            return self.remote.get_test(subject_code, test_id)
        return None

    def create_test(self, subject_code: str, payload: TestPayload) -> dict[str, Any]:
        if subject_code not in self.subjects:
            raise KeyError(subject_code)

        if self.remote is not None:
            row = self.remote.create_test(subject_code, payload)
            row.setdefault("question_count", len(payload.questions))
            row.setdefault("questions", [q.model_dump() for q in payload.questions])
            return row

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
            "source": "in-memory",
        }
        self.local_custom_tests.setdefault(subject_code, {})[test_id] = row
        return row


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


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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


@app.get("/api/health")
def health():
    return {"ok": True, "storage": repo.get_storage_status()}


@app.get("/api/storage-status")
def storage_status():
    return repo.get_storage_status()


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
    return result


@app.get("/api/tests/{subject_code}")
def get_tests(subject_code: str):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    try:
        return repo.list_tests(subject_code)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/tests/{subject_code}")
def create_test(subject_code: str, payload: dict[str, Any]):
    if subject_code not in SUBJECTS:
        raise HTTPException(status_code=404, detail="Subject not found")
    try:
        validated = TestPayload.model_validate(payload)
        created = repo.create_test(subject_code, validated)
        return {"ok": True, "test": repo._summary(created)}
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())
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
    visitor_id = websocket.headers.get("x-visitor-id", str(uuid.uuid4()))
    role = None
    room = None

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "host_join":
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
                    await return_room_to_lobby(room, keep_players=False)

            elif action == "cancel_game":
                if role == "host" and room is not None:
                    await return_room_to_lobby(room, keep_players=True)

            elif action == "end_game":
                if role == "host" and room is not None:
                    await force_end_game(room)

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
                if any(vid != visitor_id and p["name"].lower() == name_lower for vid, p in room.players.items()):
                    await websocket.send_text(json.dumps({"type": "name_taken", "name": name}))
                    continue
                if visitor_id in room.players:
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
