# ADR 0003. Development tools and code layout

Status: accepted, 2026-10-22.

## Context

The server runs without dependencies and is distributed as sources (ADR 0001), but development tools
are allowed on the developer's machine and in CI. The project needs to follow what the Python community
does today without breaking the main rule: a clone of the repository runs on bare Python 3.9+.

## Decision

| Question | Choice | Why |
|---|---|---|
| Layout | `src/pg_explorer_mcp/` | the src layout recommended by PyPA: tests import the installed package, not stray files from the current folder |
| Third-party packages | `src/pg_explorer_mcp/_vendor/`, the package adds the folder to `sys.path` itself | this is what pip does (`pip/_vendor`); the files stay exactly as they are on PyPI, which `scripts/check_vendor.py` checks |
| Metadata and settings | `pyproject.toml` (PEP 621), dependency groups (PEP 735) | one file instead of `setup.py`, `requirements-dev.txt` and separate linter configs |
| Environment | uv with `uv.lock`, `.python-version` = 3.9 | reproducible tool versions; development runs on the same version users have on macOS |
| Build | `uv_build` | needed so that uv installs the project into the environment in editable mode; the project does not go to PyPI (`Private :: Do Not Upload`) |
| Linter and formatter | ruff | replaces flake8, isort, black, pyupgrade and bandit with one fast tool |
| Types | mypy in strict mode, `<2` | mypy 2 no longer checks code for Python 3.9 |
| Tests | pytest, fixtures, `parametrize`, the `integration` marker | the de facto standard; each SQL check case is a separate named test |
| Coverage | branch coverage, 95% threshold | a drop in coverage shows up in CI at once |
| Hooks | pre-commit through `uvx` | pre-commit needs Python 3.11+, so it lives as a separate tool, not a project dependency |
| CI | uv; linters, a Linux/Windows/macOS × Python 3.9/3.14 matrix, integration on PostgreSQL 13–18 (coverage on 16) and on Windows with PostgreSQL 17. On a PR, without macOS and with PostgreSQL 13, 16 and 18; the full set on a push to `develop` and `main` | the oldest and newest supported versions on every platform users have; corporate databases run different versions; a macOS minute costs ten Linux minutes, and in a private repository they are limited |
| Actions in CI | pinned by commit SHA with the version in a comment | a tag can be moved, a commit cannot; `astral-sh/setup-uv` since v8 no longer publishes floating tags such as `v10`, and dependabot updates the SHA together with the comment |
| Updates | dependabot for GitHub Actions and uv, PRs into `develop`; mypy 1.20+ and pytest 9+ are not offered; for vendored packages only Dependabot alerts from the manifest `src/pg_explorer_mcp/_vendor/requirements.txt` | vendored packages are updated by hand, following the instructions: a Dependabot PR would change only the manifest, and the dependency graph sees vulnerabilities in them without an entry in `dependabot.yml`; Dependabot reads its settings from `main`, the default branch with the released versions; from mypy 1.20 and pytest 9 on they need Python 3.10, and uv will not resolve them for the 3.9 branch in `uv.lock` |

## Alternatives considered

- **Flat layout** (`pg_explorer_mcp/` at the root): simpler, but tests could accidentally import
  the sources instead of the installed package.
- **black + isort + flake8**: three tools and three configs where ruff is enough.
- **pyright or ty** instead of mypy: mypy remains the reference, and ty was still young when the decision was made.
- **unittest**: no dependencies for tests either, but more verbose; since development tools
  are allowed, pytest wins.
- **Poetry, PDM, Hatch**: uv is faster, installs Python itself and is already on developers' machines.

## Consequences

- Working on the project needs uv; running the server still needs only Python.
- While Python 3.9 is supported, mypy is pinned below 2, and pytest and coverage in the 3.9 environment
  stay on the last versions that support 3.9. The lock file holds both branches of versions.
- All checks except the integration ones work without a database and without a network.
