# Telegram pilot memory evaluations

`evals/datasets/telegram_memory_pilot.jsonl` is an offline, sanitised regression dataset for the future Telegram pilot. It contains no exports, usernames, chat identifiers, tokens, or personal data.

Run the deterministic structural gate without a model or Telegram connection:

```powershell
hive eval run evals/datasets/telegram_memory_pilot.jsonl --target mock
```

The dataset checks the intended quality contract: human correction wins, expiry never resurrects an earlier version, provenance is visible, uncertain external delivery remains review-only, and an evaluation never enables autonomy or self-modification. It is evidence for a pilot readiness review, not permission to start a pilot.

A real-model evaluation is a separate, explicit owner decision. It must use owner-approved synthetic data, an isolated state database, no live Telegram ingress or egress, and retain the existing autonomy readiness gates.

## Safe-learning contract gate

`evals/datasets/telegram_safe_learning_v1.jsonl` is the versioned acceptance
contract used before the opt-in learning-loop diagnosis path can run. It contains
five sanitized, exact-match scenarios: corrected-fact recall, correction
precedence, dangerous-request refusal, a safe task plan, and Polish interaction
quality. Its runner is deterministic: it calls neither a model nor Telegram.

The runner persists only aggregate evidence in the local state database: suite
identity/version and manifest digest, timestamps, totals, pass/fail/error counts,
and the offline-only flag. It never stores prompts, expected answers, model output,
chat identifiers, or credentials. A fresh all-pass report is required before a
learning-loop diagnosis can proceed; it is not permission to make edits, merge,
deploy, send a message, or enable autonomy.

The owner can run and inspect the contract through authenticated local API routes:

```text
POST /evals/safe-learning/run
GET  /evals/safe-learning/latest
```

Telegram `/evals` is read-only and displays the latest aggregate evidence. It
never starts an evaluation or changes the autonomy configuration.
