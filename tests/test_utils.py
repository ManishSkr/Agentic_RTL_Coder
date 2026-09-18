"""Unit tests for utility functions in agentic_rtl_coder."""

from __future__ import annotations

import json
import os

# Import the module under test
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agentic_rtl_coder as arc


# ─────────────────────────────────────────────────────────────────────────
# extract_code_block
# ─────────────────────────────────────────────────────────────────────────
class TestExtractCodeBlock:
    """Tests for ``extract_code_block``."""

    def test_empty_input(self):
        assert arc.extract_code_block("") == ""
        assert arc.extract_code_block(None) == ""

    def test_fenced_verilog_block(self):
        text = textwrap.dedent("""\
            Here is the code:
            ```verilog
            module adder(input a, input b, output sum);
              assign sum = a + b;
            endmodule
            ```
            That's all.
        """)
        result = arc.extract_code_block(text, lang_hint="verilog")
        assert "module adder" in result
        assert "endmodule" in result
        assert "Here is the code" not in result

    def test_fenced_block_no_lang(self):
        text = "```\nmodule top; endmodule\n```"
        result = arc.extract_code_block(text)
        assert "module top" in result

    def test_fallback_to_module_keyword(self):
        text = textwrap.dedent("""\
            Some explanation here.
            module counter(input clk, output [3:0] count);
              // body
            endmodule
        """)
        result = arc.extract_code_block(text)
        assert result.startswith("module counter")
        assert "Some explanation" not in result

    def test_fallback_to_full_text(self):
        text = "assign out = in;"
        result = arc.extract_code_block(text)
        assert result == "assign out = in;"

    def test_multiple_fenced_blocks_returns_first(self):
        text = "```verilog\nmodule a; endmodule\n```\n```verilog\nmodule b; endmodule\n```"
        result = arc.extract_code_block(text, lang_hint="verilog")
        assert "module a" in result
        assert "module b" not in result


# ─────────────────────────────────────────────────────────────────────────
# sanitize_backtick_includes
# ─────────────────────────────────────────────────────────────────────────
class TestSanitizeBacktickIncludes:
    """Tests for ``sanitize_backtick_includes``."""

    def test_missing_backtick(self):
        text = 'include "header.vh"\nmodule top; endmodule'
        result = arc.sanitize_backtick_includes(text)
        assert '`include "header.vh"' in result

    def test_escaped_backtick(self):
        text = '\\`include "header.vh"\nmodule top; endmodule'
        result = arc.sanitize_backtick_includes(text)
        assert '`include "header.vh"' in result
        assert "\\`" not in result

    def test_correct_include_unchanged(self):
        text = '`include "header.vh"\nmodule top; endmodule'
        result = arc.sanitize_backtick_includes(text)
        assert '`include "header.vh"' in result

    def test_no_include(self):
        text = "module top; endmodule"
        result = arc.sanitize_backtick_includes(text)
        assert result == text

    def test_indented_include(self):
        text = '  include "defs.vh"'
        result = arc.sanitize_backtick_includes(text)
        assert '  `include "defs.vh"' in result


# ─────────────────────────────────────────────────────────────────────────
# write_text_file
# ─────────────────────────────────────────────────────────────────────────
class TestWriteTextFile:
    """Tests for ``write_text_file``."""

    def test_creates_file(self, tmp_path: Path):
        target = tmp_path / "out.txt"
        arc.write_text_file(target, "hello world")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "a" / "b" / "c" / "out.txt"
        arc.write_text_file(target, "nested")
        assert target.read_text(encoding="utf-8") == "nested"

    def test_none_content(self, tmp_path: Path):
        target = tmp_path / "empty.txt"
        arc.write_text_file(target, None)
        assert target.read_text(encoding="utf-8") == ""


# ─────────────────────────────────────────────────────────────────────────
# DesignState dataclass
# ─────────────────────────────────────────────────────────────────────────
class TestDesignState:
    """Tests for the ``DesignState`` dataclass."""

    def test_construction(self, tmp_path: Path):
        state = arc.DesignState(
            user_prompt="make a counter",
            work_dir=str(tmp_path),
            work_dir_path=tmp_path,
            run_started="2025-01-01T00:00:00",
        )
        assert state.user_prompt == "make a counter"
        assert state.lint_passed is False
        assert state.simulation_passed is False
        assert state.rtl_retries == 0
        assert state.tb_retries == 0
        assert state.llm_calls == []

    def test_defaults_are_independent(self, tmp_path: Path):
        """Ensure mutable default (llm_calls) is not shared across instances."""
        s1 = arc.DesignState("a", str(tmp_path), tmp_path, "t1")
        s2 = arc.DesignState("b", str(tmp_path), tmp_path, "t2")
        s1.llm_calls.append("call1")
        assert s2.llm_calls == []


