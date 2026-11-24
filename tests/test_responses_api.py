def test_a_call_to_a_namespaced_tool_names_its_namespace_again():
    from provenance_core.api.responses_api import call_item, tools_to_openai

    tools, custom, shell = tools_to_openai(
        [
            {
                "type": "namespace",
                "name": "mcp__files",
                "tools": [{"type": "function", "name": "read", "parameters": {}}],
            },
            {"type": "function", "name": "plain", "parameters": {}},
        ]
    )
    assert [t["function"]["name"] for t in tools] == ["read", "plain"]
    item = call_item({"id": "c1", "name": "read", "arguments": "{}"}, custom, shell)
    assert item["namespace"] == "mcp__files"
    assert "namespace" not in call_item({"id": "c2", "name": "plain", "arguments": "{}"}, custom, shell)
