# Judge demonstration

Start with the status table in README. Install and run locally using SETUP. No credentials are required to reproduce parsing, approval, isolation or the dashboard.

1. Create a unique merchant account. Point out the sandbox indicator and the exact interpretation mode label.
2. Enter the milk example. Explain that the default parser is deterministic and no live AI result is being claimed. If a real provider has been separately configured and verified, show its live provenance instead.
3. Review quantity, unit price, currency and total. Approve. Explain that this records the merchant decision but does not create a payment.
4. Show the approved order, customer reference and audit trail. Reject another draft to demonstrate control. A separate merchant sees only their own records.
5. If real sandbox validation is complete, create a checkout, have the sandbox buyer approve, then capture and independently verify from the merchant dashboard. Show the verified capture and metrics. Otherwise, show the configuration gate and clearly state that the real integration remains pending. Never use mocked tests as proof of a real payment.
6. Show the automated test results and explain timeout recovery, stable idempotency IDs, signature verification and the independent confirmation boundary.

Prepare two browser profiles if demonstrating buyer/merchant separation. Use only test references. Avoid recording authentication input, developer environment values or credentials. The local app has no artificial paid data or seeded account.

Scope: a single-recipient sandbox demonstration for multiple isolated local merchant workspaces, not a production payment facilitator. The pre-existing Omata AI product was not modified.
