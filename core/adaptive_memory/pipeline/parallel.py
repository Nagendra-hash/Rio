"""
core/adaptive_memory/pipeline/parallel.py — WorkerPool with backpressure and async processing.

ARCHITECTURE POSITION
    Generic async worker pool for CPU-bound and I/O-bound tasks. Designed for
    adaptive memory pipelines where ingestion, embedding, and indexing stages
    must run concurrently without overwhelming memory or API quotas.

DESIGN
    - Fixed worker count (configurable) with a bounded asyncio.Queue for backpressure.
    - Tasks submitted via `submit(coro)` return an awaitable Future.
    - `submit_nowait(coro)` raises `QueueFull` when backpressure threshold hit.
    - Graceful shutdown: `shutdown(wait=True)` drains queue, `shutdown(wait=False)` cancels pending.
    - Worker exceptions are captured and attached to the returned Future.
    - Optional `max_tasks_per_worker` recycles workers to bound memory growth.
    - Structured logging via stdlib `logging` (no external deps).

USAGE
    pool = WorkerPool(num_workers=4, max_queue_size=100)
    fut = pool.submit(embed_chunk(chunk))
    result = await fut
    await pool.shutdown()

THREAD SAFETY
    All public methods are thread-safe for submission from sync threads.
    Internally uses asyncio primitives; call from the running event loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
import traceback
from collections.abc import Awaitable, Callable
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class QueueFull(Exception):
    """Raised by submit_nowait when the backpressure queue is full."""
    pass


class WorkerPoolClosed(Exception):
    """Raised when submitting to a pool that has been shut down."""
    pass


@dataclass(slots=True)
class WorkerStats:
    """Per-worker runtime statistics."""
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_latency_ms: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    last_task_at: float | None = None

    def record_success(self, latency_ms: float) -> None:
        self.tasks_completed += 1
        self.total_latency_ms += latency_ms
        self.last_task_at = time.monotonic()

    def record_failure(self, latency_ms: float) -> None:
        self.tasks_failed += 1
        self.total_latency_ms += latency_ms
        self.last_task_at = time.monotonic()

    @property
    def avg_latency_ms(self) -> float:
        total = self.tasks_completed + self.tasks_failed
        return self.total_latency_ms / total if total else 0.0

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at


@dataclass(slots=True)
class PoolStats:
    """Aggregated pool statistics."""
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    queue_full_rejections: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc_submitted(self) -> None:
        with self._lock:
            self.submitted += 1

    def inc_completed(self) -> None:
        with self._lock:
            self.completed += 1

    def inc_failed(self) -> None:
        with self._lock:
            self.failed += 1

    def inc_cancelled(self) -> None:
        with self._lock:
            self.cancelled += 1

    def inc_queue_full(self) -> None:
        with self._lock:
            self.queue_full_rejections += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "submitted": self.submitted,
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled,
                "queue_full_rejections": self.queue_full_rejections,
            }


class WorkerPool(Generic[T]):
    """
    Async worker pool with bounded queue backpressure.

    Parameters
    ----------
    num_workers: Number of concurrent worker tasks.
    max_queue_size: Maximum pending tasks (backpressure limit). 0 = unbounded.
    max_tasks_per_worker: Recycle worker after N tasks (0 = never recycle).
    worker_factory: Optional async callable to initialise per-worker state.
                    Receives worker index, returns arbitrary state object.
    worker_teardown: Optional async callable(state) to clean up worker state.
    name: Pool name for logging/metrics.
    """

    def __init__(
        self,
        num_workers: int = 4,
        max_queue_size: int = 100,
        max_tasks_per_worker: int = 0,
        worker_factory: Callable[[int], Awaitable[Any]] | None = None,
        worker_teardown: Callable[[Any], Awaitable[None]] | None = None,
        name: str = "WorkerPool",
    ) -> None:
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")

        self._num_workers = num_workers
        self._max_queue_size = max_queue_size
        self._max_tasks_per_worker = max_tasks_per_worker
        self._worker_factory = worker_factory
        self._worker_teardown = worker_teardown
        self._name = name

        self._queue: asyncio.Queue[tuple[asyncio.Future[T], Callable[[], Awaitable[T]]]] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._worker_states: list[Any] = []
        self._worker_stats: list[WorkerStats] = []
        self._stats = PoolStats()
        self._closed = False
        self._shutdown_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._init_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Bind to the current event loop on first use."""
        with self._init_lock:
            if self._loop is None:
                try:
                    self._loop = asyncio.get_running_loop()
                    self._loop_thread_id = threading.get_ident()
                    self._queue = asyncio.Queue(maxsize=self._max_queue_size)
                    self._worker_stats = [WorkerStats() for _ in range(self._num_workers)]
                    self._worker_states = [None] * self._num_workers
                    self._spawn_workers()
                    logger.info("[%s] bound to loop %s with %d workers, queue=%s",
                                self._name, id(self._loop), self._num_workers,
                                "unbounded" if self._max_queue_size == 0 else self._max_queue_size)
                except RuntimeError:
                    raise RuntimeError("WorkerPool must be used from a running event loop")
            elif threading.get_ident() != self._loop_thread_id:
                raise RuntimeError("WorkerPool bound to a different thread's event loop")
            return self._loop

    def _spawn_workers(self) -> None:
        assert self._loop is not None
        assert self._queue is not None
        for i in range(self._num_workers):
            task = self._loop.create_task(self._worker_loop(i), name=f"{self._name}-worker-{i}")
            self._workers.append(task)

    async def _worker_loop(self, worker_idx: int) -> None:
        assert self._queue is not None
        stats = self._worker_stats[worker_idx]

        # Optional per-worker initialisation
        if self._worker_factory is not None:
            try:
                self._worker_states[worker_idx] = await self._worker_factory(worker_idx)
            except Exception as e:
                logger.exception("[%s] worker-%d factory failed: %s", self._name, worker_idx, e)
                self._worker_states[worker_idx] = None

        tasks_since_recycle = 0

        while True:
            try:
                fut, coro_factory = await self._queue.get()
            except asyncio.CancelledError:
                break
            except Exception:
                # Queue closed or pool shutting down
                break

            if fut.cancelled():
                self._queue.task_done()
                self._stats.inc_cancelled()
                continue

            t0 = time.monotonic()
            try:
                result = await coro_factory()
                fut.set_result(result)
                stats.record_success((time.monotonic() - t0) * 1000)
                self._stats.inc_completed()
            except asyncio.CancelledError:
                fut.cancel()
                stats.record_failure((time.monotonic() - t0) * 1000)
                self._stats.inc_cancelled()
            except Exception as e:
                fut.set_exception(e)
                stats.record_failure((time.monotonic() - t0) * 1000)
                self._stats.inc_failed()
                logger.debug("[%s] worker-%d task failed: %s", self._name, worker_idx, e)
            finally:
                self._queue.task_done()

            tasks_since_recycle += 1
            if self._max_tasks_per_worker and tasks_since_recycle >= self._max_tasks_per_worker:
                # Recycle worker to bound memory (e.g., embedding model caches)
                await self._recycle_worker(worker_idx)
                tasks_since_recycle = 0

        # Teardown on exit
        if self._worker_teardown is not None and self._worker_states[worker_idx] is not None:
            try:
                await self._worker_teardown(self._worker_states[worker_idx])
            except Exception:
                logger.exception("[%s] worker-%d teardown failed", self._name, worker_idx)

    async def _recycle_worker(self, worker_idx: int) -> None:
        """Tear down and re-initialise a single worker slot."""
        if self._worker_teardown is not None and self._worker_states[worker_idx] is not None:
            try:
                await self._worker_teardown(self._worker_states[worker_idx])
            except Exception:
                logger.exception("[%s] worker-%d recycle teardown failed", self._name, worker_idx)
        self._worker_states[worker_idx] = None
        if self._worker_factory is not None:
            try:
                self._worker_states[worker_idx] = await self._worker_factory(worker_idx)
                logger.debug("[%s] worker-%d recycled", self._name, worker_idx)
            except Exception:
                logger.exception("[%s] worker-%d recycle factory failed", self._name, worker_idx)

    # ── submission API ──────────────────────────────────────────────────────────

    def submit(self, coro_factory: Callable[[], Awaitable[T]]) -> asyncio.Future[T]:
        """
        Submit a coroutine factory for execution.

        Parameters
        ----------
        coro_factory: Zero-argument callable returning an awaitable.
                      Called inside the worker, NOT at submission time.
                      This avoids capturing stale contextvars at submit point.

        Returns
        -------
        asyncio.Future resolving to the coroutine's result.

        Raises
        ------
        WorkerPoolClosed: if the pool has been shut down.
        RuntimeError: if called outside the bound event loop.
        """
        loop = self._ensure_loop()
        if self._closed:
            raise WorkerPoolClosed(f"{self._name} has been shut down")

        fut: asyncio.Future[T] = loop.create_future()
        self._stats.inc_submitted()

        # Capture contextvars at submission point so worker inherits them
        ctx = contextvars.copy_context()

        def wrapped_factory() -> Awaitable[T]:
            return ctx.run(coro_factory)

        try:
            if self._max_queue_size == 0:
                self._queue.put_nowait((fut, wrapped_factory))
            else:
                # Block until queue has space (backpressure)
                put_future = asyncio.run_coroutine_threadsafe(
                    self._queue.put((fut, wrapped_factory)), loop
                )
                put_future.result()  # Wait for put to complete
        except asyncio.QueueFull:
            self._stats.inc_queue_full()
            fut.cancel()
            raise QueueFull(f"{self._name} queue full (max={self._max_queue_size})")

        return fut

    def submit_nowait(self, coro_factory: Callable[[], Awaitable[T]]) -> asyncio.Future[T]:
        """
        Non-blocking submit. Raises QueueFull immediately if backpressure limit reached.
        """
        loop = self._ensure_loop()
        if self._closed:
            raise WorkerPoolClosed(f"{self._name} has been shut down")

        fut: asyncio.Future[T] = loop.create_future()
        self._stats.inc_submitted()

        ctx = contextvars.copy_context()

        def wrapped_factory() -> Awaitable[T]:
            return ctx.run(coro_factory)

        try:
            self._queue.put_nowait((fut, wrapped_factory))
        except asyncio.QueueFull:
            self._stats.inc_queue_full()
            fut.cancel()
            raise QueueFull(f"{self._name} queue full (max={self._max_queue_size})")

        return fut

    # ── introspection ───────────────────────────────────────────────────────────

    @property
    def queue_size(self) -> int:
        """Current pending task count."""
        return self._queue.qsize() if self._queue else 0

    @property
    def max_queue_size(self) -> int:
        return self._max_queue_size

    @property
    def num_workers(self) -> int:
        return self._num_workers

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def idle_workers(self) -> int:
        """Workers currently not processing a task (approximate)."""
        if not self._queue:
            return self._num_workers
        return max(0, self._num_workers - self._queue.qsize())

    def get_stats(self) -> dict[str, Any]:
        """Aggregated pool + per-worker statistics."""
        worker_stats = [
            {
                "worker": i,
                "completed": s.tasks_completed,
                "failed": s.tasks_failed,
                "avg_latency_ms": round(s.avg_latency_ms, 2),
                "uptime_seconds": round(s.uptime_seconds, 1),
            }
            for i, s in enumerate(self._worker_stats)
        ]
        return {
            "pool": self._stats.snapshot(),
            "queue_size": self.queue_size,
            "max_queue_size": self._max_queue_size,
            "num_workers": self._num_workers,
            "idle_workers": self.idle_workers,
            "workers": worker_stats,
            "closed": self._closed,
        }

    # ── shutdown ────────────────────────────────────────────────────────────────

    async def shutdown(self, wait: bool = True, timeout: float | None = 30.0) -> None:
        """
        Stop the pool.

        Parameters
        ----------
        wait: True = drain queue before stopping workers.
              False = cancel pending tasks immediately.
        timeout: Maximum seconds to wait for drain (wait=True only).
        """
        if self._closed:
            return
        self._closed = True

        if self._queue is None:
            return  # Never initialised

        if wait:
            # Wait for queue to drain
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("[%s] shutdown timeout after %.1fs, %d tasks pending",
                               self._name, timeout, self._queue.qsize())

        # Cancel workers
        for w in self._workers:
            w.cancel()

        # Wait for worker tasks to finish cancellation
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        self._shutdown_event.set()
        logger.info("[%s] shutdown complete (wait=%s)", self._name, wait)

    async def __aenter__(self) -> WorkerPool[T]:
        self._ensure_loop()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.shutdown(wait=True)


