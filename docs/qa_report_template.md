# QA Report Template - Copilot LinkedIn Job Applier

## Run metadata
- Date:
- Tester:
- Commit SHA:
- Environment:
  - OS:
  - Python:
  - Browser channel/profile mode:

## Run configuration
- Mode (`saved_only` / `discovery_only` / `discovery_and_apply`):
- `MAX_JOBS_PER_RUN`:
- `DISCOVERY_ENABLED`:
- `DISCOVERY_MAX_RESULTS`:
- `JOB_QUEUE_RETRY_LIMIT`:
- `JOB_QUEUE_RETRY_COOLDOWN_MINUTES`:
- `AGENTIC_FALLBACK_MAX_ITERATIONS`:
- `AGENTIC_TOOL_STEP_LIMIT`:

## Functional results
- Saved jobs processed:
- Discovery jobs discovered:
- Discovery jobs queued:
- Submitted:
- Not submitted:
- Skipped:
- Errors:

## KPI snapshot
- `application_success_rate`:
- `fallback_trigger_rate`:
- `fallback_recovery_success_rate`:
- `human_handoff_rate`:
- `mean_steps_per_application`:
- `mean_time_per_application_sec`:
- `playbook_hit_rate`:
- `discovery_to_apply_conversion`:

## Fallback and handoff notes
- Number of fallback traces:
- Number of handoffs:
- Recovery examples:
  1.
  2.

## Learning observations
- Newly learned field answers (count):
- Newly learned playbooks (count):
- Evidence of playbook reuse in later attempts:

## Safety checks
- Blocked actions attempted? (yes/no)
- Any destructive action clicked accidentally? (yes/no)
- Captcha/login handoff behavior correct? (yes/no)

## Test suite status
- `python -m compileall src tests`: pass/fail
- `python -m unittest -v`: pass/fail
- Additional manual checks:
  1.
  2.

## Open issues / follow-ups
1.
2.
3.
