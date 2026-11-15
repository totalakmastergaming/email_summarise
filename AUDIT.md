# InboxCura — Professional Features Audit

**Date:** 2026-11-16
**Auditor:** Antigravity (Google) — pulled latest code at commit `f2160775`
**Scope:** the complete InboxCura Gmail workspace (FastAPI + TiDB Serverless +
Gemini/Groq, Render free tier) — judged as a Gmail client with an AI
intelligence layer, not as a summarisation app that happens to have an inbox.
**Method:** static code review of every route, service and UI file, plus a
live check of the deployed app at https://emailsummarise.onrender.com/.

---

## What is good

- **API authentication.** The whole `/api` surface requires
  `Authorization: Bearer <ADMIN_TOKEN>` (503 fail-closed when unset, 401 on
  mismatch, timing-safe compare). The cron webhook keeps its own
  `CRON_SECRET` on a separate router.
- **API keys encrypted at rest.** Provider keys are Fernet-encrypted in TiDB
  (`fernet:v1:` prefix), with transparent decrypt-on-read and legacy
  plaintext rows re-encrypted on next write.
- **Resilient startup.** The lifespan never blocks on the database: DB init
  runs in a background thread with retries, and pymysql has connect/read/
  write timeouts. A cold TiDB cluster can no longer wedge the deploy.
- **Pipeline failover.** Gemini → Groq failover with per-key cooldowns,
  dedup before save, single-flight lock, bounded batches, retry backoff,
  and a base case for the context-split recursion.
- **Real Gmail operations.** Full-body reader (format `full`), real labels
  (Gmail `modify` + local mirror), bulk state via one `batchModify`,
  reply/send through the Gmail API (in-thread, with `In-Reply-To` /
  `References`), server-side search.
- **Honest error paths in the API.** Unknown mail is a 404, Gmail refusals
  are 502s, and bulk state only flips local rows when Gmail accepted the
  change.

## What is bad

- **60-mail cap with a dead pager.** `GET /api/mails` stops at `LIMIT 60`
  and the Older/Newer buttons in the toolbar are hardcoded `disabled`.
  Any mailbox with more than 60 mails cannot be browsed past the first
  page, and the range text ("1–N of N") is fabricated.
- **Mislabeled compose button.** The compose submit button says
  "Open in mail app" but actually sends through the Gmail API.
- **Snooze is a stub.** Every snooze control shows a toast saying it is
  not available; there is no snooze behaviour at all.
- **Lossy forwarding.** Forward prefills the compose box from the cached
  plain-text body only; HTML mail and attachments do not survive.
- **No conversation threading.** Every message is its own inbox row. A
  12-message thread occupies 12 rows, and replies do not collapse into
  the conversation.
- **Sync only checks the latest 50.** `/api/sync-inbox-state` reads the 50
  newest local rows and polls each one individually. Older mail never
  gets its Gmail-side changes, and there is no incremental sync — no
  Gmail History API, no checkpoint, no recovery from an expired
  history id.
- **Unindexed search.** Search is `LIKE %q%` over subject/sender/snippet
  with no full-text index; it scans the whole table on every keystroke
  (debounced) and is still capped at 60 rows.
- **Console-only errors.** Failed full-body loads, failed page loads and
  failed actions are only `console.log`ged. The UI keeps showing a
  successful-looking state when Gmail rejected the operation.
- **Optimistic state flips.** Mark-read and star toggle the row first and
  sync second, so a failed Gmail call leaves the UI lying about the
  mailbox state.

## What needs to be added

- **Attachments** — list and download (and attach on compose).
- **Conversation threading** — one inbox entry per thread, messages
  collapsed chronologically inside the reader, reply in-thread.
- **Folders** — Sent, Trash, Spam and All Mail views, not just the inbox
  tabs.
- **Draft autosave** — compose and reply drafts persisted, not lost on
  close or refresh.
- **Pub/Sub push** — Gmail push notifications instead of polling, for
  near-instant sync.
- **CC / BCC** — in compose and reply-all.
- **Server-side pagination** — cursor-based, no 60-row cap.

## What needs to improve

- **"Draft with AI" is a static template**, not AI — it inserts the same
  canned paragraph for every mail. Either generate a real draft from the
  thread or label it honestly.
- **Compose is plain-text only.** No rich text, no formatting toolbar.
- **Assistant is single-turn.** It has no memory of the conversation and
  no thread context beyond the latest briefing.
- **Priority-1 key absorbs all traffic.** Failover is standby-only; there
  is no load balancing or per-key RPM enforcement even though
  `rate_limit_rpm` is stored.
- **Updates / Forums missing from the UI.** Gmail categorises them, the
  backend maps them, but the inbox tabs only show Primary / Social /
  Promotions.
- **Error states must become visible.** Every failed load and failed
  action needs a visible error with a retry, in the UI — never a silent
  console entry.
- **Urgent / needs-reply detection is heuristic.** Urgent is a keyword
  list on the subject; "needs reply" is a sender/subject guess. True
  reply detection needs thread history (has the user already replied in
  this thread?).

## Roadmap (from the ChatGPT strategy discussion, 2026-11-16)

- **Phase 1 — next release:** server-side pagination, true conversation
  threading, History-based incremental sync foundation.
- **Phase 2 — before marketing as a complete Gmail client:** folders,
  attachments, CC/BCC, draft autosave.
- **Phase 3 — polish:** full-text search, rich-text compose, real AI
  drafting, key load balancing.
- **Phase 4 — scale:** Pub/Sub push, snooze, smarter briefing.
- **Deliberately deferred:** offline mode, Elasticsearch, Redis/Celery,
  large attachment storage, continuous background sync.
