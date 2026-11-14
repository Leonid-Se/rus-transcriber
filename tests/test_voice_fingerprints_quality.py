"""Синтетические проверки происхождения источников и качества аудио без моделей."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import core
import data_io
import paths
import settings
import voices


def write_recording(root, name="demo"):
    audio = root / f"{name}.wav"
    audio.write_bytes(b"synthetic audio")
    data = {"file": str(audio), "group": "demo-group", "names_confirmed": True,
            "segments": [{"speaker": "Анна", "words": [{"s": 0.0, "e": 3.0}]}]}
    source = root / f"{name}.transcript.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    return audio, source, data


class FakeEmbedder:
    def spans(self, audio, spans, cap):
        return [np.array([1.0, 0.0])] * len(spans)


class SourceFingerprintTests(unittest.TestCase):
    def test_same_mtime_content_change_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source, _ = write_recording(root)
            audio = root / "demo.wav"
            first = voices.source_fingerprint(source, audio)
            st = source.stat()
            source.write_text(json.dumps({"file": str(audio), "changed": True}), encoding="utf-8")
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
            second = voices.source_fingerprint(source, audio)
            self.assertEqual(first["audio_size"], second["audio_size"])
            self.assertEqual(first["audio_mtime"], second["audio_mtime"])
            self.assertNotEqual(first["transcript_sha256"], second["transcript_sha256"])
            self.assertEqual(voices.source_fingerprint_status(first, second, source.stat().st_mtime), "changed")

    def test_legacy_mtime_is_recognized_and_adopted_without_relearning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, source, _ = write_recording(root)
            key, audio_key = str(source.resolve()), str(audio.resolve())
            profile = {"speakers": {"Анна": {"parts": {audio_key: {
                          "vec": np.array([1.0, 0.0]), "sec": 30.0}}}},
                       "sources": {key: source.stat().st_mtime}}
            report = {}
            learned = voices.learn(profile, root, None, lambda _: self.fail("audio loaded"),
                                   selection_report=report)
            self.assertEqual(learned, [])
            self.assertEqual(report, {"source_needs_review": 1})
            self.assertIsInstance(profile["sources"][key], dict)
            self.assertEqual(profile["sources"][key]["transcript_sha256"],
                             voices.source_fingerprint(source, audio)["transcript_sha256"])
            self.assertEqual(profile.get("provenance_revision"), 1)
            self.assertNotIn("revision", profile)

    def test_changed_source_requires_preview_and_keeps_old_part(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, source, data = write_recording(root)
            key, audio_key = str(source.resolve()), str(audio.resolve())
            old = {"vec": np.array([0.0, 1.0]), "sec": 30.0,
                   "selection_version": voices.SELECTION_VERSION}
            profile = {"speakers": {"Анна": {"parts": {audio_key: old}}},
                       "sources": {key: voices.source_fingerprint(source, audio)}}
            st = source.stat()
            source.write_text(json.dumps({**data, "source_revision": 2}), encoding="utf-8")
            os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
            report = {}
            learned = voices.learn(profile, root, None, lambda _: self.fail("audio loaded"),
                                   selection_report=report)
            self.assertEqual(learned, [])
            self.assertEqual(report, {"source_changed_requires_preview": 1})
            self.assertIs(profile["speakers"]["Анна"]["parts"][audio_key], old)


class AudioQualityTests(unittest.TestCase):
    def test_metrics_do_not_fail_on_zero_empty_or_invalid_signal(self):
        zero = voices.audio_quality(np.zeros(16000, dtype=np.float32))
        self.assertTrue(zero["available"])
        self.assertIn("silent", zero["warnings"])
        self.assertFalse(voices.audio_quality(np.array([], dtype=np.float32))["available"])
        self.assertFalse(voices.audio_quality(np.array([0.0, float("nan")]))["available"])

    def test_clipping_and_quiet_levels_are_reported(self):
        clipped = voices.audio_quality(np.ones(32000, dtype=np.float32))
        self.assertGreater(clipped["clipping_ratio"], 0.9)
        self.assertTrue(clipped["clipped"])
        self.assertIn("clipping", clipped["warnings"])
        quiet = voices.audio_quality(np.sin(np.arange(32000) / 10) * 0.001)
        self.assertIn("very_quiet", quiet["warnings"])
        self.assertNotIn("clipping", quiet["warnings"])

    def test_default_warn_learns_clipped_audio_with_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio, source, _ = write_recording(root)
            profile = {"speakers": {}, "sources": {}}
            report, quality = {}, {}
            learned = voices.learn(profile, root, FakeEmbedder(), lambda _: np.ones(48000),
                                   only=[source], allow_replace=True, selection_report=report,
                                   quality_report=quality)
            self.assertEqual(learned, [source.name])
            self.assertIn("Анна", profile["speakers"])
            self.assertTrue(profile["audio_quality"][str(audio.resolve())]["clipped"])
            self.assertTrue(any(w.get("kind") == "audio_quality" for w in profile["warnings"]))
            self.assertFalse(quality.get("rejected"))

    def test_reject_clipping_policy_skips_source_without_vectors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, source, _ = write_recording(root)
            profile = {"speakers": {}, "sources": {}}
            report, quality = {}, {}
            learned = voices.learn(profile, root, FakeEmbedder(), lambda _: np.ones(48000),
                                   only=[source], allow_replace=True, selection_report=report,
                                   quality_policy="reject_clipping", quality_report=quality)
            self.assertEqual(learned, [])
            self.assertEqual(profile["speakers"], {})
            self.assertEqual(report.get("audio_quality_rejected"), 1)
            self.assertTrue(quality.get("rejected"))
            self.assertEqual(quality.get("reject_reason"), "clipping")
            self.assertNotIn(str(source.resolve()), profile["sources"])


class VoiceManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.groups = self.root / "groups"
        self.patches = [patch.object(paths, "GROUPS", self.groups),
                        patch.object(paths, "CONFIG_FILE", self.root / "config.json"),
                        patch.object(paths, "DATA_DIR", self.root), patch.object(paths, "JOBS", self.root / "jobs")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def group(self, name="demo-group"):
        folder = self.groups / name
        folder.mkdir(parents=True)
        (folder / "vocab.json").write_text("{}", encoding="utf-8")
        return folder

    def profile(self, group="demo-group", **extra):
        return {"version": voices.PROFILE_VERSION, "model": "synthetic", "speakers": {}, "sources": {}, **extra}

    def test_refresh_plan_reports_changed_source_and_keeps_profile(self):
        folder = self.group()
        audio, source, data = write_recording(folder)
        key = str(source.resolve())
        fingerprint = voices.source_fingerprint(source, audio)
        profile = self.profile(sources={key: fingerprint})
        target = folder / "voices.json"
        target.write_text(json.dumps(profile), encoding="utf-8")
        before = target.read_bytes()
        plan = core.voice_refresh_plan("demo-group")
        entry = plan["groups"][0]["sources"][0]
        self.assertEqual(entry["status"], "unchanged")
        self.assertFalse(entry["needs_update"])
        st = source.stat()
        source.write_text(json.dumps({**data, "source_revision": 2}), encoding="utf-8")
        os.utime(source, ns=(st.st_atime_ns, st.st_mtime_ns))
        plan = core.voice_refresh_plan("demo-group")
        entry = plan["groups"][0]["sources"][0]
        self.assertEqual(entry["status"], "changed")
        self.assertTrue(entry["transcript_changed"])
        self.assertTrue(entry["needs_update"])
        self.assertEqual(plan["needs_update"], 1)
        self.assertEqual(target.read_bytes(), before)
        self.assertNotIn("vec", json.dumps(plan))

    def test_legacy_source_is_shown_as_baseline_pending(self):
        folder = self.group()
        audio, source, _ = write_recording(folder)
        profile = self.profile(sources={str(source.resolve()): source.stat().st_mtime})
        (folder / "voices.json").write_text(json.dumps(profile), encoding="utf-8")
        entry = core.voice_refresh_plan("demo-group")["groups"][0]["sources"][0]
        self.assertEqual(entry["status"], "legacy_baseline_pending")
        self.assertTrue(entry["needs_update"])

    def test_backup_creates_separate_unencrypted_copy(self):
        folder = self.group()
        target = folder / "voices.json"
        target.write_text(json.dumps(self.profile()), encoding="utf-8")
        before = target.read_bytes()
        result = core.backup_voice_profiles("demo-group")
        self.assertEqual(result["count"], 1)
        backup = Path(result["backups"][0]["backup"])
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(backup.parent.name, "voice-backups")
        self.assertIn("биометрия", result["note"])

    def test_quality_policy_is_explicit_per_group_and_inherited_by_default(self):
        self.group()
        result = core.set_voice_quality_policy("reject_clipping", "demo-group")
        self.assertEqual(result["audio_quality_policy"], "reject_clipping")
        policy = settings.group_voice_settings("demo-group")
        self.assertEqual(policy["audio_quality_policy"], "reject_clipping")
        self.assertEqual(policy["audio_quality_policy_source"], "group")
        core.set_voice_quality_policy("warn")
        self.assertEqual(settings.group_voice_settings("demo-group")["audio_quality_policy"], "reject_clipping")
        with self.assertRaises(core.CoreError):
            core.set_voice_quality_policy("off", "demo-group")

    def test_invalid_global_quality_default_is_not_silently_used(self):
        folder = self.group()
        (self.root / "config.json").write_text(json.dumps({"voice_quality_policy_default": "off"}),
                                               encoding="utf-8")
        settings.save_group_voice_settings("demo-group", {"auto_learn": True, "reason": "manual"})
        with self.assertRaises(ValueError):
            settings.group_voice_settings("demo-group")
        self.assertTrue(folder.exists())

    def test_quality_policy_survives_other_settings_changes(self):
        self.group()
        settings.save_group_voice_settings("demo-group", {"auto_learn": True, "reason": "manual",
                                                          "audio_quality_policy": "reject_clipping"})
        core.set_voice_auto_learn(False, "demo-group")
        self.assertEqual(settings.group_voice_settings("demo-group")["audio_quality_policy"], "reject_clipping")
        settings.save_group_voice_settings("demo-group", {"auto_learn": True, "reason": "manual",
                                                          "audio_quality_policy": "reject_clipping"})
        data = {"speakers": {"Анна": {"parts": {str(i): {
            "auto_sec": 30, "human_sec": 0, "selection_version": voices.SELECTION_VERSION} for i in range(3)}}}}
        voices.auto_learn_group(data, "demo-group", ["Анна"], lambda: [], lambda: None)
        policy = settings.group_voice_settings("demo-group")
        self.assertFalse(policy["auto_learn"])
        self.assertEqual(policy["audio_quality_policy"], "reject_clipping")

    def test_quality_summary_reads_saved_measurements_without_vectors(self):
        folder = self.group()
        profile = self.profile(audio_quality={
            "demo.wav": {"at": "2026-11-14 10:00:00", "from": "demo.transcript.json",
                         "available": True, "clipping_ratio": 0.2, "warnings": ["clipping"]}})
        (folder / "voices.json").write_text(json.dumps(profile), encoding="utf-8")
        report = core.voice_quality("demo-group")["groups"][0]
        self.assertEqual(report["audio_quality_policy"], "warn")
        self.assertEqual(report["summary"]["measured_sources"], 1)
        self.assertEqual(report["summary"]["clipping_sources"], 1)
        self.assertTrue(report["measurements"][0]["warning_notes"])
        self.assertNotIn("vec", json.dumps(report))

    def test_profile_summary_separates_measured_seconds_from_weight(self):
        folder = self.group()
        profile = self.profile(speakers={"Анна": {"parts": {
            "old.wav": {"vec": [1.0, 0.0], "sec": 100.0},
            "new.wav": {"vec": [1.0, 0.0], "sec": 45.0, "auto_sec": 30.0, "human_sec": 5.0,
                        "selection_version": voices.SELECTION_VERSION}}}})
        (folder / "voices.json").write_text(json.dumps(profile), encoding="utf-8")
        item = core.voice_profiles()["profiles"][0]["speakers"]["Анна"]
        self.assertEqual(item["records"], 2)
        self.assertEqual(item["seconds"], 35.0)
        self.assertEqual(item["weighted_seconds"], 145.0)
        self.assertEqual(item["human_seconds"], 5.0)
        self.assertEqual(item["unmeasured_records"], 1)

    def test_archive_settings_keep_valid_quality_policy_only(self):
        valid = {"auto_learn": True, "reason": "manual", "audio_quality_policy": "reject_clipping"}
        self.assertEqual(data_io._voice_settings(valid), valid)
        with self.assertRaises(RuntimeError):
            data_io._voice_settings({"auto_learn": True, "reason": "manual",
                                     "audio_quality_policy": "reject_everything"})


if __name__ == "__main__":
    unittest.main()
