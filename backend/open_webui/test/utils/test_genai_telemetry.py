"""Tests for the GenAI spans in open_webui.utils.telemetry.genai (OTel GenAI semconv).

Run from the repo root:
    WEBUI_SECRET_KEY=test PYTHONPATH=backend pytest backend/open_webui/test/utils/test_genai_telemetry.py
"""

from types import SimpleNamespace

import pytest
from open_webui.utils.json_codec import JSONCodec
from open_webui.utils.telemetry import genai
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode


@pytest.fixture
def exporter(monkeypatch):
    """Route genai spans into an in-memory exporter regardless of the process env."""
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(genai, 'GENAI_ENABLED', True)
    monkeypatch.setattr(genai, '_tracer', provider.get_tracer('test'))
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_USER_PII', True)
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_TOOLS)
    genai.set_pricing_table(None)
    return exp


def _user(**overrides):
    base = dict(id='u-1', role='user', email='Ada@example.com ', name='Ada', oauth={'oidc': {'sub': 'sub-123'}})
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(auth=None):
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


METADATA = {'chat_id': 'c-1', 'session_id': 's-1', 'task': 'title_generation', 'message_id': 'm-1'}


def _start(**overrides):
    kwargs = dict(
        operation=genai.OPERATION_CHAT,
        requested_model='gpt-4o',
        request_url='https://api.openai.com/v1/chat/completions',
        api_config={},
        payload={'model': 'gpt-4o', 'stream': False, 'temperature': 0.2, 'max_tokens': 100, 'messages': []},
        user=_user(),
        metadata=METADATA,
        request=_request({'type': 'jwt', 'jti': 'jti-1', 'iat': 1, 'exp': 2}),
        openwebui_model_id='my-custom-model',
    )
    kwargs.update(overrides)
    return genai.start_llm_span(**kwargs)


def _only_span(exporter):
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


async def _lines(*chunks):
    for c in chunks:
        yield c


def _sse(obj) -> bytes:
    return b'data: ' + JSONCodec.dumps(obj).encode() + b'\n'


def _messages(span, key):
    return JSONCodec.loads(span.attributes[key])


# --------------------------------------------------------------------------- #
# disabled / no-op behaviour
# --------------------------------------------------------------------------- #


def test_disabled_returns_none_and_helpers_are_noops(monkeypatch):
    monkeypatch.setattr(genai, 'GENAI_ENABLED', False)
    assert _start() is None
    assert genai.start_tool_span(tool_call={'id': 'x'}, tool={'spec': {}}) is None
    genai.record_response(None, {'usage': {}})
    genai.end_span(None, status=500, error=RuntimeError('x'))
    genai.end_tool_span(None, result={'error': 'x'})
    with genai.use_span(None):
        pass


# --------------------------------------------------------------------------- #
# inference span: attributes per the GenAI semconv
# --------------------------------------------------------------------------- #


