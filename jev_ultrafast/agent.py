"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, choose_llm, field_context, field_text, filter_blocked_actions
from .questions import MAX_STEPS


class Agent:
    def __init__(
        self,
        url,
        goals,
        *,
        record_dir=None,
        screenshots=False,
        target_id=None,
        text_model=None,
        decision_mode="jev",
        question_log_dir=None,
    ):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        if decision_mode not in {"jev", "jev_fallback", "llm"}:
            raise ValueError("Decision mode must be jev, jev_fallback, or llm")
        plan = [task]
        self.pending_text = None
        self.browser = Browser(
            url,
            target_id=target_id,
        )
        self.record_dir = Path(record_dir) if record_dir else None
        self.question_log = None
        self.pending_requests = []
        if question_log_dir:
            log_dir = Path(question_log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            self.question_log = log_dir / f"questions-{stamp}-{secrets.token_hex(4)}.jsonl"
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            text_calls=[],
            text_model=text_model or os.environ.get("TEXT_MODEL", "deepseek-chat").split(",", 1)[0].strip(),
            decision_mode=decision_mode,
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
            question_logging=bool(self.question_log),
            blocked_indices=[],
            fallback_pending=None,
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def _write_log(self, record):
        if getattr(self, "question_log", None):
            with self.question_log.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _log_execution(self, page, action, decision, text):
        if not getattr(self, "question_log", None):
            return
        blocked = self.state.get("blocked_indices", [])
        self._write_log(
            {
                "type": "execution",
                "logged_at": datetime.now(UTC).isoformat(),
                "blocked_indices": blocked,
                "page": {key: page.get(key) for key in ("url", "title", "text", "w", "h", "scroll")},
                "actions": filter_blocked_actions(page["actions"], blocked),
                "elements": action_space(page["actions"], blocked)[0],
                "decision": {
                    "engine": decision.get("decision_engine"),
                    "mode": self.state.get("decision_mode", "jev"),
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "choice": decision["choice"],
                    "label": action["label"],
                    "model": decision.get("model"),
                    "fallback_from": decision.get("fallback_from"),
                },
                "executed_action": {
                    "id": action["id"],
                    "kind": action["kind"],
                    "label": action["label"],
                    "text": text,
                },
                "decision_requests": self.pending_requests,
            }
        )
        self.pending_requests = []

    def _log_request(self, request, decision_engine):
        if getattr(self, "question_log", None):
            self.pending_requests.append({"decision_engine": decision_engine, "request": request})

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", body)
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            state.setdefault("blocked_indices", [])
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if "blocked_indices" in body:
                supplied = body["blocked_indices"]
                available = {element["index"] for element in action_space(state["page"]["actions"])[0]}
                if (
                    not isinstance(supplied, list)
                    or any(not isinstance(index, str) or index not in available for index in supplied)
                ):
                    raise ValueError("Blocked element indices must be available on the current page")
                state["blocked_indices"] = list(dict.fromkeys(supplied))
            self.pending_requests = []
            decision_mode = state.get("decision_mode", "jev")
            fallback_pending = state.get("fallback_pending")
            state["fallback_pending"] = None
            if decision_mode == "llm" or fallback_pending:
                decision_engine = "llm_fallback" if fallback_pending else "llm"
                state["decision"] = choose_llm(
                    state["page"],
                    state["goal"],
                    state["history"],
                    model=state.get("text_model"),
                    blocked_indices=state["blocked_indices"],
                    log_request=lambda request: self._log_request(request, decision_engine),
                )
                state["decision"]["decision_engine"] = decision_engine
                if fallback_pending:
                    state["decision"]["fallback_from"] = fallback_pending
            else:
                jev_decision = choose(
                    state["page"],
                    state["goal"],
                    state["history"],
                    blocked_indices=state["blocked_indices"],
                    log_request=lambda request: self._log_request(request, "jev"),
                )
                if decision_mode == "jev_fallback" and jev_decision["choice"] == "BLOCKED":
                    state["decision"] = choose_llm(
                        state["page"],
                        state["goal"],
                        state["history"],
                        model=state.get("text_model"),
                        blocked_indices=state["blocked_indices"],
                        log_request=lambda request: self._log_request(request, "llm_fallback"),
                    )
                    state["decision"]["decision_engine"] = "llm_fallback"
                    state["decision"]["fallback_from"] = {
                        "choice": "BLOCKED",
                        "model": jev_decision["model"],
                        "latency_ms": jev_decision["latency_ms"],
                    }
                else:
                    state["decision"] = jev_decision
                    state["decision"]["decision_engine"] = "jev"
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                self.pending_requests = []
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = field_text(context, model=state.get("text_model"))
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"].get(selected),
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            self._log_execution(page, action, decision, text)
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            no_progress = len(repeated) == 3 and all(
                h["page_changed"] is False and h["kind"] != "wait" for h in repeated
            )
            if (
                no_progress
                and state.get("decision_mode") == "jev_fallback"
                and decision.get("decision_engine") == "jev"
            ):
                state["status"] = "ready"
                state["fallback_pending"] = {
                    "reason": "NO_PROGRESS",
                    "message": "Three Jev actions produced no observed page change",
                }
            else:
                state["status"] = "blocked" if no_progress else "ready"
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
