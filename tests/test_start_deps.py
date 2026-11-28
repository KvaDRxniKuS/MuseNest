"""`run` must install dependencies — from requirements.txt, not a hardcoded
triple. The old ensure_deps checked only (flask, requests, yt_dlp), so a
machine with those three never ran pip: mutagen stayed missing and tags were
silently skipped. The check is now parsed from requirements.txt — every
future dependency lands in the same auto-install path.
"""
import builtins
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = os.path.join(HERE, "requirements.txt")

spec = __import__("importlib").util.spec_from_file_location(
    "start", os.path.join(HERE, "start.py"))
start = __import__("importlib").util.module_from_spec(spec)
spec.loader.exec_module(start)


def _importer_with_missing(*names):
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name in names:
            raise ImportError(name)
        return real(name, *a, **k)

    return _imp


class RequirementsParsing(unittest.TestCase):

    def test_the_real_requirements_are_parsed_actively(self):
        pairs = start._required_modules(REQ)

        self.assertEqual(
            pairs,
            [("flask", "flask"), ("requests", "requests"),
             ("yt-dlp", "yt_dlp"), ("mutagen", "mutagen")],
            "exactly the active requirements, optional commented ones excluded")

    def test_comments_versions_and_markers_are_stripped(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# only a comment\n"
                    "\n"
                    "flask>=3.0\n"
                    "requests[socks]>=2.31 ; python_version>'3.8'\n"
                    "mutagen>=1.47   # inline comment: зачем\n"
                    "yt-dlp>=2024.1.0\n")
            path = f.name

        try:
            self.assertEqual(
                start._required_modules(path),
                [("flask", "flask"), ("requests", "requests"),
                 ("mutagen", "mutagen"), ("yt-dlp", "yt_dlp")])
        finally:
            os.unlink(path)

    def test_a_missing_requirements_file_yields_an_empty_list(self):
        self.assertEqual(start._required_modules(
            os.path.join(HERE, "no", "such", "file.txt")), [])


class MissingDetection(unittest.TestCase):

    def test_mutagen_missing_is_detected(self):
        pairs = [("flask", "flask"), ("requests", "requests"),
                 ("yt-dlp", "yt_dlp"), ("mutagen", "mutagen")]

        missing = start._missing_packages(
            pairs, importer=_importer_with_missing("mutagen"))

        self.assertEqual([p for p, _m in missing], ["mutagen"],
                         "the machine from the log had exactly this state")

    def test_nothing_missing_when_all_import(self):
        pairs = start._required_modules(REQ)
        self.assertEqual(start._missing_packages(pairs), [])


class EnsureDeps(unittest.TestCase):

    def test_a_missing_package_triggers_pip_with_requirements(self):
        with mock.patch("builtins.__import__",
                        _importer_with_missing("mutagen")), \
             mock.patch.object(start.subprocess, "run") as run_mock, \
             mock.patch.object(start.shutil, "which",
                               return_value="/usr/bin/ffmpeg"):
            run_mock.return_value.returncode = 0

            start.ensure_deps()

        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[:5],
                         [sys.executable, "-m", "pip", "install", "--user"])
        self.assertIn("-r", cmd)
        self.assertIn(REQ, cmd,
                      "install the whole active requirements, not one pkg")

    def test_all_present_means_no_pip_call(self):
        with mock.patch.object(start.subprocess, "run") as run_mock, \
             mock.patch.object(start.shutil, "which",
                               return_value="/usr/bin/ffmpeg"):
            start.ensure_deps()

        run_mock.assert_not_called()

    def test_a_failed_pip_is_reported_not_raised(self):
        with mock.patch("builtins.__import__",
                        _importer_with_missing("mutagen")), \
             mock.patch.object(start.subprocess, "run") as run_mock, \
             mock.patch.object(start.shutil, "which", return_value=None):
            run_mock.return_value.returncode = 1

            start.ensure_deps()   # must not raise

        run_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
