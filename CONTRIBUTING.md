# Contributing to Agentic RTL Coder

Thank you for considering contributing! This document explains how to get
started, what we expect from contributions, and how the review process works.

## 🛠 Development Setup

### Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥ 3.10 | Runtime |
| Ollama | latest | Local LLM inference |
| Verilator | ≥ 4.0 | RTL linting |
| Icarus Verilog | ≥ 11.0 | Simulation (iverilog + vvp) |

### Clone and install

```bash
git clone https://github.com/ManishSwarnakar/Agentic_RTL_Coder.git
cd Agentic_RTL_Coder
pip install -e ".[dev]"
```

### Run the tests

```bash
pytest
```

### Run the linter

```bash
ruff check .
```

## 📝 How to Contribute

1. **Fork** the repository.
2. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```
3. **Make your changes** — keep commits focused and atomic.
4. **Add or update tests** for any new functionality.
5. **Run the test suite** to make sure nothing is broken:
   ```bash
   pytest
   ruff check .
   ```
6. **Open a Pull Request** against `main` with a clear description of:
   - What you changed and why
   - How you tested it
   - Any open questions or trade-offs

## 🐛 Reporting Bugs

Open an issue with:

- **Steps to reproduce** (including the `--spec` you used, if relevant)
- **Expected vs. actual behaviour**
- **Environment** — OS, Python version, Ollama model, Verilator version
- **Logs** — attach `lint.log`, `simulation.log`, or `compile.log` from `rtl_pipeline_work/`

## 💡 Feature Requests

Open an issue tagged `enhancement` describing:

- **The problem** you're trying to solve
- **The proposed solution** (or alternatives you considered)
- **Use case** — who benefits and how

## 🧹 Code Style

- We use [Ruff](https://docs.astral.sh/ruff/) for linting.
- Max line length: **100 characters**.
- Type hints are encouraged for all public functions.
- Docstrings follow the NumPy/Sphinx convention.

## 📜 License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
