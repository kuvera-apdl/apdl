"""Tool-calling router: message/tool conversion per provider dialect."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.llm import router


_MESSAGES = [
    {"role": "system", "content": "You investigate."},
    {"role": "user", "content": "Go."},
    {
        "role": "assistant",
        "content": "Checking events.",
        "tool_calls": [
            {"id": "c1", "name": "discover_events", "arguments": {"limit": 5}},
            {"id": "c2", "name": "list_flags", "arguments": {}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "name": "discover_events", "content": '{"events": []}'},
    {"role": "tool", "tool_call_id": "c2", "name": "list_flags", "content": "[]"},
]

_TOOLS = [
    {
        "name": "discover_events",
        "description": "List events.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    }
]


def test_parse_arguments_tolerates_strings_and_garbage():
    assert router._parse_arguments({"a": 1}) == {"a": 1}
    assert router._parse_arguments('{"a": 1}') == {"a": 1}
    assert router._parse_arguments("not json") == {}
    assert router._parse_arguments(None) == {}
    assert router._parse_arguments("[1, 2]") == {}  # non-object JSON


def test_openai_wire_serializes_arguments_as_json_strings():
    wire = router._openai_tool_messages(_MESSAGES)
    assistant = wire[2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"limit": 5}'
    assert assistant["tool_calls"][0]["type"] == "function"
    tool_msg = wire[3]
    assert tool_msg == {"role": "tool", "tool_call_id": "c1", "content": '{"events": []}'}


def test_openai_tools_shape():
    tools = router._openai_tools(_TOOLS)
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "discover_events"
    assert "properties" in tools[0]["function"]["parameters"]


def test_anthropic_conversion_merges_consecutive_tool_results():
    system_text, messages = router._anthropic_tool_messages(_MESSAGES)
    assert system_text == "You investigate."
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    block_types = [b["type"] for b in assistant["content"]]
    assert block_types == ["text", "tool_use", "tool_use"]
    # Anthropic requires ALL tool_results for a turn in ONE user message.
    results = messages[2]
    assert results["role"] == "user"
    assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]
    assert len(messages) == 3  # user, assistant, merged-results


def test_google_conversion_uses_function_names_for_responses():
    system_instruction, contents = router._google_tool_contents(_MESSAGES)
    assert system_instruction == "You investigate."
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0] == {"text": "Checking events."}
    assert model_turn["parts"][1]["function_call"]["name"] == "discover_events"
    assert model_turn["parts"][1]["function_call"]["id"] == "c1"
    # Gemini requires all responses from one model turn in a single following
    # user content and uses IDs to disambiguate parallel same-name calls.
    assert contents[2]["parts"][0]["function_response"]["name"] == "discover_events"
    assert contents[2]["parts"][0]["function_response"]["id"] == "c1"
    assert contents[2]["parts"][1]["function_response"]["name"] == "list_flags"
    assert contents[2]["parts"][1]["function_response"]["id"] == "c2"
    assert len(contents) == 3


@pytest.mark.asyncio
async def test_google_call_id_and_thought_signature_survive_round_trip():
    signature = b"opaque-gemini-signature"

    class Models:
        async def generate_content(self, *, model, contents, config):
            part = router.genai_types.Part(
                function_call=router.genai_types.FunctionCall(
                    id="gemini-call-7",
                    name="discover_events",
                    args={"limit": 7},
                ),
                thought_signature=signature,
            )
            content = SimpleNamespace(parts=[part])
            return SimpleNamespace(candidates=[SimpleNamespace(content=content)])

    client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    completion = await router._google_completion_tools(
        "gemini-test", _MESSAGES[:2], _TOOLS, client
    )
    [call] = completion.tool_calls

    assert call.id == "gemini-call-7"
    assert call.thought_signature == signature

    replay = [
        *_MESSAGES[:2],
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "thought_signature": call.thought_signature,
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": '{"events": []}',
        },
    ]
    _, contents = router._google_tool_contents(replay)

    call_part = contents[1]["parts"][0]
    response = contents[2]["parts"][0]["function_response"]
    assert call_part["function_call"]["id"] == "gemini-call-7"
    assert call_part["thought_signature"] == signature
    assert response["id"] == "gemini-call-7"


@pytest.mark.asyncio
async def test_openai_forced_text_keeps_tools_and_disables_new_calls():
    class Completions:
        kwargs: dict | None = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            message = SimpleNamespace(content="final answer", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = await router._openai_completion_tools(
        "gpt-4o",
        _MESSAGES,
        _TOOLS,
        client,
        force_text=True,
    )

    assert result.text == "final answer"
    assert completions.kwargs is not None
    assert completions.kwargs["tools"][0]["function"]["name"] == "discover_events"
    assert completions.kwargs["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_anthropic_forced_text_keeps_tools_for_historical_tool_blocks():
    class Messages:
        kwargs: dict | None = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="final answer")]
            )

    messages_api = Messages()
    client = SimpleNamespace(messages=messages_api)

    result = await router._anthropic_completion_tools(
        "claude-test",
        _MESSAGES,
        _TOOLS,
        client,
        force_text=True,
    )

    assert result.text == "final answer"
    assert messages_api.kwargs is not None
    assert messages_api.kwargs["tools"][0]["name"] == "discover_events"
    assert messages_api.kwargs["tool_choice"] == {"type": "none"}


@pytest.mark.asyncio
async def test_google_forced_text_keeps_tools_and_disables_new_calls():
    class Models:
        config = None

        async def generate_content(self, *, model, contents, config):
            self.config = config
            return SimpleNamespace(candidates=[])

    models = Models()
    client = SimpleNamespace(aio=SimpleNamespace(models=models))

    result = await router._google_completion_tools(
        "gemini-test",
        _MESSAGES,
        _TOOLS,
        client,
        force_text=True,
    )

    assert result == router.ToolCompletion()
    assert models.config.tools is not None
    assert (
        models.config.tool_config.function_calling_config.mode
        is router.genai_types.FunctionCallingConfigMode.NONE
    )


def test_plain_messages_pass_through_all_dialects():
    plain = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]
    assert router._openai_tool_messages(plain) == plain
    system_text, msgs = router._anthropic_tool_messages(plain)
    assert system_text == "s" and [m["role"] for m in msgs] == ["user", "assistant"]
    system_instruction, contents = router._google_tool_contents(plain)
    assert system_instruction == "s"
    assert [c["role"] for c in contents] == ["user", "model"]
