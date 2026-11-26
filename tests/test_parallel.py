"""
tests/test_parallel.py — offline tests for WorkerPool (core/adaptive_memory/pipeline/parallel.py).

No network, no model: pure async logic with deterministic sleeps and mock factories.
Covers:
  * basic submit + gather
  * backpressure (QueueFull on submit_nowait)
  * worker recycling (max_tasks_per_worker)
  * contextvar propagation across threads
  * graceful shutdown (wait=True drains, wait=False cancels)
  * stats aggregation
  * map_async / map_async_unordered convenience helpers
  * submit_from_thread for sync callers

Run:  .venv/bin/python -m pytest tests/test_parallel.py -q
"""
from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
import time
from concurrent.futures import Future as ConcurrentFuture
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from core.adaptive_memory.pipeline.parallel import (  # noqa: E402
    WorkerPool,
    QueueFull,
    WorkerPoolClosed,
    map_async,
    map_async_unordered,
    submit_from_thread,
    BatchProcessor,
    map_parallel,
)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _slow_task(delay: float, value: int) -> int:
    await asyncio.sleep(delay)
    return value


async def _fail_task() -> int:
    await asyncio.sleep(0.01)
    raise ValueError("intentional failure")


def _make_factory(value: int, delay: float = 0.01):
    """Return a zero-arg factory capturing `value` and `delay`."""
    return lambda: _slow_task(delay, value)


# ── tests ────────────────────────────────────────────────────────────────────

