# Roadmap and definition of done

v1 is P0 through P4 from the build plan, plus the polish below. The build loop
works through this list and stops only when every box is checked **and** two
consecutive passes find nothing actionable.

## Built

- [x] **P0 — spine.** Four memory tiers, router, contract, hard lines, Telegram, FastAPI.
- [x] **P1 — memory + school.** Canvas, calendar `.ics`, generated shifts, course matching, `/sync`.
- [x] **P2 — rhythm.** Five daily briefs, computed conflicts, quiet hours, budget, scheduler.
- [x] Hybrid recall (vector + full-text, RRF) and memory provenance.
- [x] Deploy: Dockerfile, compose, systemd, `DEPLOY.md`.
- [x] CI on Python 3.12 against real Postgres; Dependabot.

## In progress

- [x] **P3 — voice out.** Telegram voice replies reading `speech` only; text always survives a TTS failure.
- [x] **P4 — agency machinery.** Trust ledger (10 clean approvals unlock an exact pair, 60-day decay, any reversal re-gates), Approve / Edit / Deny buttons, hard lines unlockable never.
- [ ] Every confirmed finding from the correctness and security reviews fixed, each with a regression test.

## Remaining

- [x] **P4 — Gmail.** Triage batched into one bulk call, drafts in his voice, every send through the approval flow, school staff drafts-only. Tested end to end against a stub Gmail; the one-time OAuth consent is a user step.
- [x] Docker image builds for `linux/arm64` in CI and passes a smoke test (imports, `claude` resolves as the non-root user).
- [x] Eval harness: golden questions through the real model on a seeded day, scored against the exact answers. First run 21/21.
- [ ] Dependabot PRs triaged: safe ones merged once green; risky ones (major runtime bumps) closed with a reason.
- [ ] Docs, `.env.example` and `doctor.py` agree with the code; a final review pass finds nothing high-severity.

## Deliberately out of v1

- **Infinite Campus.** Needs district credentials, can't be exercised off the
  district network, and repeated automated logins can lock the account. Canvas
  already carries assignments; IC would add grades.
- **Phone calls (P5).** Beyond v1.

## Only Landen can do

These are the whole list — everything else is automated.

1. Create the Oracle ARM instance (`DEPLOY.md` §1).
2. On the box: clone, `.env`, `docker compose build`, migrations (`DEPLOY.md` §2–5).
3. Log the Claude CLI in once (`DEPLOY.md` §6).
4. Optional: Gmail — a Google Cloud OAuth client and one run of `scripts/gmail_auth.py` (`DEPLOY.md` §7c).