def test_non_streaming_chat_span_attributes(exporter):
    llm = _start()
    assert llm is not None
    genai.record_response(
        llm,
        {
            'id': 'chatcmpl-1',
            'model': 'gpt-4o-2024-08-06',
            'choices': [{'finish_reason': 'stop', 'message': {'content': 'hi'}}],
            'usage': {
                'prompt_tokens': 10,
                'completion_tokens': 5,
                'total_tokens': 15,
                'prompt_tokens_details': {'cached_tokens': 4},
                'completion_tokens_details': {'reasoning_tokens': 2},
            },
        },
    )
    genai.end_span(llm, status=200)
    genai.end_span(llm, status=500)  # second end is ignored

    span = _only_span(exporter)
    a = span.attributes
    assert span.name == 'chat gpt-4o'
    assert span.kind == SpanKind.CLIENT
    assert span.status.status_code == StatusCode.UNSET
    assert a['gen_ai.operation.name'] == 'chat'
    assert a['gen_ai.provider.name'] == 'openai'
    assert 'gen_ai.system' not in a  # deprecated in the spec
    assert a['gen_ai.request.model'] == 'gpt-4o'
    assert a['gen_ai.request.is_stream'] is False
    assert a['gen_ai.request.temperature'] == 0.2
    assert a['gen_ai.request.max_tokens'] == 100
    assert a['gen_ai.output.type'] == 'text'
    assert a['server.address'] == 'api.openai.com'
    assert a['gen_ai.application_name'] == genai.OTEL_SERVICE_NAME
    assert a['gen_ai.environment'] == genai.OTEL_GENAI_ENVIRONMENT
    assert a['openwebui.model_id'] == 'my-custom-model'
    assert a['openwebui.api_type'] == 'chat_completions'
    # conversation / correlation
    assert a['gen_ai.conversation.id'] == 'c-1'
    assert a['openwebui.chat_id'] == 'c-1'
    assert a['openwebui.session_id'] == 's-1'
    assert a['openwebui.task'] == 'title_generation'
    assert a['openwebui.message_id'] == 'm-1'
    # user identity: spec user.* keys, enduser.id, OpenLIT's request.user, auth state
    assert a['user.id'] == 'u-1'
    assert a['enduser.id'] == 'u-1'
    assert a['gen_ai.request.user'] == 'u-1'
    assert tuple(a['user.roles']) == ('user',)
    assert a['user.email'] == 'Ada@example.com'
    assert a['user.full_name'] == 'Ada'
    assert 'enduser.role' not in a  # deprecated in favour of user.roles
    assert a['client.auth.type'] == 'jwt'
    assert a['client.auth.jti'] == 'jti-1'
    assert a['openwebui.user.oauth_provider'] == 'oidc'
    assert a['openwebui.user.oauth_sub'] == 'sub-123'
    # response
    assert a['gen_ai.response.id'] == 'chatcmpl-1'
    assert a['gen_ai.response.model'] == 'gpt-4o-2024-08-06'
    assert tuple(a['gen_ai.response.finish_reasons']) == ('stop',)
    assert a['gen_ai.usage.input_tokens'] == 10
    assert a['gen_ai.usage.output_tokens'] == 5
    assert a['gen_ai.usage.total_tokens'] == 15
    assert a['gen_ai.usage.cache_read.input_tokens'] == 4
    assert a['gen_ai.usage.reasoning.output_tokens'] == 2
    assert a['http.response.status_code'] == 200
    assert 'gen_ai.usage.cost' not in a
    # tools mode: no text anywhere, and no output message because there were no tool calls
    assert 'gen_ai.input.messages' not in a
    assert 'gen_ai.output.messages' not in a
    assert 'gen_ai.system_instructions' not in a


def test_pii_gate_and_api_key_auth(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_USER_PII', False)
    llm = _start(user=_user(oauth=None), request=_request({'type': 'api_key'}))
    genai.end_span(llm, status=200)
    a = _only_span(exporter).attributes
    assert a['user.id'] == 'u-1'
    assert 'user.email' not in a
    assert 'user.full_name' not in a
    assert 'openwebui.user.oauth_sub' not in a
    assert a['client.auth.type'] == 'api_key'
    assert 'client.auth.jti' not in a


def test_user_id_falls_back_to_metadata(exporter):
    llm = _start(user=None, request=None, metadata={'user_id': 'u-meta'})
    genai.end_span(llm)
    a = _only_span(exporter).attributes
    assert a['user.id'] == 'u-meta'
    assert 'client.auth.type' not in a


def test_json_output_type(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'messages': [], 'response_format': {'type': 'json_schema'}})
    genai.end_span(llm)
    assert _only_span(exporter).attributes['gen_ai.output.type'] == 'json'


@pytest.mark.parametrize(
    'url,api_config,expected',
    [
        ('https://api.openai.com/v1/chat/completions', {}, 'openai'),
        ('https://x.openai.azure.com/openai/v1/chat/completions', {'azure': True}, 'azure.ai.openai'),
        ('https://x/chat/completions', {'provider': 'azure'}, 'azure.ai.openai'),
        ('https://api.anthropic.com/v1/chat/completions', {}, 'anthropic'),
        ('https://openrouter.ai/api/v1/chat/completions', {'provider': 'openrouter'}, 'openrouter'),
    ],
)
def test_provider_detection(url, api_config, expected):
    assert genai.detect_provider(url, api_config) == expected


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_upstream_http_error_marks_span(exporter):
    llm = _start()
    genai.record_response(llm, {'error': {'message': 'bad key'}})
    genai.end_span(llm, status=401)
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['http.response.status_code'] == 401
    assert span.attributes['error.type'] == '401'


