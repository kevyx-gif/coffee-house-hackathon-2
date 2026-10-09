import asyncio
import json

import httpx
import pytest

from coffee_house.hackathon2.catalog import SIZE_ALIASES
from coffee_house.hackathon2.model import (
    DEFAULT_MODEL,
    GROQ_CHAT_URL,
    TOOLS,
    GroqSettings,
    GroqToolRouter,
    ModelUnavailable,
)


def response_body(tool_name="search_menu", arguments=None, *, usage=True):
    message = {"role": "assistant", "content": None}
    if tool_name is not None:
        message["tool_calls"] = [{
            "id": "call_synthetic",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(arguments if arguments is not None else {"query": "latte"}),
            },
        }]
    body = {"choices": [{"message": message}]}
    if usage:
        body["usage"] = {"prompt_tokens": 55, "completion_tokens": 12}
    return body


def make_router(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GroqToolRouter(GroqSettings(api_key="synthetic-test-api-key"), client=client), client


def test_tool_descriptions_separate_menu_search_from_price_and_stock():
    descriptions = {item["function"]["name"]: item["function"]["description"] for item in TOOLS}
    availability_schema = next(item["function"]["parameters"] for item in TOOLS if item["function"]["name"] == "check_availability")
    order_schema = next(item["function"]["parameters"] for item in TOOLS if item["function"]["name"] == "prepare_order")

    assert "No la uses para saber si" in descriptions["search_menu"]
    assert "cuánto cuesta" in descriptions["check_availability"]
    assert "hay, tienen, queda" in descriptions["check_availability"]
    assert "search_menu" in descriptions["check_availability"] or "check_availability" in descriptions["search_menu"]
    assert "temperatura" in availability_schema["properties"]["modifiers"]["description"]
    assert "temperatura" in order_schema["properties"]["items"]["items"]["properties"]["modifiers"]["description"]


def test_router_prompts_only_clear_colloquial_normalizations_and_asks_on_uncertainty():
    availability = next(item["function"]["parameters"] for item in TOOLS if item["function"]["name"] == "check_availability")["properties"]
    order_item = next(item["function"]["parameters"] for item in TOOLS if item["function"]["name"] == "prepare_order")["properties"]["items"]["items"]["properties"]

    assert all(SIZE_ALIASES[alias] == "mediano" for alias in ("medianito", "medianita", "mediani"))
    assert SIZE_ALIASES["grand"] == "grande"
    assert "lechita de avena" in availability["modifiers"]["description"]
    assert "conserva la expresión escrita" in availability["modifiers"]["description"]
    assert "frape de oreo" in availability["product_name"]["description"]
    assert "caliente, frío, hot o iced" in order_item["modifiers"]["description"]


def test_router_sends_only_minimum_message_and_validates_tool_result():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response_body(arguments={"query": "latte"}))

    router, client = make_router(handler)
    try:
        result = asyncio.run(router.route("¿Qué tipos de latte tienen?"))
    finally:
        asyncio.run(client.aclose())

    assert result.name == "search_menu"
    assert result.arguments == {"query": "latte"}
    assert result.input_tokens == 55 and result.output_tokens == 12
    assert seen["url"] == GROQ_CHAT_URL
    assert seen["body"]["model"] == DEFAULT_MODEL
    assert seen["body"]["messages"][-1] == {
        "role": "user", "content": "¿Qué tipos de latte tienen?"
    }
    assert "parallel_tool_calls" in seen["body"] and seen["body"]["parallel_tool_calls"] is False
    assert "+521555" not in json.dumps(seen["body"])
    assert "synthetic-test-api-key" not in repr(GroqSettings(api_key="synthetic-test-api-key"))


def test_router_never_returns_free_form_model_text():
    router, client = make_router(
        lambda _request: httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "invento: cuesta 1 peso"}}]},
        )
    )
    try:
        assert asyncio.run(router.route("hola")) is None
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "tool_name,arguments,expected",
    [
        ("execute_sql", {"query": "delete"}, "unknown_tool"),
        ("search_menu", ["latte"], "invalid_arguments"),
    ],
)
def test_router_rejects_unknown_tools_and_non_object_arguments(tool_name, arguments, expected):
    def handler(_request):
        result = response_body(tool_name, usage=False)
        result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=result)

    router, client = make_router(handler)
    try:
        with pytest.raises(ModelUnavailable) as failure:
            asyncio.run(router.route("hola"))
    finally:
        asyncio.run(client.aclose())
    assert failure.value.code == expected


def test_router_honors_rate_limit_without_retrying_or_exposing_body():
    router, client = make_router(
        lambda _request: httpx.Response(429, headers={"retry-after": "7"}, text="sensitive provider detail")
    )
    try:
        with pytest.raises(ModelUnavailable) as failure:
            asyncio.run(router.route("latte mediano"))
    finally:
        asyncio.run(client.aclose())
    assert failure.value.code == "rate_limited"
    assert failure.value.retry_after == 7
    assert "sensitive provider detail" not in str(failure.value)


def test_router_rejects_unapproved_model_and_bad_input():
    with pytest.raises(ValueError):
        GroqToolRouter(GroqSettings(api_key="synthetic", model="other/model"))

    router, client = make_router(lambda _request: httpx.Response(200, json=response_body()))
    try:
        with pytest.raises(ValueError):
            asyncio.run(router.route(" " * 1))
    finally:
        asyncio.run(client.aclose())
