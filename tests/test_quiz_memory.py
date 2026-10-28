"""Offline checks for quiz feedback memory."""

from jev_ultrafast.model import exclude_known_mistakes
from jev_ultrafast.quiz_memory import QuizMemory, marked_incorrect, question_on_page


def test_correction_persists_and_matches_visible_question(tmp_path):
    path = tmp_path / "incorrect.json"
    QuizMemory(path).save("What is 2 + 2?", "3")
    restored = QuizMemory(path)
    assert restored.matches({"text": "Question 1:  What is 2 + 2?  Choose one."}) == [
        {"question": "What is 2 + 2?", "incorrect_answers": ["3"]}
    ]
    assert restored.matches({"text": "What is 3 + 3?"}) == []


def test_multiple_wrong_answers_are_retained_without_duplicates(tmp_path):
    memory = QuizMemory(tmp_path / "incorrect.json")
    memory.save("Question?", "wrong one")
    memory.save("Question?", "wrong two")
    memory.save("Question?", "WRONG ONE")
    assert memory.read() == {"Question?": ["wrong one", "wrong two"]}


def test_learning_requires_explicit_new_incorrect_feedback():
    before = {"text": "What is 2 + 2?\n3\n4"}
    assert question_on_page(before) == "What is 2 + 2?"
    assert marked_incorrect(before, {"text": before["text"] + "\nIncorrect.\nCorrect answer: 4"})
    assert not marked_incorrect(before, {"text": before["text"] + "\nCorrect!"})
    assert not marked_incorrect(before, before)


def test_retake_excludes_only_observed_wrong_radio_choice():
    targets = {"CLICK": {
        "1": {"id": "e1", "role": "radio", "label": "3"},
        "2": {"id": "e2", "role": "radio", "label": "4"},
        "3": {"id": "e3", "role": "button", "label": "3"},
    }}
    known = [{"question": "What is 2 + 2?", "incorrect_answers": ["3"]}]
    assert set(exclude_known_mistakes(targets, known)["CLICK"]) == {"2", "3"}
