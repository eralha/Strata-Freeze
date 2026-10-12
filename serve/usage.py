"""serve/usage.py - the token counter that outlives a restart of the server.

`GET /metrics`' `totals` are summed in memory and start again at 0 when the server starts, so they answer "what has
this run of the server done".  This is the other question - "what has this engine produced in total": every finished
request appends one line to a JSONL ledger, the file is read back when the server starts, and the totals the Monitor's
**Tokens generated** card shows are the ledger's, not the process's.  The file is plain JSON lines, so a shell one-liner
or a spreadsheet can read it, and deleting it starts the count over.

What is counted, and why these numbers:

- `output`  - the completion tokens the client received (the same `completion_tokens` the API answers with, and the
              same figure `totals.output_tokens` sums).  It is the number an API would bill, so it is the one that
              answers "how many tokens did this engine write for me".
- `reasoning` - the tokens written while thinking, a part of `output` kept apart because a thinking model spends most
              of its output there, and the answer alone is usually what a person means by "the tokens it wrote".
- `engine_generated` - the tokens the engine says it decoded (its DONE line, `engine_generated` in the request rows):
              what the GPU actually wrote, including tokens a speculator re-did.  Higher than `output` on a model that
              retracts, and the figure to read when asking what the hardware did.
- `prompt`, `reused`, `drafts_offered`, `drafts_accepted`, `decode_ms` - the same figures one request row has, so the
              card can say what the output cost alongside it.

Nothing here is a price: an engine on your own PC has no per-token bill.  What the counts are worth against an API is
yours to work out (a provider's own token counter uses the `output` figure above, thinking included).

Off unless `"usage_log": true` is in the model's run config (or `--usage-log`): with it off no file is made, `GET
/metrics` has no `usage` key, and the card stays hidden - the server's default path is untouched.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

DAYS = 7                      # the card's day rows: today and the DAYS-1 days before it that have requests
# the sums of every aggregate, so a day, a model and the lifetime total all have the same shape
FIELDS = ("requests", "output", "reasoning", "prompt", "reused", "engine_generated",
          "drafts_offered", "drafts_accepted", "decode_ms")


def day_of(t) -> str:
    """The local date a request's unix time belongs to: the day rows are the machine's days (a person reads
    "yesterday" as their own), not UTC."""
    return time.strftime("%Y-%m-%d", time.localtime(t))


def blank() -> dict:
    """An aggregate with every sum at 0."""
    return dict.fromkeys(FIELDS, 0)


def _n(v) -> float:
    """A recorded figure as a number: a missing, null or junk value adds 0, so an older or a half-written line cannot
    break the totals (and `decode_ms`, the only one that is not a whole number, stays a float)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


