# Catalogue changelog

Written by `scripts/publish_catalogue.py` whenever an entry changes. Newest first.
The same changes are in `changes[]` of `catalogue.v1.json`.

## 2026-11-15

- Limits changed for `cerebras` (ai services): 1000000 tokens per day (gpt-oss-120b, free trial, per model) [verified]; 5 requests per minute (gpt-oss-120b, free trial, per model) [verified]; 1000000 tokens per day (qwen-3.8-27b, free trial, per model) [verified]; 5 requests per minute (qwen-3.8-27b, free trial, per model) [verified]; reset: rolling → 1000000 tokens per day (gpt-oss-120b only, while the $5 trial credit lasts; not a renewing free tier) [verified]; 5 requests per minute (gpt-oss-120b, free trial) [verified]; 300 requests per minute (qwen-3.8-27b, free trial; no hourly or daily token cap on this model) [verified]; reset: unknown.
- Policy fact changed for `cerebras` (ai services): connection.card_required unknown → yes.
- Date checked changed for `cerebras` (ai services): checked_on 2026-11-12 → 2026-11-15.
- Details changed for `cerebras` (ai services): allowance, good_for, models, notes, policy, read_remaining, source_urls, tier.
- Policy fact changed for `gemini` (ai services): policy.clinical_use None → forbidden.
- Date checked changed for `gemini` (ai services): checked_on 2026-11-12 → 2026-11-15.
- Details changed for `gemini` (ai services): models, notes, policy, source_urls.
- Date checked changed for `groq` (ai services): checked_on 2026-11-12 → 2026-11-15.
- Details changed for `groq` (ai services): models, source_urls.
- Added `sambanova` (ai services).

## 2026-11-13

- Added `line-messaging-api` (other allowances).
