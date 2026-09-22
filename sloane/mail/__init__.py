"""Gmail (P4). Reading is automatic; anything that leaves the account is not.

Two halves, kept apart on purpose:

* `gmail.py` is the transport: OAuth refresh, list, fetch, send, draft. It has
  no opinions about what should be sent.
* `inbox.py` is the judgement: triage in one batched call, drafts in Landen's
  voice, and the two actions (`reply`, `draft`) that go through the approval
  flow like everything else she does.
"""


class MailError(RuntimeError):
    """Gmail could not be reached, or refused. Carries no credential material."""
