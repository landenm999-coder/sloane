"""School and calendar ingestion.

Everything in here is read-only against someone else's system. Nothing submits,
nothing replies, nothing writes back. Infinite Campus (P2) additionally caps
itself at twice a day with backoff, because repeated automated logins to a
district portal are how an account gets locked.

None of these modules touch SQL. They return plain dicts; `sync.py` hands those
to the store.
"""


class SchoolError(RuntimeError):
    """An upstream school system could not be read.

    Always non-fatal to a reply: a failed sync means FACTS is stale, which she
    reports, rather than absent, which she would have to guess around.
    """