class TestBasicSubmit:
    def test_submit_and_gather(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                futures = [pool.submit(_make_factory(i)) for i in range(5)]
                results = await asyncio.gather(*futures)
                assert results == [0, 1, 2, 3, 4]
        asyncio.run(run())

    def test_submit_preserves_order(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                futures = [pool.submit(_make_factory(i, delay=0.05)) for i in [2, 0, 1]]
                results = await asyncio.gather(*futures)
                assert results == [2, 0, 1]  # submission order, not completion order
        asyncio.run(run())

    def test_submit_nowait_success(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                fut = pool.submit_nowait(_make_factory(42))
                assert await fut == 42
        asyncio.run(run())

    def test_submit_nowait_queue_full_raises(self):
        async def run():
            pool = WorkerPool(num_workers=1, max_queue_size=2, name="TinyPool")
            try:
                # Fill queue: 1 running + 1 queued = 2
                pool.submit_nowait(lambda: _slow_task(1.0, 1))   # starts running
                pool.submit_nowait(lambda: _slow_task(0.1, 2))   # queued
                with pytest.raises(QueueFull):
                    pool.submit_nowait(lambda: _slow_task(0.1, 3))  # rejected
            finally:
                await pool.shutdown(wait=False)
        asyncio.run(run())

    def test_submit_after_close_raises(self):
        async def run():
            pool = WorkerPool(num_workers=2, max_queue_size=5, name="TestPool")
            await pool.shutdown(wait=True)
            with pytest.raises(WorkerPoolClosed):
                pool.submit(_make_factory(1))
        asyncio.run(run())


class TestBackpressure:
    def test_submit_blocks_until_queue_space(self):
        async def run():
            pool = WorkerPool(num_workers=1, max_queue_size=2, name="BPPool")
            try:
                done = asyncio.Event()

                async def blocker():
                    await asyncio.sleep(0.5)
                    done.set()
                    return 1

                async def quick():
                    return 2

                f1 = pool.submit(blocker)
                f2 = pool.submit(quick)
                f3 = pool.submit(quick)  # this should wait

                # f2 should complete before f3 even starts
                r2 = await f2
                assert r2 == 2

                # Now f1 completes, f3 starts
                r1 = await f1
                r3 = await f3
                assert r1 == 1 and r3 == 2
            finally:
                await pool.shutdown(wait=True)
        asyncio.run(run())

    def test_unbounded_queue(self):
        async def run():
            pool = WorkerPool(num_workers=1, max_queue_size=0, name="Unbounded")
            try:
                for i in range(20):
                    pool.submit_nowait(_make_factory(i))
                results = await asyncio.gather(*[pool.submit(_make_factory(i)) for i in range(20, 30)])
                assert results == list(range(20, 30))
            finally:
                await pool.shutdown(wait=True)
        asyncio.run(run())


class TestWorkerRecycling:
    def test_recycle_after_max_tasks(self):
        async def run():
            recycle_count = 0

            async def factory(idx):
                nonlocal recycle_count
                recycle_count += 1
                return {"id": idx, "gen": recycle_count}

            async def teardown(state):
                pass

            pool = WorkerPool(
                num_workers=1,
                max_queue_size=10,
                max_tasks_per_worker=2,
                worker_factory=factory,
                worker_teardown=teardown,
                name="RecyclePool",
            )
            try:
                # Submit 5 tasks → should recycle twice (after task 2 and 4)
                futures = [pool.submit(_make_factory(i)) for i in range(5)]
                await asyncio.gather(*futures)
                # First init + 2 recycles = 3 factory calls
                assert recycle_count == 3
            finally:
                await pool.shutdown(wait=True)
        asyncio.run(run())

    def test_no_recycle_when_zero(self):
        async def run():
            factory_count = 0

            async def factory(idx):
                nonlocal factory_count
                factory_count += 1
                return idx

            pool = WorkerPool(
                num_workers=1,
                max_queue_size=10,
                max_tasks_per_worker=0,
                worker_factory=factory,
                name="NoRecyclePool",
            )
            try:
                for _ in range(10):
                    await pool.submit(lambda: _slow_task(0.01, 1))
                assert factory_count == 1  # only initial factory call
            finally:
                await pool.shutdown(wait=True)
        asyncio.run(run())


class TestContextVarPropagation:
    def test_contextvars_captured_at_submit(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                request_id_var = contextvars.ContextVar("request_id", default="unset")

                async def reader() -> str:
                    return request_id_var.get()

                # Set contextvar, then submit
                request_id_var.set("req-123")
                fut = pool.submit(reader)
                result = await fut
                assert result == "req-123"
        asyncio.run(run())

    def test_different_contexts_per_submit(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                request_id_var = contextvars.ContextVar("request_id", default="unset")

                async def reader() -> str:
                    return request_id_var.get()

                # Submit with different context values
                futures = []
                for req_id in ["a", "b", "c"]:
                    request_id_var.set(req_id)
                    futures.append(pool.submit(reader))

                results = await asyncio.gather(*futures)
                assert results == ["a", "b", "c"]
        asyncio.run(run())


class TestErrorHandling:
    def test_worker_exception_propagates(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                fut = pool.submit(_fail_task)
                with pytest.raises(ValueError, match="intentional failure"):
                    await fut
        asyncio.run(run())

    def test_partial_failure_others_succeed(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                async def mixed_factory(should_fail: bool):
                    return _fail_task() if should_fail else _slow_task(0.01, 42)

                futures = [
                    pool.submit(lambda: mixed_factory(False)),
                    pool.submit(lambda: mixed_factory(True)),
                    pool.submit(lambda: mixed_factory(False)),
                ]
                results = await asyncio.gather(*futures, return_exceptions=True)
                assert results[0] == 42
                assert isinstance(results[1], ValueError)
                assert results[2] == 42
        asyncio.run(run())


class TestShutdown:
    def test_shutdown_wait_true_drains_queue(self):
        async def run():
            pool = WorkerPool(num_workers=1, max_queue_size=10, name="DrainPool")
            done = asyncio.Event()

            async def slow():
                await asyncio.sleep(0.2)
                done.set()
                return "done"

            # Submit, then shutdown with wait=True
            fut = pool.submit(slow)
            await pool.shutdown(wait=True, timeout=1.0)

            assert done.is_set()
            assert await fut == "done"
        asyncio.run(run())

    def test_shutdown_wait_false_cancels_pending(self):
        async def run():
            pool = WorkerPool(num_workers=1, max_queue_size=10, name="CancelPool")

            async def slow():
                await asyncio.sleep(10)
                return "never"

            fut = pool.submit(slow)
            await pool.shutdown(wait=False)

            with pytest.raises(asyncio.CancelledError):
                await fut
        asyncio.run(run())

    def test_shutdown_idempotent(self):
        async def run():
            pool = WorkerPool(num_workers=2, max_queue_size=5, name="TestPool")
            await pool.shutdown(wait=True)
            await pool.shutdown(wait=True)  # should not raise
        asyncio.run(run())


class TestStats:
    def test_stats_aggregation(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=5, name="TestPool") as pool:
                for i in range(5):
                    await pool.submit(_make_factory(i))
                await pool.submit(_fail_task)

                stats = pool.get_stats()
                assert stats["pool"]["submitted"] == 6
                assert stats["pool"]["completed"] == 5
                assert stats["pool"]["failed"] == 1
                assert stats["num_workers"] == 2
                assert len(stats["workers"]) == 2
                # Each worker has some tasks
                total_worker_completed = sum(w["completed"] for w in stats["workers"])
                assert total_worker_completed == 5
        asyncio.run(run())


class TestMapAsync:
    def test_map_async_preserves_order(self):
        async def run():
            results = await map_async(
                lambda x: _slow_task(0.01, x * 2),
                list(range(5)),
                num_workers=2,
                name="MapPool",
            )
            assert results == [0, 2, 4, 6, 8]
        asyncio.run(run())

    def test_map_async_unordered_yields_as_completed(self):
        async def run():
            delays = [0.1, 0.01, 0.05]  # different delays
            results = []
            async for r in map_async_unordered(
                lambda d: _slow_task(d, d),
                delays,
                num_workers=3,
                name="UnorderedPool",
            ):
                results.append(r)
            # Should complete in order of delay: 0.01, 0.05, 0.1
            assert results == [0.01, 0.05, 0.1]
        asyncio.run(run())

    def test_map_async_with_worker_factory(self):
        async def run():
            init_count = 0

            async def factory(idx):
                nonlocal init_count
                init_count += 1
                return idx

            async def teardown(state):
                pass

            await map_async(
                lambda x: _slow_task(0.01, x),
                list(range(4)),
                num_workers=2,
                max_tasks_per_worker=2,
                worker_factory=factory,
                worker_teardown=teardown,
                name="FactoryPool",
            )
            # 2 workers, each recycled once (4 tasks, 2 per worker)
            assert init_count == 4
        asyncio.run(run())

    def test_map_parallel_alias(self):
        """map_parallel is an alias for map_async."""
        assert map_parallel is map_async


class TestSubmitFromThread:
    def test_submit_from_thread(self):
        async def run():
            pool = WorkerPool(num_workers=2, name="ThreadPool")
            result_holder = {}

            def thread_target():
                cfut = submit_from_thread(pool, lambda: _slow_task(0.05, 123))
                result_holder["result"] = cfut.result(timeout=5.0)

            t = threading.Thread(target=thread_target)
            t.start()
            t.join(timeout=5.0)

            assert result_holder.get("result") == 123
            await pool.shutdown(wait=True)
        asyncio.run(run())


class TestContextManager:
    def test_async_context_manager(self):
        async def run():
            async with WorkerPool(num_workers=2, name="CMPool") as pool:
                results = await asyncio.gather(*[
                    pool.submit(_make_factory(i)) for i in range(3)
                ])
            assert results == [0, 1, 2]
        asyncio.run(run())


class TestIdleWorkers:
    def test_idle_workers_count(self):
        async def run():
            async with WorkerPool(num_workers=2, max_queue_size=10, name="TestPool") as pool:
                # Initially all idle
                assert pool.idle_workers == 2

                # Submit 2 tasks → 0 idle (both workers busy)
                futs = [pool.submit(lambda: _slow_task(0.5, i)) for i in range(2)]
                await asyncio.sleep(0.05)  # let workers pick up
                assert pool.idle_workers == 0

                # Wait for completion → 2 idle
                await asyncio.gather(*futs)
                assert pool.idle_workers == 2
        asyncio.run(run())


class TestBatchProcessor:
    def test_batch_processor_basic(self):
        async def run():
            from core.adaptive_memory.pipeline.checkpoint import CheckpointManager
            import tempfile

            with tempfile.TemporaryDirectory() as tmpdir:
                ckpt_mgr = CheckpointManager(tmpdir)

                async def processor(item):
                    await asyncio.sleep(0.01)
                    return item * 2

                bp = BatchProcessor(
                    processor=processor,
                    checkpoint_manager=ckpt_mgr,
                    run_id="test-run",
                    batch_size=2,
                )
                try:
                    results = await bp.process_all(list(range(6)))
                    assert results == [0, 2, 4, 6, 8, 10]
                finally:
                    await bp.shutdown()
        asyncio.run(run())

    def test_batch_processor_resume(self):
        async def run():
            from core.adaptive_memory.pipeline.checkpoint import CheckpointManager
            import tempfile

            with tempfile.TemporaryDirectory() as tmpdir:
                ckpt_mgr = CheckpointManager(tmpdir)

                async def processor(item):
                    await asyncio.sleep(0.01)
                    return item * 2

                # First run: process 3 items
                bp1 = BatchProcessor(
                    processor=processor,
                    checkpoint_manager=ckpt_mgr,
                    run_id="resume-test",
                    batch_size=2,
                )
                try:
                    results1 = await bp1.process_all(list(range(3)))
                    assert results1 == [0, 2, 4]
                finally:
                    await bp1.shutdown()

                # Second run: resume and process remaining 3
                bp2 = BatchProcessor(
                    processor=processor,
                    checkpoint_manager=ckpt_mgr,
                    run_id="resume-test",
                    batch_size=2,
                )
                try:
                    results2 = await bp2.process_all(list(range(6)), resume=True)
                    assert results2 == [0, 2, 4, 6, 8, 10]
                finally:
                    await bp2.shutdown()
        asyncio.run(run())


# ── standalone runner (return PASS/FAIL/ENV-SKIP) ────────────────────────────
# Per repo convention: return (bool, str) for pytest adapter in conftest.py

if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q", "--tb=short"]))