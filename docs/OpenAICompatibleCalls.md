# How Open WebUI calls OpenAI-compatible endpoints

This document traces a chat request from the browser to an upstream
OpenAI-compatible provider (OpenAI, Azure OpenAI, OpenRouter, vLLM, LiteLLM,
llama.cpp, LM Studio, Anthropic via the compat shim, etc.) and back. Line
numbers refer to the current `main` of this fork (based on upstream tag
`0.11.3`); they drift, but the function names are stable.

The short version:

```
Browser  ──POST /api/chat/completions──▶  main.py:chat_completion
                                              │  process_chat_payload (middleware.py)
                                              ▼
                                        utils/chat.py:generate_chat_completion   ← dispatcher
                                              │  direct / arena / pipe / ollama / openai
                                              ▼
                                  routers/openai.py:generate_chat_completion     ← the proxy
                                              │  aiohttp POST {base_url}/chat/completions
                                              ▼
                                      upstream provider (SSE or JSON)
                                              │
                                              ▼
                                  middleware.py:process_chat_response
                                              │  parses SSE, runs tool loop, persists,
                                              │  emits socket.io events
                                              ▼
Browser  ◀──socket.io "chat:completion" / "chat:message:delta" events──
```

Two facts surprise most people reading this code for the first time:

1. **The main chat UI never calls `/openai/chat/completions` directly.** It
   calls Open WebUI's own `/api/chat/completions`. The `/openai/*` router is
   reached in-process by the dispatcher, and only exposed over HTTP for
   API-key clients, the admin connection UI, and the Playground.
2. **The browser does not read the SSE stream.** The POST returns immediately
   with task ids; tokens are pushed over socket.io. The server-side
   `StreamingResponse` from the proxy is consumed by `process_chat_response`,
   not by the browser.

---

## 1. Configuration: what "a connection" is

OpenAI-compatible providers are configured as three parallel lists plus a
per-index config dict. Seed values come from environment variables
(`backend/open_webui/config.py:312-365`) and are then persisted to the
per-key config table; the runtime always reads from the table via
`Config.get(...)`, not from the env.

| Config key (DB)          | Env seed                | Shape |
|--------------------------|-------------------------|-------|
| `openai.enable`          | `ENABLE_OPENAI_API`     | bool (default `True`) |
| `openai.api_base_urls`   | `OPENAI_API_BASE_URLS`  | `;`-separated list; empty entry → `https://api.openai.com/v1` |
| `openai.api_keys`        | `OPENAI_API_KEYS`       | `;`-separated list, padded/truncated to match URLs |
| `openai.api_configs`     | `OPENAI_API_CONFIGS`    | JSON object keyed by **stringified index** (`"0"`, `"1"`, …). Legacy configs keyed by URL are still honored. |

`routers/openai.py:320-345` (`get_openai_runtime_config`,
`get_openai_connection(idx)`) is the single place that resolves
`idx → (url, key, api_config)`. Every outbound call goes through it.

Per-connection `api_config` keys consumed by the router:

| Key | Effect |
|-----|--------|
| `enable` | `false` skips the connection when listing models. |
| `model_ids` | Manual model list; skips the upstream `GET /models` call entirely (`:700-720`). Required for Azure. |
| `prefix_id` | Model ids are exposed as `"{prefix_id}.{id}"` on listing (`:749-752`) and stripped again on every outbound request via `strip_provider_model_prefix` (`utils/model_ids.py`). This is how two connections can serve the same model id. |
| `connection_type` | `"external"` (default) or `"local"`; tagged onto each model for UI purposes. |
| `tags` | Arbitrary tags copied onto each model. |
| `provider` | `"azure"`, `"llama.cpp"`, `"lmstudio"`, … Drives URL shape and model-management endpoints. |
| `azure` | Legacy boolean equivalent of `provider == "azure"`. |
| `api_version` | Azure only; default `2023-03-15-preview`. |
| `api_type` | `"responses"` switches the chat call to the OpenAI **Responses API** (see §4.3). |
| `auth_type` | `bearer` (default), `none`, `session`, `system_oauth`, `azure_ad` / `microsoft_entra_id` (see §4.1). |
| `headers` | Dict of extra headers with `{{USER_ID}}`, `{{CHAT_ID}}`, `{{USER_GROUPS}}`, … placeholders (`utils/headers.py:105-154`). |

Admin UI reads/writes this through `GET /openai/config` and
`POST /openai/config/update` (`routers/openai.py:546-599`). An update
clears the model caches (`clear_openai_model_cache`, `:348`).

---

## 2. Model discovery

Model discovery is what maps a model id to a connection index (`urlIdx`),
which is what the chat proxy later uses to pick a URL and key.

### 2.1 Fetching from each connection

`routers/openai.py:678` `get_all_models_responses`:

* Fans out `GET {url}/models` to every enabled connection concurrently
  with `asyncio.gather`. Uses a dedicated short-timeout session
  (`AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST`, default 10 s) so a dead provider
  does not block startup.
