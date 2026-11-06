"""Wave 4 / E9 remainder — benchmarks/epic9_bench.py (E9-B1…B3, L1).

E9-B1: codec figures are medians (the docstring says so, the values
come from _median_ms) but the printed table labelled them "avg".

E9-B2: the cold-start child read /proc/self/status (Linux-only) and
died off-Linux; the parent then crashed on empty stdout with
IndexError/JSONDecodeError, never surfacing the child's stderr. The
child now measures RSS cross-platform, and the parent reports the
child's own error when it fails.

E9-B3: bench_ttft's receive loop had no timeout (a task that never
completes hangs the bench forever) and a typo'd --only section was a
bare KeyError traceback. Receives are deadline-bounded and section
names are validated.

E9-L1: the bench had no behavioural covering test at all; the codec /
median / cold / main-dispatch tests here are that coverage.
"""
import subprocess
import sys
import threading

import pytest

import benchmarks.epic9_bench as bench

# ---------------------------------------------------------------------------
# E9-B1 — medians labelled as medians
# ---------------------------------------------------------------------------

def test_b1_codec_labels_say_median_not_avg():
    result = bench.bench_codec()
    assert any("median of 200" in key for key in result)
    assert not any("avg" in key for key in result)


# ---------------------------------------------------------------------------
# E9-L1 — bench internals behave (median helper)
# ---------------------------------------------------------------------------

def test_median_ms_returns_a_nonnegative_float():
    value = bench._median_ms(lambda: None, 3)
    assert isinstance(value, float)
    assert value >= 0


# ---------------------------------------------------------------------------
# E9-B2 — cold-start: cross-platform RSS + child failure surfaced
# ---------------------------------------------------------------------------

def test_b2_rss_mb_reads_a_positive_value_on_this_platform():
    rss = bench._rss_mb()
    assert isinstance(rss, float)
    assert rss > 0


def _fake_run_factory(**kwargs):
    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=kwargs.get("returncode", 0),
                                           stdout=kwargs.get("stdout", ""),
                                           stderr=kwargs.get("stderr", ""))
    return fake_run


def test_b2_cold_parent_surfaces_child_stderr_on_failure(monkeypatch):
    monkeypatch.setattr(bench.subprocess, "run", _fake_run_factory(
        returncode=1, stdout="", stderr="child boom: no /proc here"))
    with pytest.raises(RuntimeError, match="child boom"):
        bench.bench_cold()


def test_b2_cold_parent_surfaces_garbage_stdout(monkeypatch):
    monkeypatch.setattr(bench.subprocess, "run", _fake_run_factory(
        returncode=0, stdout="not json at all", stderr=""))
    with pytest.raises(RuntimeError, match="no JSON"):
        bench.bench_cold()


# ---------------------------------------------------------------------------
# E9-B3 — bounded receives + validated --only
# ---------------------------------------------------------------------------

class _FakeWs:
    def __init__(self, text=None, block=False):
        self._text = text
        self._block = block
        self.release = threading.Event()

    def receive_text(self):
        if self._block:
            self.release.wait(timeout=10)
            return "too late"
        return self._text


def test_b3_receive_helper_returns_message():
    assert bench._receive_text_with_timeout(_FakeWs("hello"), 1.0) == "hello"


def test_b3_receive_helper_times_out_instead_of_hanging():
    ws = _FakeWs(block=True)
    try:
        with pytest.raises(RuntimeError, match="no websocket message"):
            bench._receive_text_with_timeout(ws, 0.2)
    finally:
        ws.release.set()


def test_b3_unknown_only_section_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["epic9_bench.py", "--only", "bogus"])
    with pytest.raises(SystemExit) as excinfo:
        bench.main()
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "bogus" in err
    assert "codec" in err  # the valid choices are named


def test_b3_only_codec_runs_just_the_codec_section(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["epic9_bench.py", "--only", "codec"])
    bench.main()
    out = capsys.readouterr().out
    assert "## codec" in out
    assert "median of 200" in out
    assert "## cold" not in out
