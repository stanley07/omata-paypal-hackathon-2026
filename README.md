# Omata Pay

**Your words. A reviewed order. A verifiable payment.**

Omata Pay is an independent PayPal AI Hackathon 2026 application for small-business merchants. Describe a sale, review its exact quantities and amounts, explicitly approve the draft, create a PayPal **sandbox** checkout link, then capture and independently verify payment from the server. The responsive dashboard includes order history, customer references, metrics by currency and a merchant-scoped audit trail.

No production Omata AI services are used. No payment or AI requests occur on startup. No sample payment is represented as real.

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** (use this exact origin). Create a local merchant account with a password of at least 12 characters. Python 3.11+ is required; see [SETUP.md](SETUP.md) for isolation, configuration and troubleshooting.

Try `Prepare a PayPal payment for 5 cartons of milk at $25 each.` Review the $125.00 proposal and approve or reject it. Approval alone performs no network request. Checkout creation remains disabled until the operator separately authorizes and enables sandbox requests.

## AI: what actually runs

The default is a deliberately narrow **deterministic development parser, not live AI**. Both logs and the dashboard label this explicitly. It supports one product, a whole quantity, an explicit USD/EUR/GBP price and optional `for Customer` reference. `$` explicitly means USD. Missing or ambiguous details cause validation errors.

The `Interpreter` adapter also supports an authorized HTTPS provider implementing the chat-completions request/response contract. It extracts structured proposals, validates them with Decimal arithmetic, and requires merchant approval. Configure `AI_PROVIDER=chat-completions`, `AI_ENABLED=true`, `AI_ENDPOINT`, `AI_MODEL` and securely inject `AI_API_KEY` into the process. There is no silent fallback after provider failures. Live AI inference has **not been verified**. Customer text is transmitted to that provider only when the operator enables it and a merchant submits a request. Review the selected provider's privacy terms first.

## PayPal: real integration, gated execution

`app/paypal.py` implements OAuth client credentials with token caching, Orders v2 creation, sandbox checkout link extraction, server capture, independent GET verification, signature verification and redacted errors. The API origin is hardcoded to `https://api-m.sandbox.paypal.com`; live endpoints cannot be configured. Checkout navigation never completes an order.

The operator must inject the dedicated sandbox application's `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` into the process securely, outside files, source control, command history and chat. This project never asks you to paste them. After explicit authorization for sandbox financial API calls, set `PAYPAL_ENABLED=true`. `.env.example` documents names only; the app intentionally does **not** load `.env` files. See [the manual sandbox procedure](docs/REAL_SANDBOX.md) before enabling anything. `PAYPAL_WEBHOOK_ID` is additionally required for webhook verification.

## Evidence and honest status

| Capability | Status |
|---|---|
| Local accounts, proposals, approvals, isolation, dashboard | Implemented; automated validation recorded in `docs/VALIDATION.md` |
| Deterministic parser | Implemented, explicitly labeled; no AI inference |
| Configurable AI adapter | Implemented; real provider validation pending |
| PayPal OAuth / create / capture / independent GET | Implemented; mocked tests only; real sandbox validation pending |
| Signed webhook receiver / deduplication | Implemented; mocked tests only; real delivery pending |
| Production payments, deployment, GitHub publication | Not performed; outside this task |

## Reproduce and judge

- [Setup](SETUP.md)
- [Architecture and security limits](docs/ARCHITECTURE.md)
- [Automated tests](docs/TESTING.md) and [actual validation record](docs/VALIDATION.md)
- [Manual real sandbox validation](docs/REAL_SANDBOX.md)
- [Judge demonstration guide](docs/JUDGE_GUIDE.md)
- [Three-minute video script](docs/VIDEO_SCRIPT.md)
- [Originality statement](docs/ORIGINALITY.md)

All implementation in this repository was created for this hackathon work: the local application, PayPal payment workflow, integrity safeguards, dashboard, tests and documentation. The broader Omata AI concept predates this submission; its production code, data and services were not accessed or reused. MIT licensed; see [LICENSE](LICENSE).

Primary API references: [PayPal REST integration](https://developer.paypal.com/api/rest/integration/orders-api), [idempotent requests](https://developer.paypal.com/api/make-api-requests), and [webhook verification](https://developer.paypal.com/api/rest/webhooks/rest/). A provider's successful response is still subject to application-level amount, currency, reference and capture checks.
