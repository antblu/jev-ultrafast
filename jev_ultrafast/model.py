"""Observed-action decisions through TypeSafe or an OpenAI-compatible LLM."""

import json
import math
import os
import time

import httpx

from .questions import LLM_DECISION, NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=60)


def model_timeout():
    try:
        value = float(os.environ.get("MODEL_TIMEOUT_SECONDS", "60"))
    except ValueError:
        value = 60
    return min(300, max(5, value))


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {key}"},
                timeout=model_timeout(),
            )
        except httpx.HTTPError as error:
            if attempt < 2:
                time.sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(
                f"Model connection failed after 3 attempts ({type(error).__name__}); no action executed."
            ) from None
        if response.status_code in {408, 425, 429, 500, 502, 503, 504, 529} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions, blocked_indices=()):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        observed_target = (action.get("frame_id", "f0"), node)
        if observed_target not in indices:
            index = str(len(elements) + 1)
            indices[observed_target] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[observed_target]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    blocked = {str(index) for index in blocked_indices}
    if blocked:
        elements = [element for element in elements if element["index"] not in blocked]
        targets = {
            operation: {
                target: action
                for target, action in candidates.items()
                if target.split(":", 1)[0] not in blocked
            }
            for operation, candidates in targets.items()
        }
        targets = {operation: candidates for operation, candidates in targets.items() if candidates}
    return elements, targets, controls


def text_model_config(model=None):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("The selected LLM needs TEXT_MODEL_API_KEY; no action executed.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = model or os.environ.get("TEXT_MODEL", "deepseek-chat").split(",", 1)[0].strip()
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {
        "reasoning": {"effort": "low"}
    }
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    return key, base, model, reasoning


def token_budget(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(8192, max(64, value))


def decision_format(model):
    configured = os.environ.get("LLM_DECISION_FORMAT", "auto").strip().lower()
    if configured not in {"auto", "json", "tool"}:
        configured = "auto"
    if configured != "auto":
        return configured
    lowered = model.lower()
    return "tool" if "glm" in lowered or lowered.startswith("zai-org/") else "json"


def choose(state, goal, history, *, blocked_indices=(), log_request=None):
    elements, targets, controls = action_space(state["actions"], blocked_indices)
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    if log_request:
        log_request(body)
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def choose_llm(state, goal, history, *, model=None, blocked_indices=(), log_request=None):
    _, targets, controls = action_space(state["actions"], blocked_indices)
    choices = {
        operation: {
            index: {
                "label": action["label"],
                **{
                    key: action[key]
                    for key in ("role", "value", "current_value", "checked", "selected", "expanded")
                    if key in action
                },
            }
            for index, action in candidates.items()
        }
        for operation, candidates in targets.items()
    }
    choices.update({operation: {"label": action["label"]} for operation, action in controls.items()})
    choices.update(
        DONE={"label": "Every requirement is visibly satisfied."},
        BLOCKED={"label": "No supported operation can progress."},
    )
    answer_candidates = {
        index: candidate
        for index, candidate in choices.get("CLICK", {}).items()
        if candidate.get("role") in {"radio", "checkbox"}
    }
    context = {
        "goal": goal,
        "page": {key: state[key] for key in ("url", "title", "text")},
        "choices": choices,
        "current_radio_checkbox_candidates": answer_candidates,
        "recent_actions": [
            {key: item.get(key) for key in ("action", "kind", "text", "page_changed")}
            for item in history[-10:]
        ],
        "rules": [NEXT_ACTION, TARGET],
    }
    key, base, model, reasoning = text_model_config(model)
    output_format = decision_format(model)
    body = {
        "model": model,
        "max_tokens": token_budget("LLM_DECISION_MAX_TOKENS", 1024),
        **reasoning,
        "messages": [
            {"role": "system", "content": LLM_DECISION},
            {"role": "user", "content": json.dumps(context)},
        ],
    }
    if output_format == "tool":
        target_ids = list(dict.fromkeys(index for candidates in targets.values() for index in candidates))
        body.update(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "choose_action",
                        "description": "Choose exactly one offered next browser action.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "operation": {"type": "string", "enum": list(choices)},
                                "target": {
                                    "anyOf": [
                                        {"type": "string", "enum": target_ids},
                                        {"type": "null"},
                                    ]
                                },
                            },
                            "required": ["operation", "target"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "choose_action"}},
        )
    else:
        body["response_format"] = {"type": "json_object"}
    if log_request:
        log_request(body)
    started = time.perf_counter()
    result = post_json(base + "/chat/completions", key, body)
    try:
        response_choice = result["choices"][0]
        message = response_choice["message"]
        if response_choice.get("finish_reason") == "length":
            raise RuntimeError(
                "LLM decision was truncated before it emitted JSON. Increase "
                "LLM_DECISION_MAX_TOKENS or disable model reasoning; no action executed."
            )
        if output_format == "tool":
            tool_calls = message.get("tool_calls") or []
            call = next(
                (
                    item
                    for item in tool_calls
                    if item.get("function", {}).get("name") == "choose_action"
                ),
                None,
            )
            arguments = call["function"]["arguments"]
            output = arguments if isinstance(arguments, dict) else json.loads(arguments)
        else:
            output = json.loads(message.get("content") or "")
        if set(output) != {"operation", "target"}:
            raise ValueError()
        operation, target = output["operation"], output["target"]
        if operation in targets:
            if isinstance(target, int) and not isinstance(target, bool):
                target = str(target)
            if not isinstance(target, str) or target not in targets[operation]:
                raise ValueError()
            choice = targets[operation][target]["id"]
        elif operation in controls or operation in {"DONE", "BLOCKED"}:
            if target is not None:
                raise ValueError()
            choice = controls[operation]["id"] if operation in controls else operation
        else:
            raise ValueError()
    except RuntimeError:
        raise
    except (ValueError, KeyError, TypeError):
        raise ValueError("LLM returned no valid observed action; no action executed.") from None
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": None,
        "probabilities": {choice: None},
        "operation_probabilities": {},
        "target_probabilities": {},
        "target_confidence": None,
        "raw_answers": output,
        "model": result.get("model", model),
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_text(context, *, model=None):
    key, base, model, reasoning = text_model_config(model)
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": token_budget("TEXT_VALUE_MAX_TOKENS", 1024),
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
