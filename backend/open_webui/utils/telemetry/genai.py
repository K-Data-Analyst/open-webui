"""GenAI spans for LLM calls and tool executions, per the OpenTelemetry GenAI
semantic conventions (https://github.com/open-telemetry/semantic-conventions-genai).

The chat proxy talks to providers over raw aiohttp, so SDK auto-instrumentation
(OpenLIT, OpenLLMetry, ...) never sees the call. This module emits:

* one ``{operation} {model}`` CLIENT span per upstream inference call
  (``gen_ai.operation.name`` = ``chat`` / ``embeddings``), with usage, finish
  reasons and, opt-in, the structured ``gen_ai.input.messages`` /
  ``gen_ai.output.messages`` / ``gen_ai.system_instructions`` /
  ``gen_ai.tool.definitions``;
* one ``execute_tool {tool}`` INTERNAL span per tool execution in the middleware
  tool loop, with the MCP attributes folded in for MCP tools as the spec asks.

A few non-spec keys OpenLIT's UI reads are kept alongside the spec ones
(``gen_ai.request.user``, ``gen_ai.application_name``, ``gen_ai.environment``,
``gen_ai.usage.total_tokens``, ``gen_ai.usage.cost``, ``gen_ai.request.is_stream``).
Fork-specific correlation keys use the ``openwebui.`` prefix.

Every helper is a no-op when tracing is off. See docs/OpenLITLayerC.md.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlparse

from open_webui.env import (
    ENABLE_OTEL,
    ENABLE_OTEL_GENAI,
    ENABLE_OTEL_TRACES,
    OTEL_GENAI_CAPTURE_CONTENT,
    OTEL_GENAI_CAPTURE_USER_PII,
    OTEL_GENAI_ENVIRONMENT,
    OTEL_GENAI_PRICING_CUSTOM,
    OTEL_GENAI_PRICING_JSON,
    OTEL_SERVICE_NAME,
)
from open_webui.utils.json_codec import JSONCodec

try:
    from opentelemetry import trace
    from opentelemetry.trace import Span, SpanKind, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # requirements-min.txt installs without opentelemetry
    _OTEL_AVAILABLE = False

log = logging.getLogger(__name__)

GENAI_ENABLED = bool(ENABLE_OTEL and ENABLE_OTEL_TRACES and ENABLE_OTEL_GENAI and _OTEL_AVAILABLE)

_tracer = trace.get_tracer('open_webui.genai') if GENAI_ENABLED else None

API_TYPE_CHAT = 'chat_completions'
API_TYPE_RESPONSES = 'responses'
API_TYPE_EMBEDDINGS = 'embeddings'

OPERATION_CHAT = 'chat'
OPERATION_EMBEDDINGS = 'embeddings'
OPERATION_EXECUTE_TOOL = 'execute_tool'

# Content capture modes (OTEL_GENAI_CAPTURE_CONTENT):
#   off   - no message content, tool definitions or system instructions on spans
#   tools - structured messages carrying only tool_call / tool_call_response parts
#           (names and ids, no arguments, no results, no text); tool names only in
#           gen_ai.tool.definitions
#   full  - everything, capped by the limits below
CAPTURE_OFF = 'off'
CAPTURE_TOOLS = 'tools'
CAPTURE_FULL = 'full'

# Largest serialized message list / tool payload stored on a span.
_CONTENT_MAX_CHARS = 32_000
_TOOL_RESULT_MAX_CHARS = 16_000
_DESCRIPTION_MAX_CHARS = 1_024

# payload key -> semconv attribute. Several payload keys map to max_tokens on purpose.
_REQUEST_PARAM_ATTRS = (
    ('temperature', 'gen_ai.request.temperature'),
    ('top_p', 'gen_ai.request.top_p'),
    ('max_tokens', 'gen_ai.request.max_tokens'),
    ('max_completion_tokens', 'gen_ai.request.max_tokens'),
    ('max_output_tokens', 'gen_ai.request.max_tokens'),
    ('seed', 'gen_ai.request.seed'),
    ('frequency_penalty', 'gen_ai.request.frequency_penalty'),
    ('presence_penalty', 'gen_ai.request.presence_penalty'),
)

# metadata key -> attribute (spec key first where one exists, then the fork's correlation key)
_METADATA_ATTRS = (
    ('chat_id', 'gen_ai.conversation.id'),
    ('chat_id', 'openwebui.chat_id'),
    ('session_id', 'openwebui.session_id'),
    ('task', 'openwebui.task'),
)

_JSON_RESPONSE_FORMATS = ('json_object', 'json_schema')


def capture_mode() -> str:
    return OTEL_GENAI_CAPTURE_CONTENT


##########################################
# Provider / pricing
##########################################


def detect_provider(request_url: str, api_config: dict | None) -> str:
    """Value for gen_ai.provider.name (well-known values: openai, azure.ai.openai, anthropic, ...)."""
    api_config = api_config or {}
    if api_config.get('azure') or api_config.get('provider') == 'azure':
        return 'azure.ai.openai'
    if 'api.anthropic.com' in (request_url or ''):
        return 'anthropic'
    return api_config.get('provider') or 'openai'


# Pricing, modelled on the OpenLIT Go SDK (sdk/go/helpers/pricing.go): a fetched
# table from an endpoint or file, plus custom per-token overrides that always win.
# Everything is normalised to USD per token as {'input', 'output', 'cache_read'}.
_pricing: dict[str, dict] = {}  # fetched / file table, keyed by normalised model name
_custom_pricing: dict[str, dict] = {}  # OTEL_GENAI_PRICING_CUSTOM entries, consulted first

_PER_TOKEN_INPUT_KEYS = ('input', 'input_cost_per_token', 'inputCostPerToken')
_PER_TOKEN_OUTPUT_KEYS = ('output', 'output_cost_per_token', 'outputCostPerToken')
_PER_TOKEN_CACHE_KEYS = ('cache_read', 'cache_read_cost_per_token', 'cacheReadCostPerToken')


def normalize_model_name(model: str) -> str:
    """Same normalisation as the Go SDK: trim and lowercase."""
    return model.strip().lower()


def _first_number(entry: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _per_token_entry(entry: Any) -> dict | None:
    """One model entry in per-token form ({'input', 'output'[, 'cache_read']}); the Go SDK's
    ModelPricing / pricing endpoint shape. Missing output is treated as 0 (embeddings)."""
    if not isinstance(entry, dict):
        return None
    input_price = _first_number(entry, _PER_TOKEN_INPUT_KEYS)
    if input_price is None:
        return None
    return {
        'input': input_price,
        'output': _first_number(entry, _PER_TOKEN_OUTPUT_KEYS) or 0.0,
        'cache_read': _first_number(entry, _PER_TOKEN_CACHE_KEYS),
    }


def _per_1k_entry(entry: Any) -> dict | None:
    """One model entry from OpenLIT's assets/pricing.json (USD per 1K tokens)."""
    if isinstance(entry, (int, float)) and not isinstance(entry, bool):  # embeddings: bare price
        return {'input': float(entry) / 1000, 'output': 0.0, 'cache_read': None}
    if not isinstance(entry, dict):
        return None
    prompt = entry.get('promptPrice')
    completion = entry.get('completionPrice')
    if not isinstance(prompt, (int, float)) or not isinstance(completion, (int, float)):
        return None
    cache = entry.get('cacheReadPrice')
    return {
        'input': float(prompt) / 1000,
        'output': float(completion) / 1000,
        'cache_read': float(cache) / 1000 if isinstance(cache, (int, float)) else None,
    }


