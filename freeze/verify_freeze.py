r"""Check this machine against freeze/FROZEN.json and say what moved.

    .venv\Scripts\python.exe freeze\verify_freeze.py

Exit 0 when everything the freeze depends on is where it was left; 1 when a pin, the engine archive,
the installed engine, the Python set, the settings files or the model files moved.  A machine that
answers 1 has not been touched the way the freeze left it.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OK, DRIFT, INFO = "OK  ", "DRIFT", "info"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def load_setup():
    """setup.py from this checkout, as a module (the pins are read from it, not copied in here)."""
    spec = importlib.util.spec_from_file_location("strata_setup", ROOT / "setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, what: str, detail: str = "") -> None:
        self.rows.append((level, what, detail))

    def equal(self, what: str, have, want) -> None:
        self.add(OK if have == want else DRIFT, what,
                 "identical" if have == want else f"have {have!r}, frozen {want!r}")

    def truthy(self, what: str, ok: bool, detail: str) -> None:
        self.add(OK if ok else DRIFT, what, detail)

    def note(self, what: str, detail: str) -> None:
        self.add(INFO, what, detail)

    def show(self) -> int:
        width = max(len(w) for _, w, _ in self.rows) + 1
        for level, what, detail in self.rows:
            print("[" + level + "] " + what.ljust(width) + " " + detail)
        drift = [w for l, w, _ in self.rows if l == DRIFT]
        print("")
        print(str(len(self.rows) - len(drift)) + " ok, " + str(len(drift)) + " drift, "
              + str(sum(1 for l, _, _ in self.rows if l == INFO)) + " notes")
        return 1 if drift else 0

def check_pins(rep: Report, frozen: dict, setup) -> None:
    """The pins a setup run obeys, read out of this checkout's setup.py right now."""
    want = frozen["setup_constants"]
    rep.equal("source version", setup.source_version(), frozen["frozen_version"])
    rep.equal("PREBUILT_URL", setup.PREBUILT_URL, want["PREBUILT_URL"])
    rep.truthy("PREBUILT_URL names no moving release", "latest" not in setup.PREBUILT_URL,
               setup.PREBUILT_URL)
    rep.equal("MIN_ENGINE", list(setup.MIN_ENGINE), list(want["MIN_ENGINE"]))
    rep.equal("CUDA wheels", setup.CUDA_WHEELS, want["CUDA_WHEELS"])
    rep.equal("CUDA 12 wheels", setup.CUDA12_WHEELS, want["CUDA12_WHEELS"])
    rep.equal("Python packages", setup.PY_PACKAGES, want["PY_PACKAGES"])
    rep.equal("llama.cpp commit", setup.LLAMA_CPP_COMMIT, want["LLAMA_CPP_COMMIT"])
    rep.equal("HF revisions", setup.HF_REVISIONS, want["HF_REVISIONS"])
    tag = frozen["frozen_version"]
    rep.truthy("engine hash pinned for this release",
               (tag, want["PREBUILT_ASSET"]) in setup.PINNED_ENGINE_SHA256,
               f"{tag} / {want['PREBUILT_ASSET']}")
    rep.equal("pinned engine hashes",
              {t + "|" + a: [int(s), h] for (t, a), (s, h) in setup.PINNED_ENGINE_SHA256.items()},
              want["PINNED_ENGINE_SHA256"])
    rep.equal("setup.py untouched since the freeze", sha256(ROOT / "setup.py"),
              want["setup_py_sha256"])
    rep.equal("requirements.txt untouched", sha256(ROOT / "requirements.txt"),
              want["requirements_sha256"])


def check_source(rep: Report, frozen: dict) -> None:
    """The commit this checkout sits on, against the commit the freeze recorded."""
    g = frozen["git"]
    rep.note("frozen tag", f"{frozen['frozen_tag']} = {g['tag_commit'][:12]}"
            + (f" (+{g['commits_after_tag']} commits)" if g["commits_after_tag"] not in ("0", "") else ""))
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head == g["head"]:
        rep.truthy("HEAD is the frozen commit", True, head[:12])
    else:
        # Commits on top of the frozen commit are expected - this copy is the user's own repository. What must not
        # happen is the tree moving away from it: a rebase, a reset, a pull from upstream.
        anc = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", g["head"], "HEAD"],
                             capture_output=True).returncode == 0
        n = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--count", g["head"] + "..HEAD"],
                           capture_output=True, text=True).stdout.strip()
        rep.truthy("HEAD is the frozen commit, or a commit on top of it", anc,
                   head[:12] + (" is " + n + " commit(s) on top of " + g["head"][:12] if anc and n.isdigit()
                                else " is not descended from " + g["head"][:12]))
    # Untracked files are this PC's (models, logs, notes); drift that matters is a tracked file changing.
    rep.truthy("working tree clean",
               subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                              capture_output=True, text=True).stdout.strip() == "",
               "no uncommitted files")
    rep.note("engine release repository", frozen["upstream_repo"]
            + " (the release the engine zip and its hash come from; the source tree is yours to push)")


def check_engine_archive(rep: Report, frozen: dict) -> None:
    """The local mirror zip, against the SHA-256 pinned in setup.py."""
    mirror = frozen["engine_local_mirror"]
    d = Path(mirror["dir"])
    for name, rec in mirror["zips"].items():
        p = d / name
        if not p.is_file():
            rep.add(DRIFT, "engine archive " + name, "missing from " + str(d))
            continue
        size, sha = setup_pinned(name)
        if not sha:
            rep.note("engine archive " + name, "no hash pinned in setup.py for it, not checked")
            continue
        got = sha256(p)
        rep.equal("engine archive " + name, (p.stat().st_size, got), (int(size), sha))


PINNED_CACHE: dict = {}