* Connections with `model_ids` set produce a synthetic list instead of
  calling upstream.
* Anthropic base URLs are detected (`utils/anthropic.py:is_anthropic_url`)
  and listed through the Anthropic-specific helper.
* Post-processes each model: applies `prefix_id`, `tags`,
  `connection_type`, `provider`.

`routers/openai.py:798` `get_all_models` (decorated with `aiocache.cached`,
TTL `MODELS_CACHE_TTL`, keyed per user):

* Merges all lists into one dict keyed by model id. **First connection
  wins** on duplicate ids (`:835`), which is why `prefix_id` exists.
* Filters out non-chat models on `api.openai.com` (embeddings, tts,
  whisper, dall-e, …).
* Each entry gets `owned_by: "openai"`, `urlIdx: idx`, and the raw upstream
  record under `openai`.
* Stores the result in `request.app.state.OPENAI_MODELS` — this is the
  lookup table the chat proxy uses.

### 2.2 Merging with Ollama, functions, arena

`utils/models.py:58` `get_all_base_models` gathers OpenAI, Ollama, and
pipe-function models (`function_models + openai_models + ollama_models`).
`utils/models.py:69` `get_all_models` adds arena models, overlays the
`Models` DB table (custom models, params, access control, base_model_id),
and populates `request.app.state.MODELS`. The frontend's `/api/models`
(`main.py:887`) serves that table after access-control filtering.

### 2.3 HTTP endpoints

* `GET /api/models` — merged list for the UI.
* `GET /openai/models` — OpenAI-only list; `GET /openai/models/{idx}`
  (admin) hits one connection live (`routers/openai.py:866-940`).
* `POST /openai/verify` — admin "test connection" button; handles Azure
  and Anthropic URL shapes (`:1065`).

---

## 3. Request flow from the browser

### 3.1 Frontend

`src/lib/apis/openai/index.ts` has two clients. Despite the module name,
both default to Open WebUI's own API:

* `generateOpenAIChatCompletion(token, body, url = "/api")` → POST
  `${url}/chat/completions`, returns parsed JSON. **This is what the main
  chat uses** (`src/lib/components/chat/Chat.svelte:3552`, inside
  `sendMessageSocket`).
* `chatCompletion(token, body, url = "/api")` → same POST but returns the raw
  `Response` for SSE reading. Used by the Playground and by the
  direct-connection path (§6).

`WEBUI_BASE_URL` is `''` (`src/lib/constants.ts:10`), so the target is
`/api/chat/completions`. `OPENAI_API_BASE_URL = '/openai'` is used only for
admin config, model listing, and TTS.

The payload sent from `Chat.svelte:3552-3617` is a normal OpenAI chat body
(`model`, `messages`, `stream`, `params`) plus Open WebUI extras:
`files`, `tool_ids`, `tool_servers`, `skill_ids`, `features`, `variables`,
`model_item`, `session_id` (the socket.io sid), `chat_id`, `id` (assistant
message id), `message_ids`, `parent_id`, and `background_tasks`.

The POST response is `{status, task_ids, chat_id}`. Everything else arrives
through `chatEventHandler` (`Chat.svelte:1194`) on socket.io events
`chat:completion`, `chat:message:delta`, `status`, `chat:active`,
`chat:tasks:cancel`, `chat:message:error`.

### 3.2 `main.py:chat_completion` (`/api/chat/completions`)

`backend/open_webui/main.py:1100-1871`. Responsibilities, in order:

1. Ensure `app.state.MODELS` is loaded; resolve the model. If
   `model_item.direct` is set, mark the request as a direct connection
   (§6). Otherwise look the model up and run `check_model_access`.
2. Merge params: global defaults → model params → request params. Force
   `stream_options.include_usage` when the model advertises usage.
3. Build `metadata` (user, chat, message ids, session id, files, tools,
   features, variables, …) and stash it on `request.state.metadata` and
   `form_data['metadata']` (`:1625-1626`).
4. Create the chat / placeholder assistant messages in the DB if needed.
5. Define an inner `process_chat` coroutine:
   * `process_chat_payload` (§3.3) → mutated `form_data`, `metadata`, and
     pre-computed events.
   * `chat_completion_handler(request, form_data, user)` (`:1644`), which is
     `utils.chat.generate_chat_completion` aliased at import (`:224`).
   * `build_chat_response_context` + `process_chat_response` (§5).
   * Error handling emits `chat:message:error`; `finally` disconnects MCP
     clients and emits `chat:active=false`.
6. **Fan-out** (`:1788-1867`): when `session_id` and `chat_id` are present,
   spawn one `asyncio` task per entry in `message_ids` (multi-model chat)
   and return `{status, task_ids, chat_id}` immediately. Title/tags
   background tasks only run for the first model. Without a session
   (plain API-key clients) the legacy synchronous path runs and the HTTP
   response is the proxied stream.

