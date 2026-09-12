# Layer C — GenAI spans for the OpenAI-compatible proxy

Companion to `docs/OpenAICompatibleCalls.md` §10.5. This turns the sketch
there into an implementation plan, and adds **user identity from the
auth token** to every LLM span.

## Status: implemented (aligned with the OTel GenAI semantic conventions)

Attribute names follow https://github.com/open-telemetry/semantic-conventions-genai
(inference spans, `execute_tool` spans, structured messages, MCP attributes).
The only non-spec keys are the ones OpenLIT's UI reads (`gen_ai.request.user`,
`gen_ai.application_name`, `gen_ai.environment`, `gen_ai.usage.total_tokens`,
`gen_ai.usage.cost`, `gen_ai.request.is_stream`), `gen_ai.server.time_to_first_token`,
`client.auth.*`, and fork correlation keys under `openwebui.*`.

| Piece | Where |
|-------|-------|
| Span helper: inference spans, `execute_tool` spans, structured messages, provider detection, pricing / cost | `backend/open_webui/utils/telemetry/genai.py` |
| Auth mechanism on the request (`request.state.auth = {'type': 'jwt' \| 'api_key', 'jti', 'iat', 'exp'}`) | `backend/open_webui/utils/auth.py` |
| Inference wiring: chat (Chat Completions and Responses-API connections), embeddings, `/responses` | `backend/open_webui/routers/openai.py` |
| Tool wiring: native tool loop, prompt-based tool handler, output tool executor | `backend/open_webui/utils/middleware.py` (three `start_tool_span` / `end_tool_span` sites) |
| MCP server URL kept on the client for `server.address` | `backend/open_webui/utils/mcp/client.py` |
| Env flags | `backend/open_webui/env.py` (`ENABLE_OTEL_GENAI`, `OTEL_GENAI_ENVIRONMENT`, `OTEL_GENAI_CAPTURE_USER_PII`, `OTEL_GENAI_CAPTURE_CONTENT`, `OTEL_GENAI_PRICING_JSON`, `OTEL_GENAI_PRICING_CUSTOM`) |
| Pricing table load at startup | `backend/open_webui/main.py` lifespan |
| Tests (43, in-memory span exporter) | `backend/open_webui/test/utils/test_genai_telemetry.py` |

Run the tests from the repo root with the app's Python environment:

```bash
WEBUI_SECRET_KEY=test PYTHONPATH=backend pytest backend/open_webui/test/utils/test_genai_telemetry.py
```

Enable with the Layer A env plus nothing else (`ENABLE_OTEL_GENAI`
defaults to true). Ollama connections are not covered (see §1.4).

### Inference span (`chat {model}` / `embeddings {model}`, kind CLIENT)

| Attribute | Value |
|-----------|-------|
| `gen_ai.operation.name` | `chat` / `embeddings` |
| `gen_ai.provider.name` | `openai`, `azure.ai.openai`, `anthropic`, or the connection's `provider` (the deprecated `gen_ai.system` is not emitted) |
| `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id` | upstream model id / response fields |
| `gen_ai.request.temperature`, `.top_p`, `.max_tokens`, `.seed`, `.frequency_penalty`, `.presence_penalty` | from the payload when present |
| `gen_ai.output.type` | `json` when a JSON response format is requested, else `text` |
| `gen_ai.response.finish_reasons` | e.g. `stop`, `tool_calls`, `length`; Responses API `status` |
| `gen_ai.usage.input_tokens`, `.output_tokens`, `.cache_read.input_tokens`, `.reasoning.output_tokens` | usage block, both API shapes |
| `gen_ai.conversation.id` | the chat id |
| `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions` | opt-in, see capture modes below |
| `server.address`, `server.port`, `http.response.status_code`, `error.type` | connection / outcome |
| `user.id`, `user.roles`, `enduser.id`; `user.email`, `user.full_name` behind `OTEL_GENAI_CAPTURE_USER_PII` | user resolved from the token |
| `client.auth.type`, `client.auth.jti`, `openwebui.user.oauth_provider`, `openwebui.user.oauth_sub` | auth mechanism / OAuth subject |
| `openwebui.chat_id`, `openwebui.message_id`, `openwebui.session_id`, `openwebui.task`, `openwebui.model_id`, `openwebui.api_type` | fork correlation |

### Tool span (`execute_tool {tool}`, kind INTERNAL)

