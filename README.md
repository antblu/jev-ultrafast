<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

Visible group headings accompany indexed controls so matching and other paired widgets can be operated as source and destination clicks. After two unchanged clicks in the same group, that group is omitted from click choices when another observed group is available. A switch to a different group after those two clicks gets one setup step to complete a pair; continued lack of progress still stops the run. The inspector distinguishes a model `BLOCKED` choice from the local guard that stops after three actions with no observed progress.

Grouped controls also expose dashed/dotted selection outlines and border styles. These visible cues participate in freshness checks and progress detection, so selecting a source in a CSS-only matching widget is observable even when its text and ARIA attributes stay unchanged. Solid focus outlines and outline color/width changes do not count as selection progress. Model context includes the last executed click and its target; paired-control instructions distinguish an active source from existing connections.

Scroll actions discover visible scrollable panels, same-origin frame documents, and the outer page. Each direction targets an observed region, prefers nested content, and moves by up to three quarters of its viewport (capped at 560 pixels). The next observation reveals newly visible options; controls and text clipped outside a panel are omitted. Scroll positions participate in freshness and progress checks, and a direction disappears when the region reaches its end.

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start task → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

For quizzes, the agent remembers a selected answer only when the page visibly marks it incorrect. Wrong choices persist server-side in `artifacts/quiz_incorrect_answers.json` (or `QUIZ_INCORRECT_FILE`). On a retake, the agent excludes those choices when the same question and option text appear again. It does not guess or save the correct answer. The final quiz result must still be checked independently.

The demo has one **Model** dropdown. It loads all IDs returned by `TEXT_MODEL_BASE_URL/models` using the server-side `TEXT_MODEL_API_KEY`, keeps Jev and configured entries, and refreshes the endpoint list at most every five minutes. If listing fails, configured entries remain available. Options show concise model names; badges beneath the picker show the selected model’s type (**Decision model** or **Standard LLM**) and image/audio support. The full model ID is available on hover. **Jev** is the default decision model and produces operation and target distributions in one request. **Torchcast** is also a decision model and uses the OpenAI-compatible action endpoint. Discovered models default to Standard LLM without image/audio support unless `MODEL_CONFIG` supplies capabilities. **Choose next** previews one observed action, **Execute choice** runs it through the freshness and safety checks, and **Run automatically** executes each OpenAI-compatible model decision immediately, even with **Slow motion** checked.

`MODEL_CONFIG` maps model names to `model_type` (`decision` or `llm`), `image_compatible` and `audio_compatible` (booleans), optional `base_url`, and optional `api_key_env` (a server-side environment variable name). Capabilities default to false. Image-compatible models receive the current browser image alongside structured state in one decision request. Audio capability is displayed as model metadata; the browser loop does not capture or send audio. Jev uses structured state and the configured default text helper for `TYPE_TEXT`. There is no fallback or screenshot sharing mode; the inspector preview remains available.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TYPESAFE_MODEL` chooses the default Jev model. `TEXT_MODEL_API_KEY`, `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, and `TEXT_MODEL_REASONING` configure the OpenAI-compatible LLM. In Jev mode that LLM is called only for `TYPE_TEXT`; in LLM decision mode it also selects actions. The model dropdown lists Jev, the default text model, comma-separated `TEXT_MODEL_OPTIONS`, and `MODEL_CONFIG` entries, so several compatible models can be selected per run without exposing credentials to the browser. The example configuration also offers LiteLLM’s `torchcast-decision-12b-GGUF:Q5_K_M` as image-compatible, using `JEV_TORCHCAST_API_KEY`.

`LLM_DECISION_MAX_TOKENS` and `TEXT_VALUE_MAX_TOKENS` default to 1024. Per-model `decision_max_tokens` and `text_max_tokens` override them (64–8192). These limits include reasoning tokens. Truncated decisions stop without executing.

For llama.cpp behind LiteLLM, set `backend: "llama_cpp"` and `reasoning_effort: "none"` in that model’s `MODEL_CONFIG` entry. This sends llama.cpp’s explicit `reasoning_effort` and `chat_template_kwargs.enable_thinking` controls rather than the generic reasoning setting. Torchcast is configured this way with a 256-token decision budget and a 512-token text budget. Images remain enabled. Static decision rules precede changing page state to improve prompt cache reuse; repeated candidate lists are omitted. Decision records retain provider timings when returned. llama.cpp requests also constrain JSON generation to the offered operation and its matching target indices, including temporary no-progress exclusions.

`LLM_DECISION_FORMAT` defaults to `auto`. It uses a forced `choose_action` function for GLM/Z.ai models, whose OpenAI-compatible providers may not preserve JSON response-format constraints, and JSON-object mode for other models. Set it explicitly to `tool` or `json` when a provider needs an override. Operation casing is normalized before validation; targets must still match the selected operation’s unblocked observed indices. Function arguments pass through the same validation before execution.

`MODEL_TIMEOUT_SECONDS` defaults to 60 seconds per attempt. Transient connection errors, read timeouts, rate limits, and retryable 5xx responses are retried up to three times with short exponential backoff. Model requests are read-only; browser mutations are still never retried.

Enable **Log indexed pages** before starting a demo to write one JSONL file per run. A record is appended only after a guarded browser action executes successfully; previewed decisions, failed actions, `DONE`, and `BLOCKED` do not write records. Each execution record contains the page state, unblocked observed actions and element table, the executed element, whether Jev or the selected LLM chose it, and the decision request(s) that led to it. Blocked element and action records, screenshots, and credentials are not logged. `QUESTION_LOG_DIR` selects the server-side destination and defaults to `artifacts/questions`.

After attaching to a tab, **Block from model** accepts current-page element numbers and inclusive ranges such as `1-8, 12`. Blocked elements remain visible in the inspector and appear as muted red dashed boxes on the webpage preview, but are omitted from the Jev/LLM state and target questions. Unblocked elements retain their displayed indices. The same expression is reapplied to each newly observed page, so adjust it when the indexed layout changes.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector captures screenshots for its preview; a Selected LLM receives one when its configuration sets `image_compatible` to true. The video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, including open shadow roots and visible same-origin iframes up to four levels deep, but not the full accessible-name specification. Closed shadow roots, cross-origin frames, canvas, uploads, pop-up tabs, and arbitrary keyboard widgets remain outside this MVP. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
