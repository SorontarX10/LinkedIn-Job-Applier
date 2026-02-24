# LinkedIn Job Applier (Playwright Stealth)

This project automates job application flows starting from your **saved/liked jobs on LinkedIn**.

Core behavior:
- Uses Playwright + stealth with `headless=false` (real visible browser).
- Opens your LinkedIn session and scans saved jobs.
- Reads each job description and scores fit with an LLM (OpenAI) using your CV and known profile facts.
- Fills Easy Apply flows and optionally external employer/agency forms.
- Uses truthful data only. If a required value is missing, it asks in terminal and remembers it in `data/knowledge.json`.
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

## 2. Run

```powershell
.venv\Scripts\Activate.ps1
python -m src.main
```

Useful flags:

```powershell
python -m src.main --dry-run
python -m src.main --max-jobs 5 --min-fit 60
python -m src.main --no-auto-submit
python -m src.main --no-external
```

## 3. Data files

- `data/knowledge.json`: remembered profile facts and field answers.
- `output/cover_letters/`: generated cover letters.
- `output/tailored_cv/`: per-job tailoring notes.
- `data/browser-profile/`: persistent browser profile/session.

## 4. Notes

- First run requires manual LinkedIn login in the opened browser.
- `AUTO_SUBMIT=true` submits automatically; set `false` if you want per-job confirmation.
- AI disclosure is automatically added in suitable comment/message fields and in generated cover letters.
- Selectors on LinkedIn and external ATS forms may change over time; adjust in `src/linkedin_bot.py` and `src/form_helper.py` when needed.
