import copy
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import storage


def uid():
    return str(uuid.uuid4())


def text():
    return {"segments": [{"segment_id": "s1", "speaker": None,
                          "words": [{"word_id": "w1", "w": "пример"}]}]}


def review():
    return {"review": [{"id": "r1", "status": "open"}], "answers": {}}


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="p1a-", dir=os.environ["TEMP"])
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = storage.initialize(self.path)
        self.user = self.store.create_user("")
        self.installation = uid()
        self.store.bind_installation(self.installation, self.user)
        self.ctx = self.store.capture_context(self.installation, "human")
        self.recording = uid()
        self.op = uid()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self):
        return self.store.create_recording(self.recording, "synthetic/tag1", text(), review(),
                                           self.op, self.ctx)

    def test_replay_and_historical_name(self):
        first = self.create()
        self.store.rename_user(self.user, "Новое имя")
        self.assertEqual(first, self.create())
        old = self.store.get_revision(self.recording, "transcript", 1)
        self.assertEqual(old["context"]["author_ref"],
                         {"user_id": self.user, "user_name_snapshot": ""})
        new_ctx = self.store.capture_context(self.installation, "human")
        with self.assertRaises(storage.Conflict):
            self.store.create_recording(self.recording, "synthetic/tag1", text(), review(),
                                        self.op, new_ctx)
        self.assertEqual(self.store.list_revisions(self.recording, "transcript"), [1])

    def test_names_and_automatic_initiator(self):
        other = self.store.create_user("")
        self.assertNotEqual(self.user, other)
        model = self.store.capture_context(self.installation, "model")
        self.assertIsNone(model["author_ref"]["user_id"])
        self.assertEqual(model["initiated_by"]["user_id"], self.user)
        self.store.bind_installation(self.installation, other)
        self.assertEqual(model["initiated_by"]["user_id"], self.user)
        self.assertEqual(self.store.capture_context(self.installation, "human")["author_ref"]["user_id"], other)
        forged = copy.deepcopy(model)
        forged["author_ref"] = model["initiated_by"]
        with self.assertRaises(ValueError):
            self.store.create_recording(self.recording, "tag", text(), review(), self.op, forged)

    def test_identical_nonempty_names_are_not_identity(self):
        first = self.store.create_user("тёзка")
        second = self.store.create_user("тёзка")
        self.assertNotEqual(first, second)
        self.store.bind_installation(self.installation, first)
        first_ref = self.store.capture_context(self.installation, "human")["author_ref"]
        self.store.bind_installation(self.installation, second)
        second_ref = self.store.capture_context(self.installation, "human")["author_ref"]
        self.assertEqual(first_ref["user_name_snapshot"], second_ref["user_name_snapshot"])
        self.assertNotEqual(first_ref["user_id"], second_ref["user_id"])

    def test_answers_and_atomic_apply_preserve_authors(self):
        initial = self.create()
        versions = {k: initial[k] for k in ("source_revision", "review_revision")}
        r = review()
        r["answers"]["r1"] = {"text": "да", "author_ref": self.ctx["author_ref"]}
        second = self.store.commit_change(self.recording, versions, uid(), self.ctx, review=r)
        self.assertEqual((second["source_revision"], second["review_revision"]), (1, 2))
        other = self.store.create_user("Другой")
        self.store.bind_installation(self.installation, other)
        applier = self.store.capture_context(self.installation, "human")
        t = text()
        t["segments"][0]["words"][0]["w"] = "исправлено"
        r["review"][0]["status"] = "resolved"
        result = self.store.commit_change(
            self.recording, {"source_revision": 1, "review_revision": 2}, uid(), applier,
            transcript=t, review=r)
        self.assertEqual((result["source_revision"], result["review_revision"]), (2, 3))
        saved = self.store.get_revision(self.recording, "review", 3)
        self.assertEqual(saved["source_revision"], 2)
        self.assertEqual(saved["payload"]["answers"]["r1"]["author_ref"]["user_id"], self.user)
        self.assertEqual(saved["context"]["author_ref"]["user_id"], other)
        self.assertEqual(self.store.get_revision(self.recording, "transcript", 1)["payload"], text())

    def test_stale_and_reused_operation_have_no_effect(self):
        self.create()
        versions = {"source_revision": 1, "review_revision": 1}
        op = uid()
        saved = self.store.commit_change(self.recording, versions, op, self.ctx, review=review())
        self.assertEqual(saved, self.store.commit_change(self.recording, versions, op, self.ctx, review=review()))
        changed = review()
        changed["review_done"] = True
        with self.assertRaises(storage.Conflict):
            self.store.commit_change(self.recording, versions, op, self.ctx, review=changed)
        with self.assertRaises(storage.Conflict):
            self.store.commit_change(self.recording, versions, uid(), self.ctx, review=changed)
        self.assertEqual(self.store.list_revisions(self.recording, "review"), [1, 2])

    def test_invalid_snapshots_and_source_requires_review(self):
        self.create()
        versions = {"source_revision": 1, "review_revision": 1}
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, versions, uid(), self.ctx, transcript=text())
        invalid = text()
        invalid["segments"][0]["words"].append(invalid["segments"][0]["words"][0])
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, versions, uid(), self.ctx,
                                     transcript=invalid, review=review())
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, versions, uid(), self.ctx, review={"segments": []})
        self.assertEqual(self.store.list_revisions(self.recording, "transcript"), [1])

    def test_existing_database_and_unknown_schema(self):
        self.store.close()
        with self.assertRaises(FileExistsError):
            storage.initialize(self.path)
        db = sqlite3.connect(self.path)
        db.execute("UPDATE store_meta SET schema_version=999")
        db.commit()
        db.close()
        with self.assertRaises(storage.UnsupportedSchema):
            storage.open_store(self.path)
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("SELECT schema_version FROM store_meta").fetchone()[0], 999)
        db.close()
        self.store = storage.initialize(Path(self.temp.name) / "other.sqlite3")

    def test_corruption_detected_and_no_create_on_open(self):
        self.create()
        self.store.db.execute("UPDATE transcript_revisions SET payload='{}' WHERE recording_id=?",
                              (self.recording,))
        with self.assertRaises(storage.StorageError):
            self.store.get_revision(self.recording, "transcript", 1)
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(storage.StorageError):
            storage.open_store(missing)
        self.assertFalse(missing.exists())

    def test_invalid_stored_json_is_a_storage_error(self):
        self.create()
        self.store.db.execute("UPDATE transcript_revisions SET payload='{bad json' WHERE recording_id=?",
                              (self.recording,))
        with self.assertRaises(storage.StorageError):
            self.store.get_revision(self.recording, "transcript", 1)
        with self.assertRaises(storage.StorageError):
            self.store.get_current(self.recording)
        with self.assertRaises(storage.StorageError):
            storage.validate_copy(self.path)

    def test_failure_rolls_back_both_revisions_and_operation(self):
        self.create()
        self.store.db.execute("""CREATE TRIGGER inject_failure BEFORE INSERT ON review_revisions
                                 BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        op = uid()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_change(self.recording, {"source_revision": 1, "review_revision": 1},
                                     op, self.ctx, transcript=text(), review=review())
        self.assertEqual(self.store.list_revisions(self.recording, "transcript"), [1])
        self.assertEqual(self.store.list_revisions(self.recording, "review"), [1])
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM operations WHERE operation_id=?", (op,)).fetchone())
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_recording_heads_are_foreign_keys(self):
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store._transaction():
                self.store.db.execute("UPDATE recordings SET source_revision=999 WHERE recording_id=?",
                                      (self.recording,))
        self.assertEqual(self.store.db.execute(
            "SELECT source_revision FROM recordings WHERE recording_id=?", (self.recording,)).fetchone()[0], 1)

    def test_snapshot_author_references_must_exist(self):
        self.create()
        r = review()
        r["answers"]["r1"] = {"author_ref": {"user_id": uid(), "user_name_snapshot": "ник"}}
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, {"source_revision": 1, "review_revision": 1},
                                     uid(), self.ctx, review=r)
        self.assertEqual(self.store.list_revisions(self.recording, "review"), [1])

    def test_replay_after_reopen(self):
        self.create()
        self.store.close()
        self.store = storage.open_store(self.path)
        self.store.rename_user(self.user, "Изменён")
        self.assertEqual(self.create()["source_revision"], 1)
        self.assertEqual(self.store.get_revision(self.recording, "transcript", 1)
                         ["context"]["author_ref"]["user_name_snapshot"], "")

    def test_read_consistent_current_pair(self):
        self.create()
        current = self.store.get_current(self.recording)
        self.assertEqual(current["transcript"]["revision"], 1)
        self.assertEqual(current["review"]["source_revision"], 1)
        self.assertEqual(current["transcript"]["payload"], text())

    def test_rejects_unknown_question_word_and_stale_answer_version(self):
        self.create()
        versions = {"source_revision": 1, "review_revision": 1}
        bad = {"review": [{"id": "r1"}], "questions": [{"id": "r1", "word_ids": ["missing"]}]}
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, versions, uid(), self.ctx, review=bad)
        bad = {"review": [{"id": "r1"}], "question_version": "v2",
               "answers": {"r1": {"id": "r1", "version": "v1"}}}
        with self.assertRaises(ValueError):
            self.store.commit_change(self.recording, versions, uid(), self.ctx, review=bad)
        self.assertEqual(self.store.list_revisions(self.recording, "review"), [1])


if __name__ == "__main__":
    unittest.main()
