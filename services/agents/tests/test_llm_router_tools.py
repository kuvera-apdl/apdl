"""Tool-calling router: message/tool conversion per provider dialect."""

from __future__ import annotations

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
    # Gemini matches function_response by NAME, not id.
    assert contents[2]["parts"][0]["function_response"]["name"] == "discover_events"
    assert contents[3]["parts"][0]["function_response"]["name"] == "list_flags"


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
