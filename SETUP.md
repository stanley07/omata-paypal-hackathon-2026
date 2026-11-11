# Isolated Windows setup

Run every command from the root directory of the cloned OmataPay repository.

1. Install/use Python 3.11 or newer. Never install this application's dependencies globally.
2. Create `.venv` with `python -m venv .venv`.
3. Install: `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
4. For the exact resolved package set from this implementation's validation machine, use `requirements.lock.txt` once generated. It targets the tested Python/Windows environment; other platforms may need the direct-dependency file.
5. Launch: `.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000`.
6. Open `http://127.0.0.1:8000`. Register your own local account. No preset or shared login exists.

Use a single Uvicorn worker for the local demo. SQLite lives under `data/omata.db` and is created at startup. It contains customer/order information and password hashes: protect the Windows account and project folder. Do not publish or upload the database. Do not point this application at any existing Omata AI database.

## Environment configuration

The app reads **process environment only**. `.env.example` is documentation, not a credential template to populate. The operator should use an approved credential manager or secure process launcher to inject credential values. Do not paste credentials into chat, command arguments/history, screenshots, files or commits. Configuration is intentionally manual; no OAuth probe is performed here.

| Variable | Default / meaning |
|---|---|
| `APP_BASE_URL` | `http://127.0.0.1:8000`; exact browser origin and PayPal return origin |
| `COOKIE_SECURE` | `false` for loopback HTTP only; HTTPS requires `true` |
| `PAYPAL_ENABLED` | `false`; requires operator authorization before setting `true` |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | Dedicated sandbox application's credentials, injected securely |
| `PAYPAL_WEBHOOK_ID` | Sandbox webhook registration ID, for verified delivery |
| `AI_PROVIDER` | `deterministic` or `chat-completions` |
| `AI_ENABLED` | `false`; set `true` only for authorized provider calls |
| `AI_ENDPOINT` | Full HTTPS chat-completions endpoint; no redirects followed |
| `AI_MODEL`, `AI_API_KEY` | Provider model and securely injected key |

The PayPal base URL is not configurable. `data/`, `.venv/`, `.env*`, caches and test artifacts are ignored by Git. Do not rely on `.gitignore` alone to protect secrets.

## Troubleshooting

- A checkout error saying calls are disabled is expected before operator setup. Drafts and approvals still work.
- A 403 on POST usually means the browser origin differs from `APP_BASE_URL` or the session's CSRF token is stale. Use the exact configured URL and sign in again.
- A payment timeout is not proof of failure. Retry the same local order; never create a replacement to work around an uncertain request. After five hours, uncertain creation requires operator reconciliation.
- If pip cannot use the system temp directory, create `artifacts/tmp` inside the project and set process `TEMP` and `TMP` to its absolute path before rerunning. Keep all package installs in `.venv`.
- Git may report dubious ownership on this machine. A session-only `git -c safe.directory=<exact-project-path> status --short` avoids modifying global Git configuration; investigate ownership separately.
- No password reset, email delivery or account recovery is included. Use a separate test account if needed; never edit financial state to force a successful demo.

No cloud deployment, tunnel, webhook registration, public hosting or GitHub push is part of setup. These need separate authorization.
