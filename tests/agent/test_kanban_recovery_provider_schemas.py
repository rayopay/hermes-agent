"""Offline production normalizers plus real registered dispatch, not provider acceptance.

The Responses converter explicitly uses strict=False: no strict=True compatibility
is claimed. Ordinary unit fixtures simulate owner context, not executable authority.
"""
import copy
import json

import pytest
from tests.hermes_cli.test_kanban_recovery_surfaces import board, block, tool, request
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_recovery import get_recovery_state


def normalized(provider, schema):
    from tools.schema_sanitizer import sanitize_tool_schemas
    tools = sanitize_tool_schemas([{"type": "function", "function": schema}])
    if provider == "responses-nonstrict":
        from agent.codex_responses_adapter import _responses_tools
        result = _responses_tools(tools)[0]
        assert result["strict"] is False
        return result["parameters"]
    if provider == "anthropic":
        from agent.anthropic_message_convert import convert_tools_to_anthropic
        return convert_tools_to_anthropic(tools)[0]["input_schema"]
    if provider == "moonshot":
        from agent.moonshot_schema import sanitize_moonshot_tools
        return sanitize_moonshot_tools(tools)[0]["function"]["parameters"]
    from agent.gemini_native_adapter import _translate_tools_to_gemini
    is_json = provider == "gemini-json"
    declaration = _translate_tools_to_gemini(tools, json_schema=is_json)[0]["functionDeclarations"][0]
    return declaration["parametersJsonSchema" if is_json else "parameters"]


def assert_schema_relationship(actual, source, provider):
    """Compare recursive constraints, not prose or remote validation behavior."""
    actual_type = actual.get("type")
    # Gemini wire representations may capitalize type names, not field names.
    if provider.startswith("gemini") and isinstance(actual_type, str):
        actual_type = actual_type.lower()
    assert actual_type == source.get("type")
    assert set(actual.get("required", [])) == set(source.get("required", []))
    for key in ("enum", "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength",
                "minProperties", "maxProperties", "pattern"):
        assert (key in actual) == (key in source), key
        if key in source:
            assert actual[key] == source[key], key
    if provider == "gemini-legacy":
        # Production intentionally drops this unsupported legacy keyword.
        assert "additionalProperties" not in actual
    else:
        assert ("additionalProperties" in actual) == ("additionalProperties" in source)
        if "additionalProperties" in source:
            assert actual["additionalProperties"] == source["additionalProperties"]
    assert actual.get("properties", {}).keys() == source.get("properties", {}).keys()
    for name, child in source.get("properties", {}).items():
        assert_schema_relationship(actual["properties"][name], child, provider)
    assert ("items" in actual) == ("items" in source)
    if "items" in source:
        assert_schema_relationship(actual["items"], source["items"], provider)


@pytest.mark.parametrize("provider", ["responses-nonstrict", "anthropic", "moonshot", "gemini-json", "gemini-legacy"])
def test_actual_recovery_shape_and_controlled_dispatch(board, provider):
    import tools.kanban_tools
    from tools.registry import registry
    original = copy.deepcopy(registry.get_schema("kanban_unblock"))
    shape = normalized(provider, copy.deepcopy(original))
    assert_schema_relationship(shape, original["parameters"], provider)
    recovery = shape["properties"]["recovery"]
    source = original["parameters"]["properties"]["recovery"]
    assert recovery["properties"]["action"]["enum"] == source["properties"]["action"]["enum"]
    assert recovery["properties"].keys() == source["properties"].keys()
    refs = recovery["properties"]["settlement_refs"]["items"]
    assert refs["properties"].keys() == source["properties"]["settlement_refs"]["items"]["properties"].keys()
    assert set(refs["required"]) == set(source["properties"]["settlement_refs"]["items"]["required"])
    assert "recovery" not in shape.get("required", [])
    assert registry.get_schema("kanban_unblock") == original

    tid = kb.create_task(board, title="provider controlled response", assignee="worker")
    block(board, tid)
    payload = request(get_recovery_state(board, tid))
    # The normalizers are NOT validators. Feed controlled JSON to the real handler.
    result = tool("kanban_unblock", **json.loads(json.dumps({"task_id": tid, "recovery": payload})))
    assert result["ok"] and result["classified"] and not result["released"]
    assert kb.get_task(board, tid).status == "blocked"
    fresh = request(get_recovery_state(board, tid))
    for invalid in (None, {**fresh, "unknown": True}, {**fresh, "existing_blocker_id": None},
                    {**fresh, "action": "unsupported"}, {**fresh, "settlement_refs": []}):
        before = list(board.iterdump())
        denied = tool("kanban_unblock", task_id=tid, recovery=invalid)
        assert not denied.get("ok"), denied
        assert list(board.iterdump()) == before
    assert tool("kanban_unblock", task_id=tid)["ok"]  # omission, not null