One per tool execution, parented under whatever is current in the tool
loop, so a turn reads: `chat` (finish `tool_calls`) → `execute_tool …` →
`chat` (…). HTTP spans made by tool servers and MCP calls nest under it.

| Attribute | Value |
|-----------|-------|
| `gen_ai.operation.name` | `execute_tool` |
| `gen_ai.tool.name` | the function name the model called (MCP tools keep their `{server}_{tool}` name) |
| `gen_ai.tool.type` | `function` for local, builtin and MCP tools; `extension` for tool servers and browser-executed direct tools |
| `gen_ai.tool.call.id`, `gen_ai.tool.description` | from the model's tool call / the tool spec |
| `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` | `full` capture mode only |
| `mcp.method.name` = `tools/call`, `server.address`, `server.port` | MCP tools, folded into the same span as the spec asks (no second span) |
| `error.type` | exception class, or `tool_error` when the loop turned a failure into an `{'error': …}` / `Error: …` result (including the fork's Power BI dataset-id rejection) |
| conversation, user and `openwebui.*` keys | as on the inference span |

### Capture modes (`OTEL_GENAI_CAPTURE_CONTENT`)

| Mode | Inference span | Tool span |
|------|----------------|-----------|
| `off` | no messages, instructions or definitions | no arguments / result |
| `tools` (default) | `gen_ai.input.messages` / `gen_ai.output.messages` containing only `tool_call` and `tool_call_response` parts (ids and names, no arguments, responses or text); `gen_ai.tool.definitions` with names only | no arguments / result |
| `full` | everything: text parts, arguments, tool responses, system instructions, full tool definitions (oldest messages dropped past 32 KB) | arguments and result (16 KB cap) |

`tools` mode answers "which tools did the model call, in what order, and
did they succeed" without exporting prompts. `full` exports user data
and, in this fork, Power BI dataset ids and query results; use it only
where the trace backend is trusted. Legacy `true` / `false` values map
to `full` / `off`.

## 0. Findings that shape the plan

* **What OpenLIT's UI actually reads** (checked against
  `src/client/src/constants/traces.ts` and `helpers/server/platform.ts`
  in the openlit repo, main branch, 2026-09):

  | UI field | Span attribute |
  |----------|----------------|
  | type (filters `!= 'vectordb'`) | `gen_ai.operation.name` |
  | model | `gen_ai.request.model` |
  | provider | `gen_ai.provider.name`, fallback `gen_ai.system` |
  | tokens | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens` |
  | cost | `gen_ai.usage.cost` (never computed server-side; must be on the span) |
  | finish reason | `gen_ai.response.finish_reasons` |
  | user | `gen_ai.request.user` |
  | stream | `gen_ai.request.is_stream` |
  | application / environment | `gen_ai.application_name`, `gen_ai.environment` (span attrs), or `deployment.environment` (resource attr) |
  | status | `StatusCode IN (Ok, Unset)` for the request tables; errors go to the exceptions view |

  Nothing requires `telemetry.sdk.name=openlit`, so hand-made spans show
  up in the same dashboards as SDK spans. `gen_ai.usage.total_tokens`
  and `gen_ai.request.user` are not in the doc's table and must be added.

* **What the token contains.** The session JWT minted by
  `utils/auth.py:create_token` carries only `id`, `jti`, `iat`, `exp`.
  Name, email, role and OAuth subject are looked up from the `user` row
  (`Users.get_user_by_id`) in `get_current_user`. API-key auth (`sk-…`)
  resolves the same `UserModel`. So "user info from the token" means:
  the resolved `UserModel` (id, name, email, role, `oauth[provider].sub`)
  plus the auth mechanism (`jwt` | `api_key`) and the JWT `jti`.

* **What already exists.** `get_current_user` already stamps
  `client.user.id / .email / .role` and `client.auth.type` on the
  *current* span, which is the FastAPI request span. For UI chat, the
  LLM call runs in a background `asyncio` task after
  `POST /api/chat/completions` has returned, so those attributes never
  reach the LLM span. OpenLIT's request table is per span, not per
  trace, which is why the LLM span must carry user attributes itself.

* **Context propagation works.** `tasks.py:create_task` uses
  `asyncio.create_task`, which copies contextvars, so the GenAI span
  becomes a child of the (already ended) HTTP span and lands in the same
  trace. Sampling decisions are inherited.

* **Stream modes.** `generate_chat_completion` uses `stream_wrapper(r)`
  in line mode, so SSE lines can be inspected cheaply. The `/responses`
  and `embeddings` routes use `passthrough=True` (raw chunks); usage
  extraction there needs line mode when a span is recording.

* **No local test tree or Python env.** The repo has no backend tests
  and none of the local interpreters have `opentelemetry` installed.
  Verification happens in the app's runtime environment (uv/Docker).

## 1. Design

### 1.1 New module `backend/open_webui/utils/telemetry/genai.py`

Keeps `routers/openai.py` small and lets embeddings, responses and
(optionally) `routers/ollama.py` reuse the same code.

```python
from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

