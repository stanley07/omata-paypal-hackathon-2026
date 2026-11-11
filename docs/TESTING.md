# Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m compileall -q app tests
```

Tests use `httpx.MockTransport` and synthetic IDs/credentials. They **do not contact PayPal**. Test databases are uniquely named under ignored `artifacts/tests`, within the project. They cover parsing, exact decimals, missing/invalid values, approval/rejection, authentication, CSRF, origin and host enforcement, tenant isolation, OAuth failure/cache/refresh, duplicate create/capture, timeouts after upstream acceptance, expired-operation blocking, payment mismatches, forged and duplicate webhooks, failed-webhook retry, body limits and sandbox-only checkout URLs.

Passing these tests proves behavior against modeled responses, not real PayPal interoperability. Follow [REAL_SANDBOX.md](REAL_SANDBOX.md) for separate operator-run evidence. Actual run results are recorded in [VALIDATION.md](VALIDATION.md).

For manual UI checks, use desktop and 390px mobile widths. Register two merchants; create and review a draft; reject one; approve another; inspect customers/audit; reload and sign back in. Confirm every button has a readable focus state, errors are announced, dialogs fit the viewport, tables scroll without page overflow and no completed revenue appears before server verification. Do not fabricate paid records for screenshots.
