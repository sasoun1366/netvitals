# Contributing to netvitals

Thanks for your interest! netvitals is deliberately small: **one file, two light
dependencies** (`rich`, `requests`), Python 3.9+.

## The rules

1. `netvitals.py` stays a single, dependency-light file. No frameworks, no new heavy deps.
2. If you add a code path, add an offline test — tests must never touch the network.
3. New checks must degrade gracefully: report `error` with a clear message instead of
   crashing the whole run.
4. Keep the CLI backward compatible; document new flags in the README and `--help`.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q          # offline unit tests
netvitals 1.1.1.1  # smoke test against the real world
```

## Pull requests

* Small, focused PRs are easiest to review.
* Show real output in the PR description (table or `--json`).
* Pick an issue from the roadmap first if one fits.

Thanks! 🙏