def setup_pinned(asset: str) -> tuple[int, str]:
    """The (size, sha256) pinned in setup.py for the frozen release's `asset`, (0, "") when it pins nothing."""
    if not PINNED_CACHE:
        setup = load_setup()
        tag = setup.source_version()
        for (t, a), v in setup.PINNED_ENGINE_SHA256.items():
            PINNED_CACHE[(t, a)] = (int(v[0]), v[1])
        PINNED_CACHE["tag"] = tag
    tag = PINNED_CACHE["tag"]
    return PINNED_CACHE.get((tag, asset), (0, ""))

def check_installed_engine(rep: Report, frozen: dict) -> None:
    """The engine in engine/, file by file, against what was installed at freeze time.

    An engine kept in engine/.previous is not drift: it is the copy an update left behind, and
    setup.py --rollback-engine is what puts it back.
    """
    want = frozen["engine_installed"]
    eng = ROOT / "engine"
    rep.truthy("engine folder", eng.is_dir(), str(eng))
    info = eng / "BUILD.json"
    if info.exists() and "BUILD.json" in want:
        rep.equal("engine BUILD.json", json.loads(info.read_text(encoding="utf-8-sig")), want["BUILD.json"])
    have = {p.name: p.stat().st_size for p in eng.iterdir() if p.is_file()} if eng.is_dir() else {}
    for name, rec in want["files"].items():
        if name not in have:
            rep.add(DRIFT, "engine file " + name, "missing")
        else:
            rep.equal("engine file " + name, have[name], rec["size"])
    for name in sorted(set(have) - set(want["files"])):
        rep.add(DRIFT, "engine file " + name, "appeared since the freeze (" + str(have[name]) + " bytes)")
    if (eng / ".previous").is_dir():
        rep.note("engine kept by an update", "engine/.previous holds an older engine "
                "(setup.py --rollback-engine puts it back)")
    else:
        rep.add(OK, "engine kept by an update", "engine/.previous is empty")


def check_python(rep: Report, frozen: dict) -> None:
    """The venv's package set, against the set the freeze recorded, and requirements.txt's hash."""
    req = ROOT / "requirements.txt"
    rep.equal("requirements.txt", sha256(req) if req.is_file() else "missing",
              frozen["pip"].get("requirements_sha256", "missing"))
    stamp = Path(sys.prefix) / ".strata-pip.json"
    if not stamp.is_file():
        stamp = ROOT / ".venv" / ".strata-pip.json"
    if not stamp.is_file():
        rep.add(DRIFT, "pip stamp", "no .strata-pip.json: setup.py has not installed packages into this venv")
        return
    rep.equal("pip stamp", sorted(json.loads(stamp.read_text(encoding="utf-8"))),
              sorted(frozen["pip"].get("setup_pip_stamp", [])))


def check_settings(rep: Report, frozen: dict) -> None:
    """The per-model configs and hand-tuned data, by hash (they carry absolute paths, so they are yours)."""
    for name, rec in frozen["settings_files"].items():
        p = ROOT / name
        if not p.is_file():
            rep.add(DRIFT, "settings " + name, "missing - restore it from the snapshot (setup.py regenerates it "
                    "with this machine's absolute paths)")
        else:
            rep.equal("settings " + name, sha256(p), rec["sha256"])
    snap = ROOT / "freeze" / "settings"
    kept = {p.relative_to(snap).as_posix() for p in snap.rglob("*") if p.is_file()} if snap.is_dir() else set()
    for name in sorted(set(frozen["settings_files"]) - kept - {"requirements.txt"}):
        rep.add(INFO, "snapshot " + name, "hash recorded, bytes in " + frozen["settings_files"][name]["path"])


def check_data(rep: Report, frozen: dict, deep: bool) -> None:
    """The model files: names and sizes (hashes only with --deep, which reads the whole data folder)."""
    want = frozen["model_files"]
    data = Path(want["dir"])
    if not data.is_dir():
        rep.add(DRIFT, "data folder", "missing: " + str(data) + " (setup.py --data-dir points elsewhere)")
        return
    for sub, rec in want["folders"].items():
        base = data / sub
        if not base.is_dir():
            rep.add(DRIFT, "data " + sub, "missing: " + str(base))
            continue
        have = {p.relative_to(base).as_posix(): p.stat().st_size
                for p in base.rglob("*") if p.is_file() and not p.name.endswith((".done", ".part", ".tmp"))}
        for name, size in rec["files"].items():
            if name not in have:
                rep.add(DRIFT, "data " + sub + "/" + name, "missing")
            elif deep:
                rec_sha = rec["files"][name].get("sha256")
                if rec_sha:
                    rep.equal("data " + sub + "/" + name, sha256(base / name), rec_sha)
        for name in sorted(set(have) - set(rec["files"])):
            rep.add(DRIFT, "data " + sub + "/" + name, "appeared since the freeze")
        rep.note("data " + sub, str(len(have)) + " files, " + format(rec["bytes"] / 1e9, ".1f") + " GB")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", default=str(ROOT / "freeze" / "FROZEN.json"))
    ap.add_argument("--deep", action="store_true", help="also re-hash the model files (slow)")
    a = ap.parse_args()
    frozen = json.loads(Path(a.frozen).read_text(encoding="utf-8"))
    rep = Report()
    rep.note("freeze", f"v{frozen['frozen_version']} captured {frozen['captured_at'][:19]}Z on "
           + frozen["machine"]["machine_root"])
    check_pins(rep, frozen, load_setup())
    check_source(rep, frozen)
    check_engine_archive(rep, frozen)
    check_installed_engine(rep, frozen)
    check_python(rep, frozen)
    check_settings(rep, frozen)
    check_data(rep, frozen, a.deep)
    return rep.show()


if __name__ == "__main__":
    raise SystemExit(main())