### 3.3 `process_chat_payload` (`utils/middleware.py:2371`)

Everything that shapes `messages` before the provider sees them. Order:

1. Arena sub-model selection.
2. Apply model params; snapshot the system prompt.
3. Load the canonical message history from the DB and re-expand structured
   Responses-API `output` items.
4. Folder/project knowledge and model knowledge → RAG retrieval.
5. Pipeline inlet filter, then function filters (`inlet`).
6. Feature handlers gated on permissions: web search, image generation,
   code interpreter prompt, skill expansion.
7. **Fork-specific:** Power BI dataset manifest injected into the system
   message (`:2847-2889`; see `docs/PowerBI.md`).
8. Tool resolution, including MCP servers. In native function-calling mode
   the tool specs land in `form_data['tools']`; in legacy "prompt" mode a
   separate tool-selection call is made first.
9. File/RAG context appended.
10. `normalize_messages_for_model` (`:190`) makes the message list valid
    for the target provider.

### 3.4 The dispatcher: `utils/chat.py:generate_chat_completion` (`:151`)

Decides which backend handles the model, in this order:

| Condition | Handler |
|-----------|---------|
| `request.state.direct` and the model id matches | `generate_direct_chat_completion` (§6) |
| `model.owned_by == "arena"` | pick a sub-model, recurse, prepend a `{"selected_model_id": …}` SSE frame |
| `model.pipe` | `generate_function_chat_completion` (Python pipe functions) |
| `model.owned_by == "ollama"` | convert payload to Ollama format, call the Ollama router, convert the response back to OpenAI format |
| **anything else** | `routers.openai.generate_chat_completion` (§4) |

It also propagates `bypass_filter` / `bypass_system_prompt` on
`request.state` so the router can read them without exposing query params.

---

## 4. The proxy: `routers/openai.py:generate_chat_completion` (`:1465`)

Mounted at `POST /openai/chat/completions` (`main.py:833`), but called
in-process by the dispatcher for UI traffic.

### 4.0 Payload preparation

1. `metadata` is popped off the body so it is never forwarded upstream.
2. If the model is a custom model with `base_model_id`, the outbound
   `model` field is rewritten to the base id and the custom model's
   `params` are applied (`apply_model_params_to_body_openai`,
   `utils/payload.py:164`) — only keys not already present in the body are
   set, cast to the right type (`temperature` → float, `max_tokens` → int,
   `stop` → list, `response_format` → dict, …). The model's `system` param
   is merged into `messages` unless `bypass_system_prompt` is set.
3. Access control: `check_model_access`.
4. Look up `urlIdx` in `app.state.OPENAI_MODELS` (refreshing via
   `get_all_models` on a miss), then `get_openai_connection(idx)`.
5. Strip `prefix_id` from the model id.
6. For pipeline models, inject a `user` object (name, id, email, role).
7. Reasoning-model handling: for `o*` and `gpt-5+` model names
   (`is_openai_new_model`, `:1193`), `max_tokens` → `max_completion_tokens`
   and the system message role → `developer` (`o1-mini`/`o1-preview` →
   `user`). For non-OpenAI hosts the reverse conversion is applied for
   backward compatibility.
8. `logit_bias` is normalised from the UI's string form to JSON.
9. Tool messages with multimodal content are flattened to text for Chat
   Completions (images in tool results are not supported there).
10. `stream_options` is removed for non-streaming requests.

### 4.1 Headers and auth: `get_headers_and_cookies` (`:154`)

Always sets `Content-Type: application/json`. Then:

| `auth_type` | Authorization |
|-------------|---------------|
| `bearer` / unset | `Bearer {key}` |
| `none` | no header |
| `session` | forwards the caller's own Open WebUI JWT and cookies (for providers that sit behind the same SSO) |
| `system_oauth` | forwards the user's OAuth access token from the OAuth session store |
| `azure_ad` / `microsoft_entra_id` | token from `DefaultAzureCredential` for scope `https://cognitiveservices.azure.com/.default` |

Extras:

* OpenRouter hosts get `HTTP-Referer` and `X-Title` attribution headers.
* With `ENABLE_FORWARD_USER_INFO_HEADERS=true`, user identity is forwarded
  as `X-OpenWebUI-User-{Name,Id,Email,Role}` headers, or as a single
  signed HS256 JWT if `FORWARD_USER_INFO_HEADER_JWT_SECRET` is set
  (`utils/headers.py:50`). The chat id goes in
  `FORWARD_SESSION_INFO_HEADER_CHAT_ID` (default `X-OpenWebUI-Chat-Id`).
* `api_config.headers` templates are expanded with user/chat/file
  placeholders and merged last, so they can override anything above.
* Azure additionally sets `api-key: {key}` unless Entra ID auth is used.

### 4.2 URL construction

