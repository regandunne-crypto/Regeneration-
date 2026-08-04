"""Phase 5: Word document import."""

import docx_fixtures as fx
import pytest
from conftest import signup
from docx_import import DocxImportError, build_template_docx, parse_docx


def warnings_text(result):
    return " | ".join(w["message"] for w in result["warnings"])


# ── Numbering variants ───────────────────────────────────────────────────────

def test_typed_numbering():
    result = parse_docx(fx.typed_numbering())
    assert result["meta"]["parsed"] == 3, warnings_text(result)

    first = result["questions"][0]
    assert first["q"] == "What is the SI unit of force?"
    assert first["options"] == ["Newton", "Joule", "Watt", "Pascal"]
    assert first["correct"] == 0
    assert first["explanation"] == "Force is measured in newtons."

    # "(a) Mass" style markers and a lower-case "Ans: c".
    assert result["questions"][1]["options"] == ["Mass", "Speed", "Velocity", "Temperature"]
    assert result["questions"][1]["correct"] == 2

    # "Question 3:" prefix, "A." markers, "Correct:" and "Rationale:".
    assert result["questions"][2]["correct"] == 1
    assert "perpendicular distance" in result["questions"][2]["explanation"]


def test_word_automatic_numbering():
    """The number lives in pPr/numPr and is not in the paragraph text at all."""
    result = parse_docx(fx.word_auto_numbering())
    assert result["meta"]["parsed"] == 2, warnings_text(result)
    assert result["questions"][0]["q"] == "What is the SI unit of pressure?"
    assert result["questions"][0]["correct"] == 1
    assert result["questions"][1]["q"] == "Which law relates force, mass and acceleration?"
    assert result["questions"][1]["options"][1] == "Newton's second law"


def test_word_auto_numbered_options_without_any_markers():
    result = parse_docx(fx.word_auto_numbered_options())
    assert result["meta"]["parsed"] == 1, warnings_text(result)
    question = result["questions"][0]
    assert question["options"] == ["One", "Two", "Three", "Six"]
    assert question["correct"] == 2          # matched "Answer: Three" by text


def test_no_numbering_at_all():
    result = parse_docx(fx.no_numbering_at_all())
    assert result["meta"]["parsed"] == 2, warnings_text(result)
    assert result["questions"][0]["correct"] == 1
    assert result["questions"][1]["correct"] == 2


# ── Table layout ─────────────────────────────────────────────────────────────

def test_table_layout_is_preferred_and_parsed():
    result = parse_docx(fx.table_layout())
    assert result["meta"]["layout"] == "table"
    assert result["meta"]["parsed"] == 2, warnings_text(result)
    assert result["questions"][0]["options"] == ["Newton", "Joule", "Watt", "Pascal"]
    assert result["questions"][1]["correct"] == 1
    assert "ΣM = 0." == result["questions"][1]["explanation"]
    assert result["questions"][1]["time_limit"] == 90


# ── Marking the correct answer ───────────────────────────────────────────────

def test_asterisk_marked_answers():
    result = parse_docx(fx.asterisk_marked_answers())
    assert result["meta"]["parsed"] == 2, warnings_text(result)
    assert result["questions"][0]["correct"] == 1
    assert result["questions"][0]["options"][1] == "Young's modulus"   # marker stripped
    assert result["questions"][1]["correct"] == 2
    assert result["questions"][1]["options"][2] == "Mass"


def test_answer_given_as_option_text():
    result = parse_docx(fx.answer_by_text())
    assert result["questions"][0]["correct"] == 0


def test_unmarked_answer_defaults_to_a_with_a_warning():
    result = parse_docx(fx.no_answer_marked())
    assert result["meta"]["parsed"] == 1, warnings_text(result)
    assert result["questions"][0]["correct"] == 0
    assert "No correct answer marked" in warnings_text(result)
    assert "defaulted to A" in warnings_text(result)


# ── Things that must be reported, not silently swallowed ─────────────────────

def test_a_question_with_three_options_is_skipped_with_a_warning():
    result = parse_docx(fx.three_options_only())
    assert result["meta"]["parsed"] == 1
    assert result["meta"]["skipped"] == 1
    assert "needs exactly 4" in warnings_text(result)
    # The good question still comes through.
    assert result["questions"][0]["q"] == "What is the SI unit of force?"


def test_word_equation_objects_are_flagged_not_dropped_silently():
    result = parse_docx(fx.with_equation_object())
    assert result["meta"]["parsed"] == 1, warnings_text(result)
    assert "Word equation that could not be imported" in warnings_text(result)
    assert "please retype it as text" in warnings_text(result)


def test_a_file_that_is_not_a_docx_is_rejected_clearly():
    with pytest.raises(DocxImportError) as exc:
        parse_docx(b"%PDF-1.4 this is not a word document")
    assert ".docx" in str(exc.value)


