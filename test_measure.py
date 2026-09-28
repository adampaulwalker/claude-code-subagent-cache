"""Tests for measure.py, using synthetic transcripts only.

Run with: python3 -m unittest -v
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import measure

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def rec(msg_id, req_id, at, model="claude-sonnet-5", read=0, w5=0, w1=0, inp=0, out=0,
        sidechain=False, agent=None, breakdown=True):
    usage = {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": w5 + w1,
        "output_tokens": out,
    }
    if breakdown:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1}
    r = {
        "type": "assistant",
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "isSidechain": sidechain,
        "message": {"id": msg_id, "model": model, "usage": usage},
    }
    if req_id is not None:
        r["requestId"] = req_id
    if agent:
        r["agentId"] = agent
    return r


class Fixture:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def write(self, relpath, records, extra_lines=()):
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
            for line in extra_lines:
                fh.write(line + "\n")
        return path

    def run(self, days=7):
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        calls, stats = measure.load_calls(self.root, cutoff)
        measure.mark_idle_rereads(calls)
        return measure.summarize(calls, cutoff), stats


class DedupTest(unittest.TestCase):
    def test_same_response_logged_per_content_block_counts_once(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [
            rec("msg_1", "req_1", t, read=1000, out=10),
            rec("msg_1", "req_1", t, read=1000, out=10),
            rec("msg_1", "req_1", t, read=1000, out=10),
            rec("msg_2", "req_2", t, read=2000, out=20),
        ])
        s, stats = f.run()
        self.assertEqual(s["tokens"]["main"]["read"], 3000)
        self.assertEqual(s["calls"]["main"], 2)
        self.assertEqual(stats["duplicates"], 2)

    def test_duplicates_keep_the_largest_count_of_each_kind(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [
            rec("msg_1", "req_1", t, read=1000, w5=500, out=7),
            rec("msg_1", "req_1", t + timedelta(seconds=5), read=1000, w5=500, out=353),
        ])
        s, _ = f.run()
        self.assertEqual(s["tokens"]["main"]["output"], 353)
        self.assertEqual(s["tokens"]["main"]["read"], 1000)
        self.assertEqual(s["tokens"]["main"]["write_5m"], 500)

    def test_records_without_ids_are_not_merged(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [
            rec(None, None, t, read=100),
            rec(None, None, t, read=100),
        ])
        s, stats = f.run()
        self.assertEqual(s["tokens"]["main"]["read"], 200)
        self.assertEqual(stats["no_ids"], 2)

    def test_malformed_lines_and_zero_usage_are_skipped(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [
            rec("msg_1", "req_1", t, read=100),
            rec("msg_x", "req_x", t, model="<synthetic>"),
            {"type": "user", "message": {"content": "hi"}},
        ], extra_lines=["{not json"])
        s, stats = f.run()
        self.assertEqual(s["calls"]["main"], 1)
        self.assertEqual(stats["bad_lines"], 1)
        self.assertNotIn("<synthetic>", s["models"])


class ScopeTest(unittest.TestCase):
    def test_subagent_folder_and_sidechain_flag_count_as_subagent(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [rec("m1", "r1", t, read=10)])
        f.write("proj/s1/subagents/agent-a.jsonl", [rec("m2", "r2", t, read=20, sidechain=True)])
        f.write("proj/agent-legacy.jsonl", [rec("m3", "r3", t, read=40, sidechain=True, agent="b")])
        s, _ = f.run()
        self.assertEqual(s["tokens"]["main"]["read"], 10)
        self.assertEqual(s["tokens"]["subagent"]["read"], 60)

    def test_window_excludes_future_dated_calls(self):
        f = Fixture(self)
        f.write("proj/s1.jsonl", [
            rec("m1", "r1", NOW + timedelta(days=3), read=999),
            rec("m2", "r2", NOW - timedelta(hours=1), read=1),
        ])
        s, _ = f.run()
        self.assertEqual(s["tokens"]["main"]["read"], 1)

    def test_window_excludes_old_calls(self):
        f = Fixture(self)
        f.write("proj/s1.jsonl", [
            rec("m1", "r1", NOW - timedelta(days=10), read=999),
            rec("m2", "r2", NOW - timedelta(hours=1), read=1),
        ])
        s, _ = f.run(days=7)
        self.assertEqual(s["tokens"]["main"]["read"], 1)


class IdleRereadTest(unittest.TestCase):
    def sub(self, f, name, records):
        return f.write(f"proj/s1/subagents/{name}.jsonl", records)

    def test_rule(self):
        f = Fixture(self)
        t0 = NOW - timedelta(hours=2)
        self.sub(f, "agent-a", [
            # First call: a large write is expected, never flagged.
            rec("a1", "r1", t0, w5=50_000, sidechain=True),
            # 301 s later, write 30k > read 1k: flagged.
            rec("a2", "r2", t0 + timedelta(seconds=301), w5=30_000, read=1_000, sidechain=True),
            # Exactly 300 s gap: not flagged.
            rec("a3", "r3", t0 + timedelta(seconds=601), w5=30_000, read=1_000, sidechain=True),
            # Long gap but write exactly 20,000: not flagged.
            rec("a4", "r4", t0 + timedelta(seconds=1000), w5=20_000, read=0, sidechain=True),
            # Long gap, write 30k but read 40k is larger: not flagged.
            rec("a5", "r5", t0 + timedelta(seconds=1400), w5=30_000, read=40_000, sidechain=True),
            # Long gap, 1h write counts too: flagged.
            rec("a6", "r6", t0 + timedelta(seconds=1800), w1=25_000, read=0, sidechain=True),
        ])
        s, _ = f.run()
        self.assertEqual(s["idle"]["calls"], 2)
        self.assertEqual(s["idle"]["write_tokens"], 55_000)
        p = measure.PRICES["claude-sonnet-5"]
        self.assertAlmostEqual(s["idle"]["write_cost"], (30_000 * p[1] + 25_000 * p[2]) / 1e6)

    def test_gaps_are_per_subagent_and_main_is_never_flagged(self):
        f = Fixture(self)
        t0 = NOW - timedelta(hours=2)
        # Two agents interleaved: each call is within 300 s of the other agent's
        # previous call, but 400 s after its own.
        self.sub(f, "agent-a", [
            rec("a1", "r1", t0, w5=50_000, sidechain=True),
            rec("a2", "r2", t0 + timedelta(seconds=400), w5=30_000, sidechain=True),
        ])
        self.sub(f, "agent-b", [
            rec("b1", "q1", t0 + timedelta(seconds=200), w5=50_000, sidechain=True),
            rec("b2", "q2", t0 + timedelta(seconds=600), w5=30_000, sidechain=True),
        ])
        f.write("proj/s1.jsonl", [
            rec("m1", "x1", t0, w5=50_000),
            rec("m2", "x2", t0 + timedelta(hours=1), w5=50_000),
        ])
        s, _ = f.run()
        self.assertEqual(s["idle"]["calls"], 2)

    def test_predecessor_before_window_still_counts_for_the_gap(self):
        f = Fixture(self)
        self.sub(f, "agent-a", [
            rec("a1", "r1", NOW - timedelta(days=2), w5=50_000, sidechain=True),
            rec("a2", "r2", NOW - timedelta(hours=1), w5=30_000, sidechain=True),
        ])
        s, _ = f.run(days=1)
        self.assertEqual(s["calls"]["subagent"], 1)
        self.assertEqual(s["idle"]["calls"], 1)


class PricingTest(unittest.TestCase):
    def test_unpriced_model_is_counted_in_tokens_not_dollars(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1.jsonl", [
            rec("m1", "r1", t, model="claude-sonnet-5", read=1_000_000),
            rec("m2", "r2", t, model="some-future-model", read=500, out=5),
        ])
        s, stats = f.run()
        self.assertEqual(s["tokens"]["main"]["read"], 1_000_500)
        self.assertAlmostEqual(sum(s["cost"]["main"].values()), 0.20)
        self.assertFalse(s["models"]["some-future-model"]["priced"])
        self.assertEqual(s["unpriced_tokens"], 505)
        text = measure.render(s, "7", stats)
        self.assertIn("not priced", text)
        self.assertIn("proxy", text)

    def test_date_suffix_matches_but_other_suffixes_do_not(self):
        self.assertEqual(measure.price_for("claude-haiku-4-5-20251001"), measure.PRICES["claude-haiku-4-5"])
        self.assertEqual(measure.price_for("claude-opus-5-5"), measure.PRICES["claude-opus-5-5"])
        self.assertIsNone(measure.price_for("claude-opus-5-9"))
        self.assertIsNone(measure.price_for("<synthetic>"))

    def test_ttl_split_and_unlogged_ttl(self):
        f = Fixture(self)
        t = NOW - timedelta(hours=1)
        f.write("proj/s1/subagents/agent-a.jsonl", [
            rec("a1", "r1", t, w5=100, w1=300, sidechain=True),
            rec("a2", "r2", t, w5=50, sidechain=True, breakdown=False),
        ])
        s, _ = f.run()
        sub = s["tokens"]["subagent"]
        self.assertEqual((sub["write_5m"], sub["write_1h"], sub["write_unknown"]), (100, 300, 50))
        p = measure.PRICES["claude-sonnet-5"]
        self.assertAlmostEqual(s["cost"]["subagent"]["write_unknown"], 50 * p[1] / 1e6)


class CliTest(unittest.TestCase):
    def test_empty_folder_runs(self):
        f = Fixture(self)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(measure.main(["--projects-dir", f.root, "--days", "1"]), 0)
        self.assertIn("No assistant calls", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
