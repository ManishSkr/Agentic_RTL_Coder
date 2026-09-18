#!/usr/bin/env python3
"""
agentic_rtl_coder.py

Agentic RTL Coder — AI-powered Verilog RTL generation pipeline.

Uses local Ollama models to:
  1. Enhance a hardware design specification
  2. Generate synthesisable Verilog RTL
  3. Lint the RTL with Verilator (self-correcting feedback loop)
  4. Generate a matching testbench
  5. Simulate with Icarus Verilog (self-correcting feedback loop)

Artifacts are written to ``rtl_pipeline_work/`` next to this script.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
log = logging.getLogger("agentic_rtl_coder")

# ---------------------------------------------------------------------------
# Defaults (overridable via CLI)
# ---------------------------------------------------------------------------
DEFAULT_PROMPT_MODEL = "llama3.1:latest"
DEFAULT_RTL_MODEL = "deepseek-coder-v2:latest"
WORKDIR_NAME = "rtl_pipeline_work"
MAX_RETRIES_DEFAULT = 25
OLLAMA_TIMEOUT_DEFAULT = 600  # seconds

VERILATOR_CMD = "verilator"
IVERILOG_CMD = "iverilog"
VVP_CMD = "vvp"


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------
@dataclass
class DesignState:
    """Tracks all mutable state throughout the RTL generation pipeline."""

    user_prompt: str
    work_dir: str
    work_dir_path: Path
    run_started: str

    # Models (configurable per run)
    prompt_model: str = DEFAULT_PROMPT_MODEL
    rtl_model: str = DEFAULT_RTL_MODEL
    ollama_timeout: int = OLLAMA_TIMEOUT_DEFAULT

    # Pipeline outputs
    enhanced_prompt: str = ""
    prompt_raw: Optional[dict] = None
    rtl_file: Optional[str] = None
    tb_file: Optional[str] = None
    last_rtl_text: str = ""
    last_tb_text: str = ""

    # Lint / simulation results
    lint_passed: bool = False
    lint_log: str = ""
    simulation_passed: bool = False
    simulation_log: str = ""

    # Retry counters
    rtl_retries: int = 0
    tb_retries: int = 0

    # Audit trail
    llm_calls: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def bail(msg: str, rc: int = 1) -> None:
    """Log a critical error and exit the process."""
    log.critical(msg)
    sys.exit(rc)


def check_tool(name: str) -> bool:
    """Return ``True`` if *name* is found on ``PATH``."""
    return shutil.which(name) is not None


def require_tools(*names: str) -> None:
    """Exit with a clear message if any required CLI tools are missing."""
    missing = [n for n in names if not check_tool(n)]
    if missing:
        bail(
            "❌ Required tool(s) not found in PATH: "
            + ", ".join(missing)
            + "\n   Please install them and ensure they are on PATH.",
            rc=2,
        )


def run_proc(
    cmd: list[str],
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict:
    """Run a subprocess and return a dict with ``rc``, ``stdout``, ``stderr``."""
    try:
        r = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"rc": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}
    except FileNotFoundError as exc:
        return {"rc": None, "stdout": "", "stderr": str(exc)}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "stdout": "", "stderr": "TimeoutExpired"}


def call_ollama(
    model: str,
    prompt: str,
    timeout: int = OLLAMA_TIMEOUT_DEFAULT,
) -> dict:
    """
    Call a local Ollama model via ``ollama run <model>`` with *prompt* on stdin.

    Returns a dict with ``rc``, ``stdout``, ``stderr``.
    """
    if not check_tool("ollama"):
        bail("❌ Ollama CLI not found. Install Ollama and ensure `ollama` is in PATH.")
    cmd = ["ollama", "run", model]
    log.debug("Calling Ollama model %s (timeout=%ds)", model, timeout)
    return run_proc(cmd, input_text=prompt, timeout=timeout)


def extract_code_block(text: str, lang_hint: Optional[str] = None) -> str:
    """
    Extract the first fenced code block from *text*.

    Falls back to returning everything from the first ``module`` keyword onwards,
    or the entire text if nothing else matches.
    """
    if not text:
        return ""
    # Try triple-backtick fences — with an explicit language hint first,
    # then fall back to any fenced block.
    if lang_hint:
        fence_re = re.compile(
            r"```\s*" + lang_hint + r"\s*\n(.*?)```", re.S | re.I
        )
        m = fence_re.search(text)
        if m:
            return m.group(1).strip()
    # Generic fence: ``` optionally followed by a language tag on the same line
    generic_re = re.compile(r"```[^\n]*\n(.*?)```", re.S)
    m = generic_re.search(text)
    if m:
        return m.group(1).strip()
    # Fall back to first line starting with 'module'
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("module"):
            return "\n".join(lines[i:]).strip()
    # Last resort: return everything
    return text.strip()


def sanitize_backtick_includes(verilog_text: str) -> str:
    """
    Fix common LLM mistakes with Verilog ```include`` directives.

    LLMs frequently emit ``include`` without the backtick, or escape it as
    ``\\`include``.  This function normalises both cases.
    """
    corrected = re.sub(
        r'(?m)^(?P<ws>\s*)(?:include)\s+["<]',
        r'\g<ws>`include "',
        verilog_text,
    )
    corrected = corrected.replace("\\`include", "`include")
    return corrected


def write_text_file(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else "", encoding="utf-8")


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------
def prompt_enhancer(state: DesignState) -> DesignState:
    """Use a language model to expand and clarify the user's design spec."""
    log.info("[Prompt Enhancer] Using %s", state.prompt_model)
    prompt = (
        "Enhance this hardware design specification for RTL generation:\n\n"
        f"{state.user_prompt}\n\n"
        "Provide a concise, implementable spec."
    )
    res = call_ollama(state.prompt_model, prompt, timeout=state.ollama_timeout)
    state.prompt_raw = res
    state.enhanced_prompt = (res["stdout"] or "").strip()
    return state


