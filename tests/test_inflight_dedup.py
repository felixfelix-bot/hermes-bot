"""Tests for inflight_dedup — the short-window duplicate-request guard.

Run:  python3 -m unittest discover -s tests -p 'test_inflight_dedup.py' -v
  or: python3 tests/test_inflight_dedup.py
"""

import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inflight_dedup import (  # noqa: E402
    DEFAULT_IGNORE_KEYS, DUPLICATE, LEADER, InflightDedup, fingerprint,
    from_env, normalize_body,
)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


BODY = {"model": "glm-5.2", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8}


class TestNormalize(unittest.TestCase):
    def test_key_order_and_whitespace_are_irrelevant(self):
        a = normalize_body(json.dumps({"b": 1, "a": 2}))
        b = normalize_body('{"a": 2,  "b":1}')
        self.assertEqual(a, b)

    def test_accepts_dict_bytes_and_str(self):
        s = normalize_body(json.dumps(BODY))
        self.assertEqual(s, normalize_body(BODY))
        self.assertEqual(s, normalize_body(json.dumps(BODY).encode()))

    def test_non_json_degrades_instead_of_raising(self):
        self.assertEqual(normalize_body(b"not json   at all"), "not json at all")

    def test_volatile_fields_stripped(self):
        a = normalize_body({"model": "m", "user": "alice", "request_id": "1"})
        b = normalize_body({"model": "m", "user": "bob", "request_id": "2"})
        self.assertEqual(a, b)
        for key in DEFAULT_IGNORE_KEYS:
            self.assertIn(key, DEFAULT_IGNORE_KEYS)  # documented contract

    def test_messages_are_not_stripped(self):
        a = normalize_body({"messages": [{"content": "one"}]})
        b = normalize_body({"messages": [{"content": "two"}]})
        self.assertNotEqual(a, b)


class TestFingerprint(unittest.TestCase):
    def test_same_inputs_same_fp(self):
        self.assertEqual(fingerprint("k", "glm-5.2", BODY),
                         fingerprint("k", "glm-5.2", json.dumps(BODY)))

    def test_different_key_model_or_body_differ(self):
        base = fingerprint("k", "glm-5.2", BODY)
        self.assertNotEqual(base, fingerprint("k2", "glm-5.2", BODY))
        self.assertNotEqual(base, fingerprint("k", "glm-5.3", BODY))
        self.assertNotEqual(base, fingerprint("k", "glm-5.2", {**BODY, "max_tokens": 9}))

    def test_delimiter_cannot_be_forged_across_fields(self):
        # ("a", "b") must not collide with ("a\x00b", "")
        self.assertNotEqual(fingerprint("a", "b", {}), fingerprint("a\x00b", "", {}))