def test_exception_marks_span(exporter):
    llm = _start()
    genai.end_span(llm, error=TimeoutError('upstream timeout'))
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'TimeoutError'
    assert any(e.name == 'exception' for e in span.events)


# --------------------------------------------------------------------------- #
# streaming
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_streaming_chat_records_usage_from_final_chunk(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'stream': True, 'messages': []})
    chunks = [
        _sse(
            {
                'id': 'c1',
                'model': 'gpt-4o-2024-08-06',
                'choices': [{'delta': {'content': 'He'}, 'finish_reason': None}],
                'usage': None,
            }
        ),
        b': keep-alive\n',
        _sse({'id': 'c1', 'choices': [{'delta': {'content': 'llo'}, 'finish_reason': None}], 'usage': None}),
        _sse({'id': 'c1', 'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': None}),
        _sse({'id': 'c1', 'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9}}),
        b'data: [DONE]\n',
    ]
    out = [line async for line in llm.traced_stream(_lines(*chunks))]
    assert out == chunks  # bytes pass through unchanged

    span = _only_span(exporter)
    a = span.attributes
    assert a['gen_ai.request.is_stream'] is True
    assert a['gen_ai.response.model'] == 'gpt-4o-2024-08-06'
    assert tuple(a['gen_ai.response.finish_reasons']) == ('stop',)
    assert a['gen_ai.usage.input_tokens'] == 7
    assert a['gen_ai.usage.output_tokens'] == 2
    assert a['gen_ai.usage.total_tokens'] == 9
    assert a['gen_ai.server.time_to_first_token'] >= 0
    assert 'gen_ai.output.messages' not in a  # tools mode, no tool calls
    assert span.status.status_code == StatusCode.UNSET


def _tool_call_chunks():
    """A streamed tool call split across chunks, as OpenAI sends it (index-keyed deltas)."""
    return [
        _sse(
            {
                'id': 'c1',
                'model': 'gpt-4o',
                'choices': [{'delta': {'role': 'assistant', 'content': None}, 'finish_reason': None}],
            }
        ),
        _sse(
            {
                'choices': [
                    {
                        'delta': {
                            'tool_calls': [
                                {
                                    'index': 0,
                                    'id': 'call_1',
                                    'type': 'function',
                                    'function': {'name': 'get_weather', 'arguments': ''},
                                }
                            ]
                        },
                        'finish_reason': None,
                    }
                ]
            }
        ),
        _sse(
            {
                'choices': [
                    {'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '{"loc'}}]}, 'finish_reason': None}
                ]
            }
        ),
        _sse(
            {
                'choices': [
                    {
                        'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': 'ation": "Paris"}'}}]},
                        'finish_reason': None,
                    }
                ]
            }
        ),
        _sse(
            {
                'choices': [
                    {
                        'delta': {
                            'tool_calls': [
                                {'index': 1, 'id': 'call_2', 'function': {'name': 'get_time', 'arguments': '{}'}}
                            ]
                        },
                        'finish_reason': None,
                    }
                ]
            }
        ),
        _sse({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}),
        _sse({'choices': [], 'usage': {'prompt_tokens': 50, 'completion_tokens': 20}}),
    ]


@pytest.mark.asyncio
async def test_streaming_tool_calls_tools_mode_records_names_without_arguments(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'stream': True, 'messages': []})
    async for _ in llm.traced_stream(_lines(*_tool_call_chunks())):
        pass
    span = _only_span(exporter)
    assert tuple(span.attributes['gen_ai.response.finish_reasons']) == ('tool_calls',)
    out = _messages(span, 'gen_ai.output.messages')
    assert out == [
        {
            'role': 'assistant',
            'parts': [
                {'type': 'tool_call', 'id': 'call_1', 'name': 'get_weather'},
                {'type': 'tool_call', 'id': 'call_2', 'name': 'get_time'},
            ],
            'finish_reason': 'tool_calls',
        }
    ]


@pytest.mark.asyncio
async def test_streaming_tool_calls_full_mode_records_assembled_arguments(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_FULL)
    llm = _start(payload={'model': 'gpt-4o', 'stream': True, 'messages': []})
    async for _ in llm.traced_stream(_lines(*_tool_call_chunks())):
        pass
    out = _messages(_only_span(exporter), 'gen_ai.output.messages')
    assert out[0]['parts'][0] == {
        'type': 'tool_call',
        'id': 'call_1',
        'name': 'get_weather',
        'arguments': {'location': 'Paris'},
    }
    assert out[0]['parts'][1]['arguments'] == {}


