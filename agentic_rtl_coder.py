#!/usr/bin/env python3
"""
rtl_pipeline_full.py

Complete RTL pipeline using local Ollama models:
 - llama3.1 for prompt enhancement
 - deepseek-coder for RTL and testbench generation

Produces a complete artifact folder `rtl_pipeline_work/` next to this script:
 - design.v
 - testbench.v
 - tb.vcd
 - sim.out
 - rtl_gen_raw.txt
 - enhanced_prompt.txt
 - lint.log
 - simulation.log
 - design_info.json
 - README.md
 - test_vectors.txt (best-effort extraction)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# -------------------------
# Configuration / Defaults
# -------------------------
LLAMA_PROMPT_MODEL = "llama3.1:latest"
DEEPSEEK_MODEL = "deepseek-coder-v2:latest"
WORKDIR_NAME = "rtl_pipeline_work"
MAX_RETRIES_DEFAULT = 25
VERILATOR_CMD = "verilator"
IVERILOG_CMD = "iverilog"
VVP_CMD = "vvp"

# -------------------------
# Utility helpers
# -------------------------
class DesignState(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)
    def __setattr__(self, k, v):
        self[k] = v

def bail(msg: str, rc: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(rc)

def check_tool(name: str) -> bool:
    return shutil.which(name) is not None

def require_tool(name: str):
    if not check_tool(name):
        bail(f"❌ Required tool '{name}' not found. Please install it and ensure it is in PATH.")

def run_proc(cmd, cwd=None, input_text: Optional[str]=None, timeout: Optional[int]=None):
    """Run subprocess and return dict with returncode/stdout/stderr."""
    try:
        r = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "stdout": r.stdout or "", "stderr": r.stderr or ""}
    except FileNotFoundError as e:
        return {"rc": None, "stdout": "", "stderr": str(e)}
    except subprocess.TimeoutExpired as e:
        return {"rc": -1, "stdout": e.stdout or "", "stderr": "TimeoutExpired"}

def call_ollama(model: str, prompt: str) -> dict:
    """
    Call local Ollama model: `ollama run <model>` with prompt on stdin.
    Returns dict {"rc", "stdout", "stderr"}.
    """
    if shutil.which("ollama") is None:
        bail("❌ Ollama CLI not found. Install Ollama and ensure `ollama` is in PATH.")
    cmd = ["ollama", "run", model]
    return run_proc(cmd, input_text=prompt)

def extract_code_block(text: str, lang_hint: Optional[str] = None) -> str:
    """Extract fenced code block or return from first 'module' occurrence."""
    if not text:
        return ""
    # First try triple-backtick fences
    fence_re = re.compile(r"```(?:\s*" + (lang_hint or r"[^\n`]*") + r")?\s*(.*?)```", re.S | re.I)
    m = fence_re.search(text)
    if m:
        return m.group(1).strip()
    # Otherwise, find first line that starts with 'module' (common in Verilog)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("module"):
            return "\n".join(lines[i:]).strip()
    # fallback: return entire text
    return text.strip()

def sanitize_backtick_includes(verilog_text: str) -> str:
    """
    Fix common issues like `include being turned into include by LLMs,
    or missing backticks. We try to ensure `include has the backtick.
    """
    # replace " include" with " `include" when pattern looks like it should be a Verilog directive
    # careful: don't double-insert
    corrected = re.sub(r'(?m)^(?P<ws>\s*)(?:include)\s+["<]', r'\g<ws>`include "', verilog_text)
    # Fix cases where LLM emitted ' `include' incorrectly escaped
    corrected = corrected.replace("\\`include", "`include")
    return corrected

def write_text_file(path: Path, content: str):
    path.write_text(content if content is not None else "", encoding="utf-8")

# -------------------------
# Pipeline stages
# -------------------------
def prompt_enhancer(state: DesignState) -> DesignState:
    print("\n[Prompt Enhancer] Using", LLAMA_PROMPT_MODEL)
    prompt = f"Enhance this hardware design specification for RTL generation:\n\n{state['user_prompt']}\n\nProvide a concise implementable spec."
    res = call_ollama(LLAMA_PROMPT_MODEL, prompt)
    state["prompt_raw"] = res
    enhanced = (res["stdout"] or "").strip()
    state["enhanced_prompt"] = enhanced
    return state

def rtl_generator(state: DesignState, lint_feedback: Optional[str] = None) -> DesignState:
    print("\n[RTL Generator] Using", DEEPSEEK_MODEL)
    prompt = f"Generate synthesizable Verilog RTL for the following specification:\n\n{state['enhanced_prompt']}\n\nRequirements: synthesizable, use non-blocking assignments in sequential blocks where appropriate, avoid compiler-specific directives, produce a single module."
    if lint_feedback:
        prompt += f"\n\nThe previously generated RTL failed lint with these errors:\n{lint_feedback}\nPlease fix the RTL to address these errors and produce clean Verilog."
    res = call_ollama(DEEPSEEK_MODEL, prompt)
    state.setdefault("llm_calls", []).append({"stage": "rtl_generator", "raw": res})
    raw_out = res["stdout"] or ""
    # Save raw LLM output
    write_text_file(state["work_dir_path"] / "rtl_gen_raw.txt", raw_out)
    # Attempt to extract verilog code
    verilog = extract_code_block(raw_out, lang_hint="verilog")
    verilog = sanitize_backtick_includes(verilog)
    # As a last-ditch, ensure there's a module keyword; if not, wrap with module skeleton
    if "module" not in verilog:
        verilog = f"// Fallback generated module\nmodule auto_gen_dummy();\nendmodule\n\n/* Original LLM output\n{raw_out}\n*/"
    # Write to design.v
    write_text_file(state["work_dir_path"] / "design.v", verilog)
    state["rtl_file"] = str(state["work_dir_path"] / "design.v")
    state["last_rtl_text"] = verilog
    return state

def run_lint(state: DesignState) -> DesignState:
    print("\n[Lint Agent] Running Verilator...")
    rtl_path = state.get("rtl_file")
    if not rtl_path or not Path(rtl_path).exists():
        state["lint_passed"] = False
        state["lint_log"] = "No RTL file"
        write_text_file(state["work_dir_path"] / "lint.log", state["lint_log"])
        return state
    # run verilator --lint-only
    cmd = [VERILATOR_CMD, "--lint-only", rtl_path]
    res = run_proc(cmd)
    lint_out = ""
    lint_out += "STDOUT:\n" + res["stdout"] + "\n" if res["stdout"] else ""
    lint_out += "STDERR:\n" + res["stderr"] + "\n" if res["stderr"] else ""
    write_text_file(state["work_dir_path"] / "lint.log", lint_out)
    state["lint_log"] = lint_out
    state["lint_passed"] = (res["rc"] == 0)
    if state["lint_passed"]:
        print("[Lint] ✅ Passed")
    else:
        print("[Lint] ❌ Failed (see lint.log)")
    return state

def testbench_generator(state: DesignState, sim_feedback: Optional[str] = None) -> DesignState:
    print("\n[Testbench Generator] Using", DEEPSEEK_MODEL)
    # Provide the cleaned RTL to the model so it can craft a matching testbench
    rtl_text = Path(state["rtl_file"]).read_text(encoding="utf-8") if state.get("rtl_file") else ""
    prompt = (
        f"Write a Verilog testbench for the following RTL module.\n"
        f"Include $dumpfile/$dumpvars to produce tb.vcd, run a reasonable number of cycles, "
        f"and do basic checks (no Xs). Provide only the testbench code block.\n\nRTL:\n{rtl_text}"
    )
    if sim_feedback:
        prompt += f"\n\nThe previous testbench/simulation failed with these errors:\n{sim_feedback}\nPlease fix the testbench accordingly."
    res = call_ollama(DEEPSEEK_MODEL, prompt)
    state.setdefault("llm_calls", []).append({"stage": "testbench_generator", "raw": res})
    raw_tb = res["stdout"] or ""
    write_text_file(state["work_dir_path"] / "tb_raw.txt", raw_tb)
    tb_code = extract_code_block(raw_tb, lang_hint="verilog")
    # Ensure $dumpfile/$dumpvars exist so VCD is produced
    if "$dumpfile" not in tb_code:
        tb_code = "$dumpfile(\"tb.vcd\");\n$dumpvars(0,tb);\n" + tb_code
    write_text_file(state["work_dir_path"] / "testbench.v", tb_code)
    state["tb_file"] = str(state["work_dir_path"] / "testbench.v")
    state["last_tb_text"] = tb_code
    return state

def simulate(state: DesignState) -> DesignState:
    print("\n[Simulator] Compiling with Icarus (iverilog)...")
    rtl = state.get("rtl_file")
    tb = state.get("tb_file")
    if not (rtl and tb):
        state["simulation_passed"] = False
        state["simulation_log"] = "Missing RTL or testbench"
        write_text_file(state["work_dir_path"] / "simulation.log", state["simulation_log"])
        return state
    # compile to sim.out inside workdir
    out_exec = str(state["work_dir_path"] / "sim.out")
    cmd_compile = [IVERILOG_CMD, "-o", out_exec, rtl, tb]
    comp_res = run_proc(cmd_compile, cwd=state["work_dir"])
    compile_log = (comp_res["stdout"] or "") + (comp_res["stderr"] or "")
    write_text_file(state["work_dir_path"] / "compile.log", compile_log)
    if comp_res["rc"] != 0:
        state["simulation_passed"] = False
        state["simulation_log"] = "Compile failed:\n" + compile_log
        write_text_file(state["work_dir_path"] / "simulation.log", state["simulation_log"])
        print("[Simulator] ❌ Compile Error (see compile.log)")
        return state
    # run vvp from workdir so tb.vcd is created there
    print("[Simulator] Running sim.out with vvp (will create tb.vcd if testbench dumps it)...")
    run_res = run_proc([VVP_CMD, out_exec], cwd=state["work_dir"])
    sim_log = (run_res["stdout"] or "") + (run_res["stderr"] or "")
    write_text_file(state["work_dir_path"] / "simulation.log", sim_log)
    state["simulation_log"] = sim_log
    state["simulation_passed"] = (run_res["rc"] == 0)
    if state["simulation_passed"]:
        print("[Simulator] ✅ Passed")
    else:
        print("[Simulator] ❌ Runtime Error (see simulation.log)")
    return state

# -------------------------
# Helpers for artifacts
# -------------------------
def make_readme(workdir_path: Path, state: DesignState):
    readme_lines = [
        "# RTL Pipeline Workdir",
        "",
        f"Generated at: {datetime.utcnow().isoformat()} UTC",
        "",
        "## Files",
        "",
        "- `design.v` - generated Verilog RTL (cleaned)",
        "- `testbench.v` - generated testbench",
        "- `tb.vcd` - waveform (if testbench writes it)",
        "- `sim.out` - compiled simulation binary",
        "- `rtl_gen_raw.txt` - raw LLM output for RTL",
        "- `tb_raw.txt` - raw LLM output for testbench",
        "- `enhanced_prompt.txt` - the enhanced spec from llama3.1",
        "- `lint.log` - verilator lint output",
        "- `compile.log` - iverilog compile output",
        "- `simulation.log` - vvp runtime output",
        "- `design_info.json` - metadata about this run",
        "",
        "## How to re-run locally",
        "```bash",
        f"iverilog -o sim.out design.v testbench.v",
        "vvp sim.out",
        "gtkwave tb.vcd   # to view waveforms (if tb.vcd exists)",
        "```",
        "",
        "## Notes",
        "- The LLM raw outputs are preserved for auditing in `rtl_gen_raw.txt` and `tb_raw.txt`.",
    ]
    write_text_file(workdir_path / "README.md", "\n".join(readme_lines))

def save_design_info(workdir_path: Path, state: DesignState):
    info = {
        "user_prompt": state.get("user_prompt"),
        "enhanced_prompt_present": bool(state.get("enhanced_prompt")),
        "rtl_file": state.get("rtl_file"),
        "tb_file": state.get("tb_file"),
        "lint_passed": bool(state.get("lint_passed")),
        "simulation_passed": bool(state.get("simulation_passed")),
        "timestamps": {
            "run_started": state.get("run_started"),
            "run_finished": datetime.utcnow().isoformat()
        },
        "retries": {
            "rtl_retries": state.get("rtl_retries", 0),
            "tb_retries": state.get("tb_retries", 0)
        },
        "notes": "Generated by rtl_pipeline_full.py"
    }
    write_text_file(workdir_path / "design_info.json", json.dumps(info, indent=2))

def extract_test_vectors(workdir_path: Path, state: DesignState):
    """
    Try to extract simple test vectors from the testbench: clock/reset/enable sequences.
    This is best-effort: we look for obvious numeric constants or $urandom_range seeds.
    """
    tb_text = Path(state["tb_file"]).read_text(encoding="utf-8") if state.get("tb_file") else ""
    vectors = []
    # find urandom_range usage and cycles loop values
    urandom_matches = re.findall(r"\$urandom_range\(([^)]*)\)", tb_text)
    for m in urandom_matches:
        vectors.append(f"urandom_range: {m.strip()}")
    cycles_match = re.search(r"for\s*\(\s*.*?;\s*.*?<\s*(\d+)\s*;\s*.*?\)", tb_text)
    if cycles_match:
        vectors.append(f"cycles: {cycles_match.group(1)}")
    if vectors:
        write_text_file(workdir_path / "test_vectors.txt", "\n".join(vectors))

# -------------------------
# Main orchestration
# -------------------------
def main(argv=None):
    p = argparse.ArgumentParser(prog="rtl_pipeline_full", description="RTL pipeline (Ollama + Verilator + Icarus).")
    p.add_argument("--spec", "-s", type=str, help="Design specification (text).")
    p.add_argument("--max-retries", "-r", type=int, default=MAX_RETRIES_DEFAULT, help="Max retries for RTL and TB.")
    p.add_argument("--keep-workdir", action="store_true", help="Do not recreate the workdir; keep existing (appends).")
    args = p.parse_args(argv)

    # ensure required tools exist
    for t in (VERILATOR_CMD, IVERILOG_CMD, VVP_CMD, "ollama"):
        if not check_tool(t):
            bail(f"❌ Required tool not found in PATH: {t}\nPlease install or add to PATH.", rc=2)

    # compute workdir next to this script
    script_dir = Path(__file__).parent.resolve()
    workdir_path = script_dir / WORKDIR_NAME

    # recreate workdir unless user asked to keep it (for fresh run)
    if workdir_path.exists() and not args.keep_workdir:
        shutil.rmtree(workdir_path)
    workdir_path.mkdir(parents=True, exist_ok=True)

    # initial state
    state = DesignState()
    state["user_prompt"] = args.spec or input("Enter RTL design spec: ").strip()
    state["work_dir"] = str(workdir_path)
    state["work_dir_path"] = workdir_path
    state["run_started"] = datetime.utcnow().isoformat()
    state["rtl_retries"] = 0
    state["tb_retries"] = 0

    # Save enhanced prompt raw and LLM outputs to files in workdir as we go
    # Start the pipeline
    max_retries = args.max_retries
    rtl_retry_count = 0
    tb_retry_count = 0

    # Save enhanced prompt file helper
    def save_enhanced_prompt_file():
        ep = state.get("enhanced_prompt", "")
        write_text_file(workdir_path / "enhanced_prompt.txt", ep)

    try:
        # Step 1: enhance prompt
        state = prompt_enhancer(state)
        save_enhanced_prompt_file()

        while True:
            # Step 2: generate RTL (with optional lint feedback)
            lint_feedback = state.get("lint_log") if rtl_retry_count > 0 else None
            state = rtl_generator(state, lint_feedback=lint_feedback)

            # save enhanced prompt (again) and raw rtl output already saved inside rtl_generator
            save_enhanced_prompt_file()

            # Step 3: lint
            state = run_lint(state)
            # If lint failed, retry with feedback
            if not state.get("lint_passed", False):
                rtl_retry_count += 1
                state["rtl_retries"] = rtl_retry_count
                if rtl_retry_count > max_retries:
                    print("❌ RTL generation retry limit reached. Aborting.")
                    break
                print("[Trackback] 🔁 Lint failed – regenerating RTL with lint feedback.")
                continue  # loop back to rtl_generator with lint feedback

            # Step 4: generate testbench (with possible sim feedback)
            sim_feedback = state.get("simulation_log") if tb_retry_count > 0 else None
            state = testbench_generator(state, sim_feedback=sim_feedback)

            # Step 5: simulate (compile + run)
            state = simulate(state)

            if not state.get("simulation_passed", False):
                tb_retry_count += 1
                state["tb_retries"] = tb_retry_count
                if tb_retry_count > max_retries:
                    print("❌ Testbench retry limit reached. Aborting.")
                    break
                print("[Trackback] 🔁 Simulation failed – regenerating testbench with simulation feedback.")
                continue  # regenerate testbench
            # success
            print("\n✅ Design, linting, and simulation succeeded!")
            break

    finally:
        # Always write artifacts and metadata
        # Ensure enhanced prompt stored
        save_enhanced_prompt_file()
        # Save simulation/lint logs already done inside functions
        # If tb.vcd exists, move/rename accordingly (it should be in workdir already)
        # Save README and design_info.json
        make_readme(workdir_path, state)
        save_design_info(workdir_path, state)
        # Extract simple test vectors if possible
        try:
            extract_test_vectors(workdir_path, state)
        except Exception:
            pass

        # Final summary to user
        print("\nArtifacts written to:", workdir_path)
        print("Important files:")
        for f in [
            "design.v", "testbench.v", "tb.vcd", "sim.out",
            "rtl_gen_raw.txt", "tb_raw.txt", "enhanced_prompt.txt",
            "lint.log", "compile.log", "simulation.log", "design_info.json", "README.md"
        ]:
            p = workdir_path / f
            print(f" - {f} {'(exists)' if p.exists() else '(missing)'}")

# Entry point
if __name__ == "__main__":
    main()
