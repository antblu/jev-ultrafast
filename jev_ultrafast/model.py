"""Observed-action decisions through TypeSafe or an OpenAI-compatible LLM."""

import json
import math
import os
import time
from copy import deepcopy

import httpx

from .questions import LLM_DECISION, NEXT_ACTION, TARGET, TEXT_VALUE
from .quiz_memory import normalize

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
            element = {
                k: action[k] for k in (
                    "role", "value", "checked", "selected", "expanded", "pressed", "group", "visual_state"
                )
                if k in action
            }
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


def filter_blocked_actions(actions, blocked_indices):
    """Remove indexed actions while preserving unindexed controls and original numbering."""
    blocked = {str(index) for index in blocked_indices}
    indices = {}
    filtered = []
    for action in actions:
        if action["kind"] not in {"click", "fill", "select"}:
            filtered.append(action)
            continue
        observed_target = (action.get("frame_id", "f0"), action["node"])
        if observed_target not in indices:
            indices[observed_target] = str(len(indices) + 1)
        if indices[observed_target] not in blocked:
            filtered.append(action)
    return filtered


def exclude_known_mistakes(targets, known_answers):
    """Remove only matching observed radio choices for the visible question."""
    wrong = {normalize(answer) for entry in known_answers for answer in entry["incorrect_answers"]}
    if not wrong or "CLICK" not in targets:
        return targets
    targets["CLICK"] = {
        index: action for index, action in targets["CLICK"].items()
        if action.get("role") != "radio" or normalize(action["label"]) not in wrong
    }
    if not targets["CLICK"]:
        del targets["CLICK"]
    return targets


def model_catalog():
    """Resolve server-side model settings; credentials are environment references only."""
    names = ",".join([os.environ.get("TEXT_MODEL", "deepseek-chat"), os.environ.get("TEXT_MODEL_OPTIONS", "")])
    defaults = {"image_compatible": False, "audio_compatible": False, "model_type": "llm"}
    catalog = {name.strip(): dict(defaults) for name in names.split(",") if name.strip()}
    try:
        configured = json.loads(os.environ.get("MODEL_CONFIG", "{}"))
        if not isinstance(configured, dict):
            raise ValueError()
        for name, settings in configured.items():
            if not isinstance(name, str) or not name or not isinstance(settings, dict):
                raise ValueError()
            if set(settings) - {
                "image_compatible", "audio_compatible", "model_type", "base_url", "api_key_env",
                "backend", "reasoning_effort", "decision_max_tokens", "text_max_tokens",
            }:
                raise ValueError()
            if any(not isinstance(settings.get(key, False), bool)
                   for key in ("image_compatible", "audio_compatible")):
                raise ValueError()
            if settings.get("model_type", "llm") not in {"decision", "llm"}:
                raise ValueError()
            if settings.get("backend", "openai") not in {"openai", "llama_cpp"}:
                raise ValueError()
            if settings.get("reasoning_effort", "none") not in {"none", "low", "medium", "high"}:
                raise ValueError()
            for key in ("decision_max_tokens", "text_max_tokens"):
                if key in settings and (type(settings[key]) is not int or not 64 <= settings[key] <= 8192):
                    raise ValueError()
            for key in ("base_url", "api_key_env"):
                if key in settings and (not isinstance(settings[key], str) or not settings[key]):
                    raise ValueError()
            catalog[name] = {**defaults, **settings}
    except (ValueError, TypeError):
        raise ValueError("MODEL_CONFIG must map model names to valid provider and image settings") from None
    return catalog


def model_settings(model=None):
    name = model or os.environ.get("TEXT_MODEL", "deepseek-chat").split(",", 1)[0].strip()
    return model_catalog().get(name, {"image_compatible": False})


def text_model_config(model=None):
    model = model or os.environ.get("TEXT_MODEL", "deepseek-chat").split(",", 1)[0].strip()
    settings = model_settings(model)
    key_env = settings.get("api_key_env", "TEXT_MODEL_API_KEY")
    key = os.environ.get(key_env)
    if not key:
        raise ValueError(f"The selected LLM needs {key_env}; no action executed.")
    base = settings.get("base_url", os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")).rstrip("/")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {
        "reasoning": {"effort": "low"}
    }
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    if settings.get("backend") == "llama_cpp":
        effort = settings.get("reasoning_effort", "none" if os.environ.get("TEXT_MODEL_REASONING") == "none" else "low")
        reasoning = {"reasoning_effort": effort, "chat_template_kwargs": {"enable_thinking": effort != "none"}}
    return key, base, model, reasoning


def token_budget(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return min(8192, max(64, value))


def completion_budget(model, kind):
    settings = model_settings(model)
    if f"{kind}_max_tokens" in settings:
        return settings[f"{kind}_max_tokens"]
    return token_budget("LLM_DECISION_MAX_TOKENS" if kind == "decision" else "TEXT_VALUE_MAX_TOKENS", 1024)


def decision_format(model):
    configured = os.environ.get("LLM_DECISION_FORMAT", "auto").strip().lower()
    if configured not in {"auto", "json", "tool"}:
        configured = "auto"
    if configured != "auto":
        return configured
    lowered = model.lower()
    return "tool" if "glm" in lowered or lowered.startswith("zai-org/") else "json"


def redact_image_data(body):
    redacted = deepcopy(body)
    for message in redacted.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            image_url = item.get("image_url") if isinstance(item, dict) else None
            if isinstance(image_url, dict) and str(image_url.get("url", "")).startswith("data:image/"):
                image_url["url"] = "[screenshot omitted]"
    return redacted


def choose(state, goal, history, *, blocked_indices=(), log_request=None, known_answers=()):
    elements, targets, controls = action_space(state["actions"], blocked_indices)
    targets = exclude_known_mistakes(targets, known_answers)
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
                {k: h.get(k) for k in ("action", "kind", "target", "text", "page_changed", "group")}
                for h in history[-10:]
            ],
            "known_quiz_mistakes": list(known_answers),
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