@pytest.mark.asyncio
async def test_streaming_responses_api_records_completed_event(exporter):
    llm = _start(api_type=genai.API_TYPE_RESPONSES, payload={'model': 'gpt-4o', 'stream': True, 'input': 'hi'})
    chunks = [
        b'event: response.created\n',
        _sse({'type': 'response.created', 'response': {'id': 'resp_1', 'model': 'gpt-4o'}}),
        b'event: response.output_text.delta\n',
        _sse({'type': 'response.output_text.delta', 'delta': 'Hello'}),
        b'event: response.completed\n',
        _sse(
            {
                'type': 'response.completed',
                'response': {
                    'id': 'resp_1',
                    'model': 'gpt-4o-2024-08-06',
                    'status': 'completed',
                    'output': [
                        {'type': 'function_call', 'call_id': 'call_9', 'name': 'lookup', 'arguments': '{"q": 1}'},
                        {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Hello'}]},
                    ],
                    'usage': {
                        'input_tokens': 3,
                        'output_tokens': 4,
                        'total_tokens': 7,
                        'input_tokens_details': {'cached_tokens': 0},
                    },
                },
            }
        ),
    ]
    out = [line async for line in llm.traced_stream(_lines(*chunks))]
    assert out == chunks
    span = _only_span(exporter)
    a = span.attributes
    assert a['openwebui.api_type'] == 'responses'
    assert a['gen_ai.response.id'] == 'resp_1'
    assert a['gen_ai.response.model'] == 'gpt-4o-2024-08-06'
    assert tuple(a['gen_ai.response.finish_reasons']) == ('completed',)
    assert a['gen_ai.usage.input_tokens'] == 3
    assert a['gen_ai.usage.output_tokens'] == 4
    assert a['gen_ai.usage.total_tokens'] == 7
    # tools mode: the function_call is recorded by name and id only; the text part is dropped
    assert _messages(span, 'gen_ai.output.messages') == [
        {
            'role': 'assistant',
            'parts': [{'type': 'tool_call', 'id': 'call_9', 'name': 'lookup'}],
            'finish_reason': 'completed',
        }
    ]


@pytest.mark.asyncio
async def test_stream_closed_early_still_ends_span(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'stream': True})
    gen = llm.traced_stream(
        _lines(
            _sse({'choices': [{'delta': {'content': 'a'}, 'finish_reason': None}]}),
            _sse({'usage': {'prompt_tokens': 1}}),
        )
    )
    await gen.__anext__()
    await gen.aclose()  # client disconnect
    span = _only_span(exporter)
    assert span.end_time is not None
    assert 'gen_ai.usage.input_tokens' not in span.attributes
    assert span.status.status_code == StatusCode.UNSET


@pytest.mark.asyncio
async def test_stream_error_marks_span_and_propagates(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'stream': True})

    async def failing():
        yield _sse({'choices': [{'delta': {'content': 'a'}, 'finish_reason': None}]})
        raise ConnectionResetError('upstream reset')

    with pytest.raises(ConnectionResetError):
        async for _ in llm.traced_stream(failing()):
            pass
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'ConnectionResetError'


@pytest.mark.asyncio
async def test_malformed_stream_lines_are_ignored(exporter):
    llm = _start(payload={'model': 'gpt-4o', 'stream': True})
    chunks = [
        b'data: {not json\n',
        b'data: "usage": {\n',
        b'garbage\n',
        _sse({'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}),
    ]
    out = [line async for line in llm.traced_stream(_lines(*chunks))]
    assert out == chunks
    a = _only_span(exporter).attributes
    assert a['gen_ai.usage.input_tokens'] == 2
    assert a['gen_ai.usage.total_tokens'] == 3


# --------------------------------------------------------------------------- #
# structured input messages / system instructions / tool definitions
# --------------------------------------------------------------------------- #

CHAT_PAYLOAD = {
    'model': 'gpt-4o',
    'messages': [
        {'role': 'system', 'content': 'Be brief.'},
        {'role': 'user', 'content': 'weather in Paris?'},
        {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call_1',
                    'type': 'function',
                    'function': {'name': 'get_weather', 'arguments': '{"location": "Paris"}'},
                }
            ],
        },
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'rainy, 57°F'},
    ],
    'tools': [
        {
            'type': 'function',
            'function': {'name': 'get_weather', 'description': 'Weather', 'parameters': {'type': 'object'}},
        }
    ],
}


