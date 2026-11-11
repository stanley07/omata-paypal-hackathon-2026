# Actual validation record

Date: 2026-11-09. Environment: Windows, Python 3.14.8, Node 24.19.0. Dependencies installed into this project's `.venv`; exact resolved package versions are in `requirements.lock.txt`.

| Check actually executed | Observed result |
|---|---|
| `.venv\Scripts\python.exe -m pytest -q` (final run) | **53 passed, 1 warning in 12.52s** |
| `.venv\Scripts\python.exe -m ruff check app tests` | **All checks passed** |
| `.venv\Scripts\python.exe -m compileall -q app tests` | Exit 0 |
| `node --check app/static/app.js` | Exit 0 |
| `.venv\Scripts\python.exe -m pip check` | No broken requirements found |
| Uvicorn factory startup on 127.0.0.1:8000 | Application startup complete |
| Local HTTP `GET /health` | `status=ok`, `mode=sandbox`, integration verification pending |
| HTML, host restrictions and response security headers | Exercised through TestClient tests |
| Desktop/mobile visual or browser end-to-end QA | **BLOCKED**: computer-use browser access host unavailable; no visual pass claimed |
| Real PayPal OAuth, checkout, capture, GET | **NOT RUN**: operator authorization/configuration required |
| Real webhook signature/delivery | **NOT RUN** |
| Live AI provider inference | **NOT RUN** |

The final warning is an upstream Starlette TestClient reference to the deprecated AnyIO BlockingPortal alias. It does not fail the suite; dependency maintenance should address it before a production release. Pytest's optional cache plugin is disabled because this managed environment denied its temporary-directory writes; test execution remains enabled.

An earlier run produced 39 passes and one failure: the deterministic parser accepted a multi-product sentence as one product. The parser was corrected to reject additional financial clauses; the case passes in the final suite. An unused import found by Ruff was removed. Further tests verify password redaction in validation errors, pre-capture amount checks, concurrent operation exclusion, process-restart key persistence, malformed checkout responses and AI adapter success/failure without network calls.

Every external provider interaction in automated tests uses `httpx.MockTransport` and synthetic identifiers/credentials. Passing tests are evidence of application behavior against those models, **not** genuine PayPal or AI responses. No financial API requests, credential entry, production access, deployment or GitHub push were performed.

Only Python 3.14.8 on this Windows machine was executed. Python 3.11+ compatibility is a code target, not a tested version matrix. Browser visual quality, accessibility and mobile interaction require manual validation when browser access is available. See ARCHITECTURE for remaining security limitations.

Next recommended validation: the operator should explicitly authorize and securely configure the dedicated sandbox process, then execute steps 1–8 of REAL_SANDBOX.md and record actual OAuth/create/capture/verification evidence.
