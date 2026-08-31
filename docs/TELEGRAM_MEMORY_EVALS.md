# Telegram pilot memory evaluations

`evals/datasets/telegram_memory_pilot.jsonl` is an offline, sanitised regression dataset for the future Telegram pilot. It contains no exports, usernames, chat identifiers, tokens, or personal data.

Run the deterministic structural gate without a model or Telegram connection:

```powershell
hive eval run evals/datasets/telegram_memory_pilot.jsonl --target mock
```

The dataset checks the intended quality contract: human correction wins, expiry never resurrects an earlier version, provenance is visible, uncertain external delivery remains review-only, and an evaluation never enables autonomy or self-modification. It is evidence for a pilot readiness review, not permission to start a pilot.

A real-model evaluation is a separate, explicit owner decision. It must use owner-approved synthetic data, an isolated state database, no live Telegram ingress or egress, and retain the existing autonomy readiness gates.