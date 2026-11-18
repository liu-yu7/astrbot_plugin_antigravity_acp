"""Dependency-free regression tests for agy's Gemini-facing enum projection."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agy_acp import history as H


def tool(schema, description="Harmless diagnostic tool."):
    return {"name": "diagnostic", "description": description, "inputSchema": schema}


class BridgeEnumTests(unittest.TestCase):
    def test_zero_enum_retains_numeric_type_and_exact_bounds(self):
        source = tool({"type": "object", "properties": {
            "limit": {"type": "integer", "enum": [0], "minimum": 0, "default": 0},
            "excerpt_offset": {"type": "integer", "enum": [0]},
        }, "required": ["limit"]})
        before = copy.deepcopy(source)
        wire = H.compact_bridge_tool(source)
        for name in ("limit", "excerpt_offset"):
            prop = wire["inputSchema"]["properties"][name]
            self.assertNotIn("enum", prop)
            self.assertEqual("integer", prop["type"])
            self.assertEqual((0, 0), (prop["minimum"], prop["maximum"]))
        self.assertEqual(["limit"], wire["inputSchema"]["required"])
        self.assertEqual(before, source, "host tool metadata must not be mutated")

    def test_valid_string_enums_are_unchanged(self):
        schema = {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "read"]},
            "enum": {"type": "string", "enum": ["literal-field-name"]},
        }, "additionalProperties": False}
        self.assertEqual(schema, H.compact_bridge_tool(tool(schema))["inputSchema"])
        self.assertNotIn("astrbot_tool_contracts", H.bridge_system_prompt("SYS", [tool(schema)]))

    def test_multiple_numeric_boolean_and_nullable_values_keep_contract(self):
        schema = {"type": "object", "properties": {
            "level": {"type": "number", "enum": [0, 2.5]},
            "enabled": {"type": "boolean", "enum": [False]},
            "optional": {"type": ["integer", "null"], "enum": [0, None]},
        }}
        wire = H.compact_bridge_tool(tool(schema))["inputSchema"]["properties"]
        for name in wire:
            self.assertNotIn("enum", wire[name])
        self.assertEqual("boolean", wire["enabled"]["type"])
        self.assertNotIn("minimum", wire["enabled"])
        self.assertNotIn("maximum", wire["level"])
        system = H.bridge_system_prompt("SYS", [tool(schema)])
        self.assertIn(json.dumps(schema, ensure_ascii=False), system)
        self.assertIn('"enum": [false]', system)
        self.assertIn('"enum": [0, null]', system)

    def test_nested_schema_nodes_are_normalized_not_data_literals(self):
        literal = {"type": "integer", "enum": [0]}
        schema = {"type": "object", "properties": {
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "offset": {"type": "integer", "enum": [0]},
            }}},
            "choice": {"anyOf": [{"type": "number", "enum": [0]}, {"type": "string", "enum": ["all"]}]},
            "metadata": {"type": "object", "default": literal, "examples": [literal]},
        }, "$defs": {"offset": {"type": "integer", "enum": [0]}},
            "additionalProperties": {"type": "integer", "enum": [0]}}
        wire = H.compact_bridge_tool(tool(schema))["inputSchema"]
        self.assertNotIn("enum", wire["properties"]["rows"]["items"]["properties"]["offset"])
        self.assertNotIn("enum", wire["properties"]["choice"]["anyOf"][0])
        self.assertEqual(["all"], wire["properties"]["choice"]["anyOf"][1]["enum"])
        self.assertNotIn("enum", wire["$defs"]["offset"])
        self.assertNotIn("enum", wire["additionalProperties"])
        self.assertEqual(literal, wire["properties"]["metadata"]["default"])
        self.assertEqual([literal], wire["properties"]["metadata"]["examples"])

    def test_existing_conflicting_bounds_are_not_weakened(self):
        source = tool({"type": "integer", "enum": [0], "minimum": 2, "maximum": 10})
        wire = H.compact_bridge_tool(source)["inputSchema"]
        self.assertEqual((2, 0), (wire["minimum"], wire["maximum"]))

    def test_empty_string_and_mixed_enum_contracts_remain_exact(self):
        for enum in ([""], ["", "read"], ["read", 0]):
            with self.subTest(enum=enum):
                source = tool({"type": "string", "enum": enum})
                self.assertNotIn("enum", H.compact_bridge_tool(source)["inputSchema"])
                self.assertIn(json.dumps(source["inputSchema"]), H.bridge_system_prompt("SYS", [source]))

    def test_compaction_keeps_original_enum_contract_and_description(self):
        source = tool({"type": "object", "properties": {
            "limit": {"type": "integer", "enum": [0], "description": "Offset rules. " * 400},
        }}, "Diagnostic instructions. " * 300)
        wire = H.compact_bridge_tool(source)
        self.assertLessEqual(H._wire_bytes(wire), H.MAX_EAGER_BRIDGE_TOOL_BYTES)
        self.assertNotIn("enum", wire["inputSchema"]["properties"]["limit"])
        system = H.bridge_system_prompt("SYS", [source])
        self.assertIn(source["description"].strip(), system)
        self.assertIn('"enum": [0]', system)
        self.assertIn(source["inputSchema"]["properties"]["limit"]["description"], system)

    def test_openai_descriptors_keep_raw_contract_until_wire_projection(self):
        params = {"type": "object", "properties": {"limit": {"type": "integer", "enum": [0]}}}
        raw = H.raw_bridge_tools([{"function": {"name": "memory_sources", "parameters": params}}])
        self.assertEqual(params, raw[0]["inputSchema"])
        self.assertNotIn("enum", H.compact_bridge_tool(raw[0])["inputSchema"]["properties"]["limit"])


if __name__ == "__main__":
    unittest.main()