| Connection | Chat Completions URL | Responses API URL |
|------------|----------------------|-------------------|
| Plain OpenAI-compatible | `{url}/chat/completions` | `{url}/responses` |
| Azure, v1 style (base ends in `/openai/v1`) | `{url}/chat/completions` | `{url}/responses` |
| Azure, deployment style | `{url}/openai/deployments/{model}/chat/completions?api-version=…` | `…/responses?api-version=…` |

Deployment-style Azure also filters the payload to an allow-list of
parameters (`get_azure_allowed_params`, `:1153`) and drops `temperature`
for o-series models. The model name is sanitised and percent-encoded before
being placed in the path (`_sanitize_model_for_url`, `:1205`).

### 4.3 Responses API mode (`api_type: "responses"`)

`convert_to_responses_payload` (`:1275`) rewrites a Chat Completions body:

* `messages` → `input` items (`message`, `function_call`,
  `function_call_output`); the system message → `instructions`.
* Content parts become `input_text` / `output_text` / `input_image` /
  `input_file`.
* `max_tokens` / `max_completion_tokens` → `max_output_tokens`.
* Chat-only params (`stream_options`, `logit_bias`, penalties, `stop`)
  are dropped.
* Tool specs are flattened from `{type, function:{…}}` to
  `{type, name, description, parameters}`.
* `previous_response_id` is forwarded when the middleware set it
  (stateful mode).

Non-streaming Responses results are converted back to a Chat Completions
shape by `convert_responses_result` (`:1430`) so downstream consumers do not
care. Streaming Responses events are handled in the middleware
(`handle_responses_streaming_event`, `middleware.py:599`).

### 4.4 Sending the request

```python
session = await get_session()          # shared aiohttp pool, utils/session_pool.py
r = await session.request(
    method='POST', url=request_url, data=JSONCodec.dumps(payload),
    headers=headers, cookies=cookies,
    ssl=AIOHTTP_CLIENT_SESSION_SSL,
    timeout=get_client_timeout(stream=is_streaming_request),
)
```

* **One shared `aiohttp.ClientSession`** for the whole process
  (`utils/session_pool.py`). Pool size is governed by
  `AIOHTTP_POOL_CONNECTIONS` (100), `AIOHTTP_POOL_CONNECTIONS_PER_HOST`
  (30), `AIOHTTP_POOL_DNS_TTL` (300 s). `trust_env=True`, so `HTTP_PROXY`
  / `HTTPS_PROXY` / `NO_PROXY` are honored.
* Timeouts: `AIOHTTP_CLIENT_TIMEOUT` (default 300 s total). Streaming
  requests additionally get `sock_read=AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT`
  so a stalled stream is cut without capping total duration.
* TLS verification via `AIOHTTP_CLIENT_SESSION_SSL` (optionally a custom
  CA bundle through `AIOHTTP_CLIENT_SSL_CERT_FILE`).
* No DB session is held during the upstream call (see the comment at
  `:1473`); DB lookups use short-lived sessions so the pool is not
  exhausted by slow LLM calls.

### 4.5 Handling the response

* **SSE** (`Content-Type: text/event-stream`): if the status is ≥ 400 the
  body is read, logged, a `MODEL_PROVIDER_REQUEST_FAILED` event is
  published, and a JSON error is returned instead of a broken stream.
  Otherwise a `StreamingResponse` wraps `stream_wrapper(r)`, which yields
  the upstream bytes line by line (`stream_chunks_handler`, `utils/misc.py:1266`,
  bounded by `CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE`) and closes the
  upstream response in a `finally`, even if the client disconnects.
  `Content-Encoding` / `Content-Length` / `Transfer-Encoding` headers are
  stripped because aiohttp already decompressed the body.
* **JSON**: parsed and returned. Status ≥ 400 is passed through with the
  provider's error body (and the same failure event).
* Any transport exception becomes `HTTP 500 "Open WebUI: Server Connection Error"`.
* `finally` releases the response back to the pool unless streaming.

---

## 5. Consuming the stream: `process_chat_response` (`middleware.py:6478`)

For UI traffic the `StreamingResponse` from §4 never reaches the browser.
`streaming_chat_response_handler` (`:4267`) iterates it server-side and:

* Parses `data: {...}` lines (Chat Completions deltas and Responses API
  events), batches deltas, and emits `chat:completion` /
  `chat:message:delta` over socket.io (`get_event_emitter_and_caller`,
  `:3170`), persisting content to the DB along the way.
* Detects `tool_calls` in the deltas, executes the tools (Python tools,
  OpenAPI tool servers, MCP servers), appends `assistant` + `tool`
  messages, and **calls `generate_chat_completion` again** — the native
  tool loop. Tool approval pauses live here too.
* Merges reasoning tags / `reasoning_details`, code-interpreter output,
  citations and sources.
* On completion runs `background_tasks_handler` (`:3704`): follow-ups,
  title, tags — each of which is itself a call through the same dispatcher
  (§7).

`non_streaming_chat_response_handler` (`:4083`) does the equivalent for a
JSON response.

---

## 6. Direct connections (browser → provider)

