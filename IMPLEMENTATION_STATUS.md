# Implementation status

Updated: 2026-09-20

## Confirmed scope

- The one-time project rules and operating defaults were confirmed by the user on 2026-09-20.
- First release is the dual-digest MVP only. MCP, RSS, Markdown export, FastAPI, a web administration UI, and generic article-platform features remain out of scope.

## Implemented

- Python 3.12 project, fully pinned Windows environment, ignored local runtime paths, blank config examples, and `start.cmd`.
- Application code reduced from 19 small modules to 8 cohesive responsibility modules, plus `__init__.py`.
- Typed contracts for frozen runs, candidates, articles, images, errors, and reports.
- Strict `Asia/Shanghai` settings and ordered recruitment/practice account configuration.
- Exact 86400-second `[start, end)` windows, practice title rules, exact account matching, and candidate de-duplication.
- Safe WeChat article/image URL policies, original HTML parsing, precise-time extraction probes, sanitization, and bounded image download helpers.
- Practice report pipeline and recruitment no-model degradation with distinct source/fetch/analysis states.
- Local RapidOCR wrapper; fixed Agnes Chat Completions client; strict JSON/Schema/source/evidence validation; job-location relationships; code-owned P0-P3 rating.
- Static recruitment/practice HTML and JSON, isolated preview paths, immutable run versions, and fixed official entry replacement.
- SQLite run ledger, atomic per-window claims, duplicate prevention, bounded two-worker scheduler, and failed-run persistence.
- Console entry for login probe, offline samples, opening reports, and an explicit refusal to schedule while real discovery is unverified.
- `README.html` generated under the frontend-design workflow and two clearly labeled synthetic sample reports.

## Offline verification

All results below are local/offline or synthetic unless explicitly marked otherwise.

- `.venv\Scripts\python.exe -m pytest -ra` → exit 0, 25 passed, 0 failed, 0 skipped.
- `.venv\Scripts\ruff.exe check app tests` → exit 0, all checks passed.
- `.venv\Scripts\ruff.exe format --check app tests` → exit 0, 12 files already formatted after consolidation.
- Offline tests install an automatic socket guard; any attempted TCP connection fails the test.
- `.venv\Scripts\python.exe -m compileall -q app tests` → exit 0.
- RapidOCR was executed against `data/verification/synthetic-recruitment.png` → exit 0; it recognized both synthetic Chinese lines with confidence above 0.99. This is not a real WeChat image test.
- Real Chromium opened the two synthetic sample reports at desktop and 390 px mobile widths. Both reports had no horizontal overflow and no console errors after the favicon fix.
- Real Chromium opened `README.html`; desktop visual inspection passed. A mobile command-block overflow was found, fixed, and rechecked at 390 px with `scrollWidth=375` and no console errors.

## Real verification

- WeRead web session in the browser used for acceptance inspection: login cookies and logged-in page state observed.
- Application-owned persistent browser profile: not logged in or verified; it is separate from the acceptance browser session.
- Public-account discovery: failed/unverified. The current logged-in `weread.qq.com` search returned electronic books and full-text book matches, not public-account articles. No public-account discovery entry was observed.
- Direct original-page access: passed for both user-supplied links in the acceptance browser and in the application-owned headless Chrome profile. Titles, exact account names, exact publication times, body content, and images were observable.
- Additional original articles: passed for one linked `我爱学逻辑` article and one `国聘` article reached through a public search result.
- Sogou WeChat discovery probe: unsuitable for official scheduling. Browser automation was redirected to an anti-spider CAPTCHA; plain HTTP search omitted the supplied newest articles under generic account queries, returned unstable signed links, and produced too many stale candidates. A live preview attempt was stopped after more than two minutes rather than publishing an unreliable result.
- Official Tencent terms found during verification describe the client syncing collected/floating-window/followed-public-account articles, but no official public web article-search entry was found.
- Known URLs → original → exact publication time: verified for both tracks. Account-name-only discovery → the same known URLs remains unverified.
- Local OCR on a real recruitment image: not verified.
- Agnes protocol: passed with one real synthetic request to the fixed service and model. Input contained no article, account, image, Cookie, or path; `max_tokens=120`; response model was `agnes-2.5-flash`, content was `{"probe":"ok"}`, and usage was 335 total tokens. The reference Key was loaded only into that child process and was not copied or printed.
- Real recruitment article extraction: not verified because no real account/article evidence is available.
- Real practice/recruitment reports: not verified.
- Real configured-minute scheduling, duplicate launch, source failure, model timeout, and publication-interruption drills: not verified.

## Known gaps and impact

- Stage A does not pass: the planned sole discovery source is not available on the verified web surface. The code intentionally does not guess hidden endpoints or selectors.
- Without a verified discovery adapter, official scheduling remains disabled; enabling it would risk false zero-result reports.
- Ignored local configuration now uses recruitment account `国聘`, practice account `我爱学逻辑`, title keyword `每日一题`, and the 21:00 schedules. These values are confirmed by the supplied originals, but account-name-only discovery is not reliable enough to enable scheduling.
- B and C have offline implementations and evidence, but their real acceptance conditions remain open because A and the required user-owned inputs are missing.

## Next action

1. Choose a discovery channel with an explicit completeness expectation: an authenticated WeChat-capable source, a third-party provider with accepted miss/availability risk, or user-forwarded/direct URLs.
2. Only after that choice, connect the verified original-page reader to scheduled reports and run the configured-minute/duplicate/failure drills.
3. Run a real practice report and a no-model recruitment report from discovered—not manually seeded—articles.
4. Use the already verified reference Key only after real article evidence exists; keep each article call text-only and budgeted.
5. Run real image OCR, recruitment extraction, configured-minute scheduling, browser review, and fault drills.
