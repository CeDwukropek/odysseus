import json

import src.agent_tools  # noqa: F401
from src.tool_capabilities import ResultIntegrity, ToolEffect, capabilities_for_tool
from src.tool_schemas import function_call_to_tool_block
from src.tool_utils import get_mcp_manager, set_mcp_manager


class FakeMcpManager:
    def get_all_tools(self):
        return [
            {
                "qualified_name": "mcp__0920f19c__get_bus_schedule",
                "server_id": "0920f19c",
                "name": "get_bus_schedule",
                "annotations": {"readOnlyHint": True, "destructiveHint": False},
            }
        ]


def test_restores_provider_stripped_mcp_prefix_only_for_live_tool():
    previous = get_mcp_manager()
    set_mcp_manager(FakeMcpManager())
    try:
        block = function_call_to_tool_block(
            "0920f19c__get_bus_schedule",
            json.dumps(
                {
                    "from_city": "Rabka-Zdrój",
                    "to_city": "Kraków",
                    "date": "2026-10-05",
                }
            ),
        )
    finally:
        set_mcp_manager(previous)

    assert block is not None
    assert block.tool_type == "mcp__0920f19c__get_bus_schedule"


def test_does_not_promote_unknown_double_underscore_name_to_mcp():
    previous = get_mcp_manager()
    set_mcp_manager(FakeMcpManager())
    try:
        block = function_call_to_tool_block("attacker__run", "{}")
    finally:
        set_mcp_manager(previous)

    assert block is None


def test_live_read_only_mcp_annotation_gets_minimal_network_read_capability():
    previous = get_mcp_manager()
    set_mcp_manager(FakeMcpManager())
    try:
        capabilities = capabilities_for_tool("mcp__0920f19c__get_bus_schedule")
    finally:
        set_mcp_manager(previous)

    assert capabilities.known is True
    assert capabilities.effects == frozenset({ToolEffect.BROKERED_NETWORK_READ})
    assert capabilities.result_integrity is ResultIntegrity.EXTERNAL_UNTRUSTED


def test_unknown_mcp_still_fails_high():
    previous = get_mcp_manager()
    set_mcp_manager(FakeMcpManager())
    try:
        capabilities = capabilities_for_tool("mcp__missing__read_everything")
    finally:
        set_mcp_manager(previous)

    assert capabilities.known is False
    assert ToolEffect.DESTRUCTIVE in capabilities.effects