Users can configure their own providers in Settings → Connections. Those
models are listed client-side (`getOpenAIModelsDirect`,
`src/lib/apis/openai/index.ts:82`) and flagged `direct: true`.

When such a model is chosen, the backend still runs the full middleware
(RAG, tools, filters) but the actual HTTP call to the provider happens
**in the browser**:

1. `utils/chat.py:generate_direct_chat_completion` (`:49`) emits a
   `request:chat:completion` socket event with the prepared `form_data`.
2. `src/routes/+layout.svelte:630-690` receives it, calls
   `chatCompletion(key, form_data, providerUrl)` against the external URL,
   and re-emits each SSE line on a per-request channel.
3. The backend collects those lines from an `asyncio.Queue` and yields
   them as an SSE `StreamingResponse`, so `process_chat_response` sees the
   same shape as a proxied stream.

This requires an active WebSocket session; background tasks cannot use
direct models.

---

## 7. Other callers of the same proxy

* **Task endpoints** (`routers/tasks.py`): title, tags, follow-ups, query
  generation, autocomplete, emoji, image prompt, MoA. Each builds a
  non-streaming payload with `metadata.task` set, runs the pipeline inlet
  filter, applies task-model params, and calls
  `utils.chat.generate_chat_completion`. The task model is chosen by
  `get_task_model_id` from `task.model.default` / `task.model.external`.
* **Embeddings** (`routers/openai.py:1721`): same connection resolution
  and header logic, URL `{url}/embeddings` (or the Azure deployment
  form). Used by the retrieval subsystem when the embedding engine is
  `openai`.
* **TTS** (`POST /openai/audio/speech`, `:602`): only works against the
  connection whose base URL is exactly `https://api.openai.com/v1`; caches
  MP3s under `CACHE_DIR/audio/speech`.
* **Responses passthrough** (`POST /openai/responses`, `:1847`): forwards a
  native Responses API body, routed by model id.
* **Anthropic token counting** (`count_anthropic_tokens`, `:496`): posts to
  `{url}/messages/count_tokens` with `x-api-key` instead of bearer auth.
* **Model management** (`/openai/models/{idx}/{catalog,download,load,unload,sse}`,
  `:948-1055`): admin-only, for `llama.cpp` and `lmstudio` providers.
* **Generic passthrough** (`/openai/{path:path}`, `:1958`): disabled unless
  `ENABLE_OPENAI_API_PASSTHROUGH=true`.

External API-key clients can also POST to `/api/chat/completions` or
`/openai/chat/completions` directly; without a `session_id` the response is
the proxied SSE stream itself.

---

## 8. Observability and failure signalling

* Every upstream 4xx/5xx publishes `MODEL_PROVIDER_REQUEST_FAILED`
  (`open_webui.events.publish_model_provider_request_failed`) with the
  base URL, status, requested model, and upstream error body.
* Config updates publish `MODEL_PROVIDER_CONFIG_UPDATED`.
* Model list failures are logged and treated as an empty list, never as a
  hard error, so one dead connection does not take down the model picker.

---

## 9. Fork-specific notes

This fork (`k-data-analyst/open-webui`) has **no changes** to
`routers/openai.py`, `utils/chat.py`, or the model-merging code. All fork
changes affecting the chat path are Power BI related and live in
`utils/middleware.py`:

* `:2000` — `powerbi_dataset` attachments are skipped by the RAG file
  handler.
* `:2847-2889` — the attached dataset manifest is appended to the system
  message before tool resolution.
* `:5735-5772` — MCP tool calls to the configured Power BI server get
  `dataset_id` / `workspace_id` bound server-side and are rejected if they
  reference an unattached dataset.

None of this changes what is sent to the LLM provider beyond the extra
system-prompt text and the tool results. See `docs/PowerBI.md`.

---

## 10. Instrumenting with OpenLIT

