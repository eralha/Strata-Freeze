"""The token ledger of serve/usage.py: its file, its totals, its day and model buckets (no GPU, no model pack).

    python -m unittest serve.test_usage -v
"""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from serve.usage import DAYS, UsageLedger, day_of, ledger_path

DAY = 86400.0


def rec(**kw):
    """A line as `record` would have written it, with today's time and this model unless said otherwise."""
    return {"t": time.time(), "model": "m", "api": "/v1/chat/completions", "finish": "stop",
            "output": 100, "reasoning": 40, "prompt": 900, "reused": 300, "engine_generated": 112,
            "drafts_offered": 30, "drafts_accepted": 22, "decode_ms": 4000.0, **kw}


class LedgerFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "strata-x.usage.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def lines(self):
        """The file's lines, read the way the ledger reads it: a line it cannot parse is not a request."""
        out = []
        for x in self.path.read_text(encoding="utf-8").splitlines():
            if not x.strip():
                continue
            try:
                out.append(json.loads(x))
            except ValueError:
                pass
        return out

    def test_a_finished_request_is_one_line(self):
        u = UsageLedger(self.path)
        u.record(rec())
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.lines()[0]["output"], 100)
        self.assertEqual(self.lines()[0]["api"], "/v1/chat/completions")

    def test_the_count_survives_a_restart_of_the_server(self):
        """The whole point: a second Service reads the file and keeps counting where the first stopped."""
        first = UsageLedger(self.path)
        first.record(rec())
        first.record(rec(output=50, reasoning=0, engine_generated=50, decode_ms=900.0))
        again = UsageLedger(self.path)
        self.assertEqual(again.lifetime["output"], 150)
        self.assertEqual(again.lifetime["requests"], 2)
        self.assertEqual(again.lifetime["engine_generated"], 162)
        self.assertEqual(again.lifetime["decode_ms"], 4900.0)
        again.record(rec(output=7))
        self.assertEqual(UsageLedger(self.path).lifetime["output"], 157)

    def test_no_ledger_no_file_and_a_zero_view(self):
        """The opt-in path: a ledger that nothing recorded leaves no file behind, and answers zeros."""
        UsageLedger(self.path)
        self.assertFalse(self.path.exists())
        v = UsageLedger(self.path).view()
        self.assertEqual(v["lifetime"]["output"], 0)
        self.assertNotIn("since", v["lifetime"])
        self.assertEqual(v["days"], [])
        self.assertEqual(v["today"]["day"], day_of(time.time()))

    def test_a_line_cut_mid_write_is_skipped_and_the_ledger_still_counts(self):
        """A kill between two writes leaves half a line.  That line is dropped, not the file."""
        UsageLedger(self.path).record(rec(output=60))
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"t": 17, "output": 999')
        u = UsageLedger(self.path)
        self.assertEqual(u.lifetime["output"], 60)
        u.record(rec(output=5))
        self.assertEqual(u.lifetime["output"], 65)                          # writing goes on
        self.assertEqual(len(self.lines()), 2)                              # the new line is not glued to the tail
        self.assertEqual(UsageLedger(self.path).lifetime["output"], 65)     # ... and is read back next start

    def test_a_value_that_is_not_a_number_adds_zero(self):
        """An older or hand-edited line must not poison the totals (and `true` is not 1)."""
        UsageLedger(self.path).record(rec(output=None, reasoning="x", decode_ms=True, engine_generated=None))
        life = UsageLedger(self.path).lifetime
        self.assertEqual((life["output"], life["reasoning"], life["decode_ms"], life["engine_generated"]),
                         (0, 0, 0, 0))
        self.assertEqual(life["requests"], 1)                               # the request itself still counts

    def test_a_disk_that_says_no_ends_the_ledger_not_the_request(self):
        """The parent is a file: opening for append cannot work.  Nothing raises, one line is printed, and the
        figures on screen keep the requests recorded so far."""
        blocked = Path(self.dir.name) / "afile"
        blocked.write_text("not a folder", encoding="utf-8")
        u = UsageLedger(blocked / "usage.jsonl")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            u.record(rec(output=60))                                        # the first write is the one that fails
            u.record(rec(output=40))                                        # after: no second write, no raise
        life = u.view()["lifetime"]
        self.assertEqual(life["output"], 100)                               # in memory the count goes on
        self.assertIn("usage.jsonl", out.getvalue())
        self.assertTrue(u.view()["broken"])

    def test_ledger_path_beside_the_run_config(self):
        self.assertEqual(ledger_path("models/strata-swift.json").name, "strata-swift.usage.jsonl")
        self.assertEqual(ledger_path("models/strata-swift.json").parent, Path("models"))
        self.assertEqual(ledger_path(None, root=Path("/tmp/p")).name, "strata-usage.jsonl")


class LedgerTotals(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "usage.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_answer_is_the_output_minus_the_thinking(self):
        u = UsageLedger(self.path)
        u.record(rec(output=100, reasoning=40))
        life, today = u.view()["lifetime"], u.view()["today"]
        self.assertEqual(life["answer"], 60)
        self.assertEqual(today["answer"], 60)
        self.assertEqual(u.view()["days"][0]["answer"], 60)

    def test_day_buckets_are_the_machines_days_oldest_first(self):
        now = time.time()
        u = UsageLedger(self.path)
        u.record(rec(t=now - 3 * DAY, output=11))
        u.record(rec(t=now - 3 * DAY, output=4))
        u.record(rec(output=9))
        days = u.view()["days"]
        self.assertEqual([d["day"] for d in days],
                         [day_of(now - 3 * DAY), day_of(now)])
        self.assertEqual([d["output"] for d in days], [15, 9])
        self.assertEqual(days[0]["requests"], 2)

    def test_only_the_last_seven_days_are_shown_and_the_totals_are_everything(self):
        now = time.time()
        u = UsageLedger(self.path)
        for back in range(9):
            u.record(rec(t=now - back * DAY, output=1))
        self.assertEqual(len(u.view()["days"]), DAYS)
        self.assertEqual(u.view()["days"][-1]["day"], day_of(now))
        self.assertEqual(u.view()["lifetime"]["requests"], 9)               # the total is not cut with the days
        self.assertAlmostEqual(u.view()["lifetime"]["since"], now - 8 * DAY, delta=1)

    def test_models_are_counted_apart_only_when_the_ledger_holds_several(self):
        u = UsageLedger(self.path)
        u.record(rec(model="qwen", output=70))
        self.assertNotIn("models", u.view())                                # one model: the card says nothing extra
        u.record(rec(model="coder", output=30))
        models = {m["model"]: m for m in u.view()["models"]}
        self.assertEqual((models["qwen"]["output"], models["coder"]["output"]), (70, 30))
        self.assertEqual(models["qwen"]["answer"], 30)                      # 70 - 40 reasoning
        self.assertEqual(u.view()["lifetime"]["output"], 100)               # the lifetime total covers both
        self.assertEqual([m["model"] for m in u.view()["models"]], ["qwen", "coder"])   # the busiest first

    def test_the_first_request_is_when_the_counting_began(self):
        t = time.time() - 10 * DAY
        u = UsageLedger(self.path)
        u.record(rec(t=t))
        self.assertAlmostEqual(u.view()["lifetime"]["since"], t, delta=1)