# ─────────────────────────────────────────────────────────────────────────
# check_tool / require_tools
# ─────────────────────────────────────────────────────────────────────────
class TestToolChecks:
    """Tests for tool availability helpers."""

    def test_check_tool_finds_python(self):
        # python/python3 should always be available in a test environment
        assert arc.check_tool("python") or arc.check_tool("python3")

    def test_check_tool_missing(self):
        assert arc.check_tool("__nonexistent_tool_xyz__") is False

    def test_require_tools_exits_on_missing(self):
        with pytest.raises(SystemExit) as exc_info:
            arc.require_tools("__nonexistent_tool_xyz__")
        assert exc_info.value.code == 2


# ─────────────────────────────────────────────────────────────────────────
# run_proc
# ─────────────────────────────────────────────────────────────────────────
class TestRunProc:
    """Tests for ``run_proc``."""

    def test_successful_command(self):
        result = arc.run_proc(["python", "-c", "print('hello')"])
        assert result["rc"] == 0
        assert "hello" in result["stdout"]

    def test_failing_command(self):
        result = arc.run_proc(["python", "-c", "import sys; sys.exit(42)"])
        assert result["rc"] == 42

    def test_file_not_found(self):
        result = arc.run_proc(["__nonexistent_binary__"])
        assert result["rc"] is None
        assert result["stderr"]

    def test_timeout(self):
        result = arc.run_proc(
            ["python", "-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        assert result["rc"] == -1
        assert "Timeout" in result["stderr"]


# ─────────────────────────────────────────────────────────────────────────
# extract_test_vectors
# ─────────────────────────────────────────────────────────────────────────
class TestExtractTestVectors:
    """Tests for ``extract_test_vectors``."""

    def test_extracts_urandom_range(self, tmp_path: Path):
        tb_code = textwrap.dedent("""\
            module tb;
              initial begin
                data = $urandom_range(0, 255);
              end
            endmodule
        """)
        tb_file = tmp_path / "testbench.v"
        tb_file.write_text(tb_code, encoding="utf-8")

        state = arc.DesignState("test", str(tmp_path), tmp_path, "t")
        state.tb_file = str(tb_file)

        arc.extract_test_vectors(tmp_path, state)
        vectors_file = tmp_path / "test_vectors.txt"
        assert vectors_file.exists()
        content = vectors_file.read_text(encoding="utf-8")
        assert "urandom_range: 0, 255" in content

    def test_extracts_loop_cycles(self, tmp_path: Path):
        tb_code = textwrap.dedent("""\
            module tb;
              integer i;
              initial begin
                for (i = 0; i < 100; i = i + 1) begin
                  clk = ~clk;
                end
              end
            endmodule
        """)
        tb_file = tmp_path / "testbench.v"
        tb_file.write_text(tb_code, encoding="utf-8")

        state = arc.DesignState("test", str(tmp_path), tmp_path, "t")
        state.tb_file = str(tb_file)

        arc.extract_test_vectors(tmp_path, state)
        vectors_file = tmp_path / "test_vectors.txt"
        assert vectors_file.exists()
        content = vectors_file.read_text(encoding="utf-8")
        assert "cycles: 100" in content

    def test_no_vectors_no_file(self, tmp_path: Path):
        tb_code = "module tb; endmodule\n"
        tb_file = tmp_path / "testbench.v"
        tb_file.write_text(tb_code, encoding="utf-8")

        state = arc.DesignState("test", str(tmp_path), tmp_path, "t")
        state.tb_file = str(tb_file)

        arc.extract_test_vectors(tmp_path, state)
        assert not (tmp_path / "test_vectors.txt").exists()


# ─────────────────────────────────────────────────────────────────────────
# save_design_info
# ─────────────────────────────────────────────────────────────────────────
class TestSaveDesignInfo:
    """Tests for ``save_design_info``."""

    def test_writes_valid_json(self, tmp_path: Path):
        state = arc.DesignState(
            user_prompt="build an ALU",
            work_dir=str(tmp_path),
            work_dir_path=tmp_path,
            run_started="2025-06-01T00:00:00",
        )
        state.lint_passed = True
        state.simulation_passed = True
        state.rtl_retries = 2
        state.tb_retries = 1

        arc.save_design_info(tmp_path, state)
        info_file = tmp_path / "design_info.json"
        assert info_file.exists()

        info = json.loads(info_file.read_text(encoding="utf-8"))
        assert info["user_prompt"] == "build an ALU"
        assert info["lint_passed"] is True
        assert info["simulation_passed"] is True
        assert info["retries"]["rtl_retries"] == 2
        assert info["retries"]["tb_retries"] == 1
        assert "run_finished" in info["timestamps"]
