# Repository Guidelines

## Project structure and module organization

Run commands from `agentedu/`. `orchestrator.py` owns the workflow state machine, while `agents/` contains the intake, generation, debate, audit, and decision roles. Shared schemas, retrieval, learner modeling, and LLM adapters live in `core/`. Keep datasets in `data/`, evaluation entry points in `evalkit/`, utilities in `tools/`, and documentation in `docs/`. The single online frontend lives at `web/index.html` and is served by `server.py`.

## Build, test, and development commands

The core uses the Python standard library and runs without an API key.

```bash
python3 cli.py P-A                         # run one offline learning workflow
python3 -m unittest discover -s tests -v  # run the complete test suite
python3 -m evalkit.run_eval --n 50        # calculate batch evaluation metrics
python3 -m evalkit.redteam                 # exercise hallucination defenses
python3 server.py                          # serve the live demo on port 8000
```

Use `py -3` instead of `python3` on Windows if needed.

## Coding style and naming conventions

Use four-space indentation, type hints, short module docstrings, and standard-library solutions where practical. Follow `snake_case` for functions and modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Keep thresholds in `config.py`. JavaScript uses two-space indentation and camelCase. No formatter or linter is configured, so match nearby code and group imports by standard library, local package, then symbols.

## Testing guidelines

Tests use `unittest`. Name files `tests/test_<area>.py`, classes `Test<Behavior>`, and methods `test_<expected_result>`. Add focused regression tests for every behavior change, then run the full suite. There is no configured coverage threshold; prioritize state transitions, evidence gates, numerical boundaries, and offline/online parity.

## Commit and pull request guidelines

Git history is absent from this repository snapshot, so no existing message pattern can be verified. Use concise, imperative subjects such as `fix: reject dangling citations`. Commit `config.py` threshold changes separately before generating evaluation results. Pull requests should explain the behavior and rationale, list commands run, link the relevant issue, and include screenshots for `web/` changes or before/after metrics for evaluation changes.

## Security and data integrity

Never commit `AGENTEDU_API_KEY` or place credentials in browser assets. Machine ingestion must not mark knowledge as verified; preserve source locators and leave final verification to a human reviewer.