[OpenLIT](https://github.com/openlit/openlit) is an OpenTelemetry-native
observability stack for LLM applications. It has two halves:

* the **`openlit` Python SDK**, which auto-instruments LLM client libraries
  (`openai`, `anthropic`, `litellm`, …), vector DB clients (`chromadb`,
  `qdrant`, `milvus`, `pinecone`, …), and frameworks, emitting spans with
  the OpenTelemetry **GenAI semantic conventions** (`gen_ai.*` attributes:
  model, token usage, cost, finish reason, optionally prompt/completion
  content);
* the **OpenLIT platform**, a Docker stack (ClickHouse + OTLP collector +
  UI) that ingests those spans and renders per-model cost, latency, token
  and error dashboards. Any OTLP-speaking backend (Grafana Tempo, Jaeger,
  Datadog, …) also accepts the same spans.

### 10.1 What the repo already has

Open WebUI ships an OpenTelemetry setup gated on `ENABLE_OTEL`
(`main.py:529-532` → `utils/telemetry/setup.py`). With
`ENABLE_OTEL_TRACES=true` it installs a `TracerProvider`, an OTLP span
exporter (gRPC by default, HTTP via `OTEL_OTLP_SPAN_EXPORTER=http`), and
instruments FastAPI, SQLAlchemy, Redis, `requests`, `httpx`, **aiohttp
client**, logging, and system metrics (`utils/telemetry/instrumentors.py:164`).
`ENABLE_OTEL_METRICS=true` adds HTTP request counters/histograms and user
gauges (`utils/telemetry/metrics.py`).

Relevant env (`env.py:1241-1276`): `ENABLE_OTEL`, `ENABLE_OTEL_TRACES`,
`ENABLE_OTEL_METRICS`, `ENABLE_OTEL_LOGS`, `OTEL_EXPORTER_OTLP_ENDPOINT`
(default `http://localhost:4317`), `OTEL_EXPORTER_OTLP_INSECURE`,
`OTEL_SERVICE_NAME` (default `open-webui`), `OTEL_RESOURCE_ATTRIBUTES`,
`OTEL_TRACES_SAMPLER`, `OTEL_BASIC_AUTH_USERNAME/PASSWORD`,
`OTEL_OTLP_SPAN_EXPORTER` (`grpc` | `http`).

### 10.2 The catch: the chat proxy does not use the `openai` SDK

`openlit.init()` works by monkey-patching client libraries. The `openai`
package is pinned in `backend/requirements.txt`, but **nothing in
`backend/open_webui` imports it**. The chat proxy (§4), embeddings, and
task calls all go through raw `aiohttp` (or `requests` for sync
embeddings). So after `openlit.init()` you will still see **zero
`gen_ai.*` spans for chat completions**. What you do get from the existing
aiohttp instrumentor is a plain HTTP client span named
`POST https://api.openai.com/v1/chat/completions` with URL, method, and
status: useful for latency and error rate per provider, useless for model,
tokens, or cost.

Auto-instrumentation still earns its keep for the parts of Open WebUI that
*do* use instrumented clients: vector stores in `retrieval/vector/dbs/`
(Chroma, Qdrant, Milvus, Pinecone, …) and any pipe/filter function or
pipeline that imports the `openai` or `anthropic` SDK itself.

There are therefore three layers, in increasing effort:

| Layer | Effort | What you get |
|-------|--------|--------------|
| A. Point existing OTel at OpenLIT | env vars only | FastAPI request spans, DB/Redis spans, HTTP spans to providers (no GenAI attrs) |
| B. Add `openlit.init()` | one call in `main.py` | Vector DB spans, GPU metrics, SDK-based spans from functions/pipelines |
| C. Manual GenAI span in the proxy (**implemented**, `utils/telemetry/genai.py`) | on by default with `ENABLE_OTEL_TRACES` | Model, tokens, cost, stream flag, chat/user correlation per LLM call |

### 10.3 Layer A: ship existing telemetry to OpenLIT

Run the OpenLIT platform (`docker compose` from the OpenLIT repo). Its
collector accepts OTLP on `4318` (HTTP) and `4317` (gRPC); the UI is on
`3000`. Then:

```bash
ENABLE_OTEL=true
ENABLE_OTEL_TRACES=true
ENABLE_OTEL_METRICS=true
OTEL_SERVICE_NAME=open-webui
OTEL_OTLP_SPAN_EXPORTER=http
OTEL_EXPORTER_OTLP_ENDPOINT=http://openlit:4318/v1/traces
OTEL_METRICS_EXPORTER_OTLP_ENDPOINT=http://openlit:4318/v1/metrics
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=dev
```

(For gRPC leave `OTEL_OTLP_SPAN_EXPORTER=grpc`, use port `4317`, no path,
and set `OTEL_EXPORTER_OTLP_INSECURE=true` for plaintext.)

Fork-specific gotcha: this fork's Vite dev server is also pinned to port
`3000` (`vite.config.ts`, commits `4f1afcbaa` / `91cdbce86`). Remap the
OpenLIT UI port in its compose file when running both locally.

### 10.4 Layer B: `openlit.init()`

Add `openlit` to `backend/requirements.txt` (and `pyproject.toml`) and
call it right after the upstream OTel block in `main.py:529`, so it reuses
the provider that `setup_opentelemetry` installed instead of creating a
second one:

```python
# main.py, directly after setup_opentelemetry(...)
if ENABLE_OTEL and ENABLE_OPENLIT:
    import openlit
    from opentelemetry import trace

    openlit.init(
        tracer=trace.get_tracer('open-webui'),        # reuse existing provider
        application_name=OTEL_SERVICE_NAME,
        environment=OPENLIT_ENVIRONMENT,             # e.g. "dev" / "prod"
        capture_message_content=OPENLIT_CAPTURE_CONTENT,  # prompts+completions in spans; default off
        disable_metrics=not ENABLE_OTEL_METRICS,
        collect_gpu_stats=OPENLIT_GPU_STATS,          # only meaningful with local Ollama/vLLM
    )
```

Notes:

* If `ENABLE_OTEL_TRACES` is off, omit `tracer=` and pass
  `otlp_endpoint=` instead; OpenLIT will build its own provider from
  `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`.
* Check the `openlit.init` signature for the pinned version; parameter
  names (`capture_message_content` was `trace_content` in older releases)
  have changed across versions.
* The env flags above (`ENABLE_OPENLIT`, `OPENLIT_ENVIRONMENT`,
  `OPENLIT_CAPTURE_CONTENT`, `OPENLIT_GPU_STATS`) do not exist yet; add
  them to `env.py` next to the `OTEL_*` block.
* `disabled_instrumentors=[...]` lets you switch off patches you do not
  want (for example `chroma` if the extra spans are noisy).

### 10.5 Layer C: a GenAI span around the proxy call

> **Implemented.** See `docs/OpenLITLayerC.md` for the design, the
> attribute list (including user identity from the auth token), env
> flags (`ENABLE_OTEL_GENAI`, `OTEL_GENAI_*`), and tests. The code lives
> in `utils/telemetry/genai.py` and is wired into the chat, embeddings
> and responses routes of `routers/openai.py`. The sketch below is the
> original design note and is kept for context.

This is the layer that makes OpenLIT's LLM dashboards light up. The right
place is `routers/openai.py:generate_chat_completion`, between header
construction and `session.request` (`:1565-1632`), because that is the
only point where the resolved base URL, the stripped upstream model id,
the final payload, and the response are all in scope. The same pattern
applies to `embeddings` (`:1721`) and `responses` (`:1847`).

Attributes to set (names follow the OTel GenAI semconv, which is what
OpenLIT's UI keys on):

| Attribute | Source |
|-----------|--------|
| `gen_ai.operation.name` | `"chat"` (`"embeddings"` for the embeddings proxy) |
| `gen_ai.system` | `"openai"`, or `"azure_openai"` / `"anthropic"` based on `api_config` / URL |
| `gen_ai.request.model` | `payload['model']` **after** `strip_provider_model_prefix` |
| `gen_ai.request.is_stream` | `payload.get('stream', False)` |
| `gen_ai.request.temperature`, `gen_ai.request.max_tokens`, `gen_ai.request.top_p` | from `payload` |
| `server.address` | `urlparse(url).hostname` |
| `gen_ai.response.model` | `response['model']` |
| `gen_ai.response.finish_reasons` | `[c['finish_reason'] for c in choices]` |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` | `response['usage']['prompt_tokens']` / `['completion_tokens']` |
| `openwebui.chat_id`, `openwebui.user_id`, `openwebui.task` | `metadata['chat_id']`, `user.id`, `metadata.get('task')` (custom keys; `task` distinguishes title/tags/follow-up calls from user turns) |

Sketch:

```python
from urllib.parse import urlparse
from opentelemetry import trace
from opentelemetry.trace import StatusCode

_tracer = trace.get_tracer('open_webui.routers.openai')


def _set_usage_attrs(span, data: dict):
    usage = data.get('usage') or {}
    if usage.get('prompt_tokens') is not None:
        span.set_attribute('gen_ai.usage.input_tokens', usage['prompt_tokens'])
    if usage.get('completion_tokens') is not None:
        span.set_attribute('gen_ai.usage.output_tokens', usage['completion_tokens'])
    if data.get('model'):
        span.set_attribute('gen_ai.response.model', data['model'])
    reasons = [c.get('finish_reason') for c in data.get('choices', []) if c.get('finish_reason')]
    if reasons:
        span.set_attribute('gen_ai.response.finish_reasons', reasons)


async def _traced_stream(span, stream):
    """Yield upstream SSE lines unchanged; record usage from the final chunk; end the span."""
    try:
        async for line in stream:
            if line.startswith(b'data: {') and b'"usage"' in line:
                try:
                    _set_usage_attrs(span, JSONCodec.loads(line[6:]))
                except Exception:
                    pass
            yield line
    except Exception as e:
        span.record_exception(e)
        span.set_status(StatusCode.ERROR)
        raise
    finally:
        span.end()
```

and in `generate_chat_completion`, replacing the `session.request` block:

```python
span = _tracer.start_span('chat ' + requested_model)      # semconv name: "{operation} {model}"
span.set_attributes({
    'gen_ai.operation.name': 'chat',
    'gen_ai.system': 'azure_openai' if api_config.get('provider') == 'azure' else 'openai',
    'gen_ai.request.model': requested_model,
    'gen_ai.request.is_stream': is_streaming_request,
    'server.address': urlparse(url).hostname or '',
    'openwebui.user_id': user.id,
    **({'openwebui.chat_id': metadata['chat_id']} if metadata and metadata.get('chat_id') else {}),
    **({'openwebui.task': metadata['task']} if metadata and metadata.get('task') else {}),
})
with trace.use_span(span, end_on_exit=False):
    r = await session.request(...)                          # unchanged

if 'text/event-stream' in r.headers.get('Content-Type', ''):
    ...error branch unchanged, but call span.end() before returning...
    streaming = True
    return StreamingResponse(_traced_stream(span, stream_wrapper(r)), ...)
else:
    response = await r.json(loads=JSONCodec.loads)
    if isinstance(response, dict):
        _set_usage_attrs(span, response)
    if r.status >= 400:
        span.set_status(StatusCode.ERROR)
    span.end()
    ...
```

Why this shape:

* **Streaming spans must outlive the handler.** The route returns a
  `StreamingResponse` immediately; the upstream call is still in flight.
  Ending the span in the generator's `finally` gives it the true
  wall-clock duration and lets it see the usage chunk. `use_span(...,
  end_on_exit=False)` keeps the aiohttp instrumentor's HTTP span parented
  under it without ending it early.
* **Usage only arrives on streams if you asked for it.** `main.py:1187`
  already sets `stream_options.include_usage=true` when the model's
  capabilities advertise `usage`. Without that, streaming spans will have
  no token counts; the middleware sees the same gap (it merges usage at
  `middleware.py:5003` for Chat Completions deltas and `:4977` for
  Responses API events).
* **Responses API mode** puts usage in `response.completed` events and
  uses `input_tokens` / `output_tokens` names. Extend `_set_usage_attrs`
  accordingly if `api_type == "responses"` connections are in use.
* **Cost.** OpenLIT computes `gen_ai.usage.cost` inside its own
  instrumentors from a pricing table keyed on model name. Manual spans do
  not get this for free; either set `gen_ai.usage.cost` yourself using
  `openlit`'s pricing helper or a local table, or rely on OpenLIT's
  server-side pricing where available.
* **Parenting.** For UI traffic the HTTP request span for
  `POST /api/chat/completions` ends when the task ids are returned
  (§3.2 step 6), but the per-message `asyncio` task inherits the context,
  so the GenAI span still lands in the same trace. Tool-loop re-invocations
  (§5) nest as sibling `chat …` spans under that trace; title/tags
  generations show up with `openwebui.task` set.

### 10.6 Privacy and cardinality

* Leave `capture_message_content` off in shared environments. Prompts
  contain user data, and this fork additionally injects Power BI dataset
  names and ids into the system prompt (§9).
* `openwebui.user_id` and `openwebui.chat_id` are fine as **span**
  attributes but must never become **metric** labels; the existing
  `metrics.py` views deliberately restrict metric attributes to method,
  route, and status for this reason.
* Exporter auth: `OTEL_BASIC_AUTH_USERNAME/PASSWORD` for the upstream
  exporter, or `OTEL_EXPORTER_OTLP_HEADERS` for OpenLIT's own exporter.
* Do not log the `Authorization` header. The aiohttp request hook only
  records URL, method, and status (`instrumentors.py:137-161`); keep it
  that way.

### 10.7 Verifying

1. Start OpenLIT, set the env from §10.3, restart the backend, and confirm
   the log line `Created shared aiohttp session pool` is followed by no
   exporter errors.
2. Send one message in the UI. In the OpenLIT UI you should see a trace
   containing `POST /api/chat/completions`, the SQLAlchemy/Redis spans,
   and, after Layer C, a `chat <model>` span with token counts.
3. Send a message that triggers a tool call; the trace should contain
   two or more `chat <model>` spans.
4. Check that the title-generation span carries `openwebui.task=title_generation`.

---

## 11. Key files

| File | Role |
|------|------|
| `src/lib/apis/openai/index.ts` | Frontend clients (`chatCompletion`, `generateOpenAIChatCompletion`, `getOpenAIModelsDirect`) |
| `src/lib/components/chat/Chat.svelte` | Builds the payload; handles socket events |
| `backend/open_webui/main.py` | `/api/chat/completions`, `/api/models`, router mounting |
| `backend/open_webui/utils/middleware.py` | `process_chat_payload`, `process_chat_response`, tool loop, background tasks |
| `backend/open_webui/utils/chat.py` | Backend dispatcher; direct-connection bridge |
| `backend/open_webui/routers/openai.py` | The OpenAI-compatible proxy: config, model listing, chat, embeddings, responses, TTS |
| `backend/open_webui/utils/session_pool.py` | Shared aiohttp session, timeouts, stream cleanup |
| `backend/open_webui/utils/headers.py` | User-info forwarding, custom header templating |
| `backend/open_webui/utils/payload.py` | Model param application, system prompt injection |
| `backend/open_webui/utils/models.py` | Merges OpenAI + Ollama + function models into `app.state.MODELS` |
| `backend/open_webui/config.py` | Env seeds for `OPENAI_API_*` |
| `backend/open_webui/env.py` | `AIOHTTP_*`, `ENABLE_FORWARD_USER_INFO_HEADERS`, `ENABLE_OPENAI_API_PASSTHROUGH`, `MODELS_CACHE_TTL` |
