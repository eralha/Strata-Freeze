# Strata 0.1.45 - frozen snapshot

This checkout stays on **Strata 0.1.45** (tag `v0.1.45`, released 2026-10-17). Nothing here fetches a newer
version. Upstream moved to a different packaging scheme at `v0.2.0-beta` (2026-10-19), whose release assets are
`strata-engine-*.tar.gz`, `Strata-*.AppImage` and `Strata-*-windows-*.exe`; this version asks for
`strata-windows-x64.zip`, which those releases do not carry, so a clone of upstream `main` on a new machine would
not find an engine and would start compiling one. Staying on 0.1.45 is a choice, and this folder is built for it.

## What START-HERE.bat does

`START-HERE.bat` finds a Python >= 3.10 (installing 3.12 for your user with `winget`, else from python.org),
creates `.venv` inside this folder, and runs `.venv\Scripts\python.exe setup.py %*`. The work is in `setup.py`:
check the PC and driver, install `requirements.txt`, get the engine, download the model files, write
`strata-<model>.json` and `run-<model>.bat`, then start the server.

`UPDATE.bat` is `git pull --ff-only` plus `setup.py --update`. On this branch it refuses to pull, and says so.

## Where the engine comes from

| | |
|---|---|
| Asset wanted (Windows) | `strata-windows-x64.zip` (CUDA 13) - and `-hip.zip`, `-cuda12.zip` for AMD and old cards |
| This checkout's version | `project(strata VERSION 0.1.45)` in `CMakeLists.txt`, read by `source_version()` |
| Pinned to | `PREBUILT_URL = "https://github.com/Niko1221/Strata/releases/download/v0.1.45/"` in `setup.py` |
| Hash pinned in the file | `PINNED_ENGINE_SHA256`: 165 243 142 bytes, `a1d4cafb8319c253f3010ff118dbd5ed07c232056b939ac7735819559f3b859e` (and the CUDA 12 and HIP zips) |
| Local mirror | `..\Strata-engine-dist\strata-windows-x64.zip` - 165 MB, over GitHub's 100 MB file limit, so it is not in git |
| Installed into | `engine/` (`strata.exe`, `strata-vision.exe`, `strata_kq.dll`, `BUILD.json`), gitignored |
| Re-downloaded only when | the installed engine is older than `MIN_ENGINE = (0, 1, 45)` - so a present 0.1.45 engine is kept |

Nothing in `src/`, `include/`, `serve/` or a generated `run-<model>.bat` looks for a new version. Only `setup.py`
downloads, and only the release named above.

## Fresh clone, same version

```bat
git clone <your repo> Strata
cd Strata
.venv\Scripts\python.exe -m venv .venv
.venv\Scripts\python.exe freeze\setup_freeze.py --venv --requirements ^
    --engine-zip ..\Strata-engine-dist\strata-windows-x64.zip
.venv\Scripts\python.exe freeze\verify_freeze.py
```

`setup_freeze.py` checks the archive against the hash pinned in `setup.py` before unpacking, and installs the
pinned Python packages. Then, for a setup run that takes the engine from the mirror and never asks GitHub:

```
.venv\Scripts\python.exe setup.py --prebuilt ..\Strata-engine-dist --data-dir ..\Strata-data --no-start
```

`--prebuilt` with a folder is treated as your own file (`is_local`), so nothing is fetched for the engine at all.

The model files are not in git: `Strata-data\` next to the checkout (153 GB on the frozen machine) holds the GGUF
shards, packs and draft layers. Every file `setup.py` downloads from Hugging Face is pinned by commit in
`HF_REVISIONS` and checked by SHA-256, so a reinstall gets the same bytes whatever those repositories do later.
Copy the folder, and point setup at it with `--data-dir`; `freeze\setup_freeze.py --restore-settings --into
<new checkout>` puts this machine's model configs back with their paths rewritten.

`freeze\FROZEN.json` records the whole state: the commit, the pins, the installed engine, the pip set, the
settings hashes, and the model file inventory (`--hash-models` adds hashes, slow on 153 GB).

## Staying on 0.1.45

- Do not run `git pull`, `UPDATE.bat` or `setup.py --update`.
- Keep `PREBUILT_URL` naming `v0.1.45`, and a copy of the zip in `..\Strata-engine-dist` (plus one offline backup).
- `freeze\verify_freeze.py` answers 0 when nothing moved and 1 when a pin, the engine, the packages, the
  settings or the model files changed; `--deep` re-hashes the model files too.
- On Windows, `pip download -r requirements.txt nvidia-cublas==13.0.2.14 nvidia-cuda-runtime==13.0.96 -d ..\Strata-engine-dist\wheels`
  keeps the Python side installable with no network.
- `setup.py --rollback-engine` restores the engine kept in `engine\.previous` if an update ever replaced one.

## What was checked, and what was not

- `python -m unittest tools.test_setup_engine_hash tools.test_setup_pins tools.test_setup_hotfix_tag
  tools.test_setup_strix_halo` after the pin: same result as before the change on this machine (the three
  failures in the first file are pre-existing here, because this PC has an engine installed and those tests
  assume none).
- `freeze\capture_baseline.py`, `freeze\verify_freeze.py` and `freeze\setup_freeze.py --snapshot` were run on
  this machine and report 45 checks ok.
- The engine zip's SHA-256 was verified two ways: against GitHub's releases API, and against the file downloaded
  to `..\Strata-engine-dist` - both `a1d4cafb83...`.
- Not tested: a fresh install on a second machine (no second PC here), the wheels-downloading command above,
  and a Linux freeze - release `v0.1.45` publishes Windows archives only, so Linux compiles the engine.