class UsageLedger:
    """One line per finished request (`record`), and the totals read back from the file at startup (`view`).

    `path` is the ledger: `<the run config>.usage.jsonl`, next to the model's `strata-<model>.log`, so one model has
    one count of its own and moving the config moves the count with it.

    The file is opened, appended and closed per request rather than kept open: a line is 231 bytes, a request is
    seconds long, and a handle that closes with each write is one less thing a restart, a kill or an antivirus can
    leave in the way.  Measured on a Windows RTX 5070 PC: writing one costs 0.25 ms, and 50,000 of them (11.6 MB)
    read back at start in 0.36 s.  The running totals are kept in memory, so `GET /metrics` never reads the file.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.broken = None                                  # why writing stopped, when the disk said no
        self._torn = False                                  # the file's last line has no end: a kill cut it mid-write
        self.lifetime = blank()
        self.since = None                                   # the first request the ledger holds
        self.days: dict[str, dict] = {}                     # "2026-01-31" -> that local day's sums
        self.models: dict[str, dict] = {}                   # model name -> its own sums (several when the same
        #                                                     ledger is used by more than one model's config)
        self._load()

    # -------------------------------------------------------------------------------- reading the ledger back
    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return                                          # no ledger yet: the first request writes the file
        # a file that does not end with a newline was cut in the middle of a write.  Its last line is dropped, and the
        # next write starts on a line of its own: the kill costs that one line, not the request written after it too
        self._torn = bool(text) and not text.endswith("\n")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue                                    # the tail a kill cut mid-write: this line, not the ledger
            if isinstance(rec, dict):
                self._add(rec)

    def _add(self, rec: dict) -> None:
        """Raise the running totals with one recorded request (the file's line, as `record` wrote it)."""
        t = _n(rec.get("t"))
        values = {"requests": 1, "output": _n(rec.get("output")), "reasoning": _n(rec.get("reasoning")),
                  "prompt": _n(rec.get("prompt")), "reused": _n(rec.get("reused")),
                  "engine_generated": _n(rec.get("engine_generated")),
                  "drafts_offered": _n(rec.get("drafts_offered")), "drafts_accepted": _n(rec.get("drafts_accepted")),
                  "decode_ms": _n(rec.get("decode_ms"))}
        for sums in (self.lifetime, self.days.setdefault(day_of(t) if t else day_of(time.time()), blank()),
                     self.models.setdefault(str(rec.get("model") or "?"), blank())):
            for k, v in values.items():
                sums[k] += v
        if t and (self.since is None or t < self.since):
            self.since = t

    # ---------------------------------------------------------------------------------- one request, one line
    def record(self, rec: dict) -> None:
        """A finished request: its line goes to the file and the totals go up.  Never raises - a counter must not take
        a request down.  A disk that is full (or a folder that was deleted) ends the ledger with one printed reason
        and the server keeps answering; the figures of the requests that followed are in the reply, just not kept."""
        with self.lock:
            self._add(rec)
            if self.broken is not None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    # after a cut line the new one starts on a fresh line, so it is not glued to the tail and lost
                    f.write(("\n" if self._torn else "") + json.dumps(rec, separators=(",", ":")) + "\n")
                self._torn = False
            except OSError as e:
                self.broken = f"{type(e).__name__}: {e}"
                print(f"[strata] the token ledger stopped: {self.path} ({self.broken}); the counters on screen keep "
                      f"what was recorded so far", flush=True)

    # --------------------------------------------------------------------------------------------------- for /metrics
    def view(self) -> dict:
        """The `usage` key of `GET /metrics`: the lifetime totals, today's, the last `DAYS` days (oldest first), and
        the models when this ledger holds more than one.  `answer` is spelled out (output minus reasoning) because
        that split is the first thing asked after the total."""
        with self.lock:
            today = day_of(time.time())
            days = [dict(self.days[d], day=d, answer=self.days[d]["output"] - self.days[d]["reasoning"])
                    for d in sorted(self.days)[-DAYS:]]
            models = [dict(v, model=m, answer=v["output"] - v["reasoning"])
                      for m, v in sorted(self.models.items(), key=lambda kv: -kv[1]["output"])]
            life = dict(self.lifetime, answer=self.lifetime["output"] - self.lifetime["reasoning"],
                        **({"since": self.since} if self.since else {}))
            day = dict(self.days.get(today, blank()), day=today)
            day["answer"] = day["output"] - day["reasoning"]
            out = {"file": str(self.path), "lifetime": life, "today": day, "days": days}
            if len(models) > 1:
                out["models"] = models
            if self.broken:
                out["broken"] = self.broken
            return out


def ledger_path(config_path, root=None) -> Path:
    """The ledger for a run config: `<config without its extension>.usage.jsonl` beside it, so
    `strata-swift-iq3_s.json` counts into `strata-swift-iq3_s.usage.jsonl` next to that model's own log.  A server
    started with no config file writes to `strata-usage.jsonl` in the project folder."""
    if not config_path:
        return Path(root) / "strata-usage.jsonl"
    p = Path(config_path)
    return p.with_name(p.stem + ".usage.jsonl")