# ── convenience: map-style parallel execution ──────────────────────────────────

async def map_async(
    func: Callable[[Any], Awaitable[T]],
    items: list[Any],
    *,
    num_workers: int = 4,
    max_queue_size: int = 100,
    max_tasks_per_worker: int = 0,
    worker_factory: Callable[[int], Awaitable[Any]] | None = None,
    worker_teardown: Callable[[Any], Awaitable[None]] | None = None,
    name: str = "map_async",
) -> list[T]:
    """
    Parallel map with backpressure. Preserves input order in results.

    Example
    -------
    results = await map_async(embed_chunk, chunks, num_workers=8)
    """
    pool = WorkerPool(
        num_workers=num_workers,
        max_queue_size=max_queue_size,
        max_tasks_per_worker=max_tasks_per_worker,
        worker_factory=worker_factory,
        worker_teardown=worker_teardown,
        name=name,
    )
    try:
        futures = [pool.submit(lambda item=item: func(item)) for item in items]
        return await asyncio.gather(*futures)
    finally:
        await pool.shutdown(wait=True)


async def map_async_unordered(
    func: Callable[[Any], Awaitable[T]],
    items: list[Any],
    *,
    num_workers: int = 4,
    max_queue_size: int = 100,
    max_tasks_per_worker: int = 0,
    worker_factory: Callable[[int], Awaitable[Any]] | None = None,
    worker_teardown: Callable[[Any], Awaitable[None]] | None = None,
    name: str = "map_async_unordered",
) -> list[T]:
    """
    Parallel map without order guarantee. Yields results as they complete.

    Example
    -------
    async for result in map_async_unordered(fetch_url, urls):
        process(result)
    """
    pool = WorkerPool(
        num_workers=num_workers,
        max_queue_size=max_queue_size,
        max_tasks_per_worker=max_tasks_per_worker,
        worker_factory=worker_factory,
        worker_teardown=worker_teardown,
        name=name,
    )
    try:
        futures = [pool.submit(lambda item=item: func(item)) for item in items]
        for fut in asyncio.as_completed(futures):
            yield await fut
    finally:
        await pool.shutdown(wait=True)


