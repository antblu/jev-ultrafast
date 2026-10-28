"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def test_model_transport_retries_a_read_timeout(monkeypatch):
    response = Mock(status_code=200, is_error=False)
    response.json.return_value = {"ok": True}
    client = Mock()
    client.post.side_effect = [httpx.ReadTimeout("slow response"), response]
    monkeypatch.setattr(model, "CLIENT", client)
    monkeypatch.setattr(model.time, "sleep", Mock())
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "75")
    assert model.post_json("https://model.test", "secret", {"input": "state"}) == {"ok": True}
    assert client.post.call_count == 2
    assert client.post.call_args.kwargs["timeout"] == 75


def test_model_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "999")
    assert model.model_timeout() == 300
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "invalid")
    assert model.model_timeout() == 60


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_blocked_indices_are_omitted_without_renumbering():
    elements, targets, controls = model.action_space(page()["actions"], {"1"})
    assert [element["index"] for element in elements] == ["2"]
    assert set(targets["CLICK"]) == {"2"}
    assert "TYPE_TEXT" not in targets
    assert "WAIT" in controls


def test_blocked_indices_are_removed_from_raw_logged_actions():
    actions = model.filter_blocked_actions(page()["actions"], {"1"})
    assert [action["id"] for action in actions] == ["e3", "wait"]


