# QA Checklist - Copilot LinkedIn Job Applier

## Scope
- [ ] Saved jobs flow (`saved_only`) works end-to-end.
- [ ] Discovery flow (`discovery_only`, `discovery_and_apply`) works end-to-end.
- [ ] Queue sync respects status rules:
  - [ ] `not_submitted` returns to queue.
  - [ ] `submitted` is not requeued and can be unsaved.
- [ ] Retry policy works:
  - [ ] retry limit is enforced.
  - [ ] retry cooldown is enforced.

## Agentic fallback and copilot
- [ ] Stuck detection triggers fallback.
- [ ] Agentic fallback writes trace to `output/agentic_traces/`.
- [ ] Risky actions are blocked by `AGENTIC_BLOCKED_ACTION_TOKENS`.
- [ ] Human handoff is triggered when fallback cannot continue safely.
- [ ] After manual intervention, bot resumes automatically.
- [ ] Learned answers are persisted in `data/knowledge.json`.
- [ ] Learned playbooks are persisted and reused.

## Complex form coverage
- [ ] Dropdown fields are handled.
- [ ] File upload fields are handled (with handoff fallback if needed).
- [ ] Dynamic suggestion fields (city/country autocomplete) are handled.
- [ ] Validation messages are read and reflected in strategy.
- [ ] Language detection for answers follows field language.

## Discovery and ranking
- [ ] Query builder uses profile + CV + include/exclude keywords.
- [ ] Ranking returns component scores and priority score.
- [ ] Hard rejects are excluded from queue.
- [ ] Discovery cache is used and expires by TTL.

## Metrics and observability
- [ ] KPI report is generated in `output/metrics/latest.json`.
- [ ] History line is appended to `output/metrics/runs.jsonl`.
- [ ] `application_success_rate` is present.
- [ ] `fallback_trigger_rate` is present.
- [ ] `fallback_recovery_success_rate` is present.
- [ ] `human_handoff_rate` is present.
- [ ] `mean_steps_per_application` is present.
- [ ] `mean_time_per_application_sec` is present.
- [ ] `playbook_hit_rate` is present.
- [ ] `discovery_to_apply_conversion` is present.

## Regression suite
- [ ] Scenario tests pass (`tests/test_e2e_scenarios.py`).
- [ ] Learning regression test passes.
- [ ] Metrics/KPI test passes.
- [ ] Blocked-action safety test passes.