def test_input_messages_tools_mode_keeps_only_tool_parts(exporter):
    llm = _start(payload=CHAT_PAYLOAD)
    genai.end_span(llm)
    span = _only_span(exporter)
    assert _messages(span, 'gen_ai.input.messages') == [
        {'role': 'assistant', 'parts': [{'type': 'tool_call', 'id': 'call_1', 'name': 'get_weather'}]},
        {'role': 'tool', 'parts': [{'type': 'tool_call_response', 'id': 'call_1'}]},
    ]
    assert _messages(span, 'gen_ai.tool.definitions') == [{'type': 'function', 'name': 'get_weather'}]
    assert 'gen_ai.system_instructions' not in span.attributes


def test_input_messages_full_mode(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_FULL)
    llm = _start(payload=CHAT_PAYLOAD)
    genai.record_response(llm, {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Rainy.'}}]})
    genai.end_span(llm)
    span = _only_span(exporter)
    assert _messages(span, 'gen_ai.system_instructions') == [{'type': 'text', 'content': 'Be brief.'}]
    assert _messages(span, 'gen_ai.input.messages') == [
        {'role': 'user', 'parts': [{'type': 'text', 'content': 'weather in Paris?'}]},
        {
            'role': 'assistant',
            'parts': [{'type': 'tool_call', 'id': 'call_1', 'name': 'get_weather', 'arguments': {'location': 'Paris'}}],
        },
        {'role': 'tool', 'parts': [{'type': 'tool_call_response', 'id': 'call_1', 'response': 'rainy, 57°F'}]},
    ]
    assert _messages(span, 'gen_ai.output.messages') == [
        {'role': 'assistant', 'parts': [{'type': 'text', 'content': 'Rainy.'}], 'finish_reason': 'stop'}
    ]
    assert _messages(span, 'gen_ai.tool.definitions')[0]['description'] == 'Weather'


def test_input_messages_off_mode(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_OFF)
    llm = _start(payload=CHAT_PAYLOAD)
    genai.record_response(
        llm,
        {
            'choices': [
                {
                    'finish_reason': 'tool_calls',
                    'message': {'tool_calls': [{'id': 'c', 'function': {'name': 'x', 'arguments': '{}'}}]},
                }
            ]
        },
    )
    genai.end_span(llm)
    a = _only_span(exporter).attributes
    assert tuple(a['gen_ai.response.finish_reasons']) == ('tool_calls',)
    for key in (
        'gen_ai.input.messages',
        'gen_ai.output.messages',
        'gen_ai.system_instructions',
        'gen_ai.tool.definitions',
    ):
        assert key not in a


def test_responses_api_input_conversion(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_FULL)
    payload = {
        'model': 'gpt-4o',
        'instructions': 'Be brief.',
        'input': [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]},
            {'type': 'function_call', 'call_id': 'call_1', 'name': 'lookup', 'arguments': '{"q": 1}'},
            {'type': 'function_call_output', 'call_id': 'call_1', 'output': '42'},
        ],
        'tools': [{'type': 'function', 'name': 'lookup', 'parameters': {}}],
    }
    llm = _start(api_type=genai.API_TYPE_RESPONSES, payload=payload)
    genai.end_span(llm)
    span = _only_span(exporter)
    assert _messages(span, 'gen_ai.system_instructions') == [{'type': 'text', 'content': 'Be brief.'}]
    assert _messages(span, 'gen_ai.input.messages') == [
        {'role': 'user', 'parts': [{'type': 'text', 'content': 'hi'}]},
        {
            'role': 'assistant',
            'parts': [{'type': 'tool_call', 'id': 'call_1', 'name': 'lookup', 'arguments': {'q': 1}}],
        },
        {'role': 'tool', 'parts': [{'type': 'tool_call_response', 'id': 'call_1', 'response': '42'}]},
    ]
    assert _messages(span, 'gen_ai.tool.definitions') == [{'type': 'function', 'name': 'lookup', 'parameters': {}}]


