import json

from kaze_calendar.server import create_server


async def test_mcp_tools_expose_read_write_validation_and_exact_ack(store):
    server = create_server(store=store)
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert names == {
        "calendar_manage",
        "calendar_query",
        "poll_calendars",
        "calendar_status",
        "todo_create",
        "todo_list",
        "todo_update",
        "get_proactive_events",
        "validate_proactive_events",
        "acknowledge_events",
    }
    schema = next(tool for tool in tools if tool.name == "todo_update").input_schema
    assert schema["properties"]["changes"]["type"] == "object"
    result = await server.call_tool(
        "todo_create", {"title": "任务", "due_at": "2026-10-16T16:50:00+08:00"}
    )
    content = "".join(item.text for item in result.content if item.type == "text")
    assert json.loads(content)["status"] == "open"