def test_frame_local_node_ids_get_distinct_global_indices():
    actions = [
        {"id": "e1", "kind": "click", "label": "Outer", "role": "button", "node": 1, "frame_id": "f0"},
        {"id": "e2", "kind": "click", "label": "Inner", "role": "button", "node": 1, "frame_id": "f1"},
    ]
    elements, targets, _ = model.action_space(actions)
    assert [element["label"] for element in elements] == ["Outer", "Inner"]
    assert set(targets["CLICK"]) == {"1", "2"}


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_question_logger_receives_request_before_transport(monkeypatch):
    logged = []
    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        model.choose(page(), "Find a book", [], log_request=logged.append)
    assert logged[0]["state"]["elements"][0]["label"] == "Search"
    assert set(logged[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_llm_mode_chooses_only_an_observed_operation_and_target(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(
        return_value={
            "model": "provider/decision-model",
            "choices": [{"message": {"content": '{"operation":"CLICK","target":2}'}}],
        }
    )
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose_llm(page(), "Submit the search", [], model="provider/decision-model")
    assert result["choice"] == "e3"
    assert result["operation"] == "CLICK"
    assert result["target"] == "2"
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert set(sent["choices"]["CLICK"]) == {"1", "2"}


@pytest.mark.parametrize("compatible", [True, False])
def test_model_image_capability_controls_request_and_redaction(monkeypatch, compatible):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("MODEL_CONFIG", json.dumps({"vision": {"image_compatible": compatible}}))
    post = Mock(return_value={"choices": [{"message": {"content": '{"operation":"CLICK","target":"2"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    current = page()
    current["screenshot"] = "jpeg-base64"
    logged = []
    result = model.choose_llm(current, "Answer the question", [], model="vision", log_request=logged.append)
    content = post.call_args.args[2]["messages"][1]["content"]
    if compatible:
        assert content[1]["image_url"]["url"] == "data:image/jpeg;base64,jpeg-base64"
        assert logged[0]["messages"][1]["content"][1]["image_url"]["url"] == "[screenshot omitted]"
    else:
        assert isinstance(content, str)
    assert "jpeg-base64" not in json.dumps(result)


def test_per_model_provider_credentials_stay_server_side(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "private-test-key")
    monkeypatch.setenv("MODEL_CONFIG", json.dumps({"torchcast": {
        "image_compatible": False, "base_url": "https://proxy.test/v1", "api_key_env": "LITELLM_API_KEY",
    }}))
    assert model.text_model_config("torchcast")[:3] == ("private-test-key", "https://proxy.test/v1", "torchcast")


def test_llm_mode_rejects_an_invented_target(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(
        model,
        "post_json",
        Mock(return_value={"choices": [{"message": {"content": '{"operation":"CLICK","target":"999"}'}}]}),
    )
    with pytest.raises(ValueError, match="valid observed action"):
        model.choose_llm(page(), "Submit the search", [])


def test_llm_mode_reports_truncated_reasoning_before_json(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(
        model,
        "post_json",
        Mock(
            return_value={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "Still reasoning"},
                    }
                ]
            }
        ),
    )
    with pytest.raises(RuntimeError, match="LLM_DECISION_MAX_TOKENS"):
        model.choose_llm(page(), "Submit the search", [])


def test_glm_decisions_use_forced_function_calling(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(
        return_value={
            "model": "zai-org/GLM-5.3-Flash",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "choose_action",
                                    "arguments": '{"operation":"CLICK","target":"2"}',
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose_llm(page(), "Submit the search", [], model="zai-org/GLM-5.3-Flash")
    assert result["choice"] == "e3"
    body = post.call_args.args[2]
    assert body["tool_choice"]["function"]["name"] == "choose_action"
    assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert "response_format" not in body


def test_llm_decision_budget_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("LLM_DECISION_MAX_TOKENS", "2048")
    assert model.token_budget("LLM_DECISION_MAX_TOKENS", 1024) == 2048
    monkeypatch.setenv("LLM_DECISION_MAX_TOKENS", "999999")
    assert model.token_budget("LLM_DECISION_MAX_TOKENS", 1024) == 8192


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_selected_text_model_is_sent_to_the_helper(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    model.field_text({"goal": "Enter Zurich"}, model="provider/selected-model")
    assert post.call_args.args[2]["model"] == "provider/selected-model"


def test_demo_accepts_comma_separated_model_choices(monkeypatch):
    from jev_ultrafast import demo

    monkeypatch.setenv("TEXT_MODEL", "provider/default,provider/second")
    monkeypatch.setenv("TEXT_MODEL_OPTIONS", "provider/second,provider/third")
    assert demo.default_text_model() == "provider/default"
    assert demo.text_model_options() == ["provider/default", "provider/second", "provider/third"]


def test_agent_logs_filtered_elements_and_decision_only_after_execution(tmp_path, monkeypatch):
    p = page()
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    agent = loop.Agent(None, "Find a book", question_log_dir=tmp_path)
    logged_decision = decision("e3")
    logged_decision.update(operation="CLICK", target="2", model="jev-test")

    def choose_with_log(*_args, log_request, **_kwargs):
        log_request({"questions": {"operation": {}}})
        return logged_decision

    monkeypatch.setattr(loop, "choose", choose_with_log)
    assert not agent.question_log.exists()
    agent.command("predict", {"blocked_indices": ["1"]})
    assert not agent.question_log.exists()
    agent.command("act", {"fingerprint": p["fingerprint"]})
    records = [json.loads(line) for line in agent.question_log.read_text().splitlines()]
    assert records[0]["type"] == "execution"
    assert [element["index"] for element in records[0]["elements"]] == ["2"]
    assert [action["id"] for action in records[0]["actions"]] == ["e3", "wait"]
    assert records[0]["decision"]["engine"] == "jev"
    assert records[0]["decision"]["target"] == "2"
    assert records[0]["executed_action"]["id"] == "e3"
    assert records[0]["decision_requests"][0]["decision_engine"] == "jev"
    assert "screenshot" not in records[0]["page"]


def test_done_or_blocked_decision_does_not_create_execution_log(tmp_path, monkeypatch):
    p = page()
    browser = Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p))
    monkeypatch.setattr(loop, "Browser", Mock(return_value=browser))
    agent = loop.Agent(None, "Find a book", question_log_dir=tmp_path)
    agent.state.update(decision=decision("BLOCKED"), status="predicted", started_at=time.perf_counter())
    agent.command("act", {"fingerprint": p["fingerprint"]})
    assert not agent.question_log.exists()


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


def test_agent_predict_dispatches_to_the_selected_llm_mode(runner, monkeypatch):
    runner.state.update(status="ready", decision=None, decision_mode="llm", text_model="provider/selected")
    llm_decision = decision("e3")
    llm_decision.update(operation="CLICK", target="2")
    llm = Mock(return_value=llm_decision)
    jev = Mock()
    monkeypatch.setattr(loop, "choose_llm", llm)
    monkeypatch.setattr(loop, "choose", jev)
    runner.command("predict")
    llm.assert_called_once()
    assert llm.call_args.args == (page(), "Find a book", [])
    assert llm.call_args.kwargs["model"] == "provider/selected"
    assert llm.call_args.kwargs["blocked_indices"] == []
    assert callable(llm.call_args.kwargs["log_request"])
    jev.assert_not_called()


def test_jev_blocked_stops_without_llm_fallback(runner, monkeypatch):
    runner.state.update(status="ready", decision=None, decision_mode="jev")
    blocked = {**decision("BLOCKED"), "operation": "BLOCKED", "target": None}
    monkeypatch.setattr(loop, "choose", Mock(return_value=blocked))
    llm = Mock()
    monkeypatch.setattr(loop, "choose_llm", llm)
    runner.command("tick")
    assert runner.state["status"] == "blocked"
    llm.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


@pytest.mark.parametrize("config", ['[]', '{"model":{"image_compatible":"false"}}',
                                  '{"model":{"api_key":"secret"}}'])
def test_invalid_model_configuration_fails_closed(monkeypatch, config):
    monkeypatch.setenv("MODEL_CONFIG", config)
    with pytest.raises(ValueError, match="MODEL_CONFIG"):
        model.model_catalog()


def test_image_model_missing_image_does_not_call_provider(monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG", '{"vision":{"image_compatible":true}}')
    post = Mock()
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="current browser image"):
        model.choose_llm(page(), "Read the diagram", [], model="vision")
    post.assert_not_called()


def test_public_model_picker_excludes_provider_settings(monkeypatch):
    from jev_ultrafast import demo

    monkeypatch.setattr(demo, "AGENT", None)
    monkeypatch.setattr(demo, "available_tabs", lambda: [])
    monkeypatch.setenv("MODEL_CONFIG", '{"vision":{"image_compatible":true,"api_key_env":"PRIVATE_KEY"}}')
    state = demo.response_state()
    entries = {item["id"]: item for item in state["models"]}
    assert entries["vision"] == {"id": "vision", "name": "vision", "image_compatible": True,
                                 "audio_compatible": False, "model_type": "llm"}
    assert entries["jev"]["model_type"] == "decision"
    assert "PRIVATE_KEY" not in json.dumps(state)


def test_model_picker_discovers_endpoint_ids_and_keeps_key_server_side(monkeypatch):
    from jev_ultrafast import demo

    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://models.test/v1")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "secret-test-key")
    monkeypatch.setenv("TEXT_MODEL", "configured")
    monkeypatch.setenv("MODEL_CONFIG", '{"vision":{"image_compatible":true}}')
    monkeypatch.setattr(demo, "MODEL_LIST_CACHE", {"key": None, "expires": 0, "ids": ()})
    monkeypatch.setattr(demo, "available_tabs", lambda: [])
    response = Mock()
    response.json.return_value = {"data": [{"id": "new-model"}, {"id": "configured"}]}
    request = Mock(return_value=response)
    monkeypatch.setattr(demo.httpx, "get", request)
    state = demo.response_state()
    entries = {item["id"]: item for item in state["models"]}
    assert {"jev", "new-model", "configured", "vision"} <= set(entries)
    assert entries["new-model"]["model_type"] == "llm"
    assert entries["vision"]["image_compatible"] is True
    assert request.call_count == 1
    demo.response_state()
    assert request.call_count == 1
    assert request.call_args.kwargs["headers"] == {"Authorization": "Bearer secret-test-key"}
    assert "secret-test-key" not in json.dumps(state)


@pytest.mark.parametrize("selected,mode", [("jev", "jev"), ("torchcast", "llm"), ("standard", "llm")])
def test_single_model_picker_selects_provider_on_server(monkeypatch, selected, mode):
    from jev_ultrafast import demo

    monkeypatch.setenv("TEXT_MODEL", "standard")
    monkeypatch.setenv("MODEL_CONFIG", json.dumps({"torchcast": {
        "model_type": "decision", "image_compatible": True, "audio_compatible": True,
    }}))
    monkeypatch.setattr(demo, "AGENT", None)
    monkeypatch.setattr(demo, "available_tabs", lambda: [{"id": "tab", "url": "https://example.test"}])
    instance = Mock(state={})
    instance.snapshot.side_effect = lambda: instance.state
    agent = Mock(return_value=instance)
    monkeypatch.setattr(demo, "Agent", agent)
    result = demo.command("reset", {"goal": "Find a book", "target_id": "tab", "model": selected})
    assert agent.call_args.kwargs["decision_mode"] == mode
    assert agent.call_args.kwargs["text_model"] == ("standard" if selected == "jev" else selected)
    assert result["selected_model"] == selected
    torchcast = next(item for item in result["models"] if item["id"] == "torchcast")
    assert torchcast["model_type"] == "decision"
    assert torchcast["image_compatible"] is True
    assert torchcast["audio_compatible"] is True



def test_torchcast_uses_dedicated_key_despite_inherited_litellm_key(monkeypatch, tmp_path):
    from jev_ultrafast import demo

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_API_KEY", "unrelated-inherited-key")
    monkeypatch.delenv("JEV_TORCHCAST_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_CONFIG", raising=False)
    config = {"torchcast": {"api_key_env": "JEV_TORCHCAST_API_KEY"}}
    (tmp_path / ".env").write_text(
        "JEV_TORCHCAST_API_KEY=torchcast-test-key\nMODEL_CONFIG=" + json.dumps(config) + "\n"
    )
    demo.load_environment()
    assert model.text_model_config("torchcast")[0] == "torchcast-test-key"


@pytest.mark.parametrize("kind,budget", [("decision", 256), ("text", 512)])
def test_llama_cpp_uses_explicit_reasoning_controls_and_model_budget(monkeypatch, kind, budget):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("LLM_DECISION_MAX_TOKENS", "8192")
    monkeypatch.setenv("TEXT_VALUE_MAX_TOKENS", "8192")
    monkeypatch.setenv("MODEL_CONFIG", json.dumps({"torchcast": {
        "backend": "llama_cpp", "reasoning_effort": "none", "decision_max_tokens": 256, "text_max_tokens": 512,
    }}))
    content = '{"operation":"CLICK","target":"2"}' if kind == "decision" else '{"text":"book"}'
    post = Mock(return_value={"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(model, "post_json", post)
    if kind == "decision":
        model.choose_llm(page(), "Find a book", [], model="torchcast")
    else:
        model.field_text({"goal": "Find a book"}, model="torchcast")
    body = post.call_args.args[2]
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning" not in body
    assert body["max_tokens"] == budget


@pytest.mark.parametrize("budget", [True, 0, 9000, "256"])
def test_invalid_per_model_budget_is_rejected(monkeypatch, budget):
    monkeypatch.setenv("MODEL_CONFIG", json.dumps({"torchcast": {"decision_max_tokens": budget}}))
    with pytest.raises(ValueError, match="MODEL_CONFIG"):
        model.model_catalog()



@pytest.mark.parametrize("operation", ["CLICK", "click", "Click", " cLiCk "])
def test_llm_operation_case_normalization_preserves_observed_target(monkeypatch, operation):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {
        "content": json.dumps({"operation": operation, "target": 2}),
    }}]})
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose_llm(page(), "Submit the search", [])
    assert result["operation"] == "CLICK"
    assert result["choice"] == "e3"
    assert post.call_count == 1


@pytest.mark.parametrize("operation,target,blocked", [
    ("click", 2, ["2"]), ("click", 999, []), ("click", True, []),
    ("click", "document.querySelector('button')", []),
    ("type_text", 2, []), ("double_click", 2, []), (None, 2, []),
])
def test_llm_case_normalization_does_not_relax_supported_targets(monkeypatch, operation, target, blocked):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {
        "content": json.dumps({"operation": operation, "target": target}),
    }}]})
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="no valid observed action"):
        model.choose_llm(page(), "Submit the search", [], blocked_indices=blocked)
    assert post.call_count == 1



def test_no_progress_stop_is_distinct_from_model_blocked(runner):
    runner.state["history"] = [{"page_changed": False, "kind": "click"}] * 2
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "target": "2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "no_progress"
    runner.state["browser"].act.assert_called_once()


def test_model_blocked_sets_its_own_stop_reason(runner):
    runner.state["decision"] = {**decision("BLOCKED"), "operation": "BLOCKED", "target": None}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["stop_reason"] == "model_blocked"
    runner.state["browser"].act.assert_not_called()


def test_model_receives_group_headings_and_previous_click_group(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    current = page()
    current["actions"][2]["group"] = "Destinations"
    current["actions"][2]["visual_state"] = {"outline": "dashed", "border": "solid"}
    post = Mock(return_value={"choices": [{"message": {"content": '{"operation":"CLICK","target":"2"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    model.choose_llm(current, "Connect the matching items", [
        {"action": "Origin", "kind": "click", "target": "1", "group": "Sources"}
    ])
    context = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert context["choices"]["CLICK"]["2"]["group"] == "Destinations"
    assert context["recent_actions"][0]["group"] == "Sources"
    assert context["last_executed_click"]["target"] == "1"
    assert context["choices"]["CLICK"]["2"]["visual_state"]["outline"] == "dashed"


def test_stalled_group_is_omitted_without_renumbering_or_fallback(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    current = page()
    current["actions"][0]["group"] = current["actions"][1]["group"] = "Sources"
    current["actions"][2]["group"] = "Destinations"
    history = [{"choice": "e3", "target": "2", "kind": "click", "page_changed": False}] * 2
    post = Mock(return_value={"choices": [{"message": {"content": '{"operation":"CLICK","target":"1"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose_llm(current, "Connect the matching items", history)
    context = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert set(context["choices"]["CLICK"]) == {"1"}
    assert context["execution_feedback"]["temporarily_omitted_click_group"] == "Destinations"
    assert result["choice"] == "e2"
    assert post.call_count == 1


def test_switching_groups_allows_one_setup_step_then_stops_if_still_unchanged(runner):
    runner.state["history"] = [{"page_changed": False, "kind": "click", "group": "Destinations"}] * 2
    runner.state["page"]["actions"][2]["group"] = "Sources"
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "target": "2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    runner.state["page"]["actions"][2]["group"] = "Destinations"
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK", "target": "2"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "no_progress"
    assert runner.state["browser"].act.call_count == 2



def test_llama_cpp_schema_contains_only_matching_observed_unblocked_targets(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setenv("MODEL_CONFIG", '{"local":{"backend":"llama_cpp"}}')
    post = Mock(return_value={"choices": [{"message": {"content": '{"operation":"CLICK","target":"2"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    model.choose_llm(page(), "Submit the search", [], model="local", blocked_indices=["1"])
    variants = post.call_args.args[2]["response_format"]["schema"]["oneOf"]
    operations = {item["properties"]["operation"]["enum"][0]: item for item in variants}
    assert set(operations) == {"CLICK", "WAIT", "DONE", "BLOCKED"}
    assert operations["CLICK"]["properties"]["target"] == {"type": "string", "enum": ["2"]}
    assert operations["WAIT"]["properties"]["target"] == {"type": "null"}


@pytest.mark.parametrize("operation,delta", [("SCROLL_DOWN", 200), ("SCROLL_UP", -200)])
def test_llm_scroll_uses_observed_region_and_null_target(monkeypatch, operation, delta):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    current = page()
    observed = {"id": operation.lower(), "kind": "scroll", "frame_id": "f1", "node": 30,
                "label": "Reveal additional choices", "delta": delta}
    current["actions"].append(observed)
    post = Mock(return_value={"choices": [{"message": {
        "content": json.dumps({"operation": operation, "target": None})
    }}]})
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose_llm(current, "Inspect all choices", [])
    assert result["choice"] == observed["id"]
    assert result["target"] is None
    assert next(a for a in current["actions"] if a["id"] == result["choice"]) == observed
    context = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert context["choices"][operation]["label"] == "Reveal additional choices"
