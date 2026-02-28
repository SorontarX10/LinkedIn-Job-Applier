# LinkedIn Job Applier (Playwright Stealth)

This project automates job application flows starting from your **saved/liked jobs on LinkedIn**.

Core behavior:
- Uses Playwright + stealth with `headless=false` (real visible browser).
- Opens your LinkedIn session and scans saved jobs.
- Reads each job description and scores fit with an LLM (OpenAI) using your CV and known profile facts.
- Fills Easy Apply flows and optionally external employer/agency forms.
- Runs in copilot mode (`COPILOT_MODE=true`): when automation gets stuck, it pauses and waits for manual help, then resumes.
- Learns from manual form inputs during those pauses and saves reusable answers to `data/knowledge.json` for future runs.
- Works without terminal confirmations by default (`TERMINAL_INPUT_ENABLED=false`): intervention happens directly in browser.
- Uses truthful data only. Missing values are remembered from profile/bootstrap data and browser-side manual interventions in `data/knowledge.json`.
- Generates optional cover letters and tailored CV notes per job.

## 1. Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

Then edit `.env` and set at minimum:
- `OPENAI_API_KEY`
- `CV_PATH`
- `AI_DISCLOSURE_ENABLED=true`
- `AI_DISCLOSURE_TEXT=This application was submitted with assistance from an AI agent.`
- `ALWAYS_APPLY_EXCEPT_OUTSIDE_POLAND=true`

Optional, to avoid startup prompts for profile facts:
- copy `data/profile.bootstrap.example.json` to `data/profile.bootstrap.json`
- fill truthful values in `profile`
- optionally add reusable form answers in `field_answers` (for recurring custom questions)
- keep `PROFILE_PROMPT_ON_START=false`

## 2. Run

```powershell
.venv\Scripts\Activate.ps1
python -m src.main
```

Useful flags:

```powershell
python -m src.main --dry-run
python -m src.main --reapply
python -m src.main --max-jobs 5 --min-fit 60
python -m src.main --no-auto-submit
python -m src.main --no-external
python -m src.main --system-chrome --chrome-profile Default
```

## 3. Data files

- `data/knowledge.json`: remembered profile facts and field answers.
- `data/profile.bootstrap.json`: optional startup profile bootstrap (local only, gitignored).
- `data/profile.bootstrap.example.json`: template for bootstrap profile data.
- `output/cover_letters/`: generated cover letters.
- `output/tailored_cv/`: per-job tailoring notes.
- `data/browser-profile/`: persistent browser profile/session.

## 4. Notes

- First run requires manual LinkedIn login in the opened browser.
- If you want to reuse your already logged Chrome account/session:
  - set `USE_SYSTEM_CHROME_PROFILE=true`
  - set `SYSTEM_CHROME_USER_DATA_DIR` to your Chrome `User Data` folder
  - set `SYSTEM_CHROME_PROFILE_NAME` (for example: `Default` or `Profile 1`)
  - close all Chrome windows before run (to avoid profile lock), then start the bot
- `AUTO_SUBMIT=true` submits automatically; set `false` if you want per-job confirmation.
- `COPILOT_MODE=true` enables human-in-the-loop recovery for difficult forms (manual action in browser -> bot auto-resumes).
- `TERMINAL_INPUT_ENABLED=false` disables terminal `input()` prompts (recommended).
- `COPILOT_WAIT_TIMEOUT_SEC` and `COPILOT_POLL_INTERVAL_MS` control non-blocking handoff wait behavior.
- `COPILOT_AUTO_SKIP_ON_TIMEOUT=true` skips current application when handoff timeout is reached.
- With terminal prompts disabled, ensure `CV_PATH` and profile/bootstrap data are configured up front.
- AI disclosure is automatically added in suitable comment/message fields and in generated cover letters.
- Form answers are generated in the language detected from each field label/context.
- With `ALWAYS_APPLY_EXCEPT_OUTSIDE_POLAND=true`, fit score is informational and the bot applies unless the role explicitly requires location outside Poland.
- Selectors on LinkedIn and external ATS forms may change over time; adjust in `src/linkedin_bot.py` and `src/form_helper.py` when needed.