def test_oversized_input_is_rejected():
    with pytest.raises(DocxImportError):
        parse_docx(b"x" * (6 * 1024 * 1024))


# ── Unicode ──────────────────────────────────────────────────────────────────

def test_unicode_symbols_survive_and_smart_quotes_are_normalised():
    result = parse_docx(fx.unicode_symbols())
    question = result["questions"][0]
    for symbol in ("θ", "°", "Σ", "±", "µ", "×"):
        assert symbol in question["q"], f"{symbol} was lost"
    assert question["options"] == ["m²", "m³", "m⁴", "kg·m²"]
    # Smart quotes and the en dash are normalised; nothing else is touched.
    assert "“" not in question["explanation"] and "”" not in question["explanation"]
    assert '"Second moment of area"' in question["explanation"]
    assert "–" not in question["explanation"]


def test_formatted_superscripts_become_unicode():
    """Word writes m⁴ as a plain "4" with superscript formatting."""
    result = parse_docx(fx.formatted_superscript())
    assert "m⁴" in result["questions"][0]["q"]


# ── Per-question time (feeds Phase 6) ────────────────────────────────────────

def test_time_lines_map_to_a_per_question_limit():
    result = parse_docx(fx.with_time_lines())
    assert result["meta"]["parsed"] == 2, warnings_text(result)
    assert result["questions"][0]["time_limit"] == 10
    assert result["questions"][1]["time_limit"] == 120


def test_out_of_range_time_is_warned_and_dropped():
    result = parse_docx(fx.out_of_range_time())
    assert result["meta"]["parsed"] == 1
    assert "time_limit" not in result["questions"][0]
    assert "outside 5-300 seconds" in warnings_text(result)


def test_prose_and_bullet_lists_are_not_imported_as_questions():
    """Instruction text and bulleted lists look exactly like a question with
    unmarked options, so they need an explicit marker before being accepted."""
    result = parse_docx(fx.prose_that_is_not_a_quiz())
    assert result["meta"]["parsed"] == 0
    assert result["warnings"] == []          # quiet, not a wall of false warnings


# ── Template ─────────────────────────────────────────────────────────────────

def test_the_generated_template_parses_back_into_questions():
    """If the template we hand out does not import cleanly, it is worthless."""
    template = build_template_docx()
    assert template[:2] == b"PK"           # a real zip/docx container
    result = parse_docx(template)
    assert result["meta"]["parsed"] >= 3, warnings_text(result)
    parsed = {q["q"] for q in result["questions"]}
    assert any("θ = 30°" in q for q in parsed), "the symbol example did not survive"
    assert any("ΣF = 0 and ΣM = 0" in opt for q in result["questions"] for opt in q["options"])


def test_the_template_warns_about_equation_objects():
    import docx

    document = docx.Document(__import__("io").BytesIO(build_template_docx()))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "equation editor cannot be imported" in text
    assert "Time: 60" in text


# ── Endpoints ────────────────────────────────────────────────────────────────

@pytest.fixture()
def authed(client):
    signup(client)
    return client


def upload(client, data, filename="questions.docx"):
    return client.post(
        "/api/import/questions",
        files={"file": (filename, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )


def test_import_endpoint_returns_json_and_writes_nothing(authed):
    resp = upload(authed, fx.typed_numbering())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["parsed"] == 3
    assert len(body["questions"]) == 3
    assert body["questions"][0]["options"] == ["Newton", "Joule", "Watt", "Pascal"]
    # Crucially: no test was created.
    assert authed.get("/api/tests/MEC105B").json() == []


def test_import_requires_lecturer_auth(client):
    assert upload(client, fx.typed_numbering()).status_code == 401


def test_import_rejects_non_docx_extensions(authed):
    resp = upload(authed, b"whatever", filename="questions.doc")
    assert resp.status_code == 400
    assert "Save As" in resp.json()["detail"]


def test_import_rejects_an_empty_file(authed):
    assert upload(authed, b"").status_code == 400


def test_template_endpoint_serves_a_docx(authed):
    resp = authed.get("/api/import/template")
    assert resp.status_code == 200, resp.text
    assert "wordprocessingml" in resp.headers["content-type"]
    assert "quiz_question_template.docx" in resp.headers["content-disposition"]
    assert resp.content[:2] == b"PK"


def test_template_requires_lecturer_auth(client):
    assert client.get("/api/import/template").status_code == 401


def test_imported_questions_are_accepted_by_the_test_save_endpoint(authed):
    """The whole point: parsed questions must save without further editing."""
    parsed = upload(authed, fx.typed_numbering()).json()["questions"]
    resp = authed.post("/api/tests/MEC105B", json={
        "title": "Imported from Word",
        "chapter": "Chapter 3",
        "description": "",
        "questions": parsed,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["test"]["questionCount"] == 3