def rtl_generator(
    state: DesignState,
    lint_feedback: Optional[str] = None,
) -> DesignState:
    """Generate synthesisable Verilog from the enhanced spec."""
    log.info("[RTL Generator] Using %s", state.rtl_model)
    prompt = (
        "Generate synthesizable Verilog RTL for the following specification:\n\n"
        f"{state.enhanced_prompt}\n\n"
        "Requirements: synthesizable, use non-blocking assignments in sequential "
        "blocks where appropriate, avoid compiler-specific directives, produce a "
        "single module."
    )
    if lint_feedback:
        prompt += (
            "\n\nThe previously generated RTL failed lint with these errors:\n"
            f"{lint_feedback}\n"
            "Please fix the RTL to address these errors and produce clean Verilog."
        )

    res = call_ollama(state.rtl_model, prompt, timeout=state.ollama_timeout)
    state.llm_calls.append({"stage": "rtl_generator", "raw": res})
    raw_out = res["stdout"] or ""

    # Persist raw LLM output for auditing
    write_text_file(state.work_dir_path / "rtl_gen_raw.txt", raw_out)

    # Extract and sanitise Verilog
    verilog = extract_code_block(raw_out, lang_hint="verilog")
    verilog = sanitize_backtick_includes(verilog)

    # Last-ditch fallback: ensure there is a module keyword
    if "module" not in verilog:
        verilog = (
            "// Fallback generated module\n"
            "module auto_gen_dummy();\nendmodule\n\n"
            f"/* Original LLM output\n{raw_out}\n*/"
        )

    write_text_file(state.work_dir_path / "design.v", verilog)
    state.rtl_file = str(state.work_dir_path / "design.v")
    state.last_rtl_text = verilog
    return state


def run_lint(state: DesignState) -> DesignState:
    """Lint the generated RTL with Verilator ``--lint-only``."""
    log.info("[Lint] Running Verilator lint-only …")
    rtl_path = state.rtl_file
    if not rtl_path or not Path(rtl_path).exists():
        state.lint_passed = False
        state.lint_log = "No RTL file found."
        write_text_file(state.work_dir_path / "lint.log", state.lint_log)
        return state

    cmd = [VERILATOR_CMD, "--lint-only", rtl_path]
    res = run_proc(cmd)

    lint_out = ""
    if res["stdout"]:
        lint_out += "STDOUT:\n" + res["stdout"] + "\n"
    if res["stderr"]:
        lint_out += "STDERR:\n" + res["stderr"] + "\n"

    write_text_file(state.work_dir_path / "lint.log", lint_out)
    state.lint_log = lint_out
    state.lint_passed = res["rc"] == 0

    if state.lint_passed:
        log.info("[Lint] ✅ Passed")
    else:
        log.warning("[Lint] ❌ Failed (see lint.log)")
    return state


