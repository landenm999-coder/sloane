# Capture → Sloane: the intake contract

The Capture app (the separate repo `landenm999-coder/capture`) sends what Landen
says to Sloane with one HTTP call. This is everything the client needs.

## Endpoint

```
POST https://<box>.<tailnet>.ts.net/capture
Authorization: Bearer <CAPTURE_TOKEN>
Content-Type: application/json
```

- The address comes from `tailscale serve` on the box (DEPLOY.md §7d). The phone must be on the same tailnet.
  The address is never public.
- The request comes from the browser on his phone, so it is cross-origin. Sloane answers the CORS preflight
  (and Chrome's Private Network Access preflight) only for origins listed in `CORS_ORIGINS`, and only on
  `/capture`. A preflight that fails shows up in the browser as a network error.
- The token is `CAPTURE_TOKEN` from the box's `.env`. It must be at least 32 characters, or the endpoint is off.
  Store it the way the app stores any secret, and never put it in a URL.

## Request body

| field | type | required | meaning |
|---|---|---|---|
| `text` | string, 1–20,000 chars | yes | what he said or typed, already transcribed |
| `kind` | `"note"` \| `"transcript"` | no (default `"note"`) | a typed note, or a transcribed voice capture |
| `captured_at` | ISO 8601 string | no | when he said it. With no offset it's read as America/Denver. Future times are clamped to now |

The whole body must be 64 KB or less.

```json
{"text": "remind me tomorrow at 7 to bring the lab", "kind": "transcript",
 "captured_at": "2026-09-22T21:14:03-06:00"}
```

## Responses

| status | body | client should |
|---|---|---|
| `201` | `{"stored": true, "id": "<uuid>", "kind": "note"}` | mark it sent |
| `201` + reminder | `…, "reminder": "tomorrow at 7:00 AM: bring the lab"` | show "Reminder set: …" |
| `201` + no time | `…, "reminder": null, "note": "…no time could be read…"` | show "Saved as a note (no time found)" |
| `201` + action | `…, "action": "Added milk to your grocery list; 3 things on it now."` | show the `action` line |
| `400` | `{"error": "…"}` | the request is wrong; don't retry, surface it |
| `401` | `{"error": "unauthorized"}` | the token is wrong; ask him to re-enter it |
| `413` | `{"error": "…"}` | too long; split it or trim it |
| `503` | `{"error": "capture is not enabled on this box"}` | `CAPTURE_TOKEN` is unset on the box |
| network error / `5xx` | — | keep it queued and retry with backoff; the capture must not be lost |

## What Sloane does with it

- Stores it as an episode in **his own voice** (`trusted`, `role=user`, `source=capture`), dated `captured_at`,
  so later questions recall it ("what was that idea about the bakery site?").
- A capture that starts with "remind me …" becomes a real reminder, read by the same rules as the chat. It is
  delivered on Telegram with snooze buttons.
- A capture that is exactly one of the chat's own skill phrases -- "add milk to my grocery list", "spent 12 on
  lunch", "did reading", "Keegan's birthday is March 3" -- is acted on by the same rules, and `action` says what
  was done. It is never taken as the answer to a quiz running in the chat.
- Nothing else. It is not an instruction channel: text that says "email Keegan" is stored as a note, not acted on.

## Client checklist

1. Settings: Sloane URL + token (secure storage), and a "Test connection" button that posts
   `{"text": "capture test", "kind": "note"}` and expects `201`.
2. After each capture is transcribed, POST it. On failure, queue it offline and retry; don't drop it.
3. Show the `reminder` line, or the `action` line, when present.
