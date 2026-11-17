"""The processes below a session, and the end of those that outlive its stop.

Every test runs against the fake process table of the fixture `process_table`: none reads
`ps` or signals a real process.
"""

from __future__ import annotations

import logging
import os
import signal

from chargehand import processes


def test_the_table_is_read_from_ps_output_with_the_start_time_as_one_field():
    rows = processes.parse_table(
        "  612     1   612 Mon Nov 16 23:02:52 2026\n"
        "89626 89205 89626 Mon Nov  9 23:02:52 2026\n"
        "garbage\n"
        "  x 1 1 Mon Nov 16 23:02:52 2026\n"
    )

    assert [(r.process.pid, r.parent, r.process.group, r.process.started) for r in rows] == [
        (612, 1, 612, "Mon Nov 16 23:02:52 2026"),
        (89626, 89205, 89626, "Mon Nov 9 23:02:52 2026"),
    ]


def test_below_finds_every_depth_and_nothing_beside_it(process_table):
    process_table.add(100, 1)
    shell = process_table.add(200, 100)
    sleep = process_table.add(201, 200, group=200)
    server = process_table.add(300, 100, group=100)
    process_table.add(400, 1)
    process_table.add(401, 400)

    assert set(processes.below(100)) == {shell, sleep, server}
    assert processes.below(999) == ()


def test_an_end_signals_the_group_of_a_shell_and_returns_what_ran(process_table):
    process_table.add(100, 1)
    shell = process_table.add(200, 100)
    sleep = process_table.add(201, 200, group=200)
    below = processes.below(100)
    process_table.reparent(200)

    found, left = processes.end(below)

    assert set(found) == {shell, sleep} and left == ()
    assert process_table.pids() == {100}
    assert (200, signal.SIGTERM, True) in process_table.signals
    assert not any(sig == signal.SIGKILL for _, sig, _ in process_table.signals)


def test_an_end_never_signals_a_pid_that_a_new_process_got(process_table):
    process_table.add(100, 1)
    process_table.add(200, 100)
    below = processes.below(100)
    process_table.replace(200)

    found, left = processes.end(below)

    assert found == () and left == ()
    assert process_table.signals == []
    assert 200 in process_table.pids()


def test_a_process_that_ignores_sigterm_gets_sigkill(process_table, monkeypatch):
    monkeypatch.setattr(processes, "END_WAIT_SECS", 0.0)
    process_table.add(100, 1)
    process_table.add(200, 100)
    process_table.ignore_term.add(200)

    found, left = processes.end(processes.below(100))

    assert [p.pid for p in found] == [200] and left == ()
    assert (200, signal.SIGKILL, False) in process_table.signals


def test_an_end_after_a_stop_logs_the_pids_and_warns_about_the_ones_left(
    process_table, monkeypatch, caplog
):
    monkeypatch.setattr(processes, "END_WAIT_SECS", 0.0)
    process_table.add(100, 1)
    process_table.add(200, 100)
    process_table.add(300, 100)
    process_table.unkillable.add(300)

    with caplog.at_level(logging.INFO, logger="chargehand.processes"):
        left = processes.end_after_stop(processes.below(100), who="impl/ABC-1", session="abcd1234")

    assert [p.pid for p in left] == [300]
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert messages == [
        (logging.INFO, "impl/ABC-1: ended 1 process(es) that outlived the stop of session "
                       "abcd1234: 200"),
        (logging.WARNING, "impl/ABC-1: 1 process(es) below the stopped session abcd1234 did "
                          "not end: 300"),
    ]


def test_an_end_never_signals_chargehand_itself_or_pid_one(process_table):
    own = os.getpid()
    process_table.add(100, 1)
    process_table.add(own, 100, group=os.getpgrp())
    process_table.add(1, 100)

    processes.end(processes.below(100))

    assert process_table.signals == []