# ── sync-friendly submission from threads ──────────────────────────────────────

def submit_from_thread(
    pool: WorkerPool[T],
    coro_factory: Callable[[], Awaitable[T]],
    *,
    timeout: float | None = 10.0,
) -> ConcurrentFuture[T]:
    """
    Submit to WorkerPool from a synchronous thread (e.g., Flask request handler).

    Returns a concurrent.futures.Future that resolves in the caller's thread.
    The coroutine executes on the pool's event loop.
    """
    loop = pool._ensure_loop()  # May raise if pool not bound
    cfut: ConcurrentFuture[T] = ConcurrentFuture()

    def _done_callback(afut: asyncio.Future[T]) -> None:
        try:
            cfut.set_result(afut.result())
        except Exception as e:
            cfut.set_exception(e)

    def _submit() -> None:
        try:
            afut = pool.submit(coro_factory)
            afut.add_done_callback(_done_callback)
        except Exception as e:
            cfut.set_exception(e)

    loop.call_soon_threadsafe(_submit)
    return cfut


# ── BatchProcessor: checkpointing wrapper for large-scale ingestion ─────────────

@dataclass(slots=True)
class BatchProcessor(Generic[T]):
    """
    Wrapper that adds checkpointing and resumability on top of WorkerPool.

    Designed for long-running ingestion jobs (thousands of items) where
    interruptions are expected. Checkpoints allow resume from the last
    completed item.

    Parameters
    ----------
    processor: Async callable(item) -> result. Processes a single item.
    checkpoint_manager: CheckpointManager instance for persistence.
    run_id: Unique identifier for this run (used as checkpoint key).
    pool: WorkerPool instance (created if not provided).
    batch_size: Checkpoint every N items (also controls pool queue size).
    """
    processor: Callable[[Any], Awaitable[T]]
    checkpoint_manager: Any  # CheckpointManager (avoid circular import)
    run_id: str
    pool: WorkerPool[T] | None = None
    batch_size: int = 100

    def __post_init__(self) -> None:
        if self.pool is None:
            self.pool = WorkerPool(
                num_workers=4,
                max_queue_size=self.batch_size * 2,
                name=f"BatchProcessor-{self.run_id}",
            )

    async def process_all(
        self,
        items: list[Any],
        *,
        resume: bool = False,
    ) -> list[T]:
        """
        Process all items, checkpointing every batch_size items.

        If resume=True, skips items already checkpointed.
        Returns results in input order (including resumed items).
        """
        # Load checkpoint if resuming
        completed: dict[int, T] = {}
        if resume:
            try:
                ckpt = await self.checkpoint_manager.load(self.run_id)
                if ckpt and "completed" in ckpt:
                    completed = {int(k): v for k, v in ckpt["completed"].items()}
            except Exception:
                logger.warning("[BatchProcessor-%s] no valid checkpoint, starting fresh", self.run_id)

        results: list[T | None] = [None] * len(items)
        # Fill in already-completed
        for idx, val in completed.items():
            if 0 <= idx < len(results):
                results[idx] = val

        # Find items to process
        to_process = [(i, item) for i, item in enumerate(items) if i not in completed]
        if not to_process:
            return [r for r in results if r is not None]

        # Submit in batches
        for batch_start in range(0, len(to_process), self.batch_size):
            batch = to_process[batch_start:batch_start + self.batch_size]
            futures = [
                self.pool.submit(lambda item=item: self.processor(item))
                for _, item in batch
            ]
            batch_results = await asyncio.gather(*futures, return_exceptions=True)

            # Collect results and checkpoint
            for (idx, _), result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    logger.error("[BatchProcessor-%s] item %d failed: %s",
                                 self.run_id, idx, result)
                    raise result
                results[idx] = result

            # Checkpoint progress
            completed.update({i: r for i, r in enumerate(results) if r is not None})
            await self.checkpoint_manager.save(self.run_id, {
                "completed": completed,
                "total": len(items),
                "timestamp": time.time(),
            })
            logger.info("[BatchProcessor-%s] checkpoint: %d/%d",
                        self.run_id, len(completed), len(items))

        return [r for r in results if r is not None]

    async def shutdown(self, wait: bool = True) -> None:
        """Shutdown the underlying pool."""
        if self.pool:
            await self.pool.shutdown(wait=wait)


# Alias for backward compatibility / expected exports
map_parallel = map_async