def test_oversized_input_messages_drop_oldest(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_FULL)
    monkeypatch.setattr(genai, '_CONTENT_MAX_CHARS', 200)
    payload = {'model': 'm', 'messages': [{'role': 'user', 'content': f'message {i} ' + 'x' * 60} for i in range(5)]}
    llm = _start(payload=payload)
    genai.end_span(llm)
    msgs = _messages(_only_span(exporter), 'gen_ai.input.messages')
    assert len(msgs) < 5
    assert msgs[-1]['parts'][0]['content'].startswith('message 4')


# --------------------------------------------------------------------------- #
# embeddings and cost
# --------------------------------------------------------------------------- #


def test_embeddings_usage(exporter):
    llm = _start(
        operation=genai.OPERATION_EMBEDDINGS,
        requested_model='text-embedding-3-small',
        request_url='https://api.openai.com/v1/embeddings',
        payload={'model': 'text-embedding-3-small', 'input': 'x'},
        metadata=None,
        api_type=genai.API_TYPE_EMBEDDINGS,
    )
    genai.record_response(
        llm, {'model': 'text-embedding-3-small', 'data': [], 'usage': {'prompt_tokens': 12, 'total_tokens': 12}}
    )
    genai.end_span(llm, status=200)
    span = _only_span(exporter)
    assert span.name == 'embeddings text-embedding-3-small'
    assert span.attributes['gen_ai.operation.name'] == 'embeddings'
    assert span.attributes['gen_ai.usage.input_tokens'] == 12
    assert span.attributes['gen_ai.usage.total_tokens'] == 12
    assert 'gen_ai.usage.output_tokens' not in span.attributes
    assert 'gen_ai.output.type' not in span.attributes
    assert 'gen_ai.input.messages' not in span.attributes


def test_cost_go_sdk_endpoint_format_is_per_token(exporter):
    # {"data": {model: {"input", "output"}}} as served to the OpenLIT Go SDK; prices per token.
    genai.set_pricing_table({'data': {'GPT-4-Custom': {'input': 0.00003, 'output': 0.00006}}})
    try:
        assert genai.compute_cost('chat', 'gpt-4-custom', 1000, 1000) == pytest.approx(0.09)
        assert genai.compute_cost('chat', ' gpt-4-custom ', 1, 0) == pytest.approx(0.00003)  # trim + lowercase
        assert genai.compute_cost('chat', 'openai/gpt-4-custom', 1, 0) == pytest.approx(0.00003)  # prefix fallback
        assert genai.compute_cost('chat', 'unknown-model', 1, 1) is None
        assert genai.compute_cost('chat', 'gpt-4-custom', None, None) is None
    finally:
        genai.set_pricing_table(None)


def test_cost_flat_custom_map_and_key_aliases():
    genai.set_pricing_table(
        {
            'a': {'input': 0.001, 'output': 0.002, 'cache_read': 0.0005},
            'b': {'input_cost_per_token': 0.001, 'output_cost_per_token': 0.002},
            'c': {'inputCostPerToken': 0.001, 'outputCostPerToken': 0.002},
            'bad': {'foo': 1},
        }
    )
    try:
        for model in ('a', 'b', 'c'):
            assert genai.compute_cost('chat', model, 10, 10) == pytest.approx(0.03)
        # cache-read tokens billed at the cache price when known, else the input price
        assert genai.compute_cost('chat', 'a', 10, 0, cached_tokens=10) == pytest.approx(0.005)
        assert genai.compute_cost('chat', 'b', 10, 0, cached_tokens=10) == pytest.approx(0.01)
        assert genai.lookup_price('bad') is None
    finally:
        genai.set_pricing_table(None)


def test_cost_openlit_pricing_json_format_is_per_1k(exporter):
    genai.set_pricing_table(
        {
            'chat': {'GPT-4o': {'promptPrice': 0.0025, 'completionPrice': 0.01, 'cacheReadPrice': 0.00125}},
            'embeddings': {'text-embedding-3-small': 0.00002},
        }
    )
    try:
        assert genai.compute_cost('chat', 'gpt-4o', 1000, 1000) == pytest.approx(0.0125)
        assert genai.compute_cost('chat', 'gpt-4o', 1000, 0, cached_tokens=1000) == pytest.approx(0.00125)
        assert genai.compute_cost('embeddings', 'text-embedding-3-small', 1000, None) == pytest.approx(0.00002)

        llm = _start()
        genai.record_response(llm, {'usage': {'prompt_tokens': 1000, 'completion_tokens': 1000}})
        genai.end_span(llm, status=200)
        assert _only_span(exporter).attributes['gen_ai.usage.cost'] == pytest.approx(0.0125)
    finally:
        genai.set_pricing_table(None)


