#!/usr/bin/env python3
r"""Record the exact state of this Strata 0.1.45 install into freeze/FROZEN.json.

Run on the machine you are freezing, before that machine changes:

    .venv\Scripts\python.exe freeze\capture_baseline.py

Add --hash-models to SHA-256 every model file too (that reads the whole data folder, so it is slow on
153 GB; off by default - names and sizes are enough to notice a file that came and went).

Reads everything, writes only freeze/FROZEN.json.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = ".previous"


def git(*args: str) -> str:
    """`git <args>` in this repo; "" when git answers nothing."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout.strip()
    except OSError:
        return ""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_source_version() -> str:
    """The engine version this source tree builds (CMakeLists.txt's project version)."""
    m = re.search(r"project\(strata VERSION ([\d.]+)", (ROOT / "CMakeLists.txt").read_text(encoding="utf-8"))
    return m.group(1) if m else "unknown"


def load_setup():
    """setup.py from this checkout, as a module - the same import the tools/test_setup_* tests do."""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("strata_setup", ROOT / "setup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_setup_constants() -> dict:
    """The download pins, read from the live module: the values that decide what a setup run fetches.

    Read from the module rather than out of the text, so what is recorded is what setup.py would obey today -
    including the `if WIN` asset names, which are an expression in the file and a filename in the code.
    """
    s = load_setup()
    return {
        "setup_py_sha256": sha256(ROOT / "setup.py"),
        "PREBUILT_URL": s.PREBUILT_URL,
        "PREBUILT_TAG_URL": s.PREBUILT_TAG_URL,
        "REPO": s.REPO,
        "PREBUILT_ASSET": s.PREBUILT_ASSET,
        "CUDA12_ASSET": s.CUDA12_ASSET,
        "WIN_HIP_ASSET": s.WIN_HIP_ASSET,
        "MIN_ENGINE": list(s.MIN_ENGINE),
        "CUDA_WHEELS": list(s.CUDA_WHEELS),
        "CUDA12_WHEELS": list(s.CUDA12_WHEELS),
        "LLAMA_CPP_COMMIT": s.LLAMA_CPP_COMMIT,
        "HF_REVISIONS": dict(s.HF_REVISIONS),
        "PINNED_ENGINE_SHA256": {repo + "|" + tag + "|" + asset: [int(size), hexa]
                                 for (repo, tag, asset), (size, hexa) in s.PINNED_ENGINE_SHA256.items()},
        "PY_PACKAGES": list(s.PY_PACKAGES),
        "requirements_sha256": sha256(ROOT / "requirements.txt"),
    }


def installed_engine() -> dict:
    """The engine this machine runs today: engine/BUILD.json and the binaries beside it."""
    eng = ROOT / "engine"
    out: dict = {"dir": str(eng), "present": eng.is_dir()}
    info = eng / "BUILD.json"
    if info.exists():
        out["BUILD.json"] = json.loads(info.read_text(encoding="utf-8-sig"))
    out["files"] = {p.name: {"size": p.stat().st_size,
                             "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()}
                    for p in sorted(eng.iterdir()) if p.is_file()}
    out["previous_engine_present"] = (eng / PREVIOUS).is_dir()
    return out


def pip_state() -> dict:
    """requirements.txt and the stamp setup.py wrote into the venv, so a restore installs the same set."""
    out: dict = {}
    req = ROOT / "requirements.txt"
    if req.exists():
        out["requirements_sha256"] = sha256(req)
        out["requirements"] = [ln.split("#", 1)[0].strip() for ln in
                               req.read_text(encoding="utf-8").splitlines() if ln.split("#", 1)[0].strip()]
    for prefix in (Path(sys.prefix), ROOT / ".venv"):
        f = prefix / ".strata-pip.json"
        if f.exists():
            out["setup_pip_stamp"] = json.loads(f.read_text(encoding="utf-8"))
            out["setup_pip_stamp_path"] = str(f)
            break
    try:
        out["pip_freeze"] = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True,
                                           text=True, encoding="utf-8", errors="replace").stdout.splitlines()
    except OSError:
        pass
    return out


def model_files(data_dir: Path, do_hash: bool) -> dict:
    """What model bytes sit in the data folder: names and sizes, hashes only when asked for."""
    out: dict = {"dir": str(data_dir), "present": data_dir.is_dir(), "folders": {}}
    if not data_dir.is_dir():
        return out
    for sub in ("models", "packs", "mtp"):
        base = data_dir / sub
        if not base.is_dir():
            continue
        entries = {}
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.name.endswith((".done", ".part", ".tmp")):
                continue
            rec = {"size": p.stat().st_size}
            if do_hash:
                rec["sha256"] = sha256(p)
            entries[p.relative_to(base).as_posix()] = rec
        out["folders"][sub] = {"bytes": sum(e["size"] for e in entries.values()), "files": entries}
    return out


def settings_files() -> dict:
    """The per-model configs and hand-tuned data that make this install yours. setup.py regenerates the
    configs with absolute paths, so they are captured here rather than committed. Keyed by the path inside
    this checkout, so a restore puts each file back where setup.py looks for it."""
    keep = sorted(list(ROOT.glob("strata-*.json")) + list(ROOT.glob("run-*.bat")) + list(ROOT.glob("run-*.sh"))
                  + list(ROOT.glob("data/expert-profile*.bin")) + [ROOT / "requirements.txt"])
    return {p.relative_to(ROOT).as_posix(): {"path": str(p), "size": p.stat().st_size, "sha256": sha256(p)}
            for p in keep if p.is_file()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash-models", action="store_true", help="also SHA-256 every model file (slow)")
    ap.add_argument("--data-dir", default=str(ROOT.parent / "Strata-data"))
    ap.add_argument("--engine-dist", default=str(ROOT.parent / "Strata-engine-dist"),
                    help="the local engine zip mirror")
    ap.add_argument("--out", default=str(ROOT / "freeze" / "FROZEN.json"))
    a = ap.parse_args()

    tag = git("describe", "--tags", "--abbrev=0")
    dist = Path(a.engine_dist)
    engine_zip = {z.name: {"path": str(z), "size": z.stat().st_size, "sha256": sha256(z)}
                  for z in sorted(dist.glob("strata-*.zip"))} if dist.is_dir() else {}

    frozen = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "frozen_version": read_source_version(),
        "frozen_tag": tag,
        "upstream_repo": read_setup_constants()["REPO"],
        "git": {
            "head": git("rev-parse", "HEAD"),
            "describe": git("describe", "--always", "--dirty"),
            "tag_commit": git("rev-list", "-n1", tag) if tag else "",
            "commits_after_tag": git("rev-list", "--count", f"{tag}..HEAD") if tag else "",
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "remotes": git("remote", "-v"),
            "clean": git("status", "--porcelain") == "",
        },
        "machine": {
            "os": f"{platform.system()} {platform.release()}",
            "machine_root": str(ROOT),
            "python": sys.version,
            "python_exe": sys.executable,
        },
        "setup_constants": read_setup_constants(),
        "engine_local_mirror": {"dir": str(dist), "zips": engine_zip},
        "engine_installed": installed_engine(),
        "pip": pip_state(),
        "settings_files": settings_files(),
        "model_files": model_files(Path(a.data_dir), a.hash_models),
    }

    out = Path(a.out)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(frozen, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote " + str(out))
    print("  version " + frozen["frozen_version"] + "  tag " + tag + "  ("
          + frozen["git"]["commits_after_tag"] + " commits after it, tree clean: "
          + str(frozen["git"]["clean"]) + ")")
    print("  engine mirror: " + (", ".join(engine_zip) or "NONE - download the release zip"))
    print("  data folder: " + frozen["model_files"]["dir"] + " (present: "
          + str(frozen["model_files"]["present"]) + ")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash-models", action="store_true", help="also SHA-256 every model file (slow)")
    ap.add_argument("--data-dir", default=str(ROOT.parent / "Strata-data"))
    ap.add_argument("--engine-dist", default=str(ROOT.parent / "Strata-engine-dist"),
                    help="the local engine zip mirror folder")
    ap.add_argument("--out", default=str(ROOT / "freeze" / "FROZEN.json"))
    a = ap.parse_args()

    tag = git("describe", "--tags", "--abbrev=0")
    dist = Path(a.engine_dist)
    engine_zip = {z.name: {"path": str(z), "size": z.stat().st_size, "sha256": sha256(z)}
                  for z in sorted(dist.glob("strata-*.zip"))} if dist.is_dir() else {}

    frozen = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "frozen_version": read_source_version(),
        "frozen_tag": tag,
        "upstream_repo": read_setup_constants()["REPO"],
        "git": {
            "head": git("rev-parse", "HEAD"),
            "describe": git("describe", "--always", "--dirty"),
            "tag_commit": git("rev-list", "-n1", tag) if tag else "",
            "commits_after_tag": git("rev-list", "--count", tag + "..HEAD") if tag else "",
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "remotes": git("remote", "-v"),
            "clean": git("status", "--porcelain") == "",
        },
        "machine": {
            "os": platform.system() + " " + platform.release(),
            "machine_root": str(ROOT),
            "python": sys.version,
            "python_exe": sys.executable,
        },
        "setup_constants": read_setup_constants(),
        "engine_local_mirror": {"dir": str(dist), "zips": engine_zip},
        "engine_installed": installed_engine(),
        "pip": pip_state(),
        "settings_files": settings_files(),
        "model_files": model_files(Path(a.data_dir), a.hash_models),
    }

    out = Path(a.out)
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(frozen, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote " + str(out))
    print("  version " + frozen["frozen_version"] + "  tag " + tag + "  ("
          + frozen["git"]["commits_after_tag"] + " commits after it, tree clean: " + str(frozen["git"]["clean"]) + ")")
    print("  engine mirror: " + (", ".join(engine_zip) or "NONE - download the release zip"))
    print("  data folder: " + frozen["model_files"]["dir"] + " present: " + str(frozen["model_files"]["present"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
