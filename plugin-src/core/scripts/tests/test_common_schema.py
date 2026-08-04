"""common.py validate_schema 关键字单元测试（R1 必修 2）。

覆盖新增关键字：allOf, anyOf, oneOf, not, contains, maxItems, if/then/else。
对未支持关键字断言 ContractError（失败关闭）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from common import ContractError, validate_schema  # noqa: E402


class AllOfTests(unittest.TestCase):
    def test_allof_pass(self):
        schema = {"allOf": [{"type": "object", "required": ["a"]}, {"type": "object", "required": ["b"]}]}
        validate_schema({"a": 1, "b": 2}, schema)

    def test_allof_fail(self):
        schema = {"allOf": [{"type": "object", "required": ["a"]}, {"type": "object", "required": ["b"]}]}
        with self.assertRaises(ContractError):
            validate_schema({"a": 1}, schema)


class AnyOfTests(unittest.TestCase):
    def test_anyof_pass(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        validate_schema("hello", schema)

    def test_anyof_fail(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        with self.assertRaises(ContractError):
            validate_schema([], schema)


class OneOfTests(unittest.TestCase):
    def test_oneof_pass(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        validate_schema(42, schema)

    def test_oneof_fail_none(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        with self.assertRaises(ContractError):
            validate_schema([], schema)

    def test_oneof_fail_multiple(self):
        schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
        with self.assertRaises(ContractError):
            validate_schema(42, schema)  # matches both


class NotTests(unittest.TestCase):
    def test_not_pass(self):
        schema = {"not": {"type": "string"}}
        validate_schema(42, schema)

    def test_not_fail(self):
        schema = {"not": {"type": "string"}}
        with self.assertRaises(ContractError):
            validate_schema("hello", schema)


class ContainsTests(unittest.TestCase):
    def test_contains_pass(self):
        schema = {"type": "array", "contains": {"type": "integer", "minimum": 5}}
        validate_schema([1, 2, 10], schema)

    def test_contains_fail(self):
        schema = {"type": "array", "contains": {"type": "integer", "minimum": 5}}
        with self.assertRaises(ContractError):
            validate_schema([1, 2, 3], schema)


class MaxItemsTests(unittest.TestCase):
    def test_maxitems_pass(self):
        schema = {"type": "array", "maxItems": 3}
        validate_schema([1, 2], schema)

    def test_maxitems_fail(self):
        schema = {"type": "array", "maxItems": 2}
        with self.assertRaises(ContractError):
            validate_schema([1, 2, 3], schema)


class IfThenElseTests(unittest.TestCase):
    def test_if_then_pass(self):
        schema = {
            "type": "object",
            "if": {"properties": {"status": {"const": "COMPLETED"}}, "required": ["status"]},
            "then": {"required": ["outputs"]},
        }
        validate_schema({"status": "COMPLETED", "outputs": [1]}, schema)

    def test_if_then_fail(self):
        schema = {
            "type": "object",
            "if": {"properties": {"status": {"const": "COMPLETED"}}, "required": ["status"]},
            "then": {"required": ["outputs"]},
        }
        with self.assertRaises(ContractError):
            validate_schema({"status": "COMPLETED"}, schema)

    def test_if_else_pass(self):
        schema = {
            "type": "object",
            "if": {"properties": {"status": {"const": "COMPLETED"}}, "required": ["status"]},
            "then": {"required": ["outputs"]},
            "else": {"required": ["error"]},
        }
        validate_schema({"status": "FAILED", "error": "something"}, schema)

    def test_if_else_fail(self):
        schema = {
            "type": "object",
            "if": {"properties": {"status": {"const": "COMPLETED"}}, "required": ["status"]},
            "then": {"required": ["outputs"]},
            "else": {"required": ["error"]},
        }
        with self.assertRaises(ContractError):
            validate_schema({"status": "FAILED"}, schema)

    def test_if_condition_not_met_no_then(self):
        """if 条件不满足且无 else → 通过。"""
        schema = {
            "type": "object",
            "if": {"properties": {"status": {"const": "COMPLETED"}}, "required": ["status"]},
            "then": {"required": ["outputs"]},
        }
        validate_schema({"status": "FAILED"}, schema)


class UnsupportedKeywordTests(unittest.TestCase):
    def test_unsupported_keyword_fails_closed(self):
        """遇到未支持关键字必须 ContractError。"""
        schema = {"type": "string", "customKeyword": True}
        with self.assertRaises(ContractError) as ctx:
            validate_schema("hello", schema)
        self.assertIn("unsupported", str(ctx.exception).lower())
        self.assertIn("customKeyword", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
