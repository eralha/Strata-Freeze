"""The freeze: setup.py must name one release for the engine, and carry that archive's own hash.

A frozen copy cannot ask the project's API which release is current. The day upstream ships a newer one, a
"latest" URL serves the new build, and a URL built from the checkout's version 404s on asset names upstream
no longer publishes (v0.2.0-beta renamed all three), which drops the install onto a full source compile. So
this file names one tag and the SHA-256 of the archives published under it, so the bytes are checked against
a hash that travels with the source rather than against a later day's releases page.
"""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import setup

URL = setup.PREBUILT_URL
TAG = setup.release_of(URL)[1]
ASSETS = {setup.PREBUILT_ASSET, setup.CUDA12_ASSET,
          setup.WIN_HIP_ASSET if setup.WIN else None} - {None}


def no_network(req, timeout=None):
    raise OSError("offline")


def an_archive(folder, arch=89):
    """A small archive shaped like a published engine, named as this platform's asset is named."""
    z = Path(folder) / setup.PREBUILT_ASSET
    with zipfile.ZipFile(z, "w") as a:
        a.writestr("BUILD.json", json.dumps({"version": setup.source_version(),
                                             "archs": [arch], "cuda_version": "13.2.78"}))
        a.writestr(setup.EXE, b"the engine")
    return z


def pin_for(z):
    """PINNED_ENGINE_SHA256-shaped entries for the archives these tests make, keyed as setup.py keys them."""
    data = z.read_bytes()
    return {(TAG, setup.PREBUILT_ASSET): (len(data), hashlib.sha256(data).hexdigest())}


def verify(z, pin, pinned=()):
    """verify_engine_archive with the network off, so only a hash in this file can speak.

    `pin` adds entries to the dict in setup.py; `pinned` lists the real entries to remove first, which is how a
    test gets to a checkout that carries the URL and no hash for it.
    """
    with mock.patch.dict(setup.PINNED_ENGINE_SHA256, pin, clear=bool(pinned)), \
            mock.patch.object(setup.urllib.request, "urlopen", no_network):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            setup.verify_engine_archive(z, setup.PREBUILT_ASSET, URL)
        return out.getvalue()


class TheEngineUrlNamesOneRelease(unittest.TestCase):
    def test_it_is_a_tag_and_not_the_moving_target(self):
        self.assertIn("/releases/download/v", URL)
        self.assertNotIn("latest", URL)

    def test_it_names_the_version_in_this_source(self):
        # A freeze of this checkout names the release this version was published from.
        self.assertEqual(URL, "https://github.com/Niko1221/Strata/releases/download/v"
                              + setup.source_version() + "/")

    def test_every_engine_we_can_download_has_a_hash(self):
        self.assertEqual(TAG, setup.source_version())
        self.assertEqual({tag for tag, _ in setup.PINNED_ENGINE_SHA256}, {TAG})
        self.assertEqual({asset for _, asset in setup.PINNED_ENGINE_SHA256}, ASSETS)
        self.assertIn(setup.PREBUILT_ASSET, ASSETS)
        for asset, (size, sha) in setup.PINNED_ENGINE_SHA256.items():
            self.assertGreater(size, 1_000_000, asset)
            self.assertRegex(sha, r"^[0-9a-f]{64}$", asset)


class ThePinnedHashIsUsedOnlyWhereItIsTrue(unittest.TestCase):
    def test_a_URL_that_names_a_release_we_pinned_answers_with_its_hash(self):
        self.assertEqual(setup.pinned_engine_digest(setup.PREBUILT_ASSET, URL),
                         setup.PINNED_ENGINE_SHA256[(TAG, setup.PREBUILT_ASSET)])

    def test_everything_else_answers_with_nothing(self):
        # A newer release of the same repo, a fork's releases, the moving target, a local folder, a plain
        # mirror: no hash committed here covers them, so setup verifies the way it did before. A hash pinned
        # against a moving install would be a lie the day a new release appears.
        for base in ("https://github.com/Niko1221/Strata/releases/download/v9.9.9/",
                     "https://github.com/someone-else/AwesomeEngine/releases/download/" + TAG + "/",
                     "https://github.com/Niko1221/Strata/releases/latest/",
                     "C:/mirror/engine", "//share/engine", "https://mirror.example/engine.zip"):
            self.assertIsNone(setup.pinned_engine_digest(setup.PREBUILT_ASSET, base), base)

class AOfflineMachineCanStillVerifyAnEngine(unittest.TestCase):
    def test_the_archive_whose_hash_we_carry_is_accepted_because_of_it(self):
        with tempfile.TemporaryDirectory() as folder:
            z = an_archive(folder)
            text = verify(z, pin_for(z))
            self.assertIn("verified", text)
            self.assertIn("PINNED_ENGINE_SHA256", text)   # the hash came from this file, not from the network
            self.assertTrue(z.exists())

    def test_an_archive_that_does_not_match_that_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            z = an_archive(folder)
            pin = pin_for(z)
            z.write_bytes(z.read_bytes() + b"tampered")
            with self.assertRaises(setup.UnverifiedEngine) as got:      # the length gives it away first
                verify(z, pin)
            self.assertIn("not the published", str(got.exception))
            self.assertFalse(z.exists())                  # a refused archive is not left for the next run

    def test_the_wrong_length_or_the_wrong_bytes_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            z = an_archive(folder)
            pin = pin_for(z)
            self.assertEqual(verify(z, pin) and None, None)   # the honest archive passes
            same = z.with_name(z.name + ".flipped")           # same length, one byte different
            data = z.read_bytes()
            same.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
            with self.assertRaises(setup.UnverifiedEngine) as got:
                verify(same, pin)
            self.assertIn("wrong SHA-256", str(got.exception))
            self.assertFalse(same.exists())
            with self.assertRaises(setup.UnverifiedEngine) as got:
                verify(z, {(TAG, setup.PREBUILT_ASSET): (z.stat().st_size + 1, pin[(TAG, setup.PREBUILT_ASSET)][1])})
            self.assertIn("not the published", str(got.exception))

    def test_with_neither_the_network_nor_a_hash_it_warns_rather_than_refusing(self):
        # No published digest has never been a reason to refuse, and a pinned hash that is absent is exactly
        # that case: the engine is installed, and setup says plainly that it was not verified.
        with tempfile.TemporaryDirectory() as folder:
            z = an_archive(folder)
            text = verify(z, {}, pinned=tuple(setup.PINNED_ENGINE_SHA256))
            self.assertIn("NOT verified", text)
            self.assertTrue(z.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
