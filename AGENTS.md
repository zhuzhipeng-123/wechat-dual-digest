# WeChat Dual Digest MVP Instructions

## Project

Build a local Windows tool that consumes one explicitly configured and verified WeChat article source, then produces and optionally emails practice and recruitment HTML/JSON reports on a daily schedule. `EXECUTION_PLAN.md` defines the first-release scope.

Do not implement deferred platform features merely because the legacy specification lists them. This release may consume one RSS/Atom source but does not build an RSS platform. It does not include FastAPI, REST APIs, MCP, Markdown export, a web administration UI, a generic article database, or a generic WeChat article platform.

## Structure and stack

- Use Python 3.12, direct Python function calls, `httpx`, browser automation where required, local OCR, strict data validation, and static HTML templates.
- Keep application code under `app/`, templates under `templates/`, configuration examples under `config/`, and tests under `tests/`.
- Prefer a few cohesive modules over one file per small concept. The MVP layout is: `core.py` for contracts/config/window/security/selection, `source.py` for discovery/article/image handling, `agnes.py` for the fixed model client, `recruitment.py` for OCR/extraction/validation/rating, plus `pipeline.py`, `reporting.py`, `scheduler.py`, and `cli.py`.
- Keep sessions, evidence, runtime state, logs, real reports, keys, and user account lists outside version control under ignored local paths.
- Prefer the standard library and existing dependencies. Add a dependency only for a verified need.

## Confirmed behavior

- Use `Asia/Shanghai` in the first release. General DST-region scheduling is not promised.
- Formal discovery uses one user-configured RSS/Atom source binding per account. Source coverage remains unverified until real target accounts pass identity, multi-article, timeliness, and failure checks. Sogou and Demo data are never formal sources.
- Treat every candidate source's account labels and displayed dates as untrusted hints; accept an article only after the original WeChat page confirms the exact account and publication time.
- A manually supplied URL remains an isolated preview or probe and must not be mistaken for scheduled discovery.
- Verify original article content and exact publication time; never invent timestamps. Introduce WeChat Official Accounts Platform login only if the verified fetching method needs it.
- Practice reports use sanitized original text and usable images, with no OCR, AI, answer generation, or rewriting.
- Recruitment reports use local OCR and Agnes model `agnes-2.5-flash` at the fixed base URL `https://apihub.agnes-ai.com/v1`.
- Models extract facts only. Code validates evidence, applies the agreed rating matrix, writes conclusions, and renders HTML.
- Preserve company → job → location → evidence relationships. Keep missing facts unknown.
- Rate a company using the best city among all of its campus full-time opportunities, not only AI-related jobs.
- Determine recruitment openness at the frozen window end from original evidence. Keep uncertain cases as pending verification.
- Manual generation is an isolated preview under `output/preview/<run_id>/`; it does not claim or overwrite an official scheduled run.
- Formal runs may look back a bounded number of hours for verified, not-yet-collected late articles. Preview never consumes collection state.
- Schedule changes take effect on the next day. An already claimed run keeps its frozen window and configuration snapshot.
- Persist scheduled execution state and prevent duplicate official runs across processes.
- Distinguish discovery, fetch, analysis, coverage, and publication failures. Never turn a failure into a false zero-result claim.
- SMTP is the only delivery channel in this release. It is disabled by default; generation and delivery are separate, and resending reuses an existing complete report.

## Workflow

- Read relevant files, imports, callers, tests, and established patterns before editing.
- Define an observable expected result for each increment, implement the smallest change, run checks, record evidence, and fix errors before continuing.
- Use fixed clocks, temporary directories, and fake remote services in offline tests. Offline tests must not access the network.
- Treat live login, live article discovery, real browser behavior, local OCR quality, and real Agnes responses as separate checks.
- Never present mock data, unrun commands, or skipped core tests as successful live validation.
- Keep `AGENTS.md`, `README.html`, and `IMPLEMENTATION_STATUS.md` aligned with actual behavior after material changes.
- Do not run `git add`, `commit`, `push`, publish, deploy, or send reports externally without explicit authorization.

## Run and verification

- Provide one simple Windows entry for login, preview, scheduled operation, and opening reports.
- The scheduler only works while the program and computer are running. Do not silently register an autostart task.
- Record exact verification commands, exit codes, and passed/failed/skipped counts when those commands exist.
- Do not claim the MVP is complete until offline checks, browser checks, real dual reports, OCR/Agnes checks, scheduling, and failure drills have all passed.

Current commands:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.lock
.venv\Scripts\python.exe -m app.cli
.venv\Scripts\python.exe -m pytest -ra
.venv\Scripts\ruff.exe check app tests
.venv\Scripts\python.exe -m compileall -q app tests
```