def testbench_generator(
    state: DesignState,
    sim_feedback: Optional[str] = None,
) -> DesignState:
    """Generate a Verilog testbench that exercises the design module."""
    log.info("[Testbench Generator] Using %s", state.rtl_model)
    rtl_text = ""
    if state.rtl_file and Path(state.rtl_file).exists():
        rtl_text = Path(state.rtl_file).read_text(encoding="utf-8")

    prompt = (
        "Write a Verilog testbench for the following RTL module.\n"
        "Include $dumpfile/$dumpvars to produce tb.vcd, run a reasonable "
        "number of cycles, and do basic checks (no Xs). "
        "Provide only the testbench code block.\n\n"
        f"RTL:\n{rtl_text}"
    )
    if sim_feedback:
        prompt += (
            "\n\nThe previous testbench/simulation failed with these errors:\n"
            f"{sim_feedback}\n"
            "Please fix the testbench accordingly."
        )

    res = call_ollama(state.rtl_model, prompt, timeout=state.ollama_timeout)
    state.llm_calls.append({"stage": "testbench_generator", "raw": res})
    raw_tb = res["stdout"] or ""
    write_text_file(state.work_dir_path / "tb_raw.txt", raw_tb)

    tb_code = extract_code_block(raw_tb, lang_hint="verilog")

    # Ensure VCD dumping — wrapped in a proper initial block
    if "$dumpfile" not in tb_code:
        tb_code = (
            'initial begin\n'
            '  $dumpfile("tb.vcd");\n'
            '  $dumpvars(0, tb);\n'
            'end\n\n'
            + tb_code
        )

    write_text_file(state.work_dir_path / "testbench.v", tb_code)
    state.tb_file = str(state.work_dir_path / "testbench.v")
    state.last_tb_text = tb_code
    return state


def simulate(state: DesignState) -> DesignState:
    """Compile and run the design + testbench with Icarus Verilog."""
    log.info("[Simulator] Compiling with Icarus Verilog …")
    rtl = state.rtl_file
    tb = state.tb_file
    if not (rtl and tb):
        state.simulation_passed = False
        state.simulation_log = "Missing RTL or testbench file."
        write_text_file(state.work_dir_path / "simulation.log", state.simulation_log)
        return state

    out_exec = str(state.work_dir_path / "sim.out")
    cmd_compile = [IVERILOG_CMD, "-o", out_exec, rtl, tb]
    comp_res = run_proc(cmd_compile, cwd=state.work_dir)
    compile_log = (comp_res["stdout"] or "") + (comp_res["stderr"] or "")
    write_text_file(state.work_dir_path / "compile.log", compile_log)

    if comp_res["rc"] != 0:
        state.simulation_passed = False
        state.simulation_log = "Compile failed:\n" + compile_log
        write_text_file(state.work_dir_path / "simulation.log", state.simulation_log)
        log.warning("[Simulator] ❌ Compile error (see compile.log)")
        return state

    log.info("[Simulator] Running vvp …")
    run_res = run_proc([VVP_CMD, out_exec], cwd=state.work_dir)
    sim_log = (run_res["stdout"] or "") + (run_res["stderr"] or "")
    write_text_file(state.work_dir_path / "simulation.log", sim_log)
    state.simulation_log = sim_log
    state.simulation_passed = run_res["rc"] == 0

    if state.simulation_passed:
        log.info("[Simulator] ✅ Passed")
    else:
        log.warning("[Simulator] ❌ Runtime error (see simulation.log)")
    return state


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------
def make_readme(workdir_path: Path, state: DesignState) -> None:
    """Write a ``README.md`` inside the work directory describing the artifacts."""
    readme_lines = [
        "# RTL Pipeline Workdir",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()} UTC",
        "",
        "## Files",
        "",
        "| File | Description |",
        "|---|---|",
        "| `design.v` | Generated Verilog RTL (cleaned) |",
        "| `testbench.v` | Generated testbench |",
        "| `tb.vcd` | Waveform dump (if testbench writes it) |",
        "| `sim.out` | Compiled simulation binary |",
        "| `rtl_gen_raw.txt` | Raw LLM output for RTL |",
        "| `tb_raw.txt` | Raw LLM output for testbench |",
        "| `enhanced_prompt.txt` | Enhanced spec from prompt model |",
        "| `lint.log` | Verilator lint output |",
        "| `compile.log` | Icarus Verilog compile output |",
        "| `simulation.log` | vvp runtime output |",
        "| `design_info.json` | Metadata about this run |",
        "",
        "## How to re-run locally",
        "",
        "```bash",
        "iverilog -o sim.out design.v testbench.v",
        "vvp sim.out",
        "gtkwave tb.vcd   # view waveforms",
        "```",
        "",
        "## Notes",
        "",
        "- Raw LLM outputs are preserved for auditing in `rtl_gen_raw.txt` and `tb_raw.txt`.",
        f"- Prompt model: `{state.prompt_model}`",
        f"- RTL model: `{state.rtl_model}`",
    ]
    write_text_file(workdir_path / "README.md", "\n".join(readme_lines))