_tracer = trace.get_tracer('open_webui.genai')

def start_llm_span(*, operation, requested_model, request_url, api_config,
                   payload, user, metadata, request, openwebui_model_id) -> Span | None
def record_response(span, data: dict, *, api_type: str) -> None   # usage / model / finish reasons
def traced_stream(span, stream, *, api_type: str)                  # async generator, ends span
def end_span(span, *, status: int | None = None, error: BaseException | None = None)
```

`start_llm_span` returns `None` when `ENABLE_OTEL_GENAI` is false or
the span is not recording, and every other helper is a no-op on `None`,
so the hot path costs one boolean check when telemetry is off.

Attributes set at start:

| Attribute | Value |
|-----------|-------|
| span name | `f'{operation} {requested_model}'` |
| `gen_ai.operation.name` | `chat` / `embeddings` |
| `gen_ai.provider.name` and `gen_ai.system` | `azure_openai` if `api_config.azure` or `provider == 'azure'`; `anthropic` if `is_anthropic_url(url)`; else `api_config.get('provider') or 'openai'` |
| `gen_ai.request.model` | `requested_model` (after `strip_provider_model_prefix`) |
| `gen_ai.request.is_stream` | `bool(payload.get('stream'))` |
| `gen_ai.request.temperature`, `.max_tokens` (or `max_completion_tokens`), `.top_p`, `.seed`, `.frequency_penalty`, `.presence_penalty` | from `payload` when present |
| `gen_ai.application_name` | `OTEL_SERVICE_NAME` |
| `gen_ai.environment` | `OTEL_GENAI_ENVIRONMENT` |
| `server.address`, `server.port` | `urlparse(request_url)` |
| `openwebui.model_id` | the Open WebUI model id before base-model resolution (custom models wrap a base model) |
| `openwebui.chat_id`, `openwebui.message_id`, `openwebui.session_id`, `openwebui.task` | from `metadata` when present |
| `openwebui.api_type` | `chat_completions` / `responses` |

User attributes (the new part, §1.2). Attributes set at end:

| Attribute | Source |
|-----------|--------|
| `gen_ai.response.model` | `response.model` |
| `gen_ai.response.id` | `response.id` |
| `gen_ai.response.finish_reasons` | `[c.finish_reason for c in choices]` (chat) or `response.status` (responses API) |
| `gen_ai.usage.input_tokens` / `.output_tokens` / `.total_tokens` | chat: `usage.prompt_tokens / completion_tokens / total_tokens`; responses: `usage.input_tokens / output_tokens / total_tokens` |
| `gen_ai.usage.cache_read_input_tokens` | `usage.prompt_tokens_details.cached_tokens` when present |
| `gen_ai.server.time_to_first_token` | streaming only: seconds from span start to first `data:` line |
| `http.response.status_code`, `error.type` | on upstream 4xx/5xx or exception; span status `ERROR` |

Span kind `CLIENT`. The upstream aiohttp span (from the existing
instrumentor) nests under it because `session.request` runs inside
`trace.use_span(span, end_on_exit=False)`.

### 1.2 User identity on the span

Two small changes:

1. `utils/auth.py:get_current_user` and `get_current_user_by_api_key`
   record how the caller authenticated, next to the existing
   `request.state.user = user`:

   ```python
   request.state.auth = {'type': 'jwt', 'jti': data.get('jti'), 'iat': data.get('iat'), 'exp': data.get('exp')}
   # or {'type': 'api_key'} — never the key or token itself
   ```

   Internal callers (`utils/chat.py`, tool loop, task generation) pass
   the original `Request`, so `request.state.auth` is still available
   when the LLM span starts.

2. `start_llm_span` reads `user` and `request.state.auth`:

   | Attribute | Value | Notes |
   |-----------|-------|-------|
   | `gen_ai.request.user` | `user.id` | what OpenLIT displays as the user |
   | `enduser.id`, `enduser.role` | `user.id`, `user.role` | stable OTel semconv |
   | `client.user.id`, `client.user.role`, `client.auth.type` | same as `auth.py` already emits on HTTP spans | lets one filter work on both span kinds |
   | `client.user.email`, `client.user.name` | gated by `OTEL_GENAI_CAPTURE_USER_PII` (default `true`, matching current `auth.py` behaviour) | |
   | `client.auth.jti` | JWT `jti` when auth type is `jwt` | correlates all calls of one login session; not secret |
   | `openwebui.user.oauth_provider`, `openwebui.user.oauth_sub` | first key of `user.oauth` and its `sub` | present only for OAuth/OIDC users |
   | `openwebui.user.groups` | **not** included by default | costs a DB query per call; add behind `OTEL_GENAI_CAPTURE_USER_GROUPS` if needed |

   `user_id` in `metadata` is a fallback when `user` is `None`
   (it never is on the proxy routes, but the helper is defensive).

   These are span attributes only. They must not become metric labels;
   `utils/telemetry/metrics.py` keeps its method/route/status views.

### 1.3 Wiring into `routers/openai.py:generate_chat_completion`

Insert after `is_streaming_request` is computed and before
`payload = JSONCodec.dumps(payload)` (the helper needs the dict):

```python
span = start_llm_span(
    operation='chat',
    requested_model=requested_model,
    request_url=request_url,
    api_config=api_config,
    payload=payload,
    user=user,
    metadata=metadata,
    request=request,
    openwebui_model_id=form_data.get('model'),
)
payload = JSONCodec.dumps(payload)
```

Then inside the existing `try`:

* wrap `session.request(...)` in `with trace.use_span(span, end_on_exit=False)`
  when `span` is not `None`;
* SSE error branch (`r.status >= 400`): `end_span(span, status=r.status)`
  before each `return JSONResponse(...)`;
* streaming success: `StreamingResponse(traced_stream(span, stream_wrapper(r), api_type=...), ...)`.
  The generator ends the span in `finally`, so it also ends on client
  disconnect (`GeneratorExit`) and on upstream errors;
* non-streaming: `record_response(span, response, api_type=...)` when
  `response` is a dict, then `end_span(span, status=r.status)`;
* `except Exception as e`: `end_span(span, error=e)` before re-raising.

A `handed_off` flag (set when `traced_stream` takes ownership) plus a
`finally: if span and not handed_off: end_span(span)` guard guarantees
no span leaks if a new early return is added later.

`traced_stream` for Chat Completions parses only lines that start with
`data: {` and contain `"usage"` or `"finish_reason"`; for Responses API
mode it parses lines containing `"response.completed"`. Everything is
yielded unchanged. A `JSONDecodeError` is swallowed; the stream is never
altered by telemetry.

### 1.4 Other proxy routes

* `embeddings()` (`routers/openai.py:1721`): `operation='embeddings'`,
  usage from `usage.prompt_tokens` → `input_tokens`, no output tokens.
  Non-streaming only in practice.
* `responses()` (`:1847`): same wiring as chat with `api_type='responses'`.
  Use `stream_wrapper(r)` (line mode) instead of `passthrough=True` when
  `span` is not `None`, so `response.completed` can be read.
* `routers/ollama.py` chat/generate: optional follow-up with
  `provider='ollama'`; usage comes from `prompt_eval_count` /
  `eval_count` in the final chunk. Not needed for this fork's Power BI
  path, so out of scope unless asked.

### 1.5 Configuration (`env.py`, next to the `OTEL_*` block)

| Env | Default | Purpose |
|-----|---------|---------|
| `ENABLE_OTEL_GENAI` | `True` | Master switch for the manual LLM spans (still requires `ENABLE_OTEL` + `ENABLE_OTEL_TRACES`) |
| `OTEL_GENAI_ENVIRONMENT` | `default` | Value of `gen_ai.environment`; matches OpenLIT's environment filter |
| `OTEL_GENAI_CAPTURE_USER_PII` | `True` | Include `user.email` / `user.full_name` |
| `OTEL_GENAI_CAPTURE_CONTENT` | `tools` | `off` / `tools` / `full`; see capture modes in the status section |
| `OTEL_GENAI_PRICING_JSON` | `''` | Pricing endpoint URL or file (Go SDK `PricingEndpoint`); enables `gen_ai.usage.cost` |
| `OTEL_GENAI_PRICING_CUSTOM` | `''` | Inline JSON or file of per-token overrides (Go SDK `PricingInfo`); wins over the table |

Named `OTEL_GENAI_*` rather than `OPENLIT_*` because Layer C does not
depend on the `openlit` package; the spans work with any OTLP backend.

### 1.6 Cost (`gen_ai.usage.cost`)

OpenLIT does not compute cost server-side; its SDK computes it from
`assets/pricing.json` in the openlit repo. Plan: at startup, when
`OTEL_GENAI_PRICING_JSON` is set, load the JSON once (URL via aiohttp
or local path), keep a `{model: {promptPrice, completionPrice}}` map
keyed on the exact `gen_ai.request.model`, and in `record_response`
set `gen_ai.usage.cost = in/1000*promptPrice + out/1000*completionPrice`.
Unknown models get no cost attribute. Refresh is not needed; a restart
picks up a new file.

## 2. Work breakdown

| # | Step | Files | Size |
|---|------|-------|------|
| 1 | Env flags | `env.py` | 10 lines |
| 2 | `request.state.auth` in both auth paths | `utils/auth.py` | 10 lines |
| 3 | `genai.py` helper: start/record/stream/end, provider detection, user attrs | `utils/telemetry/genai.py` (new) | ~180 lines |
| 4 | Wire into `generate_chat_completion` (chat + responses-mode streams) | `routers/openai.py` | ~35 lines changed |
| 5 | Wire `embeddings()` and `responses()` | `routers/openai.py` | ~30 lines |
| 6 | Tests with `InMemorySpanExporter`: non-stream usage, stream usage from last chunk, responses `response.completed`, error status, user attrs and PII gate, span ends on generator close | `backend/open_webui/test/utils/test_genai_telemetry.py` (new; `pytest-asyncio` is already a dev dep) | ~150 lines |
| 7 | Cost table loader + `gen_ai.usage.cost` | `genai.py`, `main.py` lifespan | ~50 lines |
| 8 | Content capture flag | `genai.py` | ~25 lines |
| 9 | Docs: replace §10.5 sketch in `OpenAICompatibleCalls.md` with a pointer here; add the env table | docs | |

Steps 1 to 4 are the minimum that lights up OpenLIT's LLM dashboards
with user attribution. Steps 5 and 6 complete the proxy. Steps 7 and 8
are optional.

## 3. Verification

1. Layer A env from `OpenAICompatibleCalls.md` §10.3, OpenLIT running
   (remap its UI off port 3000, which this fork's Vite dev server uses).
2. Send one chat message. OpenLIT → Requests should show one
   `chat <model>` row with provider, tokens, `is_stream=true`, and user
   = the user's id. The trace view should show it under
   `POST /api/chat/completions` with the aiohttp span nested inside.
3. Trigger a tool call: two or more `chat <model>` spans in one trace,
   all with the same `openwebui.chat_id` and `client.auth.jti`.
4. Title generation: a span with `openwebui.task=title_generation`.
5. Call `/api/chat/completions` with an API key (`sk-…`): span has
   `client.auth.type=api_key` and no `client.auth.jti`.
6. Point a connection at a wrong key: span status `ERROR`,
   `http.response.status_code=401`, row appears in OpenLIT's exceptions
   view, and the existing `MODEL_PROVIDER_REQUEST_FAILED` event still
   fires.
7. Set `ENABLE_OTEL_GENAI=false`: no `chat` spans, everything else
   unchanged.
8. Cancel a streaming reply mid-way from the UI: the span still ends
   (check for `gen_ai.usage.*` absent and duration equal to the time
   until cancel).

## 4. Risks and choices made

* **Usage on streams** exists only when `stream_options.include_usage`
  was sent; `main.py:1187` does that only for models whose capabilities
  advertise `usage`. Spans for other models will have no token counts.
  Not a Layer C problem, but worth turning on per model.
* **PII on spans.** Email and name are already on every HTTP span via
  `auth.py`. The new flag lets both be turned off in one place later;
  a follow-up can make `auth.py` honour the same flag.
* **Responses API passthrough.** Switching `/responses` streaming to line
  mode when tracing is on adds a per-line scan. Acceptable; the chat
  route already streams that way.
* **Span leak on new early returns.** Guarded by the `finally` in the
  route; the test for "span ends on generator close" covers the
  streaming side.
* **Anthropic** connections (`api.anthropic.com`) share this route; the
  provider attribute is derived from the URL, and usage keys
  (`input_tokens`/`output_tokens`) are already handled by the
  Responses-style branch. Verify with a real connection if one exists.
