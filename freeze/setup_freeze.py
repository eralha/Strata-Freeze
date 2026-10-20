r"""Build an install that matches this freeze: venv, pinned packages, the engine from the local archive.

    .venv\Scripts\python.exe freeze\setup_freeze.py --venv --requirements --engine-zip ..\Strata-engine-dist\strata-windows-x64.zip

Nothing here fetches a newer version: the engine comes from the zip you point at (checked against the
SHA-256 pinned in setup.py), the Python packages come from requirements.txt, and the model files stay
where they are.  Run from the checkout's folder.  Add --print-command to see the setup.py command that
starts the model, and --restore-settings to put this machine's model configs back after a move.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "freeze" / "FROZEN.json"
SETTINGS = ROOT / "freeze" / "settings"
PY_MIN = (3, 10)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(16 << 20):
            h.update(chunk)
    return h.hexdigest()


def say(msg: str) -> None:
    print("  " + msg)


def fail(msg: str) -> int:
    print("[fail] " + msg)
    return 1


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def make_venv() -> int:
    """Create .venv in this folder, the way START-HERE.bat does, and report the interpreter."""
    py = venv_python()
    if py.exists():
        say("venv: already there - " + str(py))
        return 0
    if sys.version_info < PY_MIN:
        return fail(f"this Python is {'.'.join(map(str, sys.version_info[:3]))}, setup needs "
                    + ".".join(map(str, PY_MIN)) + "+: run START-HERE.bat once to install one")
    subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    if not py.exists():
        return fail("venv: python.exe is not in .venv after creating it")
    say("venv: created " + str(py))
    return 0


def install_packages(dry: bool = False) -> int:
    """requirements.txt + the CUDA libraries the engine loads, into .venv (setup.py's own pip step)."""
    py = venv_python()
    if not py.exists():
        return fail("no .venv: run this with --venv first (or START-HERE.bat)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("strata_setup", ROOT / "setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    reqs = setup.requirement_lines()
    pkgs = reqs + (setup.CUDA_WHEELS if setup.WIN else [])
    cmd = [str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), *setup.CUDA_WHEELS]
    if dry:
        say("pip: would run " + " ".join(cmd))
        return 0
    say(f"pip: {len(pkgs)} pinned packages into .venv ...")
    subprocess.run(cmd, check=True)
    stamp = Path(sys.prefix) / ".strata-pip.json"
    stamp.write_text(json.dumps(pkgs, indent=0) + "\n", encoding="utf-8")
    say("pip: done, stamp written so setup.py will not reinstall them")
    return 0

def install_engine_from_zip(zip_path: Path, force: bool = False) -> int:
    """Unpack a pinned engine archive into engine/, after checking it against setup.py's pinned SHA-256.

    This is the offline path a freeze needs: a fresh clone has no engine/, and the release it names may
    outlive GitHub's keeping of it.  The check is the one setup.py would do, against the hash in this file,
    and it runs before anything is written.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("strata_setup", ROOT / "setup.py")
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    tag = setup.source_version()
    if not zip_path.is_file():
        return fail(f"engine archive not found: {zip_path}")
    if setup.PINNED_ENGINE_SHA256:
        entry = setup.PINNED_ENGINE_SHA256.get((tag, zip_path.name))
        if not entry:
            return fail(f"setup.py pins no SHA-256 for {zip_path.name} of release {tag}, so it is not installed")
        size, want = int(entry[0]), entry[1]
        if zip_path.stat().st_size != size:
            return fail(f"{zip_path.name} is {zip_path.stat().st_size:,} bytes, not the pinned {size:,}")
        got = sha256(zip_path)
        if got != want:
            return fail(f"{zip_path.name} has SHA-256 {got}, pinned {want}: not installed")
        say(f"engine archive: {zip_path.name} SHA-256 verified against setup.py ({want[:12]}...)")

    eng = ROOT / "engine"
    info = eng / "BUILD.json"
    if info.exists() and not force:
        meta = json.loads(info.read_text(encoding="utf-8-sig"))
        say(f"engine/ already holds {meta.get('version')} ({meta.get('source')}): pass --force to replace it")
        return 0
    eng.mkdir(exist_ok=True)
    tmp = eng / "_unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as f:
        f.extractall(tmp)
    meta = json.loads((tmp / "BUILD.json").read_text(encoding="utf-8"))
    if tuple(int(x) for x in str(meta.get("version", "0")).split(".")[:4] if x.isdigit()) < tuple(setup.MIN_ENGINE):
        return fail(f"the archive is engine {meta.get('version')}, this checkout needs "
                    + ".".join(map(str, setup.MIN_ENGINE)) + " or newer")
    if meta.get("backend") == "hip":
        say("note: this is the AMD HIP engine; setup.py installs its ROCm runtime beside it on Windows")
    for p in sorted(tmp.iterdir()):
        shutil.move(str(p), str(eng / p.name))
    shutil.rmtree(tmp, ignore_errors=True)
    say(f"engine {meta.get('version')} installed in {eng} (archs {', '.join(map(str, meta.get('archs', [])))})")
    return 0


def restore_settings(new_root: Path | None = None) -> int:
    """Put this machine's configs, start scripts and expert profiles back.

    setup.py writes absolute paths into strata-<model>.json, so a snapshot restored into a checkout
    somewhere else has to have the frozen machine's root rewritten to the new one.  The data folder
    (Strata-data) is not touched: setup.py finds it by name next to the checkout, docs/INSTALL.md.
    """
    if not SETTINGS.is_dir():
        return fail("freeze/settings is empty: run capture_baseline.py on the machine, then this with --snapshot")
    frozen = json.loads(FROZEN.read_text(encoding="utf-8")) if FROZEN.is_file() else {}
    old = Path(frozen.get("machine", {}).get("machine_root", str(ROOT)))
    dst_root = Path(new_root) if new_root else ROOT
    n = 0
    for src in sorted(p for p in SETTINGS.rglob("*") if p.is_file()):
        dst = dst_root / src.relative_to(SETTINGS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if new_root and (src.suffix == ".json" or src.suffix == ".bat" or src.suffix == ".sh"):
            txt = src.read_text(encoding="utf-8-sig").replace(str(old), str(dst_root))
            dst.write_text(txt, encoding="utf-8")
        else:
            shutil.copy2(src, dst)
        n += 1
    say(f"settings: {n} files into {dst_root}"
        + (f" (paths {old} -> {dst_root})" if new_root else ""))
    if new_root:
        say("note: the model data folder keeps its own path; put Strata-data where setup.py can find it "
            "or pass --data-dir")
    return 0

def print_command(data_dir: str | None, engine_dist: Path | None) -> None:
    """The setup.py command that finishes the job on this machine, with the engine taken from the local mirror."""
    py = venv_python()
    parts = [str(py), "setup.py", "--no-start", "--yes"]
    if engine_dist:
        parts += ["--prebuilt", str(engine_dist)]
    if data_dir:
        parts += ["--data-dir", data_dir]
    say("setup: " + " ".join(parts))
    say("then: START-HERE.bat (or run-<model>.bat) starts the model, serving 127.0.0.1:8080")


def snapshot_settings() -> int:
    """Copy this machine's configs, start scripts and expert profiles into freeze/settings/."""
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    SETTINGS.mkdir(parents=True, exist_ok=True)
    n = 0
    for rel, rec in frozen["settings_files"].items():
        if rel == "requirements.txt":          # already in git
            continue
        src = Path(rec["path"])
        if not src.is_file():
            say("skip (missing on this machine): " + rel)
            continue
        dst = SETTINGS / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        n += 1
    say(f"snapshot: {n} files in {SETTINGS} (they carry this machine's absolute paths)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", action="store_true", help="create .venv")
    ap.add_argument("--requirements", action="store_true", help="pip install requirements.txt and the CUDA wheels")
    ap.add_argument("--engine-zip", help="the pinned engine archive to unpack into engine/ (verified first)")
    ap.add_argument("--force", action="store_true", help="replace an engine that is already installed")
    ap.add_argument("--snapshot", action="store_true", help="save this machine's settings into freeze/settings")
    ap.add_argument("--restore-settings", action="store_true", help="copy freeze/settings back into this checkout")
    ap.add_argument("--into", help="restore the settings into another checkout folder, rewriting its paths")
    ap.add_argument("--data-dir", help="the model data folder to pass to setup.py")
    ap.add_argument("--engine-dist", help="the engine mirror folder to pass to setup.py --prebuilt")
    ap.add_argument("--print-command", action="store_true", help="print the setup.py command and stop")
    ap.add_argument("--dry-run", action="store_true", help="say what pip would do")
    a = ap.parse_args()

    dist = Path(a.engine_dist) if a.engine_dist else ROOT.parent / "Strata-engine-dist"
    if a.print_command:
        print_command(a.data_dir, dist if dist.is_dir() else None)
        return 0
    steps = [a.venv, bool(a.requirements), bool(a.engine_zip), a.snapshot, a.restore_settings]
    if not any(steps):
        print("nothing to do: pass --venv, --requirements, --engine-zip, --snapshot or --restore-settings")
        print("(--print-command shows the setup.py command that starts the model)")
        return 2
    if a.venv and make_venv():
        return 1
    if a.requirements and install_packages(a.dry_run):
        return 1
    if a.engine_zip and install_engine_from_zip(Path(a.engine_zip), a.force):
        return 1
    if a.snapshot and snapshot_settings():
        return 1
    if a.restore_settings and restore_settings(Path(a.into) if a.into else None):
        return 1
    print("done. check it with: .venv\\Scripts\\python.exe freeze\\verify_freeze.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
