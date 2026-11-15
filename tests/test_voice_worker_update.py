"""Проверка worker без моделей и настоящих пользовательских данных."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import core
import paths
import vocab
import voices
import worker


class Status:
    def __init__(self):
        self.data = {}

    def set(self, **values):
        self.data.update(values)


class VoiceWorkerUpdate(unittest.TestCase):
    @staticmethod
    def _vocab_args(**values):
        return types.SimpleNamespace(vocab=values.get("vocab", ""), group=values.get("group", ""))

    @staticmethod
    def _manual_update_args(source, audio, group_vocab, group="demo-group", profile_path=None):
        return types.SimpleNamespace(
            learn_only=str(source), vocab="", group="",
            voice_source_hash=core._voice_hash(source),
            voice_profile_hash=core._voice_hash(profile_path) if profile_path else "absent",
            voice_audio_hash=core._voice_hash(audio),
            voice_group=group, voice_group_override=group,
            voice_vocab_path=str(group_vocab), voice_vocab_hash=core._voice_hash(group_vocab))

    def test_vocab_resolution_checks_audio_directory_and_two_parents(self):
        for level in range(3):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                recording_dir = root / "one" / "two" / "three"
                recording_dir.mkdir(parents=True)
                audio = recording_dir / "demo.wav"
                audio.touch()
                sidecar_dir = [recording_dir, recording_dir.parent, recording_dir.parent.parent][level]
                (sidecar_dir / vocab.FILE_NAME).write_text(json.dumps({"group": "demo-group"}),
                                                          encoding="utf-8")
                fallback = root / "groups" / "demo-group" / "vocab.json"
                with patch.object(paths, "vocab_path", return_value=fallback), \
                     patch.object(paths, "pick_group", return_value=None):
                    result = worker.resolve_vocab_details(audio, self._vocab_args(), {})
                self.assertEqual(result["group"], "demo-group")
                self.assertEqual(result["path"], sidecar_dir / vocab.FILE_NAME)

    def test_vocab_resolution_does_not_search_beyond_two_audio_parents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording_dir = root / "one" / "two" / "three"
            recording_dir.mkdir(parents=True)
            audio = recording_dir / "demo.wav"
            audio.touch()
            (root / vocab.FILE_NAME).write_text(json.dumps({"group": "outside"}), encoding="utf-8")
            with patch.object(paths, "pick_group", return_value=None):
                result = worker.resolve_vocab_details(audio, self._vocab_args(), {})
            self.assertIsNone(result["path"])
            self.assertNotIn(str((root / vocab.FILE_NAME).resolve()),
                             [item["path"] for item in result["report"]["candidates"]])

    def test_vocab_resolution_checks_known_output_sidecar_and_group_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio_dir = root / "audio"
            audio_dir.mkdir()
            audio = audio_dir / "demo.wav"
            audio.touch()
            output = audio_dir / "demo.rus-transcriber"
            output.mkdir()
            sidecar = output / vocab.FILE_NAME
            sidecar.write_text(json.dumps({"group": "alpha"}), encoding="utf-8")
            fallback = root / "groups" / "alpha" / "vocab.json"
            with patch.object(paths, "vocab_path", return_value=fallback), \
                 patch.object(paths, "pick_group", return_value=None):
                result = worker.resolve_vocab_details(audio, self._vocab_args(), {})
            self.assertEqual(result["path"], sidecar)
            self.assertIn("known_output_sidecar",
                          [item["source"] for item in result["report"]["candidates"]])

    def test_vocab_resolution_uses_unambiguous_configured_group_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            group_vocab = root / "groups" / "alpha" / "vocab.json"
            group_vocab.parent.mkdir(parents=True)
            group_vocab.write_text(json.dumps({"group": "alpha"}), encoding="utf-8")
            audio = root / "audio" / "demo.wav"
            audio.parent.mkdir()
            audio.touch()
            with patch.object(paths, "vocab_path", return_value=group_vocab), \
                 patch.object(paths, "pick_group", return_value="alpha"):
                result = worker.resolve_vocab_details(audio, self._vocab_args(), {})
            self.assertEqual(result["path"], group_vocab)
            self.assertEqual(result["group"], "alpha")
            self.assertEqual(result["report"]["selected"], str(group_vocab.resolve()))

    def test_learning_resolution_uses_transcript_binding_and_reports_stale_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "demo.wav"
            audio.touch()
            transcript_file = root / "demo.transcript.json"
            transcript_file.write_text(json.dumps({
                "file": str(audio), "group": "alpha",
                "quality": {"vocab": str(root / "removed-vocab.json")}}), encoding="utf-8")
            group_vocab = root / "groups" / "alpha" / "vocab.json"
            group_vocab.parent.mkdir(parents=True)
            group_vocab.write_text(json.dumps({"group": "alpha"}), encoding="utf-8")
            with patch.object(paths, "vocab_path", return_value=group_vocab):
                result = worker.resolve_vocab_details(
                    audio, self._vocab_args(), {"default_group": "beta"},
                    transcript_path=transcript_file)
            self.assertEqual(result["path"], group_vocab)
            self.assertEqual(result["group"], "alpha")
            stale = next(item for item in result["report"]["candidates"]
                         if item["source"] == "quality.vocab")
            self.assertEqual(stale["status"], "missing")

    def test_vocab_resolution_rejects_foreign_and_ambiguous_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio" / "demo.wav"
            audio.parent.mkdir()
            audio.touch()
            sidecar = audio.parent / vocab.FILE_NAME
            sidecar.write_text(json.dumps({"group": "beta"}), encoding="utf-8")
            with patch.object(paths, "vocab_path", return_value=root / "groups" / "alpha" / "vocab.json"):
                result = worker.resolve_vocab_details(audio, self._vocab_args(group="alpha"), {})
            self.assertEqual(result["group"], "alpha")
            self.assertIsNone(result["path"])
            self.assertNotEqual(result["path"], sidecar)
            with patch.object(paths, "vocab_path", return_value=sidecar):
                with self.assertRaises(vocab.ResolutionError):
                    worker.resolve_vocab_details(audio, self._vocab_args(group="alpha"), {})
            sidecar.write_text(json.dumps({"group": "alpha"}), encoding="utf-8")
            fallback = root / "groups" / "alpha" / "vocab.json"
            fallback.parent.mkdir(parents=True)
            fallback.write_text(json.dumps({"group": "alpha", "terms": ["different"]}), encoding="utf-8")
            with patch.object(paths, "vocab_path", return_value=fallback):
                with self.assertRaises(vocab.ResolutionError) as error:
                    worker.resolve_vocab_details(audio, self._vocab_args(), {})
            self.assertIn("несколько", str(error.exception))

    def test_transcript_group_quality_vocab_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "demo.wav"
            audio.touch()
            foreign = root / "foreign.json"
            foreign.write_text(json.dumps({"group": "beta"}), encoding="utf-8")
            source = root / "demo.transcript.json"
            source.write_text(json.dumps({"file": str(audio), "group": "alpha",
                                          "quality": {"vocab": str(foreign)}}), encoding="utf-8")
            with self.assertRaises(vocab.ResolutionError) as error:
                worker.resolve_vocab_details(audio, self._vocab_args(), {}, transcript_path=source)
            self.assertIn("противоречит", str(error.exception))

    def test_manual_group_worker_uses_only_confirmed_source_and_keeps_transcript_unchanged(self):
        class FakeEmbedder:
            def __init__(self, pipe):
                pass

            def spans(self, audio_data, spans, cap):
                return [np.array([1.0, 0.0])] * len(spans)

        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                groups, jobs = root / "groups", root / "jobs"
                group = groups / "demo-group"
                group.mkdir(parents=True)
                group_vocab = group / "vocab.json"
                group_vocab.write_text(json.dumps(
                    {"group": "demo-group", "participants": [{"name": "Анна", "gender": "ж"}]}),
                    encoding="utf-8")
                audio = root / "demo.wav"
                audio.write_bytes(b"synthetic audio")
                profile_path = group / "voices.json"
                voices.save_profile(profile_path, voices.empty_profile())
                profile_before = profile_path.read_bytes()
                source = root / "demo.transcript.json"
                data = {"file": str(audio), "names_confirmed": confirmed,
                        "quality": {"vocab": str(root / "removed-vocab.json")},
                        "segments": [{"speaker": "Анна", "words": [{"s": 0.0, "e": 3.0, "w": "привет"}]}]}
                source.write_text(json.dumps(data), encoding="utf-8")
                source_before = source.read_bytes()
                args = self._manual_update_args(source, audio, group_vocab, profile_path=profile_path)
                fake_audio = types.ModuleType("pyannote.audio")
                fake_audio.Pipeline = types.SimpleNamespace(from_pretrained=lambda *a, **k: object())
                status = Status()
                with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                     patch.dict("sys.modules", {"pyannote.audio": fake_audio}), \
                     patch.object(worker, "load_audio", return_value=np.zeros(48000)), \
                     patch.object(voices, "Embedder", FakeEmbedder):
                    worker.learn_only(args, status, {}, audio)
                self.assertEqual(status.data["state"], "done")
                self.assertEqual(source.read_bytes(), source_before)
                target = profile_path
                if confirmed:
                    self.assertEqual(status.data["learned"], [source.name])
                    self.assertTrue(target.is_file())
                    self.assertEqual(Path(status.data["backup"]).read_bytes(), profile_before)
                else:
                    self.assertEqual(status.data["learned"], [])
                    self.assertEqual(target.read_bytes(), profile_before)
                    self.assertIsNone(status.data["backup"])
                    self.assertEqual(status.data["selection_report"], {"unconfirmed_source": 1})

    def test_manual_group_worker_blocks_vocab_change_before_backup(self):
        class ChangingEmbedder:
            def __init__(self, vocab_path):
                self.vocab_path = vocab_path

            def spans(self, audio_data, spans, cap):
                self.vocab_path.write_text(json.dumps(
                    {"group": "demo-group", "participants": [{"name": "changed"}]}), encoding="utf-8")
                return [np.array([1.0, 0.0])] * len(spans)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups, jobs = root / "groups", root / "jobs"
            group = groups / "demo-group"
            group.mkdir(parents=True)
            group_vocab = group / "vocab.json"
            group_vocab.write_text(json.dumps(
                {"group": "demo-group", "participants": [{"name": "Анна"}]}), encoding="utf-8")
            audio = root / "demo.wav"
            audio.write_bytes(b"synthetic audio")
            source = root / "demo.transcript.json"
            source.write_text(json.dumps({"file": str(audio), "names_confirmed": True,
                                          "segments": [{"speaker": "Анна", "words": [
                                              {"s": 0.0, "e": 3.0, "w": "привет"}]}]}), encoding="utf-8")
            source_before = source.read_bytes()
            args = self._manual_update_args(source, audio, group_vocab)
            fake_audio = types.ModuleType("pyannote.audio")
            fake_audio.Pipeline = types.SimpleNamespace(from_pretrained=lambda *a, **k: object())
            status = Status()
            with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                 patch.dict("sys.modules", {"pyannote.audio": fake_audio}), \
                 patch.object(worker, "load_audio", return_value=np.zeros(48000)), \
                 patch.object(voices, "Embedder", return_value=ChangingEmbedder(group_vocab)):
                with self.assertRaises(RuntimeError):
                    worker.learn_only(args, status, {}, audio)
            self.assertEqual(status.data["state"], "error")
            self.assertEqual(source.read_bytes(), source_before)
            self.assertFalse((group / "voices.json").exists())
            self.assertFalse((group / "voice-backups").exists())

    def test_manual_group_worker_requires_hash_bound_update_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups, jobs = root / "groups", root / "jobs"
            group = groups / "demo-group"
            group.mkdir(parents=True)
            group_vocab = group / "vocab.json"
            group_vocab.write_text(json.dumps({"group": "demo-group", "participants": []}), encoding="utf-8")
            audio = root / "demo.wav"
            audio.write_bytes(b"synthetic audio")
            source = root / "demo.transcript.json"
            source.write_text(json.dumps({"file": str(audio), "segments": []}), encoding="utf-8")
            args = types.SimpleNamespace(learn_only=str(source), vocab="", group="",
                                         voice_group_override="demo-group")
            fake_audio = types.ModuleType("pyannote.audio")
            mock_pipeline = types.SimpleNamespace(from_pretrained=Mock())
            status = Status()
            with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                 patch.dict("sys.modules", {"pyannote.audio": fake_audio}):
                with self.assertRaisesRegex(RuntimeError, "hash-bound"):
                    worker.learn_only(args, status, {}, audio)
            mock_pipeline.from_pretrained.assert_not_called()
            self.assertEqual(status.data["state"], "error")
            self.assertFalse((group / "voices.json").exists())

    def test_bad_profile_fails_closed_without_blocking_diarization(self):
        fixtures = [json.dumps({"version": voices.PROFILE_VERSION - 1, "speakers": {}, "sources": {}}),
                    "{bad json", json.dumps({"version": voices.PROFILE_VERSION, "speakers": {}, "sources": []}),
                    json.dumps({"version": voices.PROFILE_VERSION, "speakers": {"Анна": {"parts": {
                        "demo.wav": {"sec": 2, "vec": [float('nan')]}}}}, "sources": {}}),
                    json.dumps({"version": voices.PROFILE_VERSION, "speakers": {"Анна": {"parts": {
                        "demo.wav": {"sec": 2, "vec": [0, 0]}}}}, "sources": {}})]
        for content in fixtures:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                groups, jobs = root / "groups", root / "jobs"
                group = groups / "demo-group"
                group.mkdir(parents=True)
                target = group / "voices.json"
                target.write_text(content, encoding="utf-8")
                before = target.read_bytes()
                audio = root / "demo.wav"
                audio.touch()
                quality = {}
                with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                     patch.object(voices, "learn") as learn, patch.object(voices, "save_profile") as save:
                    _, prof, learned = worker.prepare_group_voice_profile("demo-group", audio, object(), [], quality)
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(learned, [])
                self.assertFalse(prof["speakers"])
                self.assertEqual(quality["voice_auto_learn"]["reason"], "invalid_profile")
                self.assertIn("сохранение запрещено", quality["voices_error"])
                learn.assert_not_called()
                save.assert_not_called()
                # Локальные кластеры остаются доступны; без профиля имена никому не назначаются.
                turns = [{"s": 0, "e": 3, "label": "Спикер 1"}]
                cents = voices.centroids(turns, [np.array([1.0, 0.0])])
                self.assertIn("Спикер 1", cents)
                self.assertEqual(voices.match(cents, voices.voice_vectors(prof)), {})

    def test_missing_profile_can_be_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "missing.json"
            profile = voices.load_profile(target)
            self.assertEqual(profile["version"], voices.PROFILE_VERSION)
            self.assertEqual(profile["speakers"], {})
            self.assertFalse(target.exists())

    def test_auto_learning_creates_a_missing_profile(self):
        class FakeEmbedder:
            def spans(self, audio_data, spans, cap):
                return [np.array([1.0, 0.0])] * len(spans)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups, jobs = root / "groups", root / "jobs"
            audio = root / "demo.wav"
            audio.touch()
            data = {"file": str(audio), "group": "demo-group", "names_confirmed": True,
                    "segments": [{"speaker": "Анна", "words": [{"s": 0, "e": 3}]}]}
            (root / "demo.transcript.json").write_text(json.dumps(data), encoding="utf-8")
            with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                 patch.object(worker, "load_audio", return_value=np.zeros(48000)):
                target, _, learned = worker.prepare_group_voice_profile("demo-group", audio, FakeEmbedder(), [], {})
            self.assertEqual(learned, ["demo.transcript.json"])
            stored = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(stored["version"], voices.PROFILE_VERSION)
            self.assertIn("Анна", stored["speakers"])

    def test_read_error_is_not_treated_as_missing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "voices.json"
            target.write_text("{}", encoding="utf-8")
            before = target.read_bytes()
            with patch.object(Path, "read_text", side_effect=PermissionError("synthetic denial")):
                with self.assertRaises(voices.VoiceProfileError):
                    voices.load_profile(target)
            self.assertEqual(target.read_bytes(), before)

    def test_manual_update_rejects_incompatible_profile_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            groups, jobs = root / "groups", root / "jobs"
            group = groups / "demo-group"
            group.mkdir(parents=True)
            target = group / "voices.json"
            target.write_text(json.dumps({"version": voices.PROFILE_VERSION - 1, "speakers": {"demo": {}}, "sources": {}}), encoding="utf-8")
            before = target.read_bytes()
            audio = root / "demo.wav"
            audio.touch()
            source = root / "demo.transcript.json"
            source.write_text(json.dumps({"file": str(audio), "group": "demo-group", "segments": []}), encoding="utf-8")
            args = types.SimpleNamespace(learn_only=str(source), voice_source_hash=core._voice_hash(source),
                voice_profile_hash=core._voice_hash(target), voice_audio_hash=core._voice_hash(audio), voice_group="demo-group")
            fake_audio = types.ModuleType("pyannote.audio")
            fake_audio.Pipeline = types.SimpleNamespace(from_pretrained=lambda *a, **k: object())
            status = Status()
            with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                 patch.dict("sys.modules", {"pyannote.audio": fake_audio}), \
                 patch.object(worker, "resolve_vocab", return_value=(None, {}, "demo-group")), \
                 patch.object(voices, "Embedder", return_value=object()), patch.object(voices, "save_profile") as save:
                with self.assertRaises(voices.VoiceProfileError):
                    worker.learn_only(args, status, {}, audio)
            self.assertEqual(status.data["state"], "error")
            self.assertIn("сохранение запрещено", status.data["error"])
            self.assertEqual(target.read_bytes(), before)
            save.assert_not_called()

    def test_confirmed_update_and_source_conflict(self):
        for change in ("none", "source", "audio"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                groups, jobs = root / "groups", root / "jobs"
                group = groups / "demo-group"
                group.mkdir(parents=True)
                audio = root / "demo.wav"
                audio.write_bytes(b"synthetic audio")
                source = root / "demo.transcript.json"
                data = {"file": str(audio), "group": "demo-group", "names_confirmed": True,
                        "segments": [{"speaker": "Анна", "words": [{"s": 0, "e": 3}]}]}
                source.write_text(json.dumps(data), encoding="utf-8")
                key = str(audio.resolve())
                profile = {"version": voices.PROFILE_VERSION, "model": "synthetic", "sources": {},
                           "speakers": {"Анна": {"parts": {key: {"vec": [0, 1], "sec": 5}}}}}
                target = group / "voices.json"
                target.write_text(json.dumps(profile), encoding="utf-8")
                before = target.read_bytes()
                args = types.SimpleNamespace(learn_only=str(source), voice_source_hash=core._voice_hash(source),
                    voice_profile_hash=core._voice_hash(target), voice_audio_hash=core._voice_hash(audio),
                    voice_group="demo-group")

                class FakeEmbedder:
                    def __init__(self, pipe):
                        pass

                    def spans(self, audio_data, spans, cap):
                        if change == "source":
                            source.write_text(json.dumps({**data, "source_revision": 2}), encoding="utf-8")
                        elif change == "audio":
                            audio.write_bytes(b"changed synthetic audio")
                        return [np.array([1.0, 0.0])] * len(spans)

                fake_audio = types.ModuleType("pyannote.audio")
                fake_audio.Pipeline = types.SimpleNamespace(from_pretrained=lambda *a, **k: object())
                status = Status()
                with patch.object(paths, "GROUPS", groups), patch.object(paths, "JOBS", jobs), \
                     patch.dict("sys.modules", {"pyannote.audio": fake_audio}), \
                     patch.object(worker, "resolve_vocab", return_value=(None, {}, "demo-group")), \
                     patch.object(worker, "load_audio", return_value=np.zeros(48000)), \
                     patch.object(voices, "Embedder", FakeEmbedder):
                    if change != "none":
                        with self.assertRaises(RuntimeError):
                            worker.learn_only(args, status, {}, audio)
                    else:
                        worker.learn_only(args, status, {}, audio)
                if change != "none":
                    self.assertEqual(target.read_bytes(), before)
                    self.assertFalse((group / "voice-backups").exists())
                    self.assertEqual(status.data["state"], "error")
                else:
                    self.assertEqual(status.data["state"], "done")
                    self.assertEqual(Path(status.data["backup"]).read_bytes(), before)
                    after = json.loads(target.read_text(encoding="utf-8"))
                    self.assertEqual(after["speakers"]["Анна"]["parts"][key]["selection_version"],
                                     voices.SELECTION_VERSION)
