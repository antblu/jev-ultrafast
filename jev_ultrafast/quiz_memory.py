"""Quiz choices marked incorrect by visible page feedback, stored locally."""

import json
import os
import re
from pathlib import Path


def normalize(value):
    return re.sub(r"\s+", " ", value).strip().casefold()


def question_on_page(page):
    """Use one visible question; ambiguous pages cannot produce a memory entry."""
    lines = [line.strip() for line in page.get("text", "").splitlines()]
    questions = [line for line in lines if "?" in line and len(line) <= 1000]
    return questions[0] if len(questions) == 1 else None


def marked_incorrect(before, after):
    """Require newly visible feedback that the preceding choice was wrong."""
    previous = before.get("text", "")
    current = after.get("text", "")
    if normalize(current) == normalize(previous):
        return False
    old_lines = {normalize(line) for line in previous.splitlines()}
    added = "\n".join(line for line in current.splitlines() if normalize(line) not in old_lines)
    return bool(re.search(r"\b(?:incorrect|wrong answer|not correct)\b", added, re.I))


class QuizMemory:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("QUIZ_INCORRECT_FILE", "artifacts/quiz_incorrect_answers.json"))

    def read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, list)
                                               or any(not isinstance(answer, str) for answer in v)
                                               for k, v in data.items()):
            raise ValueError("Invalid quiz incorrect answers file")
        return data

    def save(self, question, answer):
        if not isinstance(question, str) or not isinstance(answer, str):
            raise ValueError("Question and answer must be text")
        question, answer = question.strip(), answer.strip()
        if not question or not answer or len(question) > 1000 or len(answer) > 1000:
            raise ValueError("Question and answer must be 1–1,000 characters each")
        data = self.read()
        answers = data.setdefault(question, [])
        if normalize(answer) not in {normalize(item) for item in answers}:
            answers.append(answer)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def matches(self, page):
        visible = normalize(page.get("text", ""))
        return [{"question": q, "incorrect_answers": a} for q, a in self.read().items()
                if normalize(q) and normalize(q) in visible]