def parse_pricing_table(table: Any) -> dict[str, dict]:
    """Normalise any supported pricing document to {model: per-token prices}.

    Accepted shapes, detected by structure:
    * Go SDK pricing endpoint:  {"data": {"<model>": {"input": <per token>, "output": <per token>}}}
    * Custom map (Go ``PricingInfo`` as JSON): {"<model>": {"input": ..., "output": ...}}
      (``input_cost_per_token`` / ``output_cost_per_token`` / ``cache_read`` also accepted)
    * OpenLIT assets/pricing.json: {"chat": {"<model>": {"promptPrice", "completionPrice",
      "cacheReadPrice"}}, "embeddings": {"<model>": <price>}}  (USD per 1K tokens)
    """
    if not isinstance(table, dict):
        return {}
    parsed: dict[str, dict] = {}

    if isinstance(table.get('data'), dict):  # Go SDK endpoint format
        table = table['data']

    if isinstance(table.get('chat'), dict) or isinstance(table.get('embeddings'), dict):  # OpenLIT format
        for section in ('embeddings', 'chat'):  # chat last so it wins on a duplicate name
            for model, entry in (table.get(section) or {}).items():
                price = _per_1k_entry(entry)
                if price is not None and isinstance(model, str):
                    parsed[normalize_model_name(model)] = price
        return parsed

    for model, entry in table.items():  # flat per-token map
        price = _per_token_entry(entry)
        if price is not None and isinstance(model, str):
            parsed[normalize_model_name(model)] = price
    return parsed


def set_pricing_table(table: Any, custom: Any = None) -> None:
    """Install pricing. ``custom`` entries take precedence over ``table`` entries, like the
    Go SDK's PricingInfo over its fetched endpoint. Pass None for both to disable cost."""
    global _pricing, _custom_pricing
    _pricing = parse_pricing_table(table)
    _custom_pricing = parse_pricing_table(custom)