class TestInflightDedup(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.d = InflightDedup(window_s=2.0, hard_ttl_s=300.0, clock=self.clock)
        self.fp = fingerprint("routstrd", "glm-5.2", BODY)

    def test_first_is_leader_twin_is_duplicate(self):
        first = self.d.begin(self.fp)
        second = self.d.begin(self.fp)
        self.assertEqual(first.state, LEADER)
        self.assertEqual(second.state, DUPLICATE)
        self.assertFalse(first.is_duplicate)
        self.assertTrue(second.is_duplicate)
        self.assertGreaterEqual(second.retry_after_s, 0.0)

    def test_35_way_fanout_yields_one_leader(self):
        # mirrors the measured incident: 35 identical calls inside 0.7 s
        states = []
        for i in range(35):
            self.clock.advance(0.02)
            states.append(self.d.begin(self.fp).state)
        self.assertEqual(states.count(LEADER), 1)
        self.assertEqual(states.count(DUPLICATE), 34)
        snap = self.d.snapshot()
        self.assertEqual(snap["leaders"], 1)
        self.assertEqual(snap["duplicates"], 34)
        self.assertAlmostEqual(snap["dup_ratio"], 34 / 35, places=6)

    def test_different_model_is_not_a_duplicate(self):
        self.d.begin(self.fp)
        other = fingerprint("routstrd", "glm-5.3", BODY)
        self.assertEqual(self.d.begin(other).state, LEADER)

    def test_different_client_key_is_not_a_duplicate(self):
        self.d.begin(self.fp)
        other = fingerprint("deepseek", "glm-5.2", BODY)
        self.assertEqual(self.d.begin(other).state, LEADER)

    def test_volatile_field_difference_is_still_a_duplicate(self):
        a = fingerprint("k", "m", {**BODY, "user": "alice", "request_id": "1"})
        b = fingerprint("k", "m", {**BODY, "user": "bob", "request_id": "2"})
        self.assertEqual(a, b)
        self.d.begin(a)
        self.assertEqual(self.d.begin(b).state, DUPLICATE)

    def test_long_upstream_keeps_suppressing_past_the_window(self):
        # a real call takes 90-180 s; the window must not reopen mid-flight
        self.d.begin(self.fp)
        self.clock.advance(2.5)          # past window_s
        self.assertEqual(self.d.begin(self.fp).state, DUPLICATE)
        self.clock.advance(60.0)
        self.assertEqual(self.d.begin(self.fp).state, DUPLICATE)

    def test_hard_ttl_reopens_after_a_leak(self):
        self.d.begin(self.fp)            # leader never calls end()
        self.clock.advance(301.0)
        self.assertEqual(self.d.begin(self.fp).state, LEADER)

    def test_end_releases_the_entry(self):
        self.d.begin(self.fp)
        self.d.end(self.fp)
        self.assertEqual(self.d.begin(self.fp).state, LEADER)
        self.d.end("never-registered")   # must not raise

    def test_disabled_by_default(self):
        d = InflightDedup()              # window_s=0.0
        self.assertFalse(d.enabled)
        self.assertEqual(d.begin(self.fp).state, LEADER)
        self.assertEqual(d.begin(self.fp).state, LEADER)

    def test_max_entries_bound_and_eviction_counted(self):
        d = InflightDedup(window_s=60.0, max_entries=3, clock=self.clock)
        for i in range(10):
            d.begin(fingerprint("k", "m", {"i": i}))
        snap = d.snapshot()
        self.assertLessEqual(snap["inflight"], 3)
        self.assertGreater(snap["expired"], 0)

    def test_thread_safety_single_leader_under_race(self):
        d = InflightDedup(window_s=5.0, clock=lambda: 500.0)
        results, barrier = [], threading.Barrier(20)

        def worker():
            barrier.wait()
            results.append(d.begin(self.fp).state)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count(LEADER), 1)
        self.assertEqual(results.count(DUPLICATE), 19)

    def test_reset_clears_everything(self):
        self.d.begin(self.fp)
        self.d.begin(self.fp)
        self.d.reset()
        snap = self.d.snapshot()
        self.assertEqual(snap["inflight"], 0)
        self.assertEqual(snap["leaders"], 0)
        self.assertEqual(snap["duplicates"], 0)


class TestFromEnv(unittest.TestCase):
    def test_defaults_to_disabled(self):
        d = from_env({})
        self.assertFalse(d.enabled)
        self.assertEqual(d.hard_ttl_s, 300.0)
        self.assertEqual(d.max_entries, 1024)

    def test_reads_window_and_ttl(self):
        d = from_env({"PROXY_INFLIGHT_DEDUP_WINDOW_S": "2.5",
                      "PROXY_INFLIGHT_DEDUP_TTL_S": "120",
                      "PROXY_INFLIGHT_DEDUP_MAX_ENTRIES": "64"})
        self.assertTrue(d.enabled)
        self.assertEqual(d.window_s, 2.5)
        self.assertEqual(d.hard_ttl_s, 120.0)
        self.assertEqual(d.max_entries, 64)

    def test_ttl_never_below_window(self):
        d = from_env({"PROXY_INFLIGHT_DEDUP_WINDOW_S": "60",
                      "PROXY_INFLIGHT_DEDUP_TTL_S": "5"})
        self.assertGreaterEqual(d.hard_ttl_s, d.window_s)

    def test_garbage_values_fall_back(self):
        d = from_env({"PROXY_INFLIGHT_DEDUP_WINDOW_S": "banana"})
        self.assertEqual(d.window_s, 0.0)
        self.assertFalse(d.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
