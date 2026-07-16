import argparse
import importlib.util
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "plugin_eval_runner", ROOT / "tools" / "evals" / "run.py"
)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class AssertionValidationTests(unittest.TestCase):
    def validation_errors(self, assertions):
        errors = []
        RUNNER.validate_assertions(assertions, "example", errors)
        return errors

    def test_contains_requires_a_nonempty_value(self):
        errors = self.validation_errors([{"type": "contains"}])
        self.assertTrue(any("'value' must be a non-empty string" in error for error in errors))

    def test_contains_all_requires_nonempty_string_values(self):
        errors = self.validation_errors([{"type": "contains_all", "values": []}])
        self.assertTrue(any("'values' must be a non-empty array" in error for error in errors))

    def test_numeric_assertions_reject_invalid_values(self):
        errors = self.validation_errors([
            {"type": "min_words", "value": "ten"},
            {"type": "max_quoted_words", "value": "25"},
            {"type": "between_words", "min": 5, "max": 2},
        ])
        self.assertTrue(any("non-negative integer" in error for error in errors))
        self.assertTrue(any("less than or equal" in error for error in errors))

    def test_quotes_require_source_validates_its_regex(self):
        errors = self.validation_errors([
            {"type": "quotes_require_source", "source_regex": "["},
        ])
        self.assertTrue(any("invalid source_regex" in error for error in errors))

    def test_file_content_assertion_requires_value(self):
        errors = self.validation_errors([
            {"type": "file_contains", "path": "result.md"},
        ])
        self.assertTrue(any("'value' must be a non-empty string" in error for error in errors))

    def test_invalid_assertion_aborts_before_command_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            suite_path = temp_path / "invalid.json"
            sentinel = temp_path / "command-ran"
            suite_path.write_text(json.dumps({
                "name": "invalid",
                "tests": [{
                    "id": "false_green",
                    "input": "hello",
                    "assertions": [{"type": "contains"}],
                }],
            }))
            args = argparse.Namespace(
                suite=str(suite_path),
                adapter="command",
                command_template=f"touch {shlex.quote(str(sentinel))}",
                command_response_format="text",
                plugin_dir=None,
                model=None,
                timeout_sec=10,
                retries=1,
                retry_delay_sec=0,
                max_tests=None,
                include_tag=[],
                exclude_tag=[],
                allow_empty=False,
                keep_workspaces=False,
                print_responses=False,
            )
            with self.assertRaises(RUNNER.SuiteError):
                RUNNER.run_suite(args)
            self.assertFalse(sentinel.exists())


class FileUnchangedGlobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        (self.workspace / "root.md").write_text("root\n")
        (self.workspace / "nested").mkdir()
        (self.workspace / "nested" / "nested.md").write_text("nested\n")
        self.baseline = RUNNER.hash_tree(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def unchanged(self, pattern):
        return RUNNER.evaluate_file_assertion(
            {"type": "file_unchanged", "path": pattern},
            self.workspace,
            self.baseline,
        )

    def test_double_star_includes_modified_root_file(self):
        (self.workspace / "root.md").write_text("changed\n")
        result = self.unchanged("**/*.md")
        self.assertFalse(result.passed)
        self.assertIn("modified: root.md", result.message)

    def test_single_star_does_not_include_nested_file(self):
        (self.workspace / "nested" / "nested.md").write_text("changed\n")
        self.assertTrue(self.unchanged("*.md").passed)

    def test_single_star_still_detects_root_file_change(self):
        (self.workspace / "root.md").write_text("changed\n")
        self.assertFalse(self.unchanged("*.md").passed)


class QuoteLimitTests(unittest.TestCase):
    def test_multiline_quote_is_checked_in_full(self):
        response = 'Prefix "one two three\nfour five six" suffix'
        result = RUNNER.evaluate_assertion(
            {"type": "max_quoted_words", "value": 5}, response
        )
        self.assertFalse(result.passed)

    def test_quote_longer_than_one_thousand_characters_is_checked(self):
        response = '"' + ("word " * 250) + '"'
        result = RUNNER.evaluate_assertion(
            {"type": "max_quoted_words", "value": 100}, response
        )
        self.assertFalse(result.passed)


class CommandAdapterTests(unittest.TestCase):
    def test_nonzero_exit_is_an_adapter_error_even_with_expected_output(self):
        python = shlex.quote(sys.executable)
        command = f"{python} -c 'import sys; print(\"EXPECTED\"); sys.exit(7)'"
        adapter = RUNNER.CommandAdapter(command)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(RUNNER.AdapterError, "status 7"):
                adapter.send("ignored", None, Path(temp), [], 10)


if __name__ == "__main__":
    unittest.main()