async def _read_pricing_source(source: str) -> Any:
    """Return the parsed JSON behind a URL, a file path, or an inline JSON object."""
    source = source.strip()
    if source.startswith('{'):
        return JSONCodec.loads(source)
    if source.startswith(('http://', 'https://')):
        import aiohttp
        from open_webui.utils.session_pool import get_session

        session = await get_session()
        async with session.get(source, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            return JSONCodec.loads(await r.text())
    with open(source, encoding='utf-8') as f:
        return JSONCodec.loads(f.read())


async def load_pricing_table() -> None:
    """Load OTEL_GENAI_PRICING_JSON (endpoint URL or file) and OTEL_GENAI_PRICING_CUSTOM
    (inline JSON or file of per-token overrides) once at startup. Failures only log; each
    source is independent so a bad endpoint does not discard the custom overrides."""
    if not GENAI_ENABLED:
        return
    table = custom = None
    if OTEL_GENAI_PRICING_JSON.strip():
        try:
            table = await _read_pricing_source(OTEL_GENAI_PRICING_JSON)
        except Exception:
            log.exception('Failed to load GenAI pricing table from %s', OTEL_GENAI_PRICING_JSON)
    if OTEL_GENAI_PRICING_CUSTOM.strip():
        try:
            custom = await _read_pricing_source(OTEL_GENAI_PRICING_CUSTOM)
        except Exception:
            log.exception('Failed to load custom GenAI pricing from OTEL_GENAI_PRICING_CUSTOM')
    set_pricing_table(table, custom)
    if _pricing or _custom_pricing:
        log.info('GenAI pricing loaded: %d models from table, %d custom overrides', len(_pricing), len(_custom_pricing))
    elif OTEL_GENAI_PRICING_JSON.strip() or OTEL_GENAI_PRICING_CUSTOM.strip():
        log.warning('GenAI pricing configured but no usable model prices were found; gen_ai.usage.cost disabled')


def lookup_price(model: str | None) -> dict | None:
    """Per-token prices for a model: custom overrides first, then the table. Falls back to the
    name after the last '/' so provider-prefixed ids ("openai/gpt-4o") still resolve."""
    if not model or (not _pricing and not _custom_pricing):
        return None
    name = normalize_model_name(model)
    candidates = [name]
    if '/' in name:
        candidates.append(name.rsplit('/', 1)[1])
    for candidate in candidates:
        price = _custom_pricing.get(candidate) or _pricing.get(candidate)
        if price is not None:
            return price
    return None


def compute_cost(
    operation: str,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
) -> float | None:
    """USD cost of one call from per-token prices: input * input_price + output * output_price,
    with cache-read tokens billed at the cache price when known (else the input price)."""
    price = lookup_price(model)
    if price is None or (input_tokens is None and output_tokens is None):
        return None
    cached = cached_tokens or 0
    billed_input = max((input_tokens or 0) - cached, 0)
    cost = billed_input * price['input']
    if operation != OPERATION_EMBEDDINGS:
        cost += (output_tokens or 0) * price['output']
    if cached:
        cache_price = price['cache_read'] if price['cache_read'] is not None else price['input']
        cost += cached * cache_price
    return round(cost, 8)


##########################################
# Attribute builders
##########################################


def user_attributes(user: Any, request: Any = None, metadata: dict | None = None) -> dict:
    """User identity for a span: spec ``user.*`` / ``enduser.id`` keys (email and full name behind
    OTEL_GENAI_CAPTURE_USER_PII), OpenLIT's ``gen_ai.request.user``, the OAuth subject when
    present, and the auth mechanism recorded by utils/auth.py on request.state.auth."""
    attrs: dict[str, Any] = {}

    user_id = getattr(user, 'id', None) or (metadata or {}).get('user_id')
    if user_id:
        user_id = str(user_id)
        attrs['user.id'] = user_id
        attrs['enduser.id'] = user_id
        attrs['gen_ai.request.user'] = user_id  # what OpenLIT shows as the user

    role = getattr(user, 'role', None)
    if role:
        attrs['user.roles'] = [str(role)]

    if OTEL_GENAI_CAPTURE_USER_PII:
        email = getattr(user, 'email', None)
        if email:
            attrs['user.email'] = str(email).strip()
        name = getattr(user, 'name', None)
        if name:
            attrs['user.full_name'] = str(name).strip()

    attrs.update(_oauth_attributes(user))
    attrs.update(_auth_attributes(request))
    return attrs


def _oauth_attributes(user: Any) -> dict:
    oauth = getattr(user, 'oauth', None)
    if not isinstance(oauth, dict) or not oauth:
        return {}
    provider, data = next(iter(oauth.items()))
    if not isinstance(data, dict) or not data.get('sub'):
        return {}
    return {'openwebui.user.oauth_provider': str(provider), 'openwebui.user.oauth_sub': str(data['sub'])}


def _auth_attributes(request: Any) -> dict:
    """Auth mechanism recorded by utils/auth.py on request.state.auth (never the token itself)."""
    state = getattr(request, 'state', None) if request is not None else None
    auth = getattr(state, 'auth', None) if state is not None else None
    if not isinstance(auth, dict):
        return {}
    attrs: dict[str, Any] = {}
    if auth.get('type'):
        attrs['client.auth.type'] = str(auth['type'])
    if auth.get('jti'):
        attrs['client.auth.jti'] = str(auth['jti'])
    return attrs


def _request_attributes(payload: dict | None, api_type: str) -> dict:
    attrs: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return attrs
    for key, attr in _REQUEST_PARAM_ATTRS:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        attrs[attr] = value
    attrs['gen_ai.output.type'] = _output_type(payload, api_type)
    return attrs


def _output_type(payload: dict, api_type: str) -> str:
    if api_type == API_TYPE_RESPONSES:
        fmt = (payload.get('text') or {}).get('format') if isinstance(payload.get('text'), dict) else None
    else:
        fmt = payload.get('response_format')
    if isinstance(fmt, dict) and fmt.get('type') in _JSON_RESPONSE_FORMATS:
        return 'json'
    return 'text'


def _common_attributes(metadata: dict | None, user: Any, request: Any) -> dict:
    """Attributes shared by inference and tool spans: application, environment, conversation
    and message correlation, user identity."""
    attrs: dict[str, Any] = {
        'gen_ai.application_name': OTEL_SERVICE_NAME,
        'gen_ai.environment': OTEL_GENAI_ENVIRONMENT,
    }
    if isinstance(metadata, dict):
        for key, attr in _METADATA_ATTRS:
            value = metadata.get(key)
            if value:
                attrs[attr] = str(value)
        message_id = metadata.get('message_id') or metadata.get('assistant_message_id')
        if message_id:
            attrs['openwebui.message_id'] = str(message_id)
    attrs.update(user_attributes(user, request, metadata))
    return attrs


##########################################
# Structured messages (gen_ai.input.messages / gen_ai.output.messages)
##########################################


def _parse_arguments(raw: Any) -> Any:
    """Tool-call arguments as an object when they are JSON, else the raw value."""
    if isinstance(raw, str):
        try:
            return JSONCodec.loads(raw) if raw.strip() else {}
        except Exception:
            return raw
    return raw if raw is not None else {}


def _text_parts(content: Any, mode: str) -> list[dict]:
    """Text parts of an OpenAI-style message content (string or list of typed parts)."""
    if mode != CAPTURE_FULL:
        return []
    if isinstance(content, str):
        return [{'type': 'text', 'content': content}] if content else []
    parts: list[dict] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get('type')
            if item_type in ('text', 'input_text', 'output_text') and isinstance(item.get('text'), str):
                parts.append({'type': 'text', 'content': item['text']})
            elif item_type:
                parts.append({'type': str(item_type)})  # image / audio / file: type only, no payload
    return parts


def _tool_call_part(call_id: Any, name: Any, arguments: Any, mode: str) -> dict:
    part: dict[str, Any] = {'type': 'tool_call', 'id': str(call_id or ''), 'name': str(name or '')}
    if mode == CAPTURE_FULL:
        part['arguments'] = _parse_arguments(arguments)
    return part


def _tool_response_part(call_id: Any, response: Any, mode: str) -> dict:
    part: dict[str, Any] = {'type': 'tool_call_response', 'id': str(call_id or '')}
    if mode == CAPTURE_FULL:
        part['response'] = response if isinstance(response, (str, int, float, bool, dict, list)) else str(response)
    return part


def _chat_messages_to_spec(messages: list, mode: str) -> tuple[list[dict], list[dict]]:
    """Convert Chat Completions ``messages`` to (system_instructions, input_messages)."""
    system: list[dict] = []
    out: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get('role')
        if role in ('system', 'developer'):
            system.extend(_text_parts(message.get('content'), mode))
            continue
        parts: list[dict] = []
        if role == 'tool':
            parts.append(_tool_response_part(message.get('tool_call_id'), message.get('content'), mode))
        else:
            parts.extend(_text_parts(message.get('content'), mode))
            for call in message.get('tool_calls') or []:
                if isinstance(call, dict):
                    fn = call.get('function') or {}
                    parts.append(_tool_call_part(call.get('id'), fn.get('name'), fn.get('arguments'), mode))
        if parts:
            out.append({'role': str(role or 'user'), 'parts': parts})
    return system, out


def _responses_input_to_spec(payload: dict, mode: str) -> tuple[list[dict], list[dict]]:
    """Convert a Responses API ``input`` (+ ``instructions``) to (system_instructions, input_messages)."""
    system: list[dict] = []
    if isinstance(payload.get('instructions'), str) and mode == CAPTURE_FULL:
        system.append({'type': 'text', 'content': payload['instructions']})
    out: list[dict] = []
    items = payload.get('input')
    if isinstance(items, str):
        parts = _text_parts(items, mode)
        return system, [{'role': 'user', 'parts': parts}] if parts else []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')
        if item_type == 'function_call':
            out.append(
                {
                    'role': 'assistant',
                    'parts': [_tool_call_part(item.get('call_id'), item.get('name'), item.get('arguments'), mode)],
                }
            )
        elif item_type == 'function_call_output':
            out.append({'role': 'tool', 'parts': [_tool_response_part(item.get('call_id'), item.get('output'), mode)]})
        elif item.get('role'):
            if item['role'] in ('system', 'developer'):
                system.extend(_text_parts(item.get('content'), mode))
                continue
            parts = _text_parts(item.get('content'), mode)
            if parts:
                out.append({'role': str(item['role']), 'parts': parts})
    return system, out


def _responses_output_to_spec(response: dict, mode: str) -> list[dict]:
    """Convert a Responses API ``output`` list to one assistant output message."""
    parts: list[dict] = []
    for item in response.get('output') or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') == 'function_call':
            parts.append(_tool_call_part(item.get('call_id'), item.get('name'), item.get('arguments'), mode))
        elif item.get('type') == 'message':
            parts.extend(_text_parts(item.get('content'), mode))
    if not parts:
        return []
    message: dict[str, Any] = {'role': 'assistant', 'parts': parts}
    if response.get('status'):
        message['finish_reason'] = str(response['status'])
    return [message]


def _tool_definitions(payload: dict, mode: str) -> list[dict]:
    """gen_ai.tool.definitions: full definitions in ``full`` mode, names only in ``tools`` mode."""
    tools = payload.get('tools')
    if not isinstance(tools, list):
        return []
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool  # chat vs responses shape
        name = fn.get('name')
        if not name:
            continue
        if mode == CAPTURE_FULL:
            out.append({'type': str(tool.get('type') or 'function'), **{k: v for k, v in fn.items() if k != 'strict'}})
        else:
            out.append({'type': str(tool.get('type') or 'function'), 'name': str(name)})
    return out


def _fit_messages(messages: list[dict]) -> str | None:
    """Serialize a message list, dropping the oldest messages until it fits the size cap."""
    if not messages:
        return None
    messages = list(messages)
    while messages:
        text = JSONCodec.dumps(messages)
        if len(text) <= _CONTENT_MAX_CHARS:
            return text
        if len(messages) == 1:
            return JSONCodec.dumps(
                [{'role': messages[0].get('role', 'user'), 'parts': [{'type': 'text', 'content': '[truncated]'}]}]
            )
        messages.pop(0)
    return None


def _has_tool_parts(messages: list[dict]) -> bool:
    return any(part.get('type') in ('tool_call', 'tool_call_response') for m in messages for part in m.get('parts', []))


def _content_attributes(payload: dict | None, api_type: str) -> dict:
    """Opt-in content attributes for an inference span, according to the capture mode."""
    mode = capture_mode()
    if mode == CAPTURE_OFF or not isinstance(payload, dict) or api_type == API_TYPE_EMBEDDINGS:
        return {}
    attrs: dict[str, Any] = {}
    try:
        if api_type == API_TYPE_RESPONSES:
            system, messages = _responses_input_to_spec(payload, mode)
        else:
            system, messages = _chat_messages_to_spec(payload.get('messages') or [], mode)
        if mode == CAPTURE_TOOLS:
            messages = [m for m in messages if _has_tool_parts([m])]
        text = _fit_messages(messages)
        if text:
            attrs['gen_ai.input.messages'] = text
        if system and mode == CAPTURE_FULL:
            attrs['gen_ai.system_instructions'] = _truncate(JSONCodec.dumps(system))
        definitions = _tool_definitions(payload, mode)
        if definitions:
            attrs['gen_ai.tool.definitions'] = _truncate(JSONCodec.dumps(definitions))
    except Exception:
        log.debug('GenAI span: failed to build content attributes', exc_info=True)
    return attrs


def _truncate(text: str, limit: int = _CONTENT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f'… [truncated {len(text) - limit} chars]'


def _serialize(value: Any, limit: int) -> str:
    try:
        text = value if isinstance(value, str) else JSONCodec.dumps(value)
    except Exception:
        text = str(value)
    return _truncate(text, limit)


##########################################
# Inference span
##########################################


class LLMSpan:
    """One upstream LLM call. Owns the OTel span and ends it exactly once."""

    __slots__ = (
        'span',
        'operation',
        'api_type',
        'model',
        'mode',
        'started',
        'ended',
        'first_token_seen',
        'header_parsed',
        'text_parts',
        'tool_calls',
        'finish_reasons',
        'input_tokens',
        'output_tokens',
        'cached_tokens',
    )

    def __init__(self, span: Span, operation: str, api_type: str, model: str):
        self.span = span
        self.operation = operation
        self.api_type = api_type
        self.model = model
        self.mode = capture_mode() if api_type != API_TYPE_EMBEDDINGS else CAPTURE_OFF
        self.started = time.monotonic()
        self.ended = False
        self.first_token_seen = False
        self.header_parsed = False
        self.text_parts: list[str] = []
        self.tool_calls: dict[int, dict] = {}  # stream index -> {'id', 'name', 'arguments'}
        self.finish_reasons: list[str] = []
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.cached_tokens: int | None = None

    # -- context -------------------------------------------------------------

    def use(self):
        """Make this span current (so the aiohttp client span nests under it) without ending it."""
        # The route records exceptions itself via end(error=...); avoid a duplicate event here.
        return trace.use_span(self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False)

    # -- response data ---------------------------------------------------------

    def record_response(self, data: Any) -> None:
        """Record model / id / usage / finish reasons / output from a full response or a stream chunk."""
        if self.ended or not isinstance(data, dict):
            return
        try:
            if self.api_type == API_TYPE_RESPONSES:
                self._record_responses_api(data)
            else:
                self._record_chat_or_embeddings(data)
        except Exception:
            log.debug('GenAI span: failed to record response data', exc_info=True)

    def _record_chat_or_embeddings(self, data: dict) -> None:
        self._set_response_identity(data.get('id'), data.get('model'))

        usage = data.get('usage')
        if isinstance(usage, dict):
            prompt_details = usage.get('prompt_tokens_details')
            completion_details = usage.get('completion_tokens_details')
            self._set_usage(
                usage.get('prompt_tokens'),
                usage.get('completion_tokens'),
                usage.get('total_tokens'),
                prompt_details.get('cached_tokens') if isinstance(prompt_details, dict) else None,
                completion_details.get('reasoning_tokens') if isinstance(completion_details, dict) else None,
            )

        choices = data.get('choices')
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if choice.get('finish_reason'):
                self.finish_reasons.append(str(choice['finish_reason']))
            message = choice.get('message') or choice.get('delta') or {}
            if self.mode == CAPTURE_FULL and isinstance(message.get('content'), str) and message['content']:
                self.text_parts.append(message['content'])
            if self.mode != CAPTURE_OFF:
                for call in message.get('tool_calls') or []:
                    self._merge_tool_call(call)
        if self.finish_reasons:
            self.span.set_attribute('gen_ai.response.finish_reasons', self.finish_reasons)

    def _merge_tool_call(self, call: Any) -> None:
        """Merge a full or delta tool call (delta chunks arrive keyed by ``index``)."""
        if not isinstance(call, dict):
            return
        index = call.get('index')
        if not isinstance(index, int):
            index = len(self.tool_calls)
        entry = self.tool_calls.setdefault(index, {'id': '', 'name': '', 'arguments': ''})
        if call.get('id'):
            entry['id'] = str(call['id'])
        fn = call.get('function') or {}
        if fn.get('name'):
            entry['name'] = str(fn['name'])
        if isinstance(fn.get('arguments'), str):
            entry['arguments'] += fn['arguments']
        elif fn.get('arguments') is not None:
            entry['arguments'] = fn['arguments']

    def _record_responses_api(self, data: dict) -> None:
        event_type = data.get('type')
        if event_type and event_type != 'response.completed':
            if self.mode == CAPTURE_FULL and event_type == 'response.output_text.delta':
                delta = data.get('delta')
                if isinstance(delta, str):
                    self.text_parts.append(delta)
            return

        # Streams deliver the final object inside a response.completed event.
        response = data.get('response') if isinstance(data.get('response'), dict) else data
        self._set_response_identity(response.get('id'), response.get('model'))
        if response.get('status'):
            self.finish_reasons = [str(response['status'])]
            self.span.set_attribute('gen_ai.response.finish_reasons', self.finish_reasons)

        usage = response.get('usage')
        if isinstance(usage, dict):
            input_details = usage.get('input_tokens_details')
            output_details = usage.get('output_tokens_details')
            self._set_usage(
                usage.get('input_tokens'),
                usage.get('output_tokens'),
                usage.get('total_tokens'),
                input_details.get('cached_tokens') if isinstance(input_details, dict) else None,
                output_details.get('reasoning_tokens') if isinstance(output_details, dict) else None,
            )

        if self.mode != CAPTURE_OFF:
            messages = _responses_output_to_spec(response, self.mode)
            if messages and (self.mode == CAPTURE_FULL or _has_tool_parts(messages)):
                text = _fit_messages(messages)
                if text:
                    self.span.set_attribute('gen_ai.output.messages', text)
            self.text_parts = []  # the completed object is authoritative; drop streamed deltas

    def _set_response_identity(self, response_id: Any, model: Any) -> None:
        if model:
            self.span.set_attribute('gen_ai.response.model', str(model))
        if response_id:
            self.span.set_attribute('gen_ai.response.id', str(response_id))

    def _set_usage(self, input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens=None) -> None:
        span = self.span
        if isinstance(input_tokens, int):
            self.input_tokens = input_tokens
            span.set_attribute('gen_ai.usage.input_tokens', input_tokens)
        if isinstance(output_tokens, int):
            self.output_tokens = output_tokens
            span.set_attribute('gen_ai.usage.output_tokens', output_tokens)
        if not isinstance(total_tokens, int) and (self.input_tokens is not None or self.output_tokens is not None):
            total_tokens = (self.input_tokens or 0) + (self.output_tokens or 0)
        if isinstance(total_tokens, int):
            span.set_attribute('gen_ai.usage.total_tokens', total_tokens)
        if isinstance(cached_tokens, int):
            self.cached_tokens = cached_tokens
            span.set_attribute('gen_ai.usage.cache_read.input_tokens', cached_tokens)
        if isinstance(reasoning_tokens, int):
            span.set_attribute('gen_ai.usage.reasoning.output_tokens', reasoning_tokens)

    # -- streaming -------------------------------------------------------------

    def _inspect_line(self, line: bytes) -> None:
        """Pull usage / finish / tool-call data out of one SSE line. Never raises; never alters the line."""
        if not line.startswith(b'data:'):
            return
        if self.api_type == API_TYPE_RESPONSES:
            interesting = b'"response.completed"' in line or (
                self.mode == CAPTURE_FULL and b'"response.output_text.delta"' in line
            )
        else:
            interesting = (
                not self.header_parsed
                or self.mode == CAPTURE_FULL
                or b'"usage":{' in line
                or b'"usage": {' in line
                or b'"finish_reason":"' in line
                or b'"finish_reason": "' in line
                or (self.mode != CAPTURE_OFF and b'"tool_calls"' in line)
            )
        if not interesting:
            return
        body = line[5:].strip()
        if not body.startswith(b'{'):
            return
        try:
            data = JSONCodec.loads(body)
        except Exception:
            return
        self.header_parsed = True
        self.record_response(data)

    async def traced_stream(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Yield upstream SSE lines unchanged, record usage from them, and end the span when the
        stream finishes, fails, or is closed by a client disconnect."""
        try:
            async for line in stream:
                if not self.first_token_seen:
                    self.first_token_seen = True
                    self.span.set_attribute('gen_ai.server.time_to_first_token', time.monotonic() - self.started)
                self._inspect_line(line)
                yield line
        except Exception as e:
            self.end(error=e)
            raise
        finally:
            self.end()

    # -- lifecycle -------------------------------------------------------------

    def _output_messages(self) -> list[dict]:
        """Chat Completions output as one assistant message (text in full mode, tool calls in both)."""
        parts: list[dict] = []
        if self.mode == CAPTURE_FULL and self.text_parts:
            parts.append({'type': 'text', 'content': ''.join(self.text_parts)})
        for index in sorted(self.tool_calls):
            call = self.tool_calls[index]
            parts.append(_tool_call_part(call['id'], call['name'], call['arguments'], self.mode))
        if not parts:
            return []
        message: dict[str, Any] = {'role': 'assistant', 'parts': parts}
        if self.finish_reasons:
            message['finish_reason'] = self.finish_reasons[0]
        return [message]

    def end(self, status: int | None = None, error: BaseException | None = None) -> None:
        if self.ended:
            return
        self.ended = True
        span = self.span
        try:
            if status is not None:
                span.set_attribute('http.response.status_code', int(status))
                if status >= 400:
                    span.set_attribute('error.type', str(status))
                    span.set_status(StatusCode.ERROR, f'upstream HTTP {status}')
            if error is not None:
                span.record_exception(error)
                span.set_attribute('error.type', type(error).__qualname__)
                span.set_status(StatusCode.ERROR, str(error)[:200])
            if self.mode != CAPTURE_OFF and self.api_type != API_TYPE_RESPONSES:
                text = _fit_messages(self._output_messages())
                if text:
                    span.set_attribute('gen_ai.output.messages', text)
            cost = compute_cost(self.operation, self.model, self.input_tokens, self.output_tokens, self.cached_tokens)
            if cost is not None:
                span.set_attribute('gen_ai.usage.cost', cost)
        except Exception:
            log.debug('GenAI span: failed to finalize attributes', exc_info=True)
        finally:
            span.end()


def start_llm_span(
    *,
    operation: str,
    requested_model: str | None,
    request_url: str,
    api_config: dict | None,
    payload: dict | None = None,
    user: Any = None,
    metadata: dict | None = None,
    request: Any = None,
    openwebui_model_id: str | None = None,
    api_type: str = API_TYPE_CHAT,
) -> LLMSpan | None:
    """Start a CLIENT span for one upstream LLM call. Returns None when tracing is off or the
    span is not sampled; every LLMSpan method is then simply not called."""
    if not GENAI_ENABLED or _tracer is None:
        return None
    try:
        model = requested_model
        if not model and isinstance(payload, dict):
            model = payload.get('model')
        model = str(model or 'unknown')
        span = _tracer.start_span(f'{operation} {model}', kind=SpanKind.CLIENT)
        if not span.is_recording():
            span.end()
            return None

        parsed = urlparse(request_url or '')
        attrs: dict[str, Any] = {
            'gen_ai.operation.name': operation,
            'gen_ai.provider.name': detect_provider(request_url, api_config),
            'gen_ai.request.model': model,
            'gen_ai.request.is_stream': bool(isinstance(payload, dict) and payload.get('stream')),
            'openwebui.api_type': api_type,
        }
        if parsed.hostname:
            attrs['server.address'] = parsed.hostname
        if parsed.port:
            attrs['server.port'] = parsed.port
        if openwebui_model_id:
            attrs['openwebui.model_id'] = str(openwebui_model_id)
        if operation != OPERATION_EMBEDDINGS:
            attrs.update(_request_attributes(payload, api_type))
        attrs.update(_common_attributes(metadata, user, request))
        attrs.update(_content_attributes(payload, api_type))
        span.set_attributes(attrs)
        return LLMSpan(span, operation, api_type, model)
    except Exception:
        log.exception('GenAI span: failed to start span')
        return None


def use_span(llm_span: LLMSpan | ToolSpan | None):
    """Context manager making the span current; a no-op when there is no span."""
    return llm_span.use() if llm_span is not None else nullcontext()


def record_response(llm_span: LLMSpan | None, data: Any) -> None:
    if llm_span is not None:
        llm_span.record_response(data)


def end_span(llm_span: LLMSpan | None, status: int | None = None, error: BaseException | None = None) -> None:
    if llm_span is not None:
        llm_span.end(status=status, error=error)


##########################################
# Tool execution span (execute_tool)
##########################################


def _tool_type(tool: dict | None) -> str:
    """gen_ai.tool.type: ``function`` for code the model calls into (local, builtin, MCP),
    ``extension`` for tools executed elsewhere (tool servers, browser-side direct tools)."""
    if not tool:
        return 'function'
    if tool.get('direct') or tool.get('type') == 'external':
        return 'extension'
    return 'function'


def _tool_server_attributes(tool: dict | None) -> dict:
    """MCP attributes folded into the execute_tool span (the spec asks for one span, not two),
    plus the fork's server correlation keys."""
    attrs: dict[str, Any] = {}
    if not tool:
        return attrs
    tool_type = tool.get('type')
    if tool_type:
        attrs['openwebui.tool.type'] = str(tool_type)
    if tool.get('server_id'):
        attrs['openwebui.tool.server_id'] = str(tool['server_id'])
    if tool.get('direct'):
        attrs['openwebui.tool.direct'] = True
    url = None
    if tool_type == 'mcp':
        attrs['mcp.method.name'] = 'tools/call'
        url = getattr(tool.get('client'), 'url', None)
    elif isinstance(tool.get('server'), dict):
        url = tool['server'].get('url')
    if isinstance(url, str) and url:
        parsed = urlparse(url)
        if parsed.hostname:
            attrs['server.address'] = parsed.hostname
        if parsed.port:
            attrs['server.port'] = parsed.port
    return attrs


class ToolSpan:
    """One tool execution. Owns the OTel span and ends it exactly once."""

    __slots__ = ('span', 'name', 'mode', 'ended')

    def __init__(self, span: Span, name: str):
        self.span = span
        self.name = name
        self.mode = capture_mode()
        self.ended = False

    def use(self):
        """Make this span current so HTTP spans of tool servers / MCP calls nest under it."""
        return trace.use_span(self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False)

    def end(self, result: Any = None, error: BaseException | None = None) -> None:
        """End the span. A ``{'error': ...}`` result or an ``Error:`` string (the shapes the tool
        loop produces when a call fails) marks the span as failed; ``error`` records an exception."""
        if self.ended:
            return
        self.ended = True
        span = self.span
        try:
            failure = None
            if isinstance(result, dict) and result.get('error') is not None:
                failure = str(result['error'])
            elif isinstance(result, str) and result.startswith('Error:'):
                failure = result
            if error is not None:
                span.record_exception(error)
                span.set_attribute('error.type', type(error).__qualname__)
                span.set_status(StatusCode.ERROR, str(error)[:200])
            elif failure is not None:
                span.set_attribute('error.type', 'tool_error')
                span.set_status(StatusCode.ERROR, failure[:200])
            if self.mode == CAPTURE_FULL and result is not None:
                span.set_attribute('gen_ai.tool.call.result', _serialize(result, _TOOL_RESULT_MAX_CHARS))
        except Exception:
            log.debug('GenAI tool span: failed to finalize attributes', exc_info=True)
        finally:
            span.end()


def start_tool_span(
    *,
    tool_call: dict | None,
    tool: dict | None,
    arguments: Any = None,
    name: str | None = None,
    metadata: dict | None = None,
    user: Any = None,
    request: Any = None,
) -> ToolSpan | None:
    """Start an ``execute_tool {name}`` INTERNAL span. ``tool_call`` is the model's tool call
    (OpenAI shape: id + function.name); ``tool`` is the resolved tool dict from the middleware
    (spec, type, server_id, client, direct), or None when the tool was not found."""
    if not GENAI_ENABLED or _tracer is None:
        return None
    try:
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        fn = tool_call.get('function') if isinstance(tool_call.get('function'), dict) else {}
        tool_name = str(name or fn.get('name') or tool_call.get('name') or 'unknown')
        span = _tracer.start_span(f'{OPERATION_EXECUTE_TOOL} {tool_name}', kind=SpanKind.INTERNAL)
        if not span.is_recording():
            span.end()
            return None

        attrs: dict[str, Any] = {
            'gen_ai.operation.name': OPERATION_EXECUTE_TOOL,
            'gen_ai.tool.name': tool_name,
            'gen_ai.tool.type': _tool_type(tool),
        }
        call_id = tool_call.get('id') or tool_call.get('call_id')
        if call_id:
            attrs['gen_ai.tool.call.id'] = str(call_id)
        spec = tool.get('spec') if isinstance(tool, dict) and isinstance(tool.get('spec'), dict) else {}
        if spec.get('description'):
            attrs['gen_ai.tool.description'] = _truncate(str(spec['description']), _DESCRIPTION_MAX_CHARS)
        if capture_mode() == CAPTURE_FULL:
            if arguments is None:
                arguments = fn.get('arguments', tool_call.get('parameters'))
            if arguments is not None:
                attrs['gen_ai.tool.call.arguments'] = _serialize(_parse_arguments(arguments), _TOOL_RESULT_MAX_CHARS)
        attrs.update(_tool_server_attributes(tool))
        attrs.update(_common_attributes(metadata, user, request))
        span.set_attributes(attrs)
        tool_span = ToolSpan(span, tool_name)
        if tool is None:
            tool_span.end(result=f'Error: Tool "{tool_name}" not found.')
            return None
        return tool_span
    except Exception:
        log.exception('GenAI span: failed to start tool span')
        return None


def end_tool_span(tool_span: ToolSpan | None, result: Any = None, error: BaseException | None = None) -> None:
    if tool_span is not None:
        tool_span.end(result=result, error=error)
