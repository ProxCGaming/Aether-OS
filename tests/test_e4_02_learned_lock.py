"""E4-02: ``learned.record_lessons`` is an unlocked read-modify-write.

``record_lessons`` loads the current lessons, merges in memory, and
atomically replaces the file. Atomic replace prevents corruption,
not lost update: two concurrent callers (the function's only guard
against interleaving today is that its one production caller happens
to run it synchronously on the event loop — a latent fragility, one
``to_thread`` refactor away from silent loss) both read the same
``existing``, each appends only its own lessons, and the second
``os.replace`` discards the first caller's — while the first caller
still returns its lessons as "added" and logs LESSONS_RECORDED for
lessons that are not in learned.md.

The fix serialises the read-modify-write with a module lock (the
house pattern: ``threading.RLock`` in the LangMem file store, which
got exactly this lock in E4-F06). This test widens the RMW window
deterministically (a slow write) and starts every caller on a
barrier, so the pre-fix lost update is not a matter of timing luck.
"""
import threading
import time

from aether_engine.memory import learned


def test_concurrent_record_lessons_lose_nothing(tmp_path, monkeypatch):
    target = tmp_path / "learned.md"

    real_write = learned._write_lessons

    def slow_write(lessons, path):
        # Hold the write open long enough that, without serialisation,
        # every concurrent caller has loaded the same starting state
        # before the first replace lands.
        time.sleep(0.05)
        return real_write(lessons, path)

    monkeypatch.setattr(learned, "_write_lessons", slow_write)

    n_callers = 8
    barrier = threading.Barrier(n_callers)
    results: dict[int, dict] = {}
    errors: dict[int, BaseException] = {}

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[i] = learned.record_lessons(
                [f"Concurrent lesson number {i}."], task_id=f"t{i}",
                path=target)
        except BaseException as exc:  # noqa: BLE001 — collected for assertion
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(n_callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "a caller wedged"
    assert errors == {}
    # Every caller reported its lesson as added...
    for i in range(n_callers):
        assert results[i]["added"] == [f"Concurrent lesson number {i}."]
    # ...so every lesson must actually be in the document. Pre-fix the
    # last replace wins and most lessons are silently gone, even
    # though each caller was told (and logged) that its lesson landed.
    stored = learned.load_lessons(target)
    assert sorted(stored) == sorted(
        f"Concurrent lesson number {i}." for i in range(n_callers))
