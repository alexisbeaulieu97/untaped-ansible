"""Bounded thread-pool mapping shared by application use cases and adapters.

This module sits at the package root (like ``settings``) because both the
application layer and infrastructure adapters drive bounded worker pools,
and the DDD layering rules forbid them from importing each other.
It must stay dependency-free: stdlib only, no ``cli``/``application``/
``infrastructure`` imports.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed


def bounded_map[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
    *,
    concurrency: int,
    on_each: Callable[[ItemT, ResultT], None],
) -> None:
    """Apply ``fn`` to every item using at most ``concurrency`` worker threads.

    ``on_each(item, result)`` runs only on the calling thread: in input order
    when running serially (``len(items) <= 1`` or ``concurrency == 1``),
    otherwise in completion order. Exceptions raised by ``fn`` propagate to
    the caller from the consume loop. When anything escapes that loop —
    including ``KeyboardInterrupt`` — queued-but-unstarted work is cancelled
    instead of drained, so Ctrl-C stops large runs promptly rather than
    hanging while the executor's default shutdown runs every queued task.
    """
    if len(items) <= 1 or concurrency == 1:
        for item in items:
            on_each(item, fn(item))
        return
    with ThreadPoolExecutor(max_workers=min(concurrency, len(items))) as executor:
        try:
            futures = {executor.submit(fn, item): item for item in items}
            for future in as_completed(futures):
                on_each(futures[future], future.result())
        except BaseException:
            executor.shutdown(cancel_futures=True)
            raise
