# Three-minute video script

**0:00–0:25 — The problem**

“Small-business owners already describe sales in conversation. Omata Pay turns those words into a reviewed order and a verifiable PayPal payment. This is a new, independent hackathon application inspired by the broader Omata AI concept.” Show the dashboard and sandbox badge.

**0:25–0:55 — Describe and review**

Enter the milk example. “Five cartons, twenty-five dollars each: one hundred and twenty-five dollars. We validate the numbers with Decimal arithmetic and require the merchant to confirm every detail.” State the actual mode: “This recording uses our explicitly labeled development parser,” or, only after live validation, “This proposal came from our configured AI provider.”

**0:55–1:20 — Merchant control**

“The proposal is a draft. I approve it explicitly, and that decision is timestamped. Approval itself does not call PayPal. I now choose to create a sandbox checkout link.” Show the real approved order and audit.

**1:20–2:05 — Payment integrity**

If genuinely verified: open sandbox checkout, approve as test buyer, return. “A redirect is not a receipt. The merchant initiates server capture; Omata Pay independently checks the remote order, capture, currency and amount.” Show COMPLETED and the verified event only after it actually happens.

If sandbox remains pending, substitute: “The real REST adapter is implemented, but credentials and real sandbox validation are pending. The application correctly blocks checkout here. Our automated tests use mocks and are not a claim of real payment.” Show the gate and the manual procedure. Do not splice a mock into a supposed live payment.

**2:05–2:35 — Trust and resilience**

“Each merchant sees only their own records. Stable request IDs protect retries, and uncertain captures are reconciled against PayPal. Webhook events require signature verification and duplicate protection. Nothing completes solely because someone clicks a button.” Show isolation and actual test results.

**2:35–3:00 — Originality and close**

“The PayPal workflow, approval controls, independent verification, local accounts and this dashboard were built for the hackathon. The production Omata AI app is separate and untouched. Our README distinguishes what is implemented, what was tested with mocks and what has been verified live. Omata Pay: a few words, a clear decision, a payment you can verify.”
