"""The lease a Postgres-authority commit holds while it is published to ClickHouse.

Its publisher (the writer, or for memory its process's publication batch) sends nothing
once the lease is about to end, and every insert it sends starts within the transit
allowance of that end (`rmt_carry.under_lease`): an erasure that removed the delivery
waits out the lease (`pg.wait_out`) and then finds every copy the publisher could land.
Past it, the delivery worker publishes the committed rows, and a write still waiting for
them answers PUBLICATION_PENDING (the memory writes that answer at their commit:
docs/architecture/storage-authority.md, "Publication"). Shared by the memory and
knowledge authorities.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator

from server.core import pg, rmt_carry
from server.core.error_summary import error_summary
from server.core.errors import LoomServerError
from server.settings import settings

log = logging.getLogger(__name__)


class PublicationPending(LoomServerError):
    """503: the write is committed and the delivery worker publishes its rows; it is
    not to be made again. Which ops answer it: storage-authority.md, "The write path"
    (memory) and "Knowledge documents"."""

    code = "PUBLICATION_PENDING"
    status_code = 503

    def __init__(self) -> None:
        super().__init__(
            "the write is committed; do not repeat it. Its ClickHouse rows follow shortly",
            committed=True,
            retry_after_s=1,
        )

    @classmethod
    def after(cls, cause: BaseException) -> PublicationPending:
        """The answer to a committed write whose publication failed, the cause logged."""
        log.warning(
            "a committed write's publication is left to the delivery worker: %s",
            error_summary(cause),
        )
        return cls()


def lease_deadline(lease_seconds: float, started: float) -> float:
    return started + lease_seconds - settings.pg_lease_margin_seconds


def check_lease(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise PublicationPending()


def check_lease_after_durable_read(deadline: float) -> None:
    """`check_lease` after a durable read (`pg.durable_transaction`), for a lease that was
    live before it: one the read ended (its standby wait) is a Postgres outage, which a
    pass releases uncounted, not a late send counted as the delivery's attempt."""
    if time.monotonic() >= deadline:
        raise pg.PostgresUnavailableError("the durable read outlasted the lease")


@contextlib.contextmanager
def leased(deadline: float | None) -> Iterator[None]:
    """ClickHouse writes made under the lease ending at `deadline` (None: under none):
    refused once the lease is about to end, and every insert they send starts within
    the transit allowance of that end however long it waits (`rmt_carry.under_lease`),
    which an erasure waits out."""
    if deadline is None:
        yield
        return
    check_lease(deadline)
    with rmt_carry.under_lease(deadline):
        yield