def test_custom_pricing_overrides_table():
    table = {'data': {'gpt-4o': {'input': 0.0000025, 'output': 0.00001}, 'other': {'input': 0.001, 'output': 0.001}}}
    custom = {'gpt-4o': {'input': 0.0, 'output': 0.0}}  # e.g. a flat-rate contract
    genai.set_pricing_table(table, custom)
    try:
        assert genai.compute_cost('chat', 'gpt-4o', 1000, 1000) == 0.0
        assert genai.compute_cost('chat', 'other', 1, 1) == pytest.approx(0.002)
    finally:
        genai.set_pricing_table(None)


@pytest.mark.asyncio
async def test_load_pricing_table_reads_file_and_inline_custom(monkeypatch, tmp_path):
    table_file = tmp_path / 'pricing.json'
    table_file.write_text(JSONCodec.dumps({'data': {'m1': {'input': 0.001, 'output': 0.001}}}))
    monkeypatch.setattr(genai, 'GENAI_ENABLED', True)
    monkeypatch.setattr(genai, 'OTEL_GENAI_PRICING_JSON', str(table_file))
    monkeypatch.setattr(genai, 'OTEL_GENAI_PRICING_CUSTOM', '{"m2": {"input": 0.002, "output": 0.002}}')
    try:
        await genai.load_pricing_table()
        assert genai.compute_cost('chat', 'm1', 1, 1) == pytest.approx(0.002)
        assert genai.compute_cost('chat', 'm2', 1, 1) == pytest.approx(0.004)
    finally:
        genai.set_pricing_table(None)


@pytest.mark.asyncio
async def test_load_pricing_table_keeps_custom_when_endpoint_fails(monkeypatch):
    monkeypatch.setattr(genai, 'GENAI_ENABLED', True)
    monkeypatch.setattr(genai, 'OTEL_GENAI_PRICING_JSON', '/nonexistent/pricing.json')
    monkeypatch.setattr(genai, 'OTEL_GENAI_PRICING_CUSTOM', '{"m2": {"input": 0.002, "output": 0.002}}')
    try:
        await genai.load_pricing_table()
        assert genai.compute_cost('chat', 'm2', 1, 1) == pytest.approx(0.004)
    finally:
        genai.set_pricing_table(None)


# --------------------------------------------------------------------------- #
# execute_tool spans
# --------------------------------------------------------------------------- #

TOOL_CALL = {
    'id': 'call_1',
    'type': 'function',
    'function': {'name': 'srv_get_schema', 'arguments': '{"dataset_id": "d1"}'},
}


def _mcp_tool():
    return {
        'spec': {'name': 'srv_get_schema', 'description': 'Return the dataset schema', 'parameters': {}},
        'type': 'mcp',
        'server_id': 'srv',
        'client': SimpleNamespace(url='https://mcp.example.com:8443/mcp'),
        'direct': False,
    }


def test_tool_span_mcp_success(exporter):
    ts = genai.start_tool_span(
        tool_call=TOOL_CALL,
        tool=_mcp_tool(),
        arguments={'dataset_id': 'd1'},
        metadata=METADATA,
        user=_user(),
        request=_request({'type': 'jwt', 'jti': 'j'}),
    )
    assert ts is not None
    genai.end_tool_span(ts, result=[{'type': 'text', 'text': 'schema...'}])
    span = _only_span(exporter)
    a = span.attributes
    assert span.name == 'execute_tool srv_get_schema'
    assert span.kind == SpanKind.INTERNAL
    assert span.status.status_code == StatusCode.UNSET
    assert a['gen_ai.operation.name'] == 'execute_tool'
    assert a['gen_ai.tool.name'] == 'srv_get_schema'
    assert a['gen_ai.tool.type'] == 'function'
    assert a['gen_ai.tool.call.id'] == 'call_1'
    assert a['gen_ai.tool.description'] == 'Return the dataset schema'
    # MCP attributes folded into the same span, as the spec asks
    assert a['mcp.method.name'] == 'tools/call'
    assert a['server.address'] == 'mcp.example.com'
    assert a['server.port'] == 8443
    assert a['openwebui.tool.type'] == 'mcp'
    assert a['openwebui.tool.server_id'] == 'srv'
    # correlation and user carried over
    assert a['gen_ai.conversation.id'] == 'c-1'
    assert a['user.id'] == 'u-1'
    assert a['client.auth.type'] == 'jwt'
    # tools mode: no arguments / result payloads
    assert 'gen_ai.tool.call.arguments' not in a
    assert 'gen_ai.tool.call.result' not in a


