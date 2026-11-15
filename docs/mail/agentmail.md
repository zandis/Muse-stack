# AgentMail adapter (draft only)

The Muse AgentMail adapter reads `muse-aimedics@agentmail.to` and can create or update drafts. It cannot send mail.

Shapes below were checked against the [AgentMail API reference](https://docs.agentmail.to) on 2026-11-15. The REST base is `https://api.agentmail.to/v0`. Authentication is `Authorization: Bearer <token>`.

## No PHI check

This inbox and this adapter are not for protected health information. The code labels inbound mail as untrusted. That is not de-identification, and it is not a clearance to put clinical content into a model.

Before each use, confirm every item:

- [ ] The draft, subject, labels, and anything you paste from a message contain no PHI (no patient names, medical record numbers, dates of birth, addresses tied to a patient, device identifiers, or clinical content).
- [ ] Inbound mail stays labeled untrusted. Do not pass it to a model if it might contain PHI. `for_model` only adds the untrusted label. It does not remove PHI.
- [ ] The API key stays in the key file. It is not committed, not passed on the command line, and not read from `AGENTMAIL_API_KEY`.

The module docstring in `muse/mail/agentmail.py` states the same No PHI rule.

## API key

The client reads one token from the file named by `AGENTMAIL_KEY_FILE`. The default path is `~/.muse/secrets/agentmail.key`.

```bash
mkdir -p ~/.muse/secrets
umask 077
printf '%s\n' 'am_...' > ~/.muse/secrets/agentmail.key
chmod 600 ~/.muse/secrets/agentmail.key
```

The file must not be readable by group or others. The client refuses a wider mode. There is no `api_key` argument and the token is never hard-coded.

Requests go only to `https://api.agentmail.to/v0`, or to `http://127.0.0.1` / `http://localhost` with a port when tests point at a fake server. Any other host is rejected before the key is sent.

## Allowlist

Every request goes through `check_allowed`. The call is rejected when the path contains `/send`, or when the method and path are not in the table below. The inbox segment must be `muse-aimedics@agentmail.to` except on `GET /inboxes`. Redirects are refused, so a 3xx response cannot hop onto a send URL.

`send_at` is rejected on create and update. A draft cannot be scheduled to send later. There is no send function. A path segment whose text is `send` is rejected as well, because the path then contains `/send`.

| Operation | Method | Path |
| --- | --- | --- |
| List inboxes | GET | `/v0/inboxes` |
| List messages | GET | `/v0/inboxes/{inbox_id}/messages` |
| Read message | GET | `/v0/inboxes/{inbox_id}/messages/{message_id}` |
| List threads | GET | `/v0/inboxes/{inbox_id}/threads` |
| Read thread | GET | `/v0/inboxes/{inbox_id}/threads/{thread_id}` |
| List drafts | GET | `/v0/inboxes/{inbox_id}/drafts` |
| Create draft | POST | `/v0/inboxes/{inbox_id}/drafts` |
| Update draft | PATCH | `/v0/inboxes/{inbox_id}/drafts/{draft_id}` |

These endpoints are not called, and the guard raises if something tries:

- `POST /v0/inboxes/{inbox_id}/messages/send` (send message)
- `POST /v0/inboxes/{inbox_id}/drafts/{draft_id}/send` (send draft)
- Reply, reply-all, forward, delete, and every other AgentMail route

`GET /inboxes` can show other inboxes the key can see. This client will not read their messages, threads, or drafts.

### Fields

List calls accept `limit` (1–100), `page_token`, `labels`, `before`, `after`, and `ascending`. Message and thread lists also accept `include_spam`.

Create draft accepts `to`, `cc`, `bcc`, `reply_to`, `subject`, `text`, `html`, `in_reply_to`, `forward_of`, `reply_all`, `labels`, and `client_id`. At least one of `subject`, `text`, `html`, `in_reply_to`, or `forward_of` is required.

Update draft accepts `to`, `cc`, `bcc`, `reply_to`, `subject`, `text`, `html`, `add_labels`, and `remove_labels`.

`send_at` and attachments are omitted. Official create/update schemas include both. Omitting `send_at` keeps a draft from being scheduled. Attachments are out of scope for this adapter (they can also be URL fetches on the AgentMail side).

Inbox ids and message ids are percent-encoded as single path segments. Message ids from AgentMail are RFC 822 ids and may contain `<`, `>`, and `@`.

## Untrusted inbound mail

Messages and threads returned by this client are untrusted data.

- HTML bodies are reduced to visible text. `script`, `style`, and comments are dropped.
- Plain text is preferred (`extracted_text`, then `text`, then stripped HTML, then `preview`).
- A plain-text angle address such as `Ann <ann@example.com>` is left as written. It is not parsed as HTML.
- Bodies are capped at 16,000 characters (`MAX_BODY_CHARS`). Subjects are capped at 500. Truncated text ends with `[truncated]`.
- Raw HTML and message headers are not copied into the result.
- Each inbound message and thread has `trust` set to `untrusted` and a `model_text` that starts with `[untrusted]` and ends with `[/untrusted]`. The same markers inside the mail are renamed first, so the sender cannot close that region early.

Pass mail to a model only through `for_model`. It returns that labeled text and refuses anything that is not already marked untrusted, including drafts.

```python
from muse.mail.agentmail import AgentMailClient, for_model

client = AgentMailClient()
message = client.get_message("<message-id>")
prompt_text = for_model(message)
```

`for_model` does not remove PHI. If a message might contain PHI, do not pass it to a model.

## CLI

`scripts/mail_check.py` lists inbox messages or creates a draft. It cannot send.

```bash
python scripts/mail_check.py
python scripts/mail_check.py list --limit 20
python scripts/mail_check.py draft --to person@example.com --subject "Status" --text "Draft body"
```

List output is JSON. Each message is untrusted. Draft output has `"sent": false`.

## Tests

Tests use a local fake HTTP server. They do not call `api.agentmail.to`. They cover the allowlist (including send paths), key loading, untrusted wrapping, and draft creation.
