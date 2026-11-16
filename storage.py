"""Изолированный прототип хранения П1а. Не подключён к рабочему приложению."""

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
TRANSCRIPT_FIELDS = frozenset(("file", "duration", "speakers", "speaker_names",
                               "names_source", "names_confirmed", "segments",
                               "quality", "edits", "title", "recorded_at", "recorded_timezone",
                               "container", "codec", "audio_sha256", "audio_available"))
REVIEW_FIELDS = frozenset(("review", "review_done", "questions", "answers",
                           "conflicts", "rejected", "applications", "undo",
                           "interaction_events", "interaction_events_evicted", "question_version"))
ACTOR_KINDS = frozenset(("human", "model", "system", "unknown"))
KEEP_ANNOTATION = object()
MAX_INTERACTION_EVENTS = 2000


class StorageError(Exception):
    pass


class Conflict(StorageError):
    pass


class UnsupportedSchema(StorageError):
    pass


class Busy(Conflict):
    pass


def _is_busy(exc):
    code = getattr(exc, "sqlite_errorcode", None)
    return ((isinstance(code, int) and (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
            or "locked" in str(exc).lower() or "busy" in str(exc).lower())


def check_local_path(path):
    """Не принимать UNC и диски Windows, не являющиеся локальными fixed-дисками."""
    try:
        path = Path(path).absolute()
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("invalid path") from exc
    if os.name == "nt":
        import ctypes
        anchor = resolved.anchor
        if anchor.startswith("\\\\") or not anchor:
            raise ValueError("network paths are not supported")
        if ctypes.windll.kernel32.GetDriveTypeW(str(anchor)) != 3:
            raise ValueError("only local fixed drives are supported")
    return path


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError("expected UUID string")
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid UUID") from exc
    if value != canonical:
        raise ValueError("UUID must be canonical")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _loads(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise StorageError("invalid stored JSON") from exc


def _hash_key(kind):
    """Ключ хэша документа для вида цели реестра файлов."""
    return kind if kind == "archive_manifest" else kind.removeprefix("archive_")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ref(ref):
    if not isinstance(ref, dict) or set(ref) != {"user_id", "user_name_snapshot"}:
        raise ValueError("invalid author reference")
    ident, name = ref["user_id"], ref["user_name_snapshot"]
    if ident is None:
        if name is not None:
            raise ValueError("unknown author must have null name")
    else:
        _uuid(ident)
        if not isinstance(name, str):
            raise ValueError("known author needs a name snapshot")


def _context(ctx):
    if not isinstance(ctx, dict) or set(ctx) != {
            "actor_kind", "author_ref", "initiated_by", "origin_installation_id"}:
        raise ValueError("invalid actor context")
    if ctx["actor_kind"] not in ACTOR_KINDS:
        raise ValueError("invalid actor kind")
    _ref(ctx["author_ref"])
    _ref(ctx["initiated_by"])
    if ctx["actor_kind"] != "human" and ctx["author_ref"]["user_id"] is not None:
        raise ValueError("automatic action cannot have a human author")
    if ctx["origin_installation_id"] is not None:
        _uuid(ctx["origin_installation_id"])


def _snapshot(value, fields):
    if not isinstance(value, dict) or not set(value) <= fields:
        raise ValueError("unknown snapshot fields")
    _json(value)
    if fields is TRANSCRIPT_FIELDS:
        segments = value.get("segments", [])
        if not isinstance(segments, list):
            raise ValueError("segments must be a list")
        segment_ids, word_ids = set(), set()
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str):
                raise ValueError("segment_id required")
            sid = segment["segment_id"]
            if not sid or sid in segment_ids:
                raise ValueError("duplicate or empty segment_id")
            segment_ids.add(sid)
            if "speaker" in segment and segment["speaker"] is not None and not isinstance(segment["speaker"], str):
                raise ValueError("speaker must be text or null")
            for start_key, end_key in (("start", "end"),):
                _validate_times(segment, start_key, end_key)
            words = segment.get("words", [])
            if not isinstance(words, list):
                raise ValueError("words must be a list")
            for word in words:
                if not isinstance(word, dict) or not isinstance(word.get("word_id"), str):
                    raise ValueError("word_id required")
                wid = word["word_id"]
                if not wid or wid in word_ids:
                    raise ValueError("duplicate or empty word_id")
                word_ids.add(wid)
                if "w" in word and not isinstance(word["w"], str):
                    raise ValueError("word text must be text")
                _validate_times(word, "s", "e")
    else:
        items = value.get("review", [])
        if not isinstance(items, list):
            raise ValueError("review must be a list")
        ids = set()
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("review id required")
            if item["id"] in ids:
                raise ValueError("duplicate review id")
            ids.add(item["id"])
        events = value.get("interaction_events", [])
        evicted = value.get("interaction_events_evicted", 0)
        if not isinstance(events, list) or len(events) > MAX_INTERACTION_EVENTS:
            raise ValueError("too many interaction events")
        if type(evicted) is not int or evicted < 0:
            raise ValueError("invalid evicted count")


def _validate_times(value, start_key, end_key):
    for key in (start_key, end_key):
        if key in value and (type(value[key]) not in (int, float) or
                             not math.isfinite(value[key]) or value[key] < 0):
            raise ValueError("invalid time")
    if start_key in value and end_key in value and value[start_key] > value[end_key]:
        raise ValueError("time interval reversed")


def _validate_embedded_provenance(value):
    if isinstance(value, dict):
        if "actor_kind" in value:
            kind = value["actor_kind"]
            if kind not in ACTOR_KINDS:
                raise ValueError("invalid actor kind")
            author = value.get("author_ref")
            if author is not None:
                _ref(author)
                if kind != "human" and author["user_id"] is not None:
                    raise ValueError("automatic action cannot have a human author")
        for key, item in value.items():
            if key in ("author_ref", "initiated_by"):
                _ref(item)
            else:
                _validate_embedded_provenance(item)
    elif isinstance(value, list):
        for item in value:
            _validate_embedded_provenance(item)


def validate_pair(transcript, review):
    """Проверить поддерживаемые явные ссылки одного кортежа снимков."""
    _snapshot(transcript, TRANSCRIPT_FIELDS)
    _snapshot(review, REVIEW_FIELDS)
    word_ids = {word["word_id"] for segment in transcript.get("segments", [])
                for word in segment.get("words", [])}
    review_ids = {item["id"] for item in review.get("review", [])}
    answers = review.get("answers", {})
    if not isinstance(answers, dict) or not set(answers) <= review_ids:
        raise ValueError("answers must reference existing review items")
    if any(not isinstance(answer, dict) for answer in answers.values()):
        raise ValueError("answers must be objects")
    for item in review.get("review", []):
        if "word_ids" in item:
            refs = item["word_ids"]
            if (not isinstance(refs, list) or any(not isinstance(x, str) for x in refs)
                    or len(set(refs)) != len(refs) or not set(refs) <= word_ids):
                raise ValueError("review references unknown or duplicate words")
        _validate_times(item, "t1", "t2")
    questions = review.get("questions", [])
    if not isinstance(questions, list):
        raise ValueError("questions must be a list")
    question_ids = set()
    for question in questions:
        if not isinstance(question, dict) or question.get("id") not in review_ids:
            raise ValueError("question references missing review item")
        if question["id"] in question_ids:
            raise ValueError("duplicate question")
        question_ids.add(question["id"])
        refs = question.get("word_ids", [])
        if (not isinstance(refs, list) or any(not isinstance(x, str) for x in refs) or
                len(set(refs)) != len(refs) or not set(refs) <= word_ids):
            raise ValueError("question references unknown or duplicate words")
    version = review.get("question_version")
    if version is not None and not isinstance(version, str):
        raise ValueError("invalid question version")
    for ident, answer in answers.items():
        if "id" in answer and answer["id"] != ident:
            raise ValueError("answer ID mismatch")
        if "version" in answer and (version is None or answer["version"] != version):
            raise ValueError("answer question version mismatch")
    applications = review.get("applications", [])
    if not isinstance(applications, list):
        raise ValueError("applications must be a list")
    for action in applications:
        if not isinstance(action, dict):
            raise ValueError("invalid application")
        if "answer_id" in action and action["answer_id"] not in answers:
            raise ValueError("application references missing answer")
        if "context" in action:
            _context(action["context"])
    _validate_embedded_provenance(transcript)
    _validate_embedded_provenance(review)


def _connect(path, mode):
    uri = check_local_path(path).resolve().as_uri() + "?mode=" + mode
    try:
        db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    except sqlite3.OperationalError as exc:
        if _is_busy(exc):
            raise Busy("database is busy") from exc
        raise StorageError("cannot open database") from exc
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError as exc:
        db.close()
        if _is_busy(exc):
            raise Busy("database is busy") from exc
        raise UnsupportedSchema("invalid or unsupported database") from exc
    return db


def _initialize_at(path):
    """Явно создать новую пустую БД; существующий файл не заменяется."""
    path = Path(path)
    with path.open("xb"):
        pass
    db = _connect(path, "rw")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE store_meta (store_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL);
        CREATE TABLE local_users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE installations (installation_id TEXT PRIMARY KEY,
            current_user_id TEXT NOT NULL REFERENCES local_users(user_id),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
        CREATE TABLE recordings (recording_id TEXT PRIMARY KEY, location TEXT NOT NULL,
            source_revision INTEGER NOT NULL, review_revision INTEGER NOT NULL,
            FOREIGN KEY(recording_id, source_revision)
                REFERENCES transcript_revisions(recording_id, revision) DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY(recording_id, review_revision)
                REFERENCES review_revisions(recording_id, revision) DEFERRABLE INITIALLY DEFERRED);
        CREATE TABLE operations (operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            parameters_hash TEXT NOT NULL, context_json TEXT NOT NULL, result_json TEXT NOT NULL,
            author_user_id TEXT REFERENCES local_users(user_id),
            initiator_user_id TEXT REFERENCES local_users(user_id),
            installation_id TEXT REFERENCES installations(installation_id), created_at TEXT NOT NULL);
        CREATE TABLE transcript_revisions (recording_id TEXT NOT NULL REFERENCES recordings(recording_id),
            revision INTEGER NOT NULL, parent_revision INTEGER, payload TEXT NOT NULL,
            payload_hash TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            restored_from INTEGER,
            PRIMARY KEY(recording_id, revision),
            FOREIGN KEY(recording_id, parent_revision) REFERENCES transcript_revisions(recording_id, revision),
            FOREIGN KEY(recording_id, restored_from) REFERENCES transcript_revisions(recording_id, revision));
        CREATE TABLE review_revisions (recording_id TEXT NOT NULL REFERENCES recordings(recording_id),
            revision INTEGER NOT NULL, parent_revision INTEGER, source_revision INTEGER NOT NULL,
            payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
            operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            restored_from INTEGER,
            PRIMARY KEY(recording_id, revision),
            FOREIGN KEY(recording_id, parent_revision) REFERENCES review_revisions(recording_id, revision),
            FOREIGN KEY(recording_id, source_revision) REFERENCES transcript_revisions(recording_id, revision),
            FOREIGN KEY(recording_id, restored_from) REFERENCES review_revisions(recording_id, revision));
        CREATE TABLE document_versions (
            recording_id TEXT NOT NULL REFERENCES recordings(recording_id),
            document_revision INTEGER NOT NULL,
            source_revision INTEGER NOT NULL,
            review_revision INTEGER NOT NULL,
            md BLOB NOT NULL, json BLOB NOT NULL, manifest BLOB NOT NULL, archive_manifest BLOB NOT NULL,
            hashes TEXT NOT NULL, operation_id TEXT NOT NULL REFERENCES operations(operation_id),
            PRIMARY KEY(recording_id, document_revision),
            FOREIGN KEY(recording_id, source_revision) REFERENCES transcript_revisions(recording_id, revision),
            FOREIGN KEY(recording_id, review_revision) REFERENCES review_revisions(recording_id, revision));
        CREATE TABLE allowed_roots (
            root_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            requires_remap INTEGER NOT NULL DEFAULT 0 CHECK(requires_remap IN (0,1)),
            role TEXT NOT NULL DEFAULT 'documents');
        CREATE TABLE restore_guard (id INTEGER PRIMARY KEY CHECK(id=1),
            locked INTEGER NOT NULL CHECK(locked IN (0,1)));
        INSERT INTO restore_guard VALUES (1, 0);
        CREATE TABLE document_outbox (
            recording_id TEXT NOT NULL, document_revision INTEGER NOT NULL,
            root_id TEXT NOT NULL REFERENCES allowed_roots(root_id),
            state TEXT NOT NULL CHECK(state IN ('pending', 'writing', 'synced', 'conflict', 'error')),
            generation INTEGER NOT NULL, expected_previous_hashes TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
            PRIMARY KEY(recording_id, document_revision),
            FOREIGN KEY(recording_id, document_revision)
                REFERENCES document_versions(recording_id, document_revision));
        CREATE TABLE file_locations (
            root_id TEXT NOT NULL REFERENCES allowed_roots(root_id), relative_path TEXT NOT NULL,
            canonical_path TEXT NOT NULL UNIQUE,
            recording_id TEXT NOT NULL REFERENCES recordings(recording_id), kind TEXT NOT NULL,
            confirmed_revision INTEGER, confirmed_hash TEXT, generation INTEGER NOT NULL DEFAULT 0,
            conflict INTEGER NOT NULL DEFAULT 0 CHECK(conflict IN (0,1)),
            PRIMARY KEY(root_id, relative_path));
        CREATE TABLE document_conflicts (
            conflict_id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, document_revision INTEGER NOT NULL,
            kind TEXT NOT NULL, relative_path TEXT NOT NULL, observed_bytes BLOB,
            observed_hash TEXT NOT NULL, observed_size INTEGER NOT NULL,
            resolution_operation_id TEXT REFERENCES operations(operation_id),
            FOREIGN KEY(recording_id, document_revision)
                REFERENCES document_outbox(recording_id, document_revision));
        CREATE TABLE materialization_events (
            event_id TEXT PRIMARY KEY, recording_id TEXT NOT NULL, document_revision INTEGER NOT NULL,
            kind TEXT NOT NULL, event_type TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(recording_id, document_revision)
                REFERENCES document_outbox(recording_id, document_revision));
        """)
        db.execute("INSERT INTO store_meta VALUES (?, ?)", (str(uuid.uuid4()), SCHEMA_VERSION))
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    return open_store(path)


def initialize(path):
    """Подготовить новую БД отдельно и опубликовать без замены существующего файла."""
    path = check_local_path(path)
    if path.exists():
        raise FileExistsError(path)
    staged = path.with_name(path.name + ".incomplete-" + uuid.uuid4().hex)
    created = _initialize_at(staged)
    created.close()
    validate_copy(staged)
    os.link(staged, path)
    staged.unlink()
    return open_store(path)


def _check_schema(db):
    required = {"store_meta", "local_users", "installations", "recordings", "operations",
                "transcript_revisions", "review_revisions", "document_versions", "allowed_roots",
                "restore_guard", "document_outbox", "document_conflicts", "file_locations",
                "materialization_events"}
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= names:
            raise UnsupportedSchema("required tables missing")
        columns = {
            "store_meta": {"store_id", "schema_version"},
            "recordings": {"recording_id", "source_revision", "review_revision"},
            "operations": {"operation_id", "context_json", "parameters_hash", "result_json"},
            "transcript_revisions": {"recording_id", "revision", "payload", "payload_hash"},
            "review_revisions": {"recording_id", "revision", "source_revision", "payload", "payload_hash"},
            "document_versions": {"recording_id", "document_revision", "source_revision", "review_revision",
                                  "md", "json", "manifest", "archive_manifest", "hashes"},
            "document_outbox": {"recording_id", "document_revision", "root_id", "state",
                                "generation", "expected_previous_hashes"},
            "file_locations": {"root_id", "relative_path", "canonical_path", "confirmed_revision",
                               "confirmed_hash", "generation", "conflict"},
        }
        for table, expected in columns.items():
            if not expected <= {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                raise UnsupportedSchema("required columns missing")
        rows = db.execute("SELECT schema_version FROM store_meta").fetchall()
        if len(rows) != 1 or rows[0][0] != SCHEMA_VERSION:
            raise UnsupportedSchema("unsupported schema")
    except sqlite3.DatabaseError as exc:
        if _is_busy(exc):
            raise Busy("database is busy") from exc
        raise UnsupportedSchema("invalid or unsupported database") from exc


def open_store(path):
    """Открыть существующую совместимую БД, не создавать и не мигрировать."""
    db = _connect(path, "rw")
    try:
        _check_schema(db)
        db.execute("PRAGMA synchronous=FULL")
        return Store(db)
    except BaseException:
        db.close()
        raise


def open_store_readonly(path):
    """Открыть существующую совместимую БД только для чтения."""
    db = _connect(path, "ro")
    try:
        _check_schema(db)
        db.execute("PRAGMA query_only=ON")
        return Store(db)
    except BaseException:
        db.close()
        raise


def validate_copy(path):
    """Проверка копии без записи и запуска восстановленных очередей."""
    db = _connect(path, "ro")
    try:
        _check_schema(db)
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StorageError("integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StorageError("foreign key check failed")
        checker = Store(db)
        for table in ("transcript_revisions", "review_revisions"):
            for row in db.execute(f"SELECT payload, payload_hash FROM {table}"):
                payload = _loads(row["payload"])
                if _hash(payload) != row["payload_hash"]:
                    raise StorageError("snapshot hash mismatch")
                checker._check_snapshot_refs(payload)
        for row in db.execute("SELECT * FROM operations"):
            context = _loads(row["context_json"])
            _context(context)
            if (context["author_ref"]["user_id"] != row["author_user_id"] or
                    context["initiated_by"]["user_id"] != row["initiator_user_id"] or
                    context["origin_installation_id"] != row["installation_id"]):
                raise StorageError("operation author reference mismatch")
        for row in db.execute("SELECT r.payload AS review, t.payload AS transcript FROM review_revisions r "
                              "JOIN transcript_revisions t ON t.recording_id=r.recording_id "
                              "AND t.revision=r.source_revision"):
            validate_pair(_loads(row["transcript"]), _loads(row["review"]))
        for row in db.execute("SELECT r.source_revision, v.source_revision AS reviewed "
                              "FROM recordings r JOIN review_revisions v ON v.recording_id=r.recording_id "
                              "AND v.revision=r.review_revision"):
            if row["source_revision"] != row["reviewed"]:
                raise StorageError("incompatible current revisions")
        for row in db.execute("SELECT * FROM document_versions"):
            reviewed = db.execute("SELECT source_revision, payload FROM review_revisions "
                                  "WHERE recording_id=? AND revision=?",
                                  (row["recording_id"], row["review_revision"])).fetchone()
            source = db.execute("SELECT payload FROM transcript_revisions WHERE recording_id=? AND revision=?",
                                (row["recording_id"], row["source_revision"])).fetchone()
            if reviewed is None or source is None or reviewed["source_revision"] != row["source_revision"]:
                raise StorageError("document has incompatible snapshots")
            validate_pair(_loads(source["payload"]), _loads(reviewed["payload"]))
            hashes = _loads(row["hashes"])
            for key in ("md", "json", "manifest", "archive_manifest"):
                if hashlib.sha256(row[key]).hexdigest() != hashes[key]:
                    raise StorageError("document hash mismatch")
            passport = _loads(row["json"])["passport"]
            for key in ("recording_id", "document_revision", "source_revision", "review_revision"):
                if passport[key] != row[key]:
                    raise StorageError("document passport mismatch")
            manifest = _loads(row["manifest"])
            archive_manifest = _loads(row["archive_manifest"])
            if manifest["passport"] != passport or archive_manifest["passport"] != passport:
                raise StorageError("manifest passport mismatch")
            expected = {hashes["md"], hashes["json"]}
            if ({item["sha256"] for item in manifest["files"].values()} != expected or
                    {item["sha256"] for item in archive_manifest["files"].values()} != expected):
                raise StorageError("manifest hash mismatch")
        for row in db.execute("SELECT observed_bytes, observed_hash, observed_size FROM document_conflicts"):
            if row["observed_bytes"] is not None and (
                    len(row["observed_bytes"]) != row["observed_size"] or
                    hashlib.sha256(row["observed_bytes"]).hexdigest() != row["observed_hash"]):
                raise StorageError("conflict copy hash mismatch")
        for row in db.execute("SELECT * FROM document_outbox"):
            if row["generation"] != row["document_revision"]:
                raise StorageError("outbox generation mismatch")
            previous = {}
            if row["document_revision"] > 1:
                previous_row = db.execute("SELECT hashes FROM document_versions WHERE recording_id=? "
                                          "AND document_revision=?",
                                          (row["recording_id"], row["document_revision"] - 1)).fetchone()
                if previous_row is None:
                    raise StorageError("missing prior document")
                previous = _loads(previous_row["hashes"])
            expected = {kind: previous.get(kind) for kind in ("md", "json", "manifest")}
            if _loads(row["expected_previous_hashes"]) != expected:
                raise StorageError("outbox expected hash mismatch")
        for row in db.execute("SELECT f.*, r.path AS root_path FROM file_locations f "
                              "JOIN allowed_roots r ON r.root_id=f.root_id"):
            canonical = os.path.normcase(os.path.abspath(os.path.join(row["root_path"], row["relative_path"])))
            if canonical != row["canonical_path"]:
                raise StorageError("file target mismatch")
            if row["confirmed_revision"] is not None:
                doc = db.execute("SELECT hashes FROM document_versions WHERE recording_id=? AND document_revision=?",
                                 (row["recording_id"], row["confirmed_revision"])).fetchone()
                key = _hash_key(row["kind"])
                if doc is None or _loads(doc["hashes"]).get(key) != row["confirmed_hash"]:
                    raise StorageError("confirmed file hash mismatch")
        return {"integrity": "ok", "foreign_keys": "ok", "schema_version": SCHEMA_VERSION,
                "inactive": bool(db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0])}
    except StorageError:
        raise
    except sqlite3.DatabaseError as exc:
        if _is_busy(exc):
            raise Busy("database is busy") from exc
        raise StorageError("invalid or unreadable database") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise StorageError("invalid or unreadable database") from exc
    finally:
        db.close()


class Store:
    def __init__(self, db):
        self.db = db

    def close(self):
        self.db.close()

    def backup(self, destination):
        """SQLite backup в новый файл; восстановленная копия неактивна."""
        if self.db.in_transaction:
            raise Conflict("cannot back up an open transaction")
        destination = check_local_path(destination)
        if destination.exists():
            raise FileExistsError(destination)
        staged = destination.with_name(destination.name + ".incomplete-" + uuid.uuid4().hex)
        with staged.open("xb"):
            pass
        target = _connect(staged, "rw")
        try:
            self.db.backup(target)
            target.execute("BEGIN IMMEDIATE")
            target.execute("UPDATE restore_guard SET locked=1 WHERE id=1")
            target.execute("UPDATE allowed_roots SET requires_remap=1")
            target.execute("UPDATE installations SET active=0")
            target.execute("COMMIT")
        except BaseException:
            if target.in_transaction:
                target.execute("ROLLBACK")
            raise
        finally:
            target.close()
        report = validate_copy(staged)
        os.link(staged, destination)
        staged.unlink()
        return report

    def rebind_restored_copy(self, root_mapping, installation_id, user_id):
        """Явно переназначить все корни и новую установку в подготовленной копии."""
        import documents
        _uuid(installation_id)
        _uuid(user_id)
        if not isinstance(root_mapping, dict):
            raise ValueError("complete root mapping required")
        existing = self.db.execute("SELECT root_id, path FROM allowed_roots").fetchall()
        if set(root_mapping) != {row["root_id"] for row in existing}:
            raise ValueError("all roots must be remapped")
        paths = {key: os.path.normcase(str(documents.checked_root(value)))
                 for key, value in root_mapping.items()}
        def overlaps(a, b):
            try:
                shared = os.path.commonpath((a, b))
            except ValueError:
                return False
            return shared == a or shared == b
        values = list(paths.values())
        if any(overlaps(a, b) for i, a in enumerate(values) for b in values[i + 1:]):
            raise ValueError("restored roots overlap")
        old_roots = [row["path"] for row in existing]
        for new in paths.values():
            for old in old_roots:
                if overlaps(new, old):
                    raise ValueError("old and new roots must not overlap")
        with self._transaction():
            if not self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
                raise Conflict("store is not an inactive restore")
            if self.db.execute("SELECT 1 FROM installations WHERE installation_id=?",
                               (installation_id,)).fetchone():
                raise Conflict("new installation ID required")
            if not self.db.execute("SELECT 1 FROM local_users WHERE user_id=?", (user_id,)).fetchone():
                raise ValueError("unknown user")
            for root_id, path in paths.items():
                self.db.execute("UPDATE allowed_roots SET path=?, requires_remap=0 WHERE root_id=?",
                                (path, root_id))
                for location in self.db.execute("SELECT relative_path FROM file_locations WHERE root_id=?",
                                                (root_id,)).fetchall():
                    canonical = os.path.normcase(os.path.abspath(os.path.join(path, location["relative_path"])))
                    self.db.execute("UPDATE file_locations SET canonical_path=?, confirmed_revision=NULL, "
                                    "confirmed_hash=NULL, generation=0 WHERE root_id=? AND relative_path=?",
                                    (canonical, root_id, location["relative_path"]))
            self.db.execute("UPDATE document_outbox SET state='pending', error_code=NULL WHERE state!='conflict'")
            self.db.execute("INSERT INTO installations VALUES (?, ?, 1)", (installation_id, user_id))
            self.db.execute("UPDATE restore_guard SET locked=0 WHERE id=1")

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            name = "sp_" + uuid.uuid4().hex
            self.db.execute("SAVEPOINT " + name)
            try:
                yield
                self.db.execute("RELEASE SAVEPOINT " + name)
            except BaseException:
                self.db.execute("ROLLBACK TO SAVEPOINT " + name)
                self.db.execute("RELEASE SAVEPOINT " + name)
                raise
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise Busy("database is busy") from exc
            raise StorageError("cannot begin transaction") from exc
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create_user(self, display_name):
        if not isinstance(display_name, str):
            raise ValueError("name must be text")
        ident = str(uuid.uuid4())
        with self._transaction():
            if self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
                raise Conflict("restored copy is inactive")
            now = _now()
            self.db.execute("INSERT INTO local_users VALUES (?, ?, ?, ?)", (ident, display_name, now, now))
        return ident

    def rename_user(self, user_id, display_name):
        _uuid(user_id)
        if not isinstance(display_name, str):
            raise ValueError("name must be text")
        with self._transaction():
            if self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
                raise Conflict("restored copy is inactive")
            cur = self.db.execute("UPDATE local_users SET display_name=?, updated_at=? WHERE user_id=?",
                                  (display_name, _now(), user_id))
            if cur.rowcount != 1:
                raise KeyError(user_id)

    def bind_installation(self, installation_id, user_id):
        _uuid(installation_id)
        _uuid(user_id)
        with self._transaction():
            if self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
                raise Conflict("restore copy requires explicit rebind")
            if not self.db.execute("SELECT 1 FROM local_users WHERE user_id=?", (user_id,)).fetchone():
                raise ValueError("unknown user")
            self.db.execute("INSERT INTO installations VALUES (?, ?, 1) "
                            "ON CONFLICT(installation_id) DO UPDATE SET current_user_id=excluded.current_user_id, active=1",
                            (installation_id, user_id))

    def register_root(self, path):
        """Явно зарегистрировать существующий корень; без создания каталогов."""
        import documents
        root = documents.checked_root(path)
        normalized = os.path.normcase(str(root))
        root_id = str(uuid.uuid4())
        with self._transaction():
            if self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
                raise Conflict("restore copy requires explicit rebind")
            if self.db.execute("SELECT 1 FROM allowed_roots WHERE path=?", (normalized,)).fetchone():
                raise Conflict("root already registered")
            self.db.execute("INSERT INTO allowed_roots(root_id, path) VALUES (?, ?)",
                            (root_id, normalized))
        return root_id

    def capture_context(self, installation_id, actor_kind):
        _uuid(installation_id)
        if actor_kind not in ACTOR_KINDS:
            raise ValueError("invalid actor kind")
        row = self.db.execute("SELECT u.user_id, u.display_name FROM installations i "
                              "JOIN local_users u ON u.user_id=i.current_user_id "
                              "WHERE i.installation_id=? AND i.active=1", (installation_id,)).fetchone()
        if row is None:
            raise KeyError(installation_id)
        known = {"user_id": row["user_id"], "user_name_snapshot": row["display_name"]}
        unknown = {"user_id": None, "user_name_snapshot": None}
        return {"actor_kind": actor_kind, "author_ref": known if actor_kind == "human" else unknown,
                "initiated_by": unknown if actor_kind == "human" else known,
                "origin_installation_id": installation_id}

    def _check_context_refs(self, ctx, *, require_active=True):
        if self.db.execute("SELECT locked FROM restore_guard WHERE id=1").fetchone()[0]:
            raise Conflict("restored copy is inactive")
        for field in ("author_ref", "initiated_by"):
            ident = ctx[field]["user_id"]
            if ident is not None and not self.db.execute(
                    "SELECT 1 FROM local_users WHERE user_id=?", (ident,)).fetchone():
                raise ValueError("unknown user reference")
        ident = ctx["origin_installation_id"]
        if ident is not None:
            query = "SELECT 1 FROM installations WHERE installation_id=?"
            if require_active:
                query += " AND active=1"
            if not self.db.execute(query, (ident,)).fetchone():
                raise ValueError("unknown installation reference")

    def _check_document_conflict(self, recording_id):
        if self.db.execute("SELECT 1 FROM document_outbox WHERE recording_id=? AND state='conflict'",
                           (recording_id,)).fetchone():
            raise Conflict("unresolved document conflict")

    def resolve_document_conflict(self, conflict_id, operation_id, context):
        """Сохранённые внешние байты остаются в БД; повтор сверит их хэш перед заменой."""
        _uuid(conflict_id)
        _uuid(operation_id)
        _context(context)
        if context["actor_kind"] != "human":
            raise ValueError("conflict resolution requires a human action")
        digest = _hash({"conflict_id": conflict_id, "context": context})
        with self._transaction():
            replay = self._replay(operation_id, "resolve_document_conflict", digest)
            if replay is not None:
                return replay
            self._check_context_refs(context)
            row = self.db.execute("SELECT * FROM document_conflicts WHERE conflict_id=?",
                                  (conflict_id,)).fetchone()
            if row is None:
                raise KeyError(conflict_id)
            if row["kind"].startswith("archive_"):
                raise Conflict("immutable archive conflict requires separate resolution")
            if row["observed_bytes"] is None:
                raise Conflict("external file too large to preserve; manual resolution required")
            if row["resolution_operation_id"] is not None:
                raise Conflict("conflict already resolved")
            result = {"recording_id": row["recording_id"], "document_revision": row["document_revision"]}
            self._insert_operation(operation_id, "resolve_document_conflict", digest, context, result)
            self.db.execute("UPDATE document_conflicts SET resolution_operation_id=? WHERE conflict_id=?",
                            (operation_id, conflict_id))
            self.db.execute("UPDATE file_locations SET conflict=0 WHERE recording_id=? AND relative_path=?",
                            (row["recording_id"], row["relative_path"]))
            self.db.execute("UPDATE document_outbox SET state='pending', error_code=NULL "
                            "WHERE recording_id=? AND document_revision=? AND state='conflict'",
                            (row["recording_id"], row["document_revision"]))
            return result

    def _check_snapshot_refs(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("author_ref", "initiated_by"):
                    _ref(item)
                    ident = item["user_id"]
                    if ident is not None and not self.db.execute(
                            "SELECT 1 FROM local_users WHERE user_id=?", (ident,)).fetchone():
                        raise ValueError("unknown snapshot user reference")
                elif key == "origin_installation_id":
                    if item is not None:
                        _uuid(item)
                        if not self.db.execute("SELECT 1 FROM installations WHERE installation_id=?",
                                               (item,)).fetchone():
                            raise ValueError("unknown snapshot installation reference")
                else:
                    self._check_snapshot_refs(item)
        elif isinstance(value, list):
            for item in value:
                self._check_snapshot_refs(item)

    def _replay(self, operation_id, kind, digest):
        row = self.db.execute("SELECT kind, parameters_hash, result_json FROM operations "
                              "WHERE operation_id=?", (operation_id,)).fetchone()
        if row:
            if row["kind"] != kind or row["parameters_hash"] != digest:
                raise Conflict("operation_id reused with different parameters")
            return _loads(row["result_json"])
        return None

    def _insert_operation(self, operation_id, kind, digest, context, result):
        self.db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (operation_id, kind, digest, _json(context), _json(result),
                         context["author_ref"]["user_id"], context["initiated_by"]["user_id"],
                         context["origin_installation_id"], _now()))

    def create_recording(self, recording_id, location, transcript, review, operation_id, context,
                         *, _document_spec=None):
        _uuid(recording_id)
        _uuid(operation_id)
        _context(context)
        if not isinstance(location, str) or not location:
            raise ValueError("location required")
        _snapshot(transcript, TRANSCRIPT_FIELDS)
        _snapshot(review, REVIEW_FIELDS)
        validate_pair(transcript, review)
        params = {"recording_id": recording_id, "location": location, "transcript": transcript,
                  "review": review, "context": context, "document_spec": _document_spec}
        digest = _hash(params)
        with self._transaction():
            replay = self._replay(operation_id, "create", digest)
            if replay is not None:
                return replay
            self._check_context_refs(context)
            self._check_document_conflict(recording_id)
            self._check_snapshot_refs(transcript)
            self._check_snapshot_refs(review)
            if self.db.execute("SELECT 1 FROM recordings WHERE recording_id=?", (recording_id,)).fetchone():
                raise Conflict("recording already exists")
            result = {"recording_id": recording_id, "source_revision": 1, "review_revision": 1}
            self._insert_operation(operation_id, "create", digest, context, result)
            self.db.execute("INSERT INTO recordings VALUES (?, ?, 1, 1)", (recording_id, location))
            self.db.execute("INSERT INTO transcript_revisions VALUES (?, 1, NULL, ?, ?, ?, NULL)",
                            (recording_id, _json(transcript), _hash(transcript), operation_id))
            self.db.execute("INSERT INTO review_revisions VALUES (?, 1, NULL, 1, ?, ?, ?, NULL)",
                            (recording_id, _json(review), _hash(review), operation_id))
            return result

    def commit_change(self, recording_id, expected_versions, operation_id, context,
                      *, transcript=None, review=None, _restore_from=None, _document_spec=None):
        _uuid(recording_id)
        _uuid(operation_id)
        _context(context)
        if (not isinstance(expected_versions, dict) or
                set(expected_versions) != {"source_revision", "review_revision"} or
                any(type(v) is not int or v < 1 for v in expected_versions.values())):
            raise ValueError("expected source and review revisions required")
        if transcript is None and review is None:
            raise ValueError("no change")
        if transcript is not None:
            _snapshot(transcript, TRANSCRIPT_FIELDS)
            if review is None:
                raise ValueError("source change requires compatible review snapshot")
        if review is not None:
            _snapshot(review, REVIEW_FIELDS)
        params = {"recording_id": recording_id, "expected_versions": expected_versions,
                  "context": context, "transcript": transcript, "review": review,
                  "restore_from": _restore_from, "document_spec": _document_spec}
        digest = _hash(params)
        with self._transaction():
            replay = self._replay(operation_id, "change", digest)
            if replay is not None:
                return replay
            self._check_context_refs(context)
            self._check_document_conflict(recording_id)
            self._check_snapshot_refs(transcript)
            self._check_snapshot_refs(review)
            row = self.db.execute("SELECT source_revision, review_revision FROM recordings "
                                  "WHERE recording_id=?", (recording_id,)).fetchone()
            if row is None:
                raise KeyError(recording_id)
            if dict(row) != expected_versions:
                raise Conflict("stale revisions")
            current_transcript = (transcript if transcript is not None else
                                  self.get_revision(recording_id, "transcript",
                                                    row["source_revision"])["payload"])
            validate_pair(current_transcript, review)
            if _restore_from is not None:
                old_source, old_review = _restore_from
                old_t = self.get_revision(recording_id, "transcript", old_source)
                old_r = self.get_revision(recording_id, "review", old_review)
                if (old_r["source_revision"] != old_source or old_t["payload"] != transcript
                        or old_r["payload"] != review):
                    raise Conflict("restore tuple is incompatible")
            source = row["source_revision"] + (transcript is not None)
            rev = row["review_revision"] + (review is not None)
            result = {"recording_id": recording_id, "source_revision": source, "review_revision": rev}
            self._insert_operation(operation_id, "change", digest, context, result)
            if transcript is not None:
                self.db.execute("INSERT INTO transcript_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (recording_id, source, row["source_revision"], _json(transcript),
                                 _hash(transcript), operation_id,
                                 _restore_from[0] if _restore_from else None))
            if review is not None:
                self.db.execute("INSERT INTO review_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (recording_id, rev, row["review_revision"], source, _json(review),
                                 _hash(review), operation_id,
                                 _restore_from[1] if _restore_from else None))
            self.db.execute("UPDATE recordings SET source_revision=?, review_revision=? WHERE recording_id=?",
                            (source, rev, recording_id))
            return result

    def restore_as_new_revision(self, recording_id, source_revision, review_revision,
                                expected_versions, operation_id, context):
        old_t = self.get_revision(recording_id, "transcript", source_revision)
        old_r = self.get_revision(recording_id, "review", review_revision)
        if old_r["source_revision"] != source_revision:
            raise Conflict("restore tuple is incompatible")
        return self.commit_change(recording_id, expected_versions, operation_id, context,
                                  transcript=old_t["payload"], review=old_r["payload"],
                                  _restore_from=(source_revision, review_revision))

    def _insert_prepared_document(self, recording_id, revision, source_revision, review_revision,
                                  prepared, root_id, operation_id):
        latest = self.db.execute("SELECT COALESCE(MAX(document_revision), 0) FROM document_versions "
                                 "WHERE recording_id=?", (recording_id,)).fetchone()[0]
        if latest + 1 != revision:
            raise Conflict("document revision changed")
        if revision > 1:
            prev = self.db.execute("SELECT root_id FROM document_outbox WHERE recording_id=? "
                                   "AND document_revision=?", (recording_id, revision - 1)).fetchone()
            if prev is None or prev["root_id"] != root_id:
                raise Conflict("document generations require a single registered root")
        self.db.execute("INSERT INTO document_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (recording_id, revision, source_revision, review_revision,
                         prepared["md"], prepared["json"], prepared["manifest"],
                         prepared["archive_manifest"], _json(prepared["hashes"]), operation_id))
        self._enqueue_document(recording_id, revision, root_id, prepared["hashes"])

    def _enqueue_document(self, recording_id, revision, root_id, hashes):
        import documents
        previous = self.get_document(recording_id, revision - 1)["hashes"] if revision > 1 else {}
        paths = documents.current_paths(recording_id)
        archives = documents.archive_paths(recording_id, revision)
        entries = [(kind, path) for kind, path in paths.items()]
        entries += [("archive_" + kind, path) for kind, path in archives.items()]
        root = self.db.execute("SELECT path FROM allowed_roots WHERE root_id=?", (root_id,)).fetchone()
        if root is None:
            raise ValueError("unknown root")
        for kind, path in entries:
            canonical = os.path.normcase(os.path.abspath(os.path.join(root["path"], path)))
            self.db.execute("INSERT OR IGNORE INTO file_locations(root_id,relative_path,canonical_path,recording_id,kind) "
                            "VALUES (?, ?, ?, ?, ?)", (root_id, path, canonical, recording_id, kind))
            row = self.db.execute("SELECT recording_id,kind FROM file_locations "
                                  "WHERE root_id=? AND relative_path=?", (root_id, path)).fetchone()
            if row is None or row["recording_id"] != recording_id or row["kind"] != kind:
                raise Conflict("file target collision")
        self.db.execute("INSERT INTO document_outbox(recording_id, document_revision, root_id, state, "
                        "generation, expected_previous_hashes) VALUES (?, ?, ?, 'pending', ?, ?)",
                        (recording_id, revision, root_id, revision,
                         _json({kind: previous.get(kind) for kind in paths})))

    def _document_result(self, operation_id, result):
        row = self.db.execute("SELECT d.document_revision, o.state FROM document_versions d "
                              "JOIN document_outbox o USING(recording_id, document_revision) "
                              "WHERE d.operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise StorageError("operation has no queued document")
        return {**result, "document_revision": row["document_revision"],
                "document_sync": row["state"]}

    def create_recording_with_document(self, recording_id, location, transcript, review,
                                       operation_id, context, *, root_id, generated_at):
        import documents
        _uuid(root_id)
        spec = {"root_id": root_id, "generated_at": generated_at}
        params = {"recording_id": recording_id, "location": location, "transcript": transcript,
                  "review": review, "context": context, "document_spec": spec}
        digest = _hash(params)
        replay = self._replay(operation_id, "create", digest)
        if replay is not None:
            return self._document_result(operation_id, replay)
        prepared = documents.prepare(recording_id, 1, 1, 1, transcript, review, context, context, generated_at)
        with self._transaction():
            replay = self._replay(operation_id, "create", digest)
            if replay is not None:
                return self._document_result(operation_id, replay)
            result = self.create_recording(recording_id, location, transcript, review,
                                           operation_id, context, _document_spec=spec)
            self._insert_prepared_document(recording_id, 1, 1, 1, prepared, root_id, operation_id)
            return self._document_result(operation_id, result)

    def commit_change_with_document(self, recording_id, expected_versions, operation_id, context,
                                    *, transcript=None, review=None, root_id, generated_at,
                                    _restore_from=None):
        import documents
        _uuid(root_id)
        spec = {"root_id": root_id, "generated_at": generated_at}
        params = {"recording_id": recording_id, "expected_versions": expected_versions,
                  "context": context, "transcript": transcript, "review": review,
                  "restore_from": _restore_from, "document_spec": spec}
        digest = _hash(params)
        replay = self._replay(operation_id, "change", digest)
        if replay is not None:
            return self._document_result(operation_id, replay)
        old_t = self.get_revision(recording_id, "transcript", expected_versions["source_revision"])
        old_r = self.get_revision(recording_id, "review", expected_versions["review_revision"])
        if old_r["source_revision"] != old_t["revision"]:
            raise Conflict("snapshot tuple is incompatible")
        if transcript is not None and review is None:
            raise ValueError("source change requires review")
        if transcript is None and review is None:
            raise ValueError("no change")
        next_t = transcript if transcript is not None else old_t["payload"]
        next_r = review if review is not None else old_r["payload"]
        source_rev = old_t["revision"] + (transcript is not None)
        review_rev = old_r["revision"] + (review is not None)
        source_ctx = context if transcript is not None else old_t["context"]
        review_ctx = context if review is not None else old_r["context"]
        origin = (self.get_revision(recording_id, "transcript", _restore_from[0])
                  if _restore_from else old_t)
        while origin["restored_from"] is not None:
            origin = self.get_revision(recording_id, "transcript", origin["restored_from"])
        content_ctx = context if transcript is not None and _restore_from is None else origin["context"]
        revision = self.db.execute("SELECT COALESCE(MAX(document_revision),0)+1 "
                                   "FROM document_versions WHERE recording_id=?", (recording_id,)).fetchone()[0]
        annotation = (_loads(self.get_document(recording_id, revision - 1)["json"])["annotation"]
                      if revision > 1 else None)
        prepared = documents.prepare(recording_id, source_rev, review_rev, revision,
                                     next_t, next_r, source_ctx, review_ctx, generated_at,
                                     source_content_context=content_ctx, annotation=annotation)
        with self._transaction():
            replay = self._replay(operation_id, "change", digest)
            if replay is not None:
                return self._document_result(operation_id, replay)
            if annotation is not None:
                self._check_context_refs(annotation["context"], require_active=False)
                self.get_revision(recording_id, "transcript", annotation["source_revision"])
            result = self.commit_change(recording_id, expected_versions, operation_id, context,
                                        transcript=transcript, review=review, _restore_from=_restore_from,
                                        _document_spec=spec)
            self._insert_prepared_document(recording_id, revision, source_rev, review_rev,
                                           prepared, root_id, operation_id)
            return self._document_result(operation_id, result)

    def restore_with_document(self, recording_id, source_revision, review_revision, expected_versions,
                              operation_id, context, *, root_id, generated_at):
        old_t = self.get_revision(recording_id, "transcript", source_revision)
        old_r = self.get_revision(recording_id, "review", review_revision)
        if old_r["source_revision"] != source_revision:
            raise Conflict("restore tuple is incompatible")
        return self.commit_change_with_document(
            recording_id, expected_versions, operation_id, context, transcript=old_t["payload"],
            review=old_r["payload"], root_id=root_id, generated_at=generated_at,
            _restore_from=(source_revision, review_revision))

    def commit_player_event(self, recording_id, expected_versions, operation_id, context, event_type):
        """Сохранить событие плеера без текстовой и документной ревизии."""
        _uuid(recording_id)
        _uuid(operation_id)
        _context(context)
        if event_type not in ("card_visible", "audio_play", "audio_ended", "heartbeat"):
            raise ValueError("unsupported player event")
        if (not isinstance(expected_versions, dict) or
                set(expected_versions) != {"source_revision", "review_revision"} or
                any(type(v) is not int or v < 1 for v in expected_versions.values())):
            raise ValueError("expected source and review revisions required")
        digest = _hash({"recording_id": recording_id, "expected_versions": expected_versions,
                        "context": context, "event_type": event_type})
        with self._transaction():
            replay = self._replay(operation_id, "player_event", digest)
            if replay is not None:
                return replay
            self._check_context_refs(context)
            self._check_document_conflict(recording_id)
            row = self.db.execute("SELECT source_revision, review_revision FROM recordings "
                                  "WHERE recording_id=?", (recording_id,)).fetchone()
            if row is None:
                raise KeyError(recording_id)
            if dict(row) != expected_versions:
                raise Conflict("stale revisions")
            if event_type == "heartbeat":
                result = {"recording_id": recording_id, "source_revision": row["source_revision"],
                          "review_revision": row["review_revision"]}
                self._insert_operation(operation_id, "player_event", digest, context, result)
                return result
            transcript = self.get_revision(recording_id, "transcript", row["source_revision"])["payload"]
            review = self.get_revision(recording_id, "review", row["review_revision"])["payload"]
            events = review.setdefault("interaction_events", [])
            if not isinstance(events, list):
                raise ValueError("invalid interaction events")
            events.append({"event_type": event_type, "context": context})
            if len(events) > MAX_INTERACTION_EVENTS:
                removed = len(events) - MAX_INTERACTION_EVENTS
                del events[:removed]
                review["interaction_events_evicted"] = review.get("interaction_events_evicted", 0) + removed
            validate_pair(transcript, review)
            result = {"recording_id": recording_id, "source_revision": row["source_revision"],
                      "review_revision": row["review_revision"] + 1}
            self._insert_operation(operation_id, "player_event", digest, context, result)
            self.db.execute("INSERT INTO review_revisions VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                            (recording_id, result["review_revision"], row["review_revision"],
                             row["source_revision"], _json(review), _hash(review), operation_id))
            self.db.execute("UPDATE recordings SET review_revision=? WHERE recording_id=?",
                            (result["review_revision"], recording_id))
            return result

    def save_document(self, recording_id, expected_versions, generated_at, operation_id, context,
                      *, annotation=KEEP_ANNOTATION, root_id=None):
        """Сохранить точные байты документа; публикация файлов здесь не выполняется."""
        import documents
        _uuid(recording_id)
        _uuid(operation_id)
        _context(context)
        if (not isinstance(expected_versions, dict) or
                set(expected_versions) != {"source_revision", "review_revision"} or
                any(type(v) is not int or v < 1 for v in expected_versions.values())):
            raise ValueError("expected source and review revisions required")
        if not isinstance(generated_at, str) or not generated_at:
            raise ValueError("generated_at required")
        if root_id is not None:
            _uuid(root_id)
        annotation_param = {"mode": "inherit"} if annotation is KEEP_ANNOTATION else {
            "mode": "replace", "value": annotation}
        digest = _hash({"recording_id": recording_id, "expected_versions": expected_versions,
                        "generated_at": generated_at, "context": context, "annotation": annotation_param,
                        "root_id": root_id})
        replay = self._replay(operation_id, "document", digest)
        if replay is not None:
            return replay
        source = self.get_revision(recording_id, "transcript", expected_versions["source_revision"])
        review = self.get_revision(recording_id, "review", expected_versions["review_revision"])
        if review["source_revision"] != source["revision"]:
            raise Conflict("document snapshot tuple is incompatible")
        latest = self.db.execute("SELECT COALESCE(MAX(document_revision), 0) FROM document_versions "
                                 "WHERE recording_id=?", (recording_id,)).fetchone()[0]
        revision = latest + 1
        if annotation is KEEP_ANNOTATION:
            annotation = (_loads(self.get_document(recording_id, latest)["json"])["annotation"]
                          if latest else None)
        origin = source
        while origin["restored_from"] is not None:
            origin = self.get_revision(recording_id, "transcript", origin["restored_from"])
        prepared = documents.prepare(recording_id, source["revision"], review["revision"], revision,
                                     source["payload"], review["payload"], source["context"],
                                     review["context"], generated_at,
                                     source_content_context=origin["context"], annotation=annotation)
        with self._transaction():
            replay = self._replay(operation_id, "document", digest)
            if replay is not None:
                return replay
            self._check_context_refs(context)
            self._check_document_conflict(recording_id)
            if annotation is not None:
                self._check_context_refs(annotation["context"], require_active=False)
                self.get_revision(recording_id, "transcript", annotation["source_revision"])
                self.get_revision(recording_id, "transcript", annotation["source_revision"])
            head = self.db.execute("SELECT source_revision, review_revision FROM recordings "
                                   "WHERE recording_id=?", (recording_id,)).fetchone()
            if head is None or dict(head) != expected_versions:
                raise Conflict("stale document snapshot")
            latest = self.db.execute("SELECT COALESCE(MAX(document_revision), 0) FROM document_versions "
                                     "WHERE recording_id=?", (recording_id,)).fetchone()[0]
            if latest + 1 != revision:
                raise Conflict("document revision changed")
            result = {"recording_id": recording_id, "document_revision": revision}
            self._insert_operation(operation_id, "document", digest, context, result)
            self.db.execute("INSERT INTO document_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (recording_id, revision, source["revision"], review["revision"],
                             prepared["md"], prepared["json"], prepared["manifest"],
                             prepared["archive_manifest"], _json(prepared["hashes"]), operation_id))
            if root_id is not None:
                if revision > 1:
                    previous = self.db.execute(
                        "SELECT root_id FROM document_outbox WHERE recording_id=? AND document_revision=?",
                        (recording_id, revision - 1)).fetchone()
                    if previous is None or previous["root_id"] != root_id:
                        raise Conflict("document generations require a single registered root")
                self._enqueue_document(recording_id, revision, root_id, prepared["hashes"])
            return result

    def get_document(self, recording_id, document_revision):
        """Прочитать сохранённые исходные байты без нового рендера."""
        _uuid(recording_id)
        if type(document_revision) is not int or document_revision < 1:
            raise ValueError("invalid document revision")
        row = self.db.execute("SELECT * FROM document_versions WHERE recording_id=? AND document_revision=?",
                              (recording_id, document_revision)).fetchone()
        if row is None:
            raise KeyError((recording_id, document_revision))
        result = dict(row)
        result["hashes"] = _loads(result["hashes"])
        for field in ("md", "json", "manifest", "archive_manifest"):
            if hashlib.sha256(result[field]).hexdigest() != result["hashes"][field]:
                raise StorageError("document hash mismatch")
        return result

    def get_revision(self, recording_id, kind, revision):
        _uuid(recording_id)
        if kind not in ("transcript", "review") or type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        table = "transcript_revisions" if kind == "transcript" else "review_revisions"
        row = self.db.execute(f"SELECT r.*, o.context_json FROM {table} r "
                              "JOIN operations o ON o.operation_id=r.operation_id "
                              "WHERE r.recording_id=? AND r.revision=?", (recording_id, revision)).fetchone()
        if row is None:
            raise KeyError((recording_id, kind, revision))
        result = dict(row)
        result["payload"] = _loads(result["payload"])
        if _hash(result["payload"]) != result["payload_hash"]:
            raise StorageError("snapshot hash mismatch")
        result["context"] = _loads(result.pop("context_json"))
        return result

    def get_current(self, recording_id):
        """Прочитать согласованную пару текущих снимков в одной read-транзакции."""
        _uuid(recording_id)
        if self.db.in_transaction:
            raise Conflict("read requires its own transaction")
        try:
            self.db.execute("BEGIN")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise Busy("database is busy") from exc
            raise StorageError("cannot begin read transaction") from exc
        try:
            head = self.db.execute("SELECT source_revision, review_revision FROM recordings "
                                   "WHERE recording_id=?", (recording_id,)).fetchone()
            if head is None:
                raise KeyError(recording_id)
            transcript = self.get_revision(recording_id, "transcript", head["source_revision"])
            review = self.get_revision(recording_id, "review", head["review_revision"])
            if review["source_revision"] != transcript["revision"]:
                raise StorageError("incompatible current snapshots")
            try:
                validate_pair(transcript["payload"], review["payload"])
            except ValueError as exc:
                raise StorageError("invalid stored snapshots") from exc
            self.db.execute("COMMIT")
            return {"transcript": transcript, "review": review}
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def list_revisions(self, recording_id, kind):
        _uuid(recording_id)
        if kind not in ("transcript", "review"):
            raise ValueError("invalid revision kind")
        table = "transcript_revisions" if kind == "transcript" else "review_revisions"
        return [r[0] for r in self.db.execute(
            f"SELECT revision FROM {table} WHERE recording_id=? ORDER BY revision", (recording_id,))]