def test_tool_span_full_mode_records_arguments_and_result(exporter, monkeypatch):
    monkeypatch.setattr(genai, 'OTEL_GENAI_CAPTURE_CONTENT', genai.CAPTURE_FULL)
    ts = genai.start_tool_span(tool_call=TOOL_CALL, tool=_mcp_tool(), arguments={'dataset_id': 'd1'})
    genai.end_tool_span(ts, result={'rows': 3})
    a = _only_span(exporter).attributes
    assert JSONCodec.loads(a['gen_ai.tool.call.arguments']) == {'dataset_id': 'd1'}
    assert JSONCodec.loads(a['gen_ai.tool.call.result']) == {'rows': 3}


@pytest.mark.parametrize(
    'tool,expected',
    [
        ({'type': 'external', 'server': {'url': 'https://tools.example.com/openapi'}}, 'extension'),
        ({'type': '', 'direct': True, 'server': {'url': 'http://localhost:9000'}}, 'extension'),
        ({'type': 'builtin'}, 'function'),
        ({'type': ''}, 'function'),
    ],
)
def test_tool_type_mapping(exporter, tool, expected):
    ts = genai.start_tool_span(tool_call={'id': 'x', 'function': {'name': 't'}}, tool={'spec': {}, **tool})
    genai.end_tool_span(ts, result='ok')
    a = _only_span(exporter).attributes
    assert a['gen_ai.tool.type'] == expected
    if 'server' in tool:
        assert a['server.address'] in ('tools.example.com', 'localhost')
    assert 'mcp.method.name' not in a


def test_tool_span_error_result_marks_failure(exporter):
    ts = genai.start_tool_span(tool_call=TOOL_CALL, tool=_mcp_tool())
    genai.end_tool_span(ts, result={'error': 'boom'})
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'tool_error'


def test_tool_span_error_string_marks_failure(exporter):
    ts = genai.start_tool_span(tool_call=TOOL_CALL, tool=_mcp_tool())
    genai.end_tool_span(ts, result='Error: dataset id "x" is not attached')
    assert _only_span(exporter).status.status_code == StatusCode.ERROR


def test_tool_span_exception(exporter):
    ts = genai.start_tool_span(tool_call=TOOL_CALL, tool=_mcp_tool())
    genai.end_tool_span(ts, error=RuntimeError('mcp down'))
    genai.end_tool_span(ts, result={'error': 'mcp down'})  # the loop calls end again with the error dict; ignored
    span = _only_span(exporter)
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'RuntimeError'
    assert any(e.name == 'exception' for e in span.events)


def test_tool_not_found_emits_failed_span(exporter):
    assert genai.start_tool_span(tool_call=TOOL_CALL, tool=None) is None
    span = _only_span(exporter)
    assert span.name == 'execute_tool srv_get_schema'
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes['error.type'] == 'tool_error'


def test_tool_span_prompt_based_call_shape(exporter):
    # chat_completion_tools_handler passes {'name': ...} with no id
    ts = genai.start_tool_span(
        tool_call={'name': 'calc'}, tool={'spec': {'description': 'd'}, 'type': ''}, arguments={'x': 1}
    )
    genai.end_tool_span(ts, result='2')
    a = _only_span(exporter).attributes
    assert a['gen_ai.tool.name'] == 'calc'
    assert 'gen_ai.tool.call.id' not in a


def test_tool_span_nests_inner_spans(exporter, monkeypatch):
    ts = genai.start_tool_span(tool_call=TOOL_CALL, tool=_mcp_tool())
    with genai.use_span(ts):
        inner = genai._tracer.start_span('http request')
        inner.end()
    genai.end_tool_span(ts, result='ok')
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans['http request'].parent.span_id == spans['execute_tool srv_get_schema'].context.span_id
