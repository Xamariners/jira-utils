# CLAUDE.md — Jira Attachment Cleaner

Python CLI that inventories Jira issues for large/old attachments, lets you mark them for removal, and deletes them while tracking progress (`TODO → IN_PROGRESS → DONE/ERROR`). Falls back to scrubbing referencing comments when Jira refuses deletion.

## Tech stack

- Python 3.9+ (`requires-python = ">=3.9"` in `pyproject.toml`)
- Dependency manager: **pip + editable install** (`pip install -e .`); single runtime dep — `requests>=2.31`
- Build backend: `setuptools>=68` (both `pyproject.toml` and a parallel `setup.py` are present)
- `curses` (stdlib) for the interactive picker — macOS/Linux only, Windows users need WSL
- `pytest` for tests (the only test is opt-in live, gated on `JIRA_LIVE_CONFIG`)
- Jira Cloud REST API v3 (Server/DC with v3 also works)

## Project structure

- `jira_utils/` — package; entry point is `jira_utils.cli:main`, exposed as `jira-utils` console script
  - `cli.py` — argparse subcommands (`list`, `mark`, `process`, `status`, `interactive`) + ncurses UI
  - `jira_client.py` — `requests.Session`-backed Jira API wrapper, raises `JiraAPIError`
  - `config.py` — `JiraConfig` dataclass + `load_config(path)` JSON loader
  - `state.py` — `StateManager` persists `AttachmentState` records to `state/state.json`
- `tests/test_live_search.py` — single opt-in live test (calls real Jira)
- `state/` — runtime state directory (created on demand; `state.json` is gitignored data)
- `config.example.json` — copy to `config.json` and fill in; contains secrets, never commit
- `pyproject.toml` + `setup.py` — both ship; keep them in sync if you change deps/metadata

## Commands

### Python

- Install (editable): `python -m venv .venv && source .venv/bin/activate && pip install -e .`
- Run CLI: `jira-utils <subcommand>` (or `python -m jira_utils.cli` if not installed)
- Run all tests: `pytest` (live test auto-skips without `JIRA_LIVE_CONFIG`)
- Run live test: `JIRA_LIVE_CONFIG=config.json pytest tests/test_live_search.py -k live -s`
- Optional live overrides: `JIRA_LIVE_PROJECTS`, `JIRA_LIVE_BEFORE`, `JIRA_LIVE_MIN_SIZE_MB`, `JIRA_LIVE_MAX_ISSUES`
- Lint / format / type check: **no tooling configured** — `<fill-in>` if you want ruff/mypy added

### CLI subcommands (see README for full flag list)

- `jira-utils list --before <YYYY-MM-DD> --min-size-mb <n>` — preview candidate attachments
- `jira-utils mark ...` — write matches into `state/state.json` as `TODO`
- `jira-utils process [--retry-errors] [--limit N]` — delete queued items sequentially
- `jira-utils status` — show every tracked attachment + last error
- `jira-utils interactive ...` — ncurses picker with live status updates

## Architecture (WHY)

Deletes are **sequential** and **state-tracked** because Jira's delete API is irreversible and the original user (ben) needs to halt/resume mid-run and audit what was removed. The two-phase split (`mark` → `process`) exists so a destructive batch can be reviewed in `state.json` before any API call mutates Jira. Comment-scrubbing fallback exists because Jira refuses to delete attachments that are still referenced in comments — the tool finds those references (by id, filename, or `[^name]` wiki marker) and deletes the comments to unblock retry.

## Patterns we use

### Python

EXAMPLE — keep / edit / delete:

- `from __future__ import annotations` at the top of every module (used consistently in `cli.py`, `jira_client.py`, `state.py`, `config.py`).
- `@dataclass` for structured records (`AttachmentRecord`, `AttachmentState`, `JiraConfig`, `JiraClient`).
- `pathlib.Path` over `os.path`; `Path(...).expanduser()` for user-supplied paths.
- Type hints on public functions; `Optional[...]` / `List[...]` from `typing` (kept Py3.9-compatible — no `X | Y` unions).
- Custom exception (`JiraAPIError`) carries `status_code` + `payload` for retry logic.
- `requests.Session` reused across calls (auth + headers set once in `__post_init__`).

## Patterns we do NOT use (don't suggest)

### Python

- `from x import *`.
- Bare `except:` — `JiraAPIError` is the typed boundary.
- Mutable default arguments.
- `os.system` / `os.popen` — use `subprocess.run` (currently no shell-out at all).
- `X | Y` PEP 604 unions or `list[X]` builtins in annotations — repo targets Python 3.9, use `Optional` / `List`.
- `print()` for non-CLI output — CLI is the entire app surface, so `print` IS the output channel here; don't add it inside `jira_client.py` / `state.py` / `config.py`.

## Canonical code references

- API call shape: `jira_utils/jira_client.py`
- Persisted record shape: `jira_utils/state.py` (`AttachmentState`)
- CLI subcommand wiring: `jira_utils/cli.py` (`parse_args`, `main`)
- Config schema: `jira_utils/config.py` + `config.example.json`

## Testing

- One test file: `tests/test_live_search.py`. It is marked `@pytest.mark.live` and **skips by default** unless `JIRA_LIVE_CONFIG` is set.
- No unit tests, no mocking layer, no CI workflow. `<fill-in>` if adding offline coverage — `responses` or `requests-mock` would fit cleanly given the `requests.Session` abstraction.
- Coverage gate: none.

## Git workflow

- Branch naming: `feat/<short-desc>`, `fix/<short-desc>`, `chore/<short-desc>`, `docs/<short-desc>`.
- Commit format: `<fill-in>` — recent history is freeform single-line ("Implements Jira attachment management CLI"); adopt Conventional Commits if standardising.
- PR gates: `<fill-in>` — no CI configured, no `.github/` directory.

## Known gotchas & workarounds

- `pyproject.toml` and `setup.py` both declare the package; keep deps + version in lockstep across both or builds will diverge.
- Interactive mode imports `curses` lazily and degrades to `None` on Windows — handle the `curses is None` branch when adding picker features.
- Comment cleanup is intentionally aggressive: any comment mentioning the attachment id/name/`[^name]` marker is deleted. Narrow filters before running `process` against shared projects.
- `state/state.json` is the single source of truth for what's been queued/done; deleting it loses progress and there is no recovery from Jira's side (the attachments are already gone).
