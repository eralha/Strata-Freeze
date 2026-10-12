"""The token ledger as the HTTP APIs use it: the line a finished request writes, what GET /metrics shows with it,
and that the count outlives the server (no GPU, no model pack).

    python -m unittest serve.test_usage_api -v
"""
import json
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, Service, serve
from serve.usage import UsageLedger, day_of
from serve.test_request_accounting import ClockedThinkingEngine

TEMPLATE = Path(__file__).with_name("chat_template.jinja")


class UsageThroughTheApi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.httpds = []
        self.engine, self.svc = self.start()

    def tearDown(self):
        for httpd in self.httpds:
            httpd.shutdown()
            httpd.server_close()
        self.dir.cleanup()

    def start(self, usage="usage.jsonl"):
        """A Service of its own - new engine, port and ledger, on one ledger file: what restarting the server is,
        with the process, the engine and the tokenizer all new.  `usage=None` is a server that never turned the
        ledger on."""
        self.engine = ClockedThinkingEngine(ByteTokenizer())
        svc = Service(self.engine, self.engine.tok, ChatTemplate(TEMPLATE))
        svc.usage = UsageLedger(Path(self.dir.name) / usage) if usage else None
        httpd = serve(svc, port=0)
        self.httpds.append(httpd)
        svc.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        return self.engine, svc

    def request(self, svc, path, body=None):
        req = urllib.request.Request(svc.base + path, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read().decode()

    def chat(self, svc, path="/v1/chat/completions", stream=False, budget=20):
        code, raw = self.request(svc, path, {"model": "m", "messages": [{"role": "user", "content": "2+2?"}],
                                             "max_tokens": 400, "reasoning_budget_tokens": budget, "stream": stream})
        self.assertEqual(code, 200, raw)
        if stream:
            return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
        return json.loads(raw)

    def lines(self, svc):
        try:
            text = svc.usage.path.read_text(encoding="utf-8")
        except OSError:
            return []
        return [json.loads(x) for x in text.splitlines() if x.strip()]

    # --------------------------------------------------------------------------------------------- the line it writes
    def test_a_finished_request_is_one_line_of_what_the_client_received(self):
        reply = self.chat(self.svc)
        lines = self.lines(self.svc)
        self.assertEqual(len(lines), 1, "one API request, one line: the ledger counts requests, not the engine's legs")
        line, usage = lines[0], reply["usage"]
        self.assertEqual(line["api"], "/v1/chat/completions")
        self.assertEqual(line["finish"], "stop")
        self.assertEqual(line["model"], self.svc.model)
        self.assertEqual(line["output"], usage["completion_tokens"], "the count is the completion tokens the API gave")
        self.assertEqual(line["prompt"], usage["prompt_tokens"])
        self.assertEqual(line["reused"], usage["prompt_tokens_details"]["cached_tokens"])
        self.assertGreater(line["reasoning"], 0, "a thinking model spends most of its output thinking")
        self.assertLessEqual(line["reasoning"], line["output"])
        self.assertGreater(line["engine_generated"], 0)
        self.assertEqual((line["drafts_offered"], line["drafts_accepted"]), (29, 15), "both legs' MTP drafts, summed")

    def test_the_anthropic_door_is_named_as_one(self):
        self.chat(self.svc, "/v1/messages")
        self.assertEqual(self.lines(self.svc)[0]["api"], "/v1/messages")

    def test_the_thinking_budget_continuation_is_counted_once(self):
        """#123: a thinking budget sends the prompt a second time.  That is one request, so one line and one request
        in the totals - the same shape /metrics' own `totals` have."""
        self.chat(self.svc, budget=20)
        self.assertEqual(self.engine.attempts, 2, "the fixture really did continue")
        self.assertEqual(len(self.lines(self.svc)), 1)
        metrics = self.svc.metrics()
        self.assertEqual(metrics["usage"]["lifetime"]["requests"], metrics["totals"]["requests"])
        self.assertEqual(metrics["usage"]["lifetime"]["output"], metrics["totals"]["output_tokens"])
        self.assertEqual(self.lines(self.svc)[0]["engine_generated"],
                         sum(d["generated"] for d in self.engine.done), "the engine's own count, of both legs")

    def test_a_cancelled_request_is_counted_as_far_as_it_got(self):
        self.engine.cancel_continuation = True
        final = self.chat(self.svc, stream=True, budget=20)[-1]
        line = self.lines(self.svc)[-1]
        self.assertEqual(line["finish"], "cancel")
        self.assertEqual(line["output"], final["usage"]["completion_tokens"])

    def test_a_request_that_never_ran_writes_nothing(self):
        """A 400 (the prompt plus max tokens do not fit) is not a generation: no line, no count."""
        code, _ = self.request(self.svc, "/v1/chat/completions",
                               {"model": "m", "messages": [{"role": "user", "content": "2+2?"}], "max_tokens": 900000})
        self.assertEqual(code, 400)
        self.assertEqual(self.lines(self.svc), [])
        self.assertEqual(self.svc.metrics()["usage"]["lifetime"]["requests"], 0)

    # ----------------------------------------------------------------------------------------- what GET /metrics says
    def test_metrics_carries_the_lifetime_the_day_and_the_file(self):
        n = self.chat(self.svc)["usage"]["completion_tokens"]
        u = self.svc.metrics()["usage"]
        self.assertEqual(u["lifetime"]["output"], n)
        self.assertEqual(u["lifetime"]["answer"], n - u["lifetime"]["reasoning"])
        self.assertEqual(u["lifetime"]["requests"], 1)
        today = u["today"]
        self.assertEqual(today["day"], day_of(time.time()))
        self.assertEqual(today["output"], n)
        self.assertEqual([d["output"] for d in u["days"]], [n])
        self.assertTrue(u["file"].endswith("usage.jsonl"))
        self.assertNotIn("models", u, "one model in this ledger: nothing to split")
        self.assertNotIn("broken", u)

    def test_the_key_is_absent_from_a_server_that_never_turned_the_ledger_on(self):
        """The opt-in path: with no ledger, /metrics is the JSON it always was, and no file is made."""
        _, svc = self.start(usage=None)
        self.chat(svc)
        self.assertNotIn("usage", svc.metrics())
        self.assertEqual(list(Path(self.dir.name).glob("*.jsonl")), [])

    def test_the_count_outlives_the_server(self):
        """The point of the ledger: a second Service, new engine and port, starts from the first server's lines and
        adds its own - while its `totals`, which answer a different question, still start at 0."""
        first = self.chat(self.svc)["usage"]["completion_tokens"]
        _, svc2 = self.start()
        second = self.chat(svc2)["usage"]["completion_tokens"]
        u = svc2.metrics()["usage"]
        self.assertEqual(u["lifetime"]["requests"], 2)
        self.assertEqual(u["lifetime"]["output"], first + second)
        self.assertEqual(self.lines(svc2)[0]["output"], first, "the file carries the first server's line")
        self.assertEqual(svc2.metrics()["totals"]["output_tokens"], second)