def observed_decision_schema(targets, controls):
    variants = []
    for operation in [*targets, *controls, "DONE", "BLOCKED"]:
        target_schema = ({"type": "string", "enum": list(targets[operation])}
                         if operation in targets else {"type": "null"})
        variants.append({
            "type": "object",
            "properties": {"operation": {"type": "string", "enum": [operation]}, "target": target_schema},
            "required": ["operation", "target"],
            "additionalProperties": False,
        })
    return {"oneOf": variants}


def unchanged_clicks(state, history, limit=3):
    """Recover recent observed group context only while every click left this page unchanged."""
    unchanged = []
    for item in reversed(history):
        if item.get("page_changed") is not False or item.get("kind") != "click":
            break
        action = next((action for action in state["actions"] if action["id"] == item.get("choice")), {})
        unchanged.append({"target": item.get("target"), "group": item.get("group") or action.get("group")})
        if len(unchanged) == limit:
            break
    return list(reversed(unchanged))


def choose_llm(state, goal, history, *, model=None, blocked_indices=(), log_request=None,
               known_answers=()):
    image_compatible = model_settings(model)["image_compatible"]
    screenshot = state.get("screenshot") if image_compatible else None
    if image_compatible and not screenshot:
        raise ValueError("Image-compatible model requires a current browser image; no action executed")
    _, targets, controls = action_space(state["actions"], blocked_indices)
    targets = exclude_known_mistakes(targets, known_answers)
    unchanged = unchanged_clicks(state, history)
    stalled_group = None
    if len(unchanged) >= 2 and unchanged[-1]["group"] and unchanged[-1]["group"] == unchanged[-2]["group"]:
        stalled_group = unchanged[-1]["group"]
        # Only restrict a stalled group when another observed group offers a different interaction.
        if any(action.get("group") and action["group"] != stalled_group
               for action in targets.get("CLICK", {}).values()):
            targets["CLICK"] = {index: action for index, action in targets["CLICK"].items()
                                if action.get("group") != stalled_group}
        else:
            stalled_group = None
    choices = {
        operation: {
            index: {
                "label": action["label"],
                **{
                    key: action[key]
                    for key in ("role", "value", "current_value", "checked", "selected", "expanded",
                                "pressed", "group", "visual_state")
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
    context = {
        "goal": goal,
        "known_quiz_mistakes": list(known_answers),
        "page": {key: state[key] for key in ("url", "title", "text")},
        "choices": choices,
        "recent_actions": [
            {key: item.get(key) for key in ("action", "kind", "target", "text", "page_changed", "group")}
            for item in history[-10:]
        ],
        "screenshot_supplied": bool(screenshot),
        "last_executed_click": ({key: history[-1].get(key) for key in ("action", "target", "group", "page_changed")}
                                if history and history[-1].get("kind") == "click" else None),
    }
    if unchanged:
        context["execution_feedback"] = {
            "unchanged_clicks": unchanged,
            "temporarily_omitted_click_group": stalled_group,
            "instruction": "Repeated clicks in one group made no observed change. Try a different relevant group. "
                           "For paired controls, select one source then its destination; a source selection "
                           "can require the next destination click to produce an observed change. "
                           "Existing drawn connections are completed pairs, not an active source selection.",
        }
    key, base, model, reasoning = text_model_config(model)
    output_format = decision_format(model)
    body = {
        "model": model,
        "max_tokens": completion_budget(model, "decision"),
        **reasoning,
        "messages": [
            {"role": "system", "content": "\n".join([LLM_DECISION, NEXT_ACTION, TARGET])},
            {
                "role": "user",
                "content": (
                    [
                        {"type": "text", "text": json.dumps(context, separators=(",", ":"))},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot}"}},
                    ]
                    if screenshot
                    else json.dumps(context, separators=(",", ":"))
                ),
            },
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
                                "operation": {
                                    "anyOf": [
                                        {"type": "string", "enum": list(choices)},
                                        {"type": "null"},
                                    ]
                                },
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
        if model_settings(model).get("backend") == "llama_cpp":
            body["response_format"]["schema"] = observed_decision_schema(targets, controls)
    logged_body = redact_image_data(body)
    if log_request:
        log_request(logged_body)
    started = time.perf_counter()
    result = post_json(base + "/chat/completions", key, body)
    try:
        response_choice = result["choices"][0]
        message = response_choice["message"]
        if response_choice.get("finish_reason") == "length":
            raise RuntimeError(
                "LLM decision was truncated before it emitted JSON. Increase "
                "the model decision_max_tokens (or LLM_DECISION_MAX_TOKENS) or disable reasoning; no action executed."
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
        if not isinstance(output, dict) or set(output) != {"operation", "target"}:
            raise ValueError()
        operation, target = output["operation"], output["target"]
        if not isinstance(operation, str):
            raise ValueError()
        operation = operation.strip().upper()
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
        "timings": result.get("timings", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": logged_body,
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
            "max_tokens": completion_budget(model, "text"),
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context, separators=(",", ":")),
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
