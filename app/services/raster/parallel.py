"""Ordered parallel map so tiled writers can stay streaming."""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def default_workers() -> int:
    return max(1, os.cpu_count() or 1)


def ordered_parallel_map(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    workers: int,
) -> Iterator[R]:
    """Apply ``fn`` with a thread pool, yielding results in input order."""
    sequence = items if isinstance(items, list) else list(items)
    if workers <= 1 or len(sequence) <= 1:
        for item in sequence:
            yield fn(item)
        return

    workers = max(1, int(workers))
    prefetch = max(workers * 2, workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(sequence)
        pending: deque = deque()

        def _refill() -> None:
            while len(pending) < prefetch:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                pending.append(pool.submit(fn, item))

        _refill()
        while pending:
            result = pending.popleft().result()
            _refill()
            yield result


def unordered_parallel_map(
    items: Iterable[T],
    fn: Callable[[T], R],
    *,
    workers: int,
) -> Iterator[R]:
    sequence = items if isinstance(items, list) else list(items)
    if workers <= 1 or len(sequence) <= 1:
        for item in sequence:
            yield fn(item)
        return
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(fn, item) for item in sequence]
        for fut in as_completed(futures):
            yield fut.result()


def run_unordered(
    items: Iterable[T],
    fn: Callable[[T], None],
    *,
    workers: int,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Apply ``fn`` with a bounded thread pool; completion order is undefined."""
    sequence = items if isinstance(items, list) else list(items)
    if workers <= 1 or len(sequence) <= 1:
        for item in sequence:
            fn(item)
            if on_done is not None:
                on_done()
        return

    workers = max(1, int(workers))
    prefetch = max(workers * 4, workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        iterator = iter(sequence)
        inflight: set = set()

        def _submit() -> bool:
            try:
                item = next(iterator)
            except StopIteration:
                return False
            inflight.add(pool.submit(fn, item))
            return True

        while len(inflight) < prefetch and _submit():
            pass
        while inflight:
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                fut.result()
                if on_done is not None:
                    on_done()
                _submit()
