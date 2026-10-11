"""The real OpenAI client, exercised against the official SDK with no network."""

from types import SimpleNamespace

import httpx2
import openai
import pytest

from app.ai.client import LLMError, OpenAIResponsesClient, client_for, user_hash
from app.ai.schemas import JobAnalysisOut
from tests.conftest import make_settings

ANALYSIS = JobAnalysisOut(role_title="Data Analyst", seniority=None, requirements=[])


def make_client(**overrides):
    return OpenAIResponsesClient(make_settings(openai_api_key="sk-test-not-real", **overrides))


def fake_response(parsed=ANALYSIS, status="completed", refusal=False):
    content = [SimpleNamespace(type="refusal" if refusal else "output_text")]
    usage = SimpleNamespace(
        input_tokens=1200,
        output_tokens=300,
        input_tokens_details=SimpleNamespace(cached_tokens=200, cache_write_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=120),
    )
    return SimpleNamespace(
        output_parsed=parsed,
        status=status,
        output=[SimpleNamespace(content=content)],
        usage=usage,
        model="gpt-5.6-luna",
    )


def call(client, **kwargs):
    return client.parse(
        instructions="Extract requirements.",
        payload={"job_description": "SQL"},
        schema=JobAnalysisOut,
        max_output_tokens=4000,
        user_hash=user_hash("user-1"),
        **kwargs,
    )


def test_request_is_built_privately_and_with_structured_output(monkeypatch):
    client = make_client()
    captured = {}

    def parse(**kwargs):
        captured.update(kwargs)
        return fake_response()

    monkeypatch.setattr(client._client.responses, "parse", parse)
    result = call(client)
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["store"] is False
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["text_format"] is JobAnalysisOut
    assert captured["max_output_tokens"] == 4000
    assert captured["safety_identifier"] == user_hash("user-1") != "user-1"
    assert "temperature" not in captured
    assert captured["input"][0]["content"] == '{"job_description": "SQL"}'
    assert result.output == ANALYSIS and result.model == "gpt-5.6-luna"
    assert (
        result.usage.input_tokens,
        result.usage.cached_input_tokens,
        result.usage.reasoning_tokens,
    ) == (1200, 200, 120)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (fake_response(status="incomplete"), "invalid_output"),
        (fake_response(refusal=True), "refused"),
        (fake_response(parsed=None), "invalid_output"),
    ],
)
def test_bad_responses_raise_with_billed_usage(monkeypatch, response, code):
    client = make_client()
    monkeypatch.setattr(client._client.responses, "parse", lambda **kwargs: response)
    with pytest.raises(LLMError) as excinfo:
        call(client)
    assert excinfo.value.code == code
    assert excinfo.value.usage is not None and excinfo.value.usage.output_tokens == 300


def _status_error(cls, status):
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    return cls("error", response=httpx2.Response(status, request=request), body=None)


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (
            lambda: openai.APITimeoutError(
                request=httpx2.Request("POST", "https://api.openai.com")
            ),
            "timeout",
        ),
        (lambda: _status_error(openai.RateLimitError, 429), "rate_limited"),
        (lambda: _status_error(openai.AuthenticationError, 401), "unavailable"),
        (lambda: _status_error(openai.InternalServerError, 500), "unavailable"),
        (
            lambda: openai.APIConnectionError(
                request=httpx2.Request("POST", "https://api.openai.com")
            ),
            "unavailable",
        ),
    ],
)
def test_sdk_errors_map_to_safe_codes(monkeypatch, exception, code):
    client = make_client()

    def fail(**kwargs):
        raise exception()

    monkeypatch.setattr(client._client.responses, "parse", fail)
    with pytest.raises(LLMError) as excinfo:
        call(client)
    assert excinfo.value.code == code


def test_provider_selection():
    assert client_for(make_settings()) is None  # no key: AI disabled
    assert client_for(make_settings(openai_api_key="sk-test-not-real")).provider == "openai"
    assert client_for(make_settings(llm_provider="demo")).provider == "demo"
    assert make_client(openai_reasoning_effort="medium")._effort == "medium"


def test_os_trust_store_is_opt_in_and_keeps_verification(monkeypatch):
    import ssl

    import truststore

    built = []

    class RecordingClient(openai.DefaultHttpxClient):
        def __init__(self, **kwargs):
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(openai, "DefaultHttpxClient", RecordingClient)
    make_client()
    assert built == []  # default: the SDK's own bundled certificates
    make_client(use_os_trust_store=True)
    context = built[0]["verify"]
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
