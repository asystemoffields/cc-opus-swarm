# Contributing to CC-Collab

## Setup

1. **Clone the repo:**
   ```bash
   git clone https://github.com/asystemoffields/CC-Collab.git
   cd CC-Collab
   ```

2. **Requirements:** Python 3.12+ (stdlib only, no pip dependencies needed).

3. **Run tests:**
   ```bash
   python -m pytest test_collab.py -v
   ```

4. **Try it locally:**
   ```bash
   python launcher.py /path/to/any/project
   ```

## Project Structure

| File | Purpose |
|------|---------|
| `collab.py` | Core CLI tool -- all collaboration commands |
| `inject.py` | Cross-platform terminal injection backends |
| `launcher.py` | Launches N Claude Code instances with role config |
| `test_collab.py` | Test suite |
| `pyproject.toml` | Package metadata and entry points |

## Code Style

- **Zero external dependencies.** Everything uses Python 3.12+ stdlib. Do not add pip packages.
- **Single-file preference.** Keep `collab.py` as the main tool. Only split into separate modules (like `inject.py`) when the code is large and logically independent.
- **Type hints** are welcome but not required. Don't add them retroactively to unchanged code.
- **No docstring/comment churn.** Only add comments where logic isn't self-evident. Don't rewrite existing docstrings for style.
- **OS-level file locking** for all state access -- never read-modify-write without `FileLock`.

## Making Changes

1. **Fork and branch** from `main`.
2. **Run the test suite** before and after your changes:
   ```bash
   python -m pytest test_collab.py -v
   ```
3. **Test cross-platform** if touching `inject.py` or `launcher.py`. The injection backends are:
   - Windows: Win32 `AttachConsole` + `WriteConsoleInputW`
   - Linux/macOS: `tmux send-keys` or `screen -X stuff`
4. **Keep the CLI stable.** Existing command syntax must not break. New commands and flags are fine; changing existing ones requires a deprecation path.

## Pull Request Guidelines

- Keep PRs focused -- one feature or fix per PR.
- Title should be concise (under 70 chars). Use the body for details.
- Include a test plan or describe how you verified the change.
- If adding a new command, add it to the `build_parser()` function and update the README command reference.
- If adding a new `--json` output path, return structured data via `_emit_json()`.

## Architecture Notes

- **State is file-based JSON** in `state/`. Each collection (`nodes`, `tasks`, `messages`, etc.) is a separate `.json` file with its own OS-level lock.
- **Signal files** (`_signal_<node>`) are the push notification mechanism -- write a reason line, the target reads and clears on next `pending`/`poll`.
- **Terminal injection** is abstracted in `inject.py` with a backend pattern (`InjectionBackend` ABC). Adding a new backend (e.g., iTerm2, Kitty) means subclassing and adding to `_BACKENDS`.
- **The launcher** generates platform-specific scripts (`.bat` on Windows, `.sh` on Unix) and opens them in appropriate terminals.
