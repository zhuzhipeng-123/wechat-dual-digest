# Implementation status

Updated: 2026-09-23

## Delivery level

- **Code complete for the implementable A-D scope:** yes.
- **Offline verification complete:** yes, for the cases listed below.
- **Real target-account source verified:** no; real Feed URLs have not been configured.
- **Real email received:** no; SMTP remained disabled and no message was sent.
- **Real recruitment OCR/Agnes analysis verified:** no; stage E remains separate.

This is not a claim that unattended WeChat subscription and delivery already works in the user environment. The remaining real checks require a source that actually covers the two target accounts and explicit authorization/configuration for email.

## Baseline before this change

- Branch and commit: `main` at `b6b415f32e5c2cabb3dbadc11ae38510f4e7ce79`.
- Existing uncommitted user material: untracked `11/`; preserved and not edited.
- `.venv\Scripts\python.exe -m pytest -ra` → exit 0, 25 passed.
- `.venv\Scripts\ruff.exe check app tests` → exit 0.
- `.venv\Scripts\ruff.exe format --check app tests` → exit 0, 12 files already formatted.
- `.venv\Scripts\python.exe -m compileall -q app tests` → exit 0.

The improvement plan's main findings were still present: production used no real discoverer, scheduling was permanently refused, images were not placed into immutable report assets, recruitment lacked full-text fallback, and there was no cross-run article ledger or delivery step. Existing original-page parsing, task claiming, templates, Agnes validation, and immutable run directories were retained.

## Implemented in this change

### Source and original content

- One RSS/Atom consumer, configured per account through an environment-variable name in TOML. It distinguishes valid empty sources, authentication/HTML responses, malformed XML, network failure, and unsafe cross-origin redirects.
- Feed labels, titles, and dates remain hints. Formal inclusion uses the original WeChat page's exact account, title, and publication time. A missing original account is not filled from `expected_account`.
- Stable article identity uses token paths or identity-bearing WeChat query fields; original fetch URLs are retained separately.
- HTML uses a tag/attribute allowlist that also cleans the content root. Article and Feed bodies have size limits.
- Images use validated HTTPS hosts, per-redirect checks, streaming 12 MiB limits, an 80 MiB run limit, generated safe filenames, hashes, and original-position replacement. Repeated references reuse one file without appending a duplicate gallery.
- Practice articles with missing images remain pending and retryable. Recruitment reports keep sanitized full original text and available images when analysis is disabled or fails.

### Collection, publication, and delivery

- A bounded late-arrival range is separate from the fixed 24-hour main window. Preview does not consume formal collection state.
- SQLite migrations are repeatable and preserve the existing `runs` table. A thin `articles` table records formal collection; only a complete published report consumes an article.
- The claim's `run_id` is reused by the ledger, report directory, JSON, manifest, article records, and delivery record.
- A complete run directory is written first with HTML, JSON, assets, and `.complete.json`. Only then is the single fixed HTML entry atomically replaced.
- SMTP is the only delivery channel. It is disabled by default, uses environment variables for secrets, creates a multipart message with CID images, and rejects messages over 20 MiB instead of silently dropping content.
- Delivery states distinguish `not_requested`, `pending`, `sending`, `sent`, `failed`, and `unknown`. Automatic retry is limited to pending/failed. A forced retry of sent/unknown requires an explicit flag and warns about duplicates.
- `retry-send` reads an existing complete report and does not discover, fetch, OCR, or call a model.

### Scheduling and recovery

- The real Feed runner is connected to the two-worker scheduler; Demo and WeRead probes are not formal inputs.
- Startup checks require enabled accounts, stable account keys, configured Feed environment variables, writable runtime paths, and complete SMTP settings when delivery is enabled.
- Optional same-day catch-up only claims an unclaimed task. Existing failed, running, or uncertain runs are not automatically reset.
- Schedule changes are stored and take effect the next day. Each claim stores a secret-safe snapshot; the worker reconstructs its task/accounts/delivery settings from that frozen snapshot.
- The cooperative total budget starts at claim time. Network calls remain individually bounded; Python threads and local OCR are not falsely described as hard-cancelled.
- Suspicious `claimed`, `running`, or `sending` rows are listed but never auto-stolen. `recover-report <run_id> --confirm-stopped` only reconciles a matching complete manifest after the user confirms the old process stopped; it does not refetch or resend.

### Commands

```powershell
.venv\Scripts\python.exe -m app.cli check
.venv\Scripts\python.exe -m app.cli preview --kind both
.venv\Scripts\python.exe -m app.cli schedule
.venv\Scripts\python.exe -m app.cli retry-send <run_id>
.venv\Scripts\python.exe -m app.cli recover-report <run_id> --confirm-stopped
```

The menu remains available through `start.cmd` or `.venv\Scripts\python.exe -m app.cli`. `probe-login` remains a diagnostic only. `samples` remains explicitly synthetic.

## Offline verification after this change

All tests use fixed clocks, temporary directories, `httpx` mock transports, fake SMTP, and an automatic socket guard.

- `.venv\Scripts\python.exe -m pytest -ra` → exit 0, 38 passed, 0 failed, 0 skipped.
- `.venv\Scripts\ruff.exe check app tests` → exit 0, all checks passed.
- `.venv\Scripts\ruff.exe format --check app tests` → exit 0, 14 files already formatted.
- `.venv\Scripts\python.exe -m compileall -q app tests` → exit 0.
- `.venv\Scripts\python.exe -m app.cli --help` → commands were registered.
- `.venv\Scripts\python.exe -m app.cli check` → intentionally non-zero with the existing ignored local config because both accounts lack the new source bindings; no Feed URL or SMTP secret was printed.

New tests cover RSS/Atom items and valid emptiness, login pages, malformed sources, cross-origin redirects, streaming Feed cut-off, missing original identity, original-title filtering, duplicate URLs with inconsistent Feed GUIDs, in-place repeated images, streaming image cut-off, bounded late collection, pre-fetch cross-run skip, pre-discovery budget expiry, disabled-task configuration, hostless Feed rejection, practice incomplete-image retry, recruitment full-text fallback, manifest publication, portable CID email with repeated-asset reuse, sent/failed/unknown states, no automatic unknown resend, idempotent migration, snapshot redaction, next-day schedule activation, same-day catch-up semantics, unified run ID, formal collection after complete publication, and explicit report recovery.

## Real verification still open

1. Configure `PRACTICE_FEED_URL` and `RECRUITMENT_FEED_URL` for sources that actually include “我爱学逻辑” and “国聘”.
2. For each account, compare a multi-article or limited-history sample and record identity match, discovered/missed counts, publication-to-discovery delay, content/image completeness, and source failure behavior.
3. Run a real practice preview and recruitment preview with analysis disabled; inspect original order and images in a browser.
4. Explicitly configure and authorize SMTP, run one real send, and verify the received HTML and images on the intended device. SMTP `sent` only means the service accepted the message.
5. Keep the program running across a configured minute and verify claim time, frozen window, report completion, duplicate prevention, catch-up, source failure, and interrupted recovery.
6. Stage E: separately validate real recruitment images with OCR and real article evidence with the existing fixed Agnes service/model. No paid call was made in this change.

## Intentionally not added

No FastAPI/REST server, Web administration UI, Redis/Celery, multiple source providers, source auto-fallback, multiple delivery channels, RSS publishing, MCP, generic crawler/database, vector store, or multi-agent system was added. The project remains a local single-user tool.