def save_design_info(workdir_path: Path, state: DesignState) -> None:
    """Write a JSON metadata file summarising the pipeline run."""
    info = {
        "user_prompt": state.user_prompt,
        "enhanced_prompt_present": bool(state.enhanced_prompt),
        "models": {
            "prompt": state.prompt_model,
            "rtl": state.rtl_model,
        },
        "rtl_file": state.rtl_file,
        "tb_file": state.tb_file,
        "lint_passed": state.lint_passed,
        "simulation_passed": state.simulation_passed,
        "timestamps": {
            "run_started": state.run_started,
            "run_finished": datetime.now(timezone.utc).isoformat(),
        },
        "retries": {
            "rtl_retries": state.rtl_retries,
            "tb_retries": state.tb_retries,
        },
        "notes": "Generated by agentic_rtl_coder.py",
    }
    write_text_file(workdir_path / "design_info.json", json.dumps(info, indent=2))


def extract_test_vectors(workdir_path: Path, state: DesignState) -> None:
    """
    Best-effort extraction of test-vector metadata from the testbench.

    This is intentionally simple — it scrapes ``$urandom_range`` parameters and
    ``for``-loop bounds.  A more sophisticated version could parse full
    stimulus/response tables.
    """
    if not state.tb_file or not Path(state.tb_file).exists():
        return
    tb_text = Path(state.tb_file).read_text(encoding="utf-8")
    vectors: list[str] = []

    for m in re.findall(r"\$urandom_range\(([^)]*)\)", tb_text):
        vectors.append(f"urandom_range: {m.strip()}")

    cycles_match = re.search(
        r"for\s*\(\s*.*?;\s*.*?<\s*(\d+)\s*;\s*.*?\)", tb_text
    )
    if cycles_match:
        vectors.append(f"cycles: {cycles_match.group(1)}")

    if vectors:
        write_text_file(workdir_path / "test_vectors.txt", "\n".join(vectors))


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------
def _setup_logging(verbose: bool = False) -> None:
    """Configure structured logging for the pipeline."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")


def main(argv: Optional[list[str]] = None) -> None:
    """Entry-point for the Agentic RTL Coder pipeline."""
    p = argparse.ArgumentParser(
        prog="agentic_rtl_coder",
        description=(
            "Agentic RTL Coder — AI-powered Verilog generation pipeline "
            "(Ollama + Verilator + Icarus Verilog)."
        ),
    )
    p.add_argument(
        "--spec", "-s",
        type=str,
        help="Design specification (text). If omitted you will be prompted interactively.",
    )
    p.add_argument(
        "--prompt-model",
        type=str,
        default=DEFAULT_PROMPT_MODEL,
        help=f"Ollama model for prompt enhancement (default: {DEFAULT_PROMPT_MODEL}).",
    )
    p.add_argument(
        "--rtl-model",
        type=str,
        default=DEFAULT_RTL_MODEL,
        help=f"Ollama model for RTL & testbench generation (default: {DEFAULT_RTL_MODEL}).",
    )
    p.add_argument(
        "--max-retries", "-r",
        type=int,
        default=MAX_RETRIES_DEFAULT,
        help=f"Max retry attempts for RTL and testbench (default: {MAX_RETRIES_DEFAULT}).",
    )
    p.add_argument(
        "--timeout", "-t",
        type=int,
        default=OLLAMA_TIMEOUT_DEFAULT,
        help=f"Timeout in seconds for each Ollama call (default: {OLLAMA_TIMEOUT_DEFAULT}).",
    )
    p.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Preserve existing workdir instead of recreating it.",
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging.",
    )
    args = p.parse_args(argv)

    _setup_logging(verbose=args.verbose)

    # Pre-flight: ensure required CLI tools are available
    require_tools(VERILATOR_CMD, IVERILOG_CMD, VVP_CMD, "ollama")

    # Resolve workdir next to this script
    script_dir = Path(__file__).parent.resolve()
    workdir_path = script_dir / WORKDIR_NAME

    if workdir_path.exists() and not args.keep_workdir:
        log.info("Removing previous workdir: %s", workdir_path)
        shutil.rmtree(workdir_path)
    workdir_path.mkdir(parents=True, exist_ok=True)

    # Initialise pipeline state
    state = DesignState(
        user_prompt=args.spec or input("Enter RTL design spec: ").strip(),
        work_dir=str(workdir_path),
        work_dir_path=workdir_path,
        run_started=datetime.now(timezone.utc).isoformat(),
        prompt_model=args.prompt_model,
        rtl_model=args.rtl_model,
        ollama_timeout=args.timeout,
    )

    max_retries = args.max_retries

    def save_enhanced_prompt_file() -> None:
        write_text_file(workdir_path / "enhanced_prompt.txt", state.enhanced_prompt)

    try:
        # ── Step 1: Enhance the user's spec ───────────────────────────
        state = prompt_enhancer(state)
        save_enhanced_prompt_file()

        # ── Steps 2–3: RTL generation ↔ Verilator lint feedback loop ─
        while True:
            lint_feedback = state.lint_log if state.rtl_retries > 0 else None
            state = rtl_generator(state, lint_feedback=lint_feedback)
            save_enhanced_prompt_file()

            state = run_lint(state)
            if state.lint_passed:
                break

            state.rtl_retries += 1
            if state.rtl_retries > max_retries:
                log.error(
                    "❌ RTL lint retry limit (%d) reached. Aborting.",
                    max_retries,
                )
                break
            log.info(
                "🔁 Lint failed — regenerating RTL (attempt %d/%d)",
                state.rtl_retries,
                max_retries,
            )

        # ── Steps 4–5: Testbench generation ↔ simulation loop ────────
        #    (only runs if lint passed)
        if state.lint_passed:
            while True:
                sim_feedback = (
                    state.simulation_log if state.tb_retries > 0 else None
                )
                state = testbench_generator(state, sim_feedback=sim_feedback)
                state = simulate(state)

                if state.simulation_passed:
                    log.info("✅ Design, linting, and simulation all passed!")
                    break

                state.tb_retries += 1
                if state.tb_retries > max_retries:
                    log.error(
                        "❌ Testbench retry limit (%d) reached. Aborting.",
                        max_retries,
                    )
                    break
                log.info(
                    "🔁 Simulation failed — regenerating testbench (attempt %d/%d)",
                    state.tb_retries,
                    max_retries,
                )

    finally:
        # Always persist artifacts and metadata, regardless of outcome
        save_enhanced_prompt_file()
        make_readme(workdir_path, state)
        save_design_info(workdir_path, state)

        try:
            extract_test_vectors(workdir_path, state)
        except Exception:
            log.debug("Test-vector extraction failed (non-critical).", exc_info=True)

        # Final summary
        log.info("Artifacts written to: %s", workdir_path)
        expected_files = [
            "design.v", "testbench.v", "tb.vcd", "sim.out",
            "rtl_gen_raw.txt", "tb_raw.txt", "enhanced_prompt.txt",
            "lint.log", "compile.log", "simulation.log",
            "design_info.json", "README.md",
        ]
        for f in expected_files:
            exists = (workdir_path / f).exists()
            log.info("  %s %s", "✔" if exists else "✘", f)


if __name__ == "__main__":
    main()
