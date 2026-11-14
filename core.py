"""Ядро rus-transcriber: задачи (запуск, статус, отмена), чтение стенограмм, настройки, данные.

Здесь нет ничего, что знало бы про MCP, Claude или любой другой клиент. Слои управления — server.py (MCP) и
rt.py (командная строка) — только вызывают эти функции и показывают результат. Любой третий слой (HTTP, окно,
другой протокол) строится так же.

Соглашения: функции возвращают обычные структуры (dict, list, str); понятная пользователю ошибка — CoreError.
Опасные действия (выгрузка, загрузка, удаление, очистка, перенос) здесь только исполняются; решение человека
получает слой управления (confirm.py для MCP, ввод в терминале для rt.py).
"""
import json
import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import paths  # noqa: E402  (дочерние процессы унаследуют HF_HOME и др.)
import settings  # noqa: E402
import migrate  # noqa: E402
import data_io  # noqa: E402

JOBS = paths.JOBS
JOBS.mkdir(parents=True, exist_ok=True)

ACTIVE = ("queued", "running")
FINISHED = ("done", "error", "cancelled")


class CoreError(Exception):
    """Ошибка, текст которой можно показать человеку как есть."""


# ---------- процессы и статусы задач ----------
def alive(pid):
    """Жив ли процесс (без psutil; os.kill на Windows процесс бы завершил)."""
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))       # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259           # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def pid_of(job_id):
    try:
        return int((JOBS / f"{job_id}.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def job_status(job_id):
    """Статус задачи (dict) или None, если такой задачи нет. Если процесс пропал без итога — state=error."""
    p = JOBS / f"{job_id}.json"
    if not p.exists():
        return None
    for _ in range(3):        # файл могут как раз заменять (os.replace в worker) — пробуем ещё раз
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            break
        except (json.JSONDecodeError, OSError):
            time.sleep(0.1)
    else:
        return {"state": "running", "stage": "обновляется"}
    if d.get("state") in ACTIVE:
        pid = pid_of(job_id)
        if pid and not alive(pid):
            time.sleep(0.5)          # процесс мог только что записать итог
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
            if d.get("state") in ACTIVE:
                d.update(state="error", stage="процесс завершился без итога",
                         error=f"процесс задачи пропал — подробности в jobs\\{job_id}.log")
    return d


def _need(job_id):
    s = job_status(job_id)
    if not s:
        raise CoreError(f"Задача {job_id} не найдена")
    return s


def launch(script, args, src, kind):
    """Запускает фоновый процесс-задачу (worker.py и др.), возвращает job_id."""
    job_id = time.strftime("%Y%m%d-%H%M%S-") + kind[0] + uuid.uuid4().hex[:5]
    status = JOBS / f"{job_id}.json"
    status.write_text(json.dumps({"state": "queued", "kind": kind, "file": str(src)}, ensure_ascii=False),
                      encoding="utf-8")
    cmd = [sys.executable, str(HERE / script), str(src), "--status", str(status), *args]
    log = open(JOBS / f"{job_id}.log", "w", encoding="utf-8")
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                            cwd=str(HERE), creationflags=flags, env=env)
    (JOBS / f"{job_id}.pid").write_text(str(proc.pid), encoding="utf-8")
    return job_id


def cancel_job(job_id):
    """Останавливает идущую или ждущую задачу вместе с дочерними процессами; готовые результаты не трогает."""
    s = _need(job_id)
    if s.get("state") in FINISHED:
        return {"job_id": job_id, "state": s.get("state"), "note": "задача уже не идёт"}
    pid = pid_of(job_id)
    killed = False
    if pid and alive(pid):
        if os.name == "nt":
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True,
                               creationflags=0x08000000)
            killed = r.returncode == 0
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
            killed = True
    for _ in range(10):
        if not pid or not alive(pid):
            break
        time.sleep(0.3)
    st = {**s, "state": "cancelled", "stage": "отменено", "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    (JOBS / f"{job_id}.json").write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"job_id": job_id, "state": "cancelled", "was": s.get("stage"), "process_killed": killed}


def start_transcription(audio_path, num_speakers=0, emotions=True, vocab_path="", tag="", voices=True,
                        names="", whisper="", whisper_max_repeat=-1, group=""):
    src = Path(audio_path).expanduser()
    if not src.exists():
        raise CoreError(f"Файл не найден: {src}")
    args = (["--speakers", str(num_speakers)] if num_speakers else []) + ([] if emotions else ["--no-emotions"])
    if vocab_path:
        args += ["--vocab", vocab_path]
    if group:
        args += ["--group", group]
    if tag:
        args += ["--tag", tag]
    if not voices:
        args.append("--no-voices")
    if whisper:
        if whisper not in ("all", "doubt", "off"):
            raise CoreError('whisper: "all", "doubt" или "off"')
        args += ["--whisper", whisper]
    if whisper_max_repeat >= 0:
        args += ["--whisper-max-repeat", str(int(whisper_max_repeat))]
    names_list = [n.strip() for n in names.replace(";", ",").split(",") if n.strip()]
    if names_list:
        args += ["--names", ",".join(names_list)]
    return {"job_id": launch("worker.py", args, src, "transcribe"), "file": str(src)}


def start_benchmark(audio_path, seconds=180, only="", chunk_max=20.0, min_similarity=None):
    src = Path(audio_path).expanduser()
    if not src.exists():
        raise CoreError(f"Файл не найден: {src}")
    args = ["--seconds", str(seconds), "--chunk-max", str(chunk_max)] + (["--only", only] if only else [])
    if min_similarity is not None and float(min_similarity) > 0:
        args += ["--min-similarity", str(min_similarity)]
    return {"job_id": launch("bench.py", args, src, "benchmark")}


def live(s, job_id):
    """Статус с вычисленным: сколько идёт задача и текущий этап, давно ли обновлялся файл статуса."""
    s = dict(s)
    now = time.time()
    t0, ts = s.pop("started_ts", None), s.pop("stage_started_ts", None)
    if s.get("state") in ACTIVE:
        if t0:
            s["elapsed_sec"] = round(now - t0)
        if ts:
            s["stage_elapsed_sec"] = round(now - ts)
        try:
            s["since_update_sec"] = round(now - (JOBS / f"{job_id}.json").stat().st_mtime)
        except OSError:
            pass
    return s


def stage_key(s):
    """Состояние и этап без счётчика в конце («поиск речи 12/321») — смена этапа, а не прогресс."""
    return s.get("state"), re.sub(r"\s*\d+/\d+\s*$", "", str(s.get("stage") or ""))


def status(job_id, wait_sec=0, sleep=time.sleep):
    """Статус задачи; при wait_sec>0 ждёт (до 50 с) смены этапа или конца. sleep можно подменить (асинхронно
    это делает server.py через poll())."""
    s = _need(job_id)
    k0 = stage_key(s)
    deadline = time.time() + min(max(int(wait_sec or 0), 0), 50)
    while s.get("state") in ACTIVE and time.time() < deadline:
        sleep(1)
        s2 = job_status(job_id)
        if s2 and s2.get("stage") != "обновляется":
            s = s2
        if stage_key(s) != k0:
            break
    return live(s, job_id)


def list_jobs(limit=10):
    rows = []
    for p in sorted(JOBS.glob("*.json"), reverse=True)[:limit]:
        s = job_status(p.stem) or {}
        rows.append({"job_id": p.stem, "kind": s.get("kind"), "state": s.get("state"),
                     "stage": s.get("stage"), "progress": s.get("progress"), "file": s.get("file")})
    return rows


def job_json(job_id):
    """(путь к json, путь к md) готовой расшифровки или (None, None)."""
    s = job_status(job_id)
    if not s or s.get("state") != "done" or "json" not in s:
        return None, None
    return s["json"], s.get("markdown")


# ---------- чтение стенограммы ----------
def _text_only(s, key):
    """Только реплики с пометками ⟦…⟧ у нерешённых мест; служебные разделы — одной строкой."""
    import transcript
    data = transcript.load(s["json"])
    marks = transcript.doubt_marks(data) if data.get("review") else {}
    if key == "markdown":
        full = transcript.render_md(data)
        body = transcript.render_md(data, text_only=True).rstrip()
        rest = [l[3:] for l in full.split("\n") if l.startswith("## ")]
        if rest:
            body += f"\n\n[без разделов: {', '.join(rest)} — {len(full) - len(body)} симв.; extra=True — полностью]"
        return body
    quality = data.get("quality") or {}
    summary = {k: quality[k] for k in ("asr_incomplete_chunks", "vote3_skipped") if quality.get(k)}
    if summary:
        data["asr_summary"] = summary
    for k in ("review", "edits", "quality", "timing"):
        data.pop(k, None)
    segs = []
    for si, seg in enumerate(data.get("segments", [])):
        x = {k: v for k, v in seg.items() if k not in ("words", "text_raw")}
        if marks.get(si):
            x["doubts"] = sorted(set(marks[si].values()))
        segs.append(x)
    data["segments"] = segs
    return json.dumps(data, ensure_ascii=False)


def read_transcript(job_id, fmt="md", offset=0, max_chars=60000, extra=False):
    s = _need(job_id)
    if s.get("state") != "done":
        raise CoreError(f"Ещё не готово: {s.get('stage')} {s.get('progress', 0)}%")
    key = "report" if "report" in s else ("json" if fmt == "json" else "markdown")
    if key != "report" and not extra and s.get("json"):
        text = _text_only(s, key)
    else:
        text = Path(s[key]).read_text(encoding="utf-8")
    part = text[offset:offset + max_chars]
    if offset + max_chars < len(text):
        part += f"\n\n[...продолжение: offset={offset + max_chars}, всего символов {len(text)}]"
    return part


def _done_json(job_id):
    s = job_status(job_id)
    if not s or s.get("state") != "done" or "json" not in s:
        raise CoreError("Расшифровка не найдена или ещё не готова")
    return s


REASONS = ("asr", "conf", "voice", "overlap", "gender", "missed")


def review_items(job_id, offset=0, limit=40, reasons="", min_sec=0.0, brief=False, status="open"):
    """Сомнительные места готовой расшифровки (список для смыслового разбора). reasons — через запятую."""
    import transcript
    s = _done_json(job_id)
    want = [r.strip() for r in (reasons or "").split(",") if r.strip()]
    bad = [r for r in want if r not in REASONS]
    if bad:
        raise CoreError(f"неизвестные причины: {', '.join(bad)}; есть: {', '.join(REASONS)}")
    if status not in ("open", "human", "resolved", "all"):
        raise CoreError("status: open, human, resolved или all")
    limit = max(1, min(int(limit), 200 if brief else 60))
    return transcript.review_list(s["json"], max(0, int(offset)), limit, want, float(min_sec or 0), bool(brief), status)


def build_review_pack(job_id, items=None):
    """Подготовка карточек без зависимости от MCP или конкретной модели."""
    import review_pack as P
    import transcript
    jp = _done_json(job_id)["json"]
    if items is None:
        data = transcript.load(jp)
        items = [{"id": it["id"], "question": "Проверьте текст и говорящего. При необходимости введите свой ответ.",
                  "options": [{"label": "Оставить как есть", "decision": {}}]}
                 for it in data.get("review", []) if it.get("status", "open") in ("open", "human")]
    return P.build(jp, items)


def start_review_page(job_id, port=8765):
    import review_pack as P
    job = _done_json(job_id)
    jp = job["json"]
    if not P.manifest(jp):
        raise CoreError("Сначала подготовьте карточки: review-build / build_review_pack")
    if not 1 <= int(port) <= 65516:
        raise CoreError("port: от 1 до 65516")
    args = ["--port", str(int(port))]
    if job.get("markdown"):
        args += ["--markdown", job["markdown"]]
    jid = launch("review_page.py", args, jp, "page")
    for _ in range(40):
        s = job_status(jid) or {}
        if s.get("url"):
            return {"job_id": jid, "url": s["url"], "hint": "Откройте адрес в браузере на этом компьютере"}
        if s.get("state") == "error":
            raise CoreError(s.get("error") or "Не удалось запустить страницу")
        time.sleep(0.25)
    raise CoreError(f"Страница ещё запускается; проверьте status {jid}")


def _merge_free_reports(parts):
    """Свести отчёты по свободным ответам одного вызова, сохранив причины по каждой карточке."""
    if not parts:
        return {}
    merged = dict(parts[0])
    for key in ("successful_ids", "not_found", "cards", "conflicts", "left_to_human",
                "proposed_delete", "vocab_suggestions"):
        merged[key] = [x for part in parts for x in part.get(key) or []]
    for key in ("closed_via", "failed", "conspects"):
        merged[key] = {k: v for part in parts for k, v in (part.get(key) or {}).items()}
    merged["counts"] = {k: sum((part.get("counts") or {}).get(k, 0) for part in parts)
                        for k in ("relabel", "replace", "insert")}
    return merged


def describe_conspect_confirmation(job_id, name):
    """Снимок для подтверждения: хэши обоих файлов связывают разрешение с их содержимым."""
    import hashlib
    import conspect
    jp = Path(_done_json(job_id)["json"])
    cj, _ = conspect.path_for(jp, name)
    try:
        source_bytes, conspect_bytes = jp.read_bytes(), cj.read_bytes()
        data, obj = json.loads(source_bytes), json.loads(conspect_bytes)
        if not obj.get("recording_id") or obj.get("recording_id") != data.get("recording_id"):
            raise CoreError("конспект относится к другой или неизвестной записи")
        if data.get("source_revision") is None:
            raise CoreError("ревизия источника неизвестна")
        return {"job_id": job_id, "name": name, "recording_id": data["recording_id"],
                "source_revision": data["source_revision"],
                "source_hash": hashlib.sha256(source_bytes).hexdigest(),
                "conspect_hash": hashlib.sha256(conspect_bytes).hexdigest()}
    except (OSError, ValueError, KeyError) as ex:
        raise CoreError("не удалось прочитать стенограмму или конспект") from ex


def preview_conspect_candidates(job_id, text):
    import conspect
    jp = Path(_done_json(job_id)["json"])
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
        _, report = conspect.link(text, data)
        return report
    except (OSError, ValueError) as ex:
        raise CoreError("не удалось подготовить кандидатов цитат") from ex


MAX_CONSPECT_SELECTIONS = 3


def selected_conspect_confirmation_lines(job_id, name, snapshot, report, selections):
    """Полный ограниченный текст для человека; обрезка важной информации запрещена."""
    import math

    def visible(value, limit):
        if not isinstance(value, str) or not value or len(value) > limit or not value.isprintable():
            raise CoreError("выбор нельзя полностью и безопасно показать человеку")
        return value

    def time_text(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 864000:
            raise CoreError("недопустимое время выбранного места")
        return str(value)

    visible(job_id, 80)
    visible(name, 80)
    revision = snapshot.get("source_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or not 0 <= revision <= 2**53:
        raise CoreError("недопустимая ревизия источника")
    lines = [f"Новый конспект: {name}", f"Задача: {job_id}, ревизия: {revision}"]
    shown = set()
    for entry in report.get("candidates") or []:
        key = str(entry["quote_index"])
        selection = selections.get(key)
        if selection is None:
            continue
        try:
            option = next(x for x in entry["candidates"] if x["word_ids"] == selection["word_ids"])
            quote = visible(entry["quote"], 120)
            speaker = visible(option["speaker"], 50)
            start, end = time_text(option["t1"]), time_text(option["t2"])
            if len(key) > 8 or not key.isascii() or not key.isdecimal():
                raise CoreError("недопустимый номер цитаты")
            label = (speaker, start, end)
            if entry.get("truncated") or sum(
                    (visible(x["speaker"], 50), time_text(x["t1"]), time_text(x["t2"])) == label
                    for x in entry["candidates"]) != 1:
                raise CoreError("кандидатов нельзя однозначно различить по отображаемым данным")
            lines.append(f"Цитата {key} «{quote}»: {speaker}, {start}–{end} с")
            shown.add(key)
        except (KeyError, TypeError, StopIteration) as ex:
            raise CoreError("выбранный кандидат отсутствует") from ex
    if shown != set(selections):
        raise CoreError("не все выбранные цитаты можно показать человеку")
    lines.append("Создать новый конспект с этими выбранными местами?")
    if any(len(line) > 220 for line in lines) or len("\n".join(lines)) > 1200:
        raise CoreError("список выбора слишком длинный для подтверждения")
    return lines


def prepare_selected_conspect(job_id, name, text, selections):
    import hashlib
    import conspect
    jp = Path(_done_json(job_id)["json"])
    cj, cm = conspect.path_for(jp, name)
    if cj.exists() or cm.exists():
        raise CoreError("конспект с таким названием уже существует; выберите другое название")
    if not isinstance(selections, dict) or not 1 <= len(selections) <= MAX_CONSPECT_SELECTIONS:
        raise CoreError(f"за одно подтверждение выберите от 1 до {MAX_CONSPECT_SELECTIONS} цитат")
    try:
        source = jp.read_bytes()
        data = json.loads(source)
        obj, report = conspect.link(text, data, selections=selections)
        if len(selections) > report["linked"]:
            raise ValueError("не все выбранные цитаты связаны")
        payload = json.dumps({"text": text, "selections": selections}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        snapshot = {"job_id": job_id, "name": name, "source_hash": hashlib.sha256(source).hexdigest(),
                    "request_hash": hashlib.sha256(payload).hexdigest(), "recording_id": data.get("recording_id"),
                    "source_revision": data.get("source_revision")}
        selected_conspect_confirmation_lines(job_id, name, snapshot, report, selections)
        return snapshot, report
    except (OSError, ValueError, TypeError, StopIteration, KeyError) as ex:
        raise CoreError("выбранные цитаты устарели или недействительны") from ex


def save_selected_conspect(snapshot, text, selections):
    import conspect
    jp = Path(_done_json(snapshot["job_id"])["json"])
    with conspect.write_lock(jp):
        conspect.recover_pending(jp)
        return _save_selected_conspect_locked(snapshot, text, selections)


def _save_selected_conspect_locked(snapshot, text, selections):
    import conspect
    current, _ = prepare_selected_conspect(snapshot["job_id"], snapshot["name"], text, selections)
    if current != snapshot:
        raise CoreError("стенограмма или выбор изменились после подтверждения")
    jp = Path(_done_json(snapshot["job_id"])["json"])
    data = json.loads(jp.read_text(encoding="utf-8"))
    obj, report = conspect.link(text, data, selections=selections)
    obj.update(name=snapshot["name"], job_json=str(jp))
    md, render_report = conspect.render(obj, data)
    if prepare_selected_conspect(snapshot["job_id"], snapshot["name"], text, selections)[0] != snapshot:
        raise CoreError("стенограмма или выбор изменились после подтверждения")
    cj, cm = conspect.path_for(jp, snapshot["name"])
    try:
        conspect.write_files(jp, [(cj, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")),
                                  (cm, md.encode("utf-8"))], create=True)
    except OSError as ex:
        raise CoreError("не удалось полностью сохранить конспект; проверьте файлы результата") from ex
    return {**report, "conspect_json": str(cj), "conspect_md": str(cm),
            "analysis_needs_review": render_report["analysis_needs_review"],
            "assembly_revision": render_report["assembly_revision"],
            "analysis_revision": render_report["analysis_revision"],
            "external_publication": render_report["external_publication"]}


def confirm_conspect_analysis(snapshot):
    """Записать подтверждение только если файлы не изменились после показа человеку."""
    import conspect
    jp = Path(_done_json(snapshot["job_id"])["json"])
    with conspect.write_lock(jp):
        conspect.recover_pending(jp)
        return _confirm_conspect_analysis_locked(snapshot)


def _confirm_conspect_analysis_locked(snapshot):
    import conspect
    current = describe_conspect_confirmation(snapshot["job_id"], snapshot["name"])
    if current != snapshot:
        raise CoreError("конспект или стенограмма изменились после запроса подтверждения")
    jp = Path(_done_json(snapshot["job_id"])["json"])
    cj, _ = conspect.path_for(jp, snapshot["name"])
    obj = json.loads(cj.read_text(encoding="utf-8"))
    data = json.loads(jp.read_text(encoding="utf-8"))
    conspect.confirm_analysis(obj, data)
    # Повторно проверяем снимок; внешние писатели могут не соблюдать блокировку.
    if describe_conspect_confirmation(snapshot["job_id"], snapshot["name"]) != snapshot:
        raise CoreError("конспект или стенограмма изменились после запроса подтверждения")
    conspect.write_files(jp, [(cj, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))])
    return {"name": snapshot["name"], "source_revision": snapshot["source_revision"],
            "analysis_revision": snapshot["source_revision"], "analysis_needs_review": False,
            "external_publication": "not_tracked"}


def apply_answers(job_id):
    """Применить сохранённые ответы человека; аудио не удаляется."""
    jp, md = job_json(job_id)
    if not jp:
        raise CoreError("Расшифровка не найдена или ещё не готова")
    return apply_answers_files(jp, md)


def apply_answers_files(jp, md):
    """Применить ответы к указанной паре файлов готовой задачи."""
    import conspect
    with conspect.write_lock(jp):
        return _apply_answers_files_locked(jp, md)


def _apply_answers_files_locked(jp, md):
    import review_pack as P
    import transcript
    decisions, rep = P.decisions(jp)
    free = P.free_answers(jp)
    preflight = P.preflight_answers(jp, md, decisions, free)
    blocked = {aid for conflict in preflight for aid in conflict["cards"]}
    blocked_free = {aid: answer for aid, answer in free.items() if aid in blocked}
    decisions = [d for d in decisions if d.get("id") not in blocked]
    free = {aid: answer for aid, answer in free.items() if aid not in blocked}
    if preflight:
        rep["preflight_conflicts"] = preflight
        rep["human"] = list(dict.fromkeys((rep.get("human") or []) + sorted(blocked)))
        rep["changed"] = [aid for aid in rep.get("changed") or [] if aid not in blocked]
    undo = [d for d in decisions if "_undo_snapshot" in d]
    decisions = [d for d in decisions if "_undo_snapshot" not in d]
    undone, undo_failed = [], set()
    for d in undo:
        result = transcript.undo_answer(jp, md, d["id"], d["_undo_snapshot"])
        if result.get("reverted"):
            undone.append(d["id"])
            rep["reverted"].append(d["id"])
        else:
            rep.setdefault("undo_conflicts", []).append(result)
            undo_failed.add(d["id"])
    decisions = [d for d in decisions if d.get("id") not in undo_failed]
    free = {aid: answer for aid, answer in free.items() if aid not in undo_failed}
    # Изменённый ответ тоже сначала отменяется, но в отчёте это изменение, а не отмена.
    rep["reverted"] = [x for x in rep.get("reverted") or [] if x not in rep.get("changed", [])]
    snapshots, report = {}, {}
    snapshot_bytes = 0

    def remember_snapshot(aid, before, after):
        nonlocal snapshot_bytes
        snapshot = transcript.answer_snapshot(before, after)
        size = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        # Даже компактные снимки крупных изменений могут быть большими.
        # Ответ без снимка останется применённым, но его
        # автоматическая отмена будет недоступна и об этом сообщается.
        if snapshot_bytes + size > 16 * 1024 * 1024:
            rep.setdefault("undo_unavailable", []).append(aid)
            return
        snapshots[aid] = snapshot
        snapshot_bytes += size

    if decisions:
        state = transcript.load(jp)
        order = {it["id"]: it.get("t", 0) for it in state.get("review") or []}
        for decision in sorted(decisions, key=lambda d: -order.get(d.get("id"), 0)):
            before = transcript.load(jp)
            after, result = transcript.apply_review(jp, md, [decision], by="human")
            for key, value in result.items():
                if isinstance(value, list) and key != "vocab_suggestions":
                    report.setdefault(key, []).extend(value)
                elif key in ("applied", "kept", "human"):
                    report[key] = report.get(key, 0) + value
                else:
                    report[key] = value
            aid = decision.get("id")
            if aid in result.get("successful_ids", []) and aid not in rep.get("human", []):
                remember_snapshot(aid, before, after)
    free_items = []
    if blocked_free:
        free_items.append({"conflicts": preflight,
                           "left_to_human": [{"id": aid, "why": "конфликт с другой карточкой до применения",
                                              "answer": answer.get("text")} for aid, answer in blocked_free.items()]})
    if free:
        # Свободные ответы нередко затрагивают одни и те же слова. Применяем их
        # отдельно, чтобы у каждого успешного ответа был обратимый структурный
        # снимок; это снимает консервативный отказ от пакетной отмены.
        order = {it["id"]: it.get("t1", 0) for it in (P.manifest(jp) or {}).get("items") or []}
        for aid in sorted(free, key=lambda x: -order.get(x, 0)):
            before = transcript.load(jp)
            part = P.apply_free(jp, md, {aid: free[aid]})
            free_items.append(part)
            if aid in part.get("successful_ids", []):
                remember_snapshot(aid, before, transcript.load(jp))
    if free_items:
        rep["free_text"] = _merge_free_reports(free_items)
    successful = set(report.get("successful_ids", []))
    successful.update(rep.get("free_text", {}).get("successful_ids", []))
    successful.difference_update(rep.get("human", []))
    rep["applied_from_answers"] = sum(1 for d in decisions if d.get("id") in successful)
    snapshots = {aid: snapshot for aid, snapshot in snapshots.items() if aid in successful}
    successful.update(undone)
    if successful:
        if snapshots:
            P.mark_applied(jp, successful, snapshots=snapshots)
        else:
            P.mark_applied(jp, successful)
    if undone:
        rep["voice_profile_warning"] = {
            "answer_ids": undone,
            "message": ("Если эта запись ранее пополнила голосовой профиль, её прежний вклад мог остаться. "
                        "Профиль не изменён: проверьте вклад и при необходимости отдельно подтвердите "
                        "его обновление или отзыв."),
        }
    return {**rep, "review": report}


def edit_span(job_id, t1, t2, text, speaker="", note="", by="claude"):
    """Правка отрезка t1–t2 (с): текст и говорящий; без пересборки карточек."""
    import transcript
    s = _done_json(job_id)
    if by not in ("claude", "human"):
        raise CoreError('by: "claude" или "human"')
    try:
        _, rep = transcript.edit_span(s["json"], s["markdown"], t1, t2, text, speaker or None, note or None, by)
    except ValueError as ex:
        raise CoreError(str(ex))
    return rep


def reopen_review(job_id, ids):
    """Снова открыть места разбора (через запятую или списком)."""
    import transcript
    s = _done_json(job_id)
    ids = [x.strip() for x in (ids.split(",") if isinstance(ids, str) else ids) if str(x).strip()]
    if not ids:
        raise CoreError("укажите id мест, например r57,r88")
    _, rep = transcript.reopen(s["json"], s["markdown"], ids)
    return rep


# ---------- настройки, группы, профили ----------
def settings_info():
    return {"data_dir": str(paths.DATA_DIR), "config_file": str(paths.CONFIG_FILE),
            "overrides": settings.load_overrides(), "effective": settings.load_config(),
            "groups": paths.list_groups()}


def preview_voice_learning(job_id):
    """Предварительная оценка по разметке, без моделей, биометрических векторов и изменения файлов."""
    import voices
    jp, _ = job_json(job_id)
    if not jp:
        raise CoreError("готовая расшифровка не найдена")
    try:
        data = json.loads(Path(jp).read_text(encoding="utf-8"))
        if data.get("no_profile"):
            raise CoreError("для этой расшифровки профили групп отключены")
        group = voices.group_of(data)
        if not group:
            raise CoreError("группа расшифровки неизвестна; предварительный просмотр невозможен")
        path = paths.voices_path(group)
        try:
            profile = voices.load_profile(path, convert=False)
        except voices.VoiceProfileError as ex:
            raise CoreError(f"голосовой профиль группы несовместим или повреждён: {ex}. "
                            "Просмотр и изменение не выполняются; перенесите профиль отдельно.") from ex
        audio = data.get("file")
        if not audio:
            raise CoreError("в расшифровке не указан исходный аудиофайл")
        key = str(Path(audio).resolve())
        existing = sorted(name for name, speaker in profile.get("speakers", {}).items()
                          if key in speaker.get("parts", {}))
        report = {}
        eligible = voices._confirmed(data) or voices.has_human_edits(data)
        spans = voices._spans(data, voices._confirmed(data), report) if eligible else []
        if not eligible:
            report["unconfirmed_source"] = 1
        candidates = {}
        for name, start, end, human in spans:
            item = candidates.setdefault(name, {"windows": 0, "seconds": 0.0, "human_windows": 0})
            item["windows"] += 1
            item["seconds"] += end - start
            item["human_windows"] += int(human)
        for item in candidates.values():
            item["seconds"] = round(item["seconds"], 2)
        return {"group": group, "existing_contributions": existing, "candidates": candidates,
                "selection_report": report, "selection_explanation": voices.explain_selection_report(report),
                "audio_available": Path(audio).is_file(),
                "replacement_available": True,
                "note": "Только разметка; модель, качество голоса и карантин не проверялись. "
                        "Окончательный результат обновления зависит от модели и карантина."}
    except (OSError, ValueError, KeyError, TypeError) as ex:
        raise CoreError(f"не удалось построить предварительный просмотр: {ex}") from ex


def _voice_hash(path):
    p = Path(path)
    if not p.is_file():
        return "absent"
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_voice_action(job_id):
    """Снимок предварительного плана для привязки подтверждения к конкретным файлам."""
    jp, _ = job_json(job_id)
    if not jp:
        raise CoreError("готовая расшифровка не найдена")
    try:
        before = _voice_hash(jp)
        report = preview_voice_learning(job_id)
        group = report["group"]
        if paths.safe_group(group) != group:
            raise CoreError("недопустимое имя группы")
        with settings.group_voice_lock(group):
            profile = paths.voices_path(group)
            profile_hash = _voice_hash(profile)
            source_data = json.loads(Path(jp).read_text(encoding="utf-8"))
            audio = source_data.get("file")
            audio_hash = _voice_hash(audio) if audio else "absent"
            report = preview_voice_learning(job_id)
            if before != _voice_hash(jp) or report["group"] != group or profile_hash != _voice_hash(profile):
                raise CoreError("исходные файлы изменились; повторите предварительный просмотр")
        snapshot = {"job_id": job_id, "json": str(Path(jp).resolve()), "group": group,
                    "source_hash": before, "profile_hash": profile_hash, "audio_hash": audio_hash, "report": report}
        if any(len(line) > 200 for line in voice_action_lines(snapshot)) or len("\n".join(voice_action_lines(snapshot))) > 1100:
            raise CoreError("список участников слишком длинный для безопасного подтверждения; действие не запущено")
        return snapshot
    except (OSError, ValueError) as ex:
        raise CoreError(f"не удалось подготовить подтверждение: {ex}") from ex


def voice_action_lines(snapshot, revoke=False):
    report = snapshot["report"]
    candidates = report["candidates"]
    lines = [f"Группа: {snapshot['group']}", f"Расшифровка: {Path(snapshot['json']).name}",
             "Существующие вклады: " + (", ".join(report["existing_contributions"]) or "нет"),
             "Кандидаты по разметке: " + (", ".join(f"{n}: {v['seconds']} с" for n, v in candidates.items()) or "нет")]
    if report.get("selection_explanation"):
        lines.append("Предварительный отбор (результат модели может отличаться):")
        lines.extend(f"{item['description']}: {item['count']}" for item in report["selection_explanation"])
    lines.extend([("Удалить перечисленные вклады источника; другие источники не меняются." if revoke else
                   "Новый отпечаток может заменить вклад или попасть в карантин; без подходящего отпечатка вклад сохраняется."),
                  "Будет создана резервная копия профиля. Подтвердите только после проверки."])
    return lines


def backup_voice_profile(path):
    """Сохранить точную копию биометрического файла до изменения, не заменяя старые копии."""
    path = Path(path)
    if not path.exists():
        return None
    folder = path.parent / "voice-backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("voices-" + uuid.uuid4().hex + ".json")
    with target.open("xb") as f:
        f.write(path.read_bytes())
    return str(target)


def backup_voice_profiles(group=""):
    """Сделать отдельную резервную копию профиля одной группы или всех групп.

    Копии лежат в `voice-backups` внутри группы, не шифруются, не входят в экспорт и
    автоматически не удаляются. Векторы и имена не возвращаются.
    """
    import voices
    if group:
        if group not in paths.list_groups():
            raise CoreError("группа не найдена")
        groups = [group]
    else:
        groups = paths.list_groups()
    backups = []
    for name in groups:
        path = paths.voices_path(name)
        try:
            with settings.group_voice_lock(name):
                target = backup_voice_profile(path)
        except (OSError, ValueError) as ex:
            raise CoreError(f"не удалось сохранить резерв группы «{name}»: {ex}") from ex
        if target:
            backups.append({"group": name, "backup": target})
    return {"backups": backups, "count": len(backups),
            "note": "Резерв — биометрия в открытом виде; храните и удаляйте по решению владельца."}


def voice_quality(group=""):
    """Политика качества и сохранённые измерения по профилям; аудио не декодируется."""
    import voices
    if group and group not in paths.list_groups():
        raise CoreError("группа не найдена")
    groups = [group] if group else paths.list_groups()
    out = []
    for name in groups:
        try:
            policy = settings.group_voice_settings(name)
        except ValueError as ex:
            out.append({"group": name, "settings_error": str(ex)})
            continue
        entry = {"group": name,
                 "audio_quality_policy": policy.get("audio_quality_policy", "warn"),
                 "policy_source": policy.get("audio_quality_policy_source", "default")}
        path = paths.voices_path(name)
        if not path.exists():
            entry.update({"profile": "absent", "measurements": [], "summary": {"measured_sources": 0}})
            out.append(entry)
            continue
        try:
            data = voices.load_profile(path, convert=False)
        except voices.VoiceProfileError as ex:
            entry.update({"profile_error": str(ex)})
            out.append(entry)
            continue
        measurements = []
        for source, q in (data.get("audio_quality") or {}).items():
            if not isinstance(q, dict):
                continue
            measurements.append({
                "recording": Path(source).name, "from": q.get("from"),
                "at": q.get("at"), "available": q.get("available"),
                "duration_sec": q.get("duration_sec"), "peak_dbfs": q.get("peak_dbfs"),
                "rms_dbfs": q.get("rms_dbfs"), "clipping_ratio": q.get("clipping_ratio"),
                "silence_ratio": q.get("silence_ratio"), "snr_db_estimate": q.get("snr_db_estimate"),
                "warnings": q.get("warnings") or [],
                "warning_notes": voices.explain_audio_quality(q)})
        entry.update({
            "profile": "ok", "measurements": measurements[-50:],
            "summary": {"measured_sources": len(measurements),
                        "clipping_sources": sum(1 for m in measurements if "clipping" in (m["warnings"] or [])),
                        "warning_sources": sum(1 for m in measurements if m["warnings"])},
            "recent_quality_warnings": [w for w in (data.get("warnings") or [])
                                        if w.get("kind") == "audio_quality"][-5:]})
        out.append(entry)
    return {"groups": out,
            "note": "Измерения приблизительны. Политика warn только предупреждает; "
                    "reject_clipping не учитывает источник с клиппингом. Тихая речь не отбраковывается."}


def set_voice_quality_policy(policy, group=""):
    """Политика качества: warn (по умолчанию) или reject_clipping; без group — для новых групп."""
    import voices
    try:
        policy = voices.validate_quality_policy(policy)
    except ValueError as ex:
        raise CoreError(str(ex)) from ex
    if not group:
        settings.update(explicit=("voice_quality_policy_default",), voice_quality_policy_default=policy)
        return {"default_for_new_groups": policy,
                "note": "Группы без явной политики наследуют это значение; уже выбранная политика группы не меняется."}
    if group not in paths.list_groups():
        raise CoreError("группа не найдена")
    try:
        with settings.group_voice_lock(group):
            current = settings.group_voice_settings(group)
            data = {k: v for k, v in current.items() if k != "audio_quality_policy_source"}
            data["audio_quality_policy"] = policy
            saved = settings.save_group_voice_settings(group, data)
    except (ValueError, OSError) as ex:
        raise CoreError(str(ex)) from ex
    return {"group": group, "audio_quality_policy": saved.get("audio_quality_policy", policy),
            "auto_learn": saved.get("auto_learn")}


REFRESH_MEANINGS = {
    "unchanged": "Источник учтён, отпечаток совпадает.",
    "changed": "Отпечаток стенограммы или метаданные аудио отличаются — нужен просмотр и подтверждённое обновление.",
    "legacy_baseline_pending": "Отпечатка нет; при следующем обучении текущее состояние будет принято как исходное.",
    "not_recorded": "Источник есть в профиле, но без записи происхождения.",
    "transcript_missing": "Файл стенограммы не найден.",
    "transcript_unreadable": "Стенограмма не читается.",
    "audio_missing": "Исходное аудио не найдено или не указано.",
}


def _voice_job_map():
    """{путь стенограммы: id задачи} по готовым задачам; нужен только для подсказки обновления."""
    out = {}
    try:
        files = sorted(paths.JOBS.glob("*.json"))
    except OSError:
        return out
    for p in files:
        try:
            status = json.loads(p.read_text(encoding="utf-8"))
            jp = status.get("json")
            if status.get("state") == "done" and jp:
                out[str(Path(jp).resolve())] = p.stem
        except (OSError, ValueError, TypeError):
            continue
    return out


def voice_refresh_plan(group=""):
    """Что изменилось после прежнего обучения; только чтение, без моделей и записи профиля."""
    import voices
    if group and group not in paths.list_groups():
        raise CoreError("группа не найдена")
    groups = [group] if group else paths.list_groups()
    jobs = _voice_job_map()
    out = []
    for name in groups:
        path = paths.voices_path(name)
        if not path.exists():
            out.append({"group": name, "profile": "absent", "sources": [], "summary": {"needs_update": 0}})
            continue
        try:
            data = voices.load_profile(path, convert=False)
        except voices.VoiceProfileError as ex:
            out.append({"group": name, "profile_error": str(ex)})
            continue
        entries = []
        for key, stored in sorted((data.get("sources") or {}).items()):
            jp = Path(key)
            audio_path, audio_key = None, None
            entry = {"transcript": jp.name, "recording": None, "job_id": jobs.get(str(jp.resolve())),
                     "status": "transcript_missing", "needs_update": True,
                     "transcript_changed": None, "audio_changed": None,
                     "comparison_basis": "unavailable", "legacy_mtime_changed": None,
                     "needs_review_reasons": []}
            if jp.is_file():
                try:
                    source_data = json.loads(jp.read_text(encoding="utf-8"))
                    audio_path = source_data.get("file")
                except (OSError, ValueError, TypeError):
                    entry["status"] = "transcript_unreadable"
                else:
                    if not audio_path:
                        entry["status"] = "audio_missing"
                    else:
                        audio_key = str(Path(audio_path).resolve())
                        entry["recording"] = Path(audio_path).name
                        if not Path(audio_path).is_file():
                            entry["status"] = "audio_missing"
                        else:
                            current = voices.source_fingerprint(jp, audio_path)
                            status = voices.source_fingerprint_status(stored, current, jp.stat().st_mtime)
                            entry["status"] = {"unchanged": "unchanged", "changed": "changed",
                                               "legacy_unchanged": "legacy_baseline_pending",
                                               "legacy_changed": "changed",
                                               "absent": "not_recorded"}.get(status, "changed")
                            if status in ("legacy_changed", "legacy_unchanged"):
                                entry["comparison_basis"] = "legacy_mtime"
                                entry["legacy_mtime_changed"] = status == "legacy_changed"
                            elif isinstance(stored, dict):
                                entry["comparison_basis"] = "fingerprint"
                                transcript_keys = ("transcript_sha256", "transcript_size")
                                audio_keys = ("audio_size", "audio_mtime")
                                if all(stored.get(k) is not None for k in transcript_keys):
                                    entry["transcript_changed"] = any(stored[k] != current[k] for k in transcript_keys)
                                if all(stored.get(k) is not None for k in audio_keys):
                                    entry["audio_changed"] = any(stored[k] != current[k] for k in audio_keys)
            versions = []
            if audio_key:
                for speaker in (data.get("speakers") or {}).values():
                    part = (speaker.get("parts") or {}).get(audio_key)
                    if part:
                        versions.append(part.get("selection_version"))
            current_selection = bool(versions) and all(v == voices.SELECTION_VERSION for v in versions)
            entry["selection_version_current"] = current_selection
            if entry["status"] == "changed":
                if entry["comparison_basis"] == "legacy_mtime":
                    entry["needs_review_reasons"].append("время стенограммы отличается от сохранённого старого значения")
                elif entry["comparison_basis"] == "unavailable":
                    entry["needs_review_reasons"].append("прежние данные источника не позволяют выполнить сравнение")
                else:
                    entry["needs_review_reasons"].append("отпечаток стенограммы или метаданные аудио отличаются")
            if versions and not current_selection:
                entry["needs_review_reasons"].append("вклад без текущей версии отбора")
            if entry["status"] in ("transcript_missing", "transcript_unreadable", "audio_missing"):
                entry["needs_review_reasons"].append(REFRESH_MEANINGS[entry["status"]])
            if entry["status"] == "not_recorded":
                entry["needs_review_reasons"].append("нет отпечатка источника")
            if entry["status"] == "legacy_baseline_pending":
                entry["needs_review_reasons"].append("нет постоянного отпечатка источника")
            if audio_key and audio_key in (data.get("audio_quality") or {}):
                q = data["audio_quality"][audio_key]
                entry["audio_quality_warnings"] = (q or {}).get("warnings") or []
                entry["audio_quality_notes"] = voices.explain_audio_quality(q)
                if entry["audio_quality_warnings"]:
                    entry["needs_review_reasons"].append("есть предупреждения качества аудио")
            entry["needs_update"] = bool(entry["needs_review_reasons"])
            entry["meaning"] = REFRESH_MEANINGS.get(entry["status"], entry["status"])
            if entry["status"] == "changed" and entry["comparison_basis"] == "legacy_mtime":
                entry["meaning"] = ("Время стенограммы отличается от старого значения; сравнить содержимое "
                                    "стенограммы и прежнее аудио невозможно. Нужен просмотр.")
            elif entry["status"] == "changed" and entry["comparison_basis"] == "unavailable":
                entry["meaning"] = "Прежние данные источника не позволяют выполнить сравнение; нужен просмотр."
            entries.append(entry)
        summary = {}
        for item in entries:
            summary[item["status"]] = summary.get(item["status"], 0) + 1
        summary["needs_update"] = sum(1 for item in entries if item["needs_update"])
        out.append({"group": name, "profile": "ok", "revision": data.get("revision"),
                    "sources": entries, "summary": summary})
    return {"groups": out,
            "needs_update": sum(item.get("summary", {}).get("needs_update", 0) for item in out),
            "note": "План только показывает различия. Автоматическая замена вкладов не выполняется; "
                    "обновляйте через preview_voice_profiles и update_voice_profiles отдельной задачи."}


def start_voice_update(snapshot):
    group, jp = snapshot["group"], Path(snapshot["json"])
    with settings.group_voice_lock(group):
        if _voice_hash(jp) != snapshot["source_hash"] or _voice_hash(paths.voices_path(group)) != snapshot["profile_hash"]:
            raise CoreError("стенограмма или профиль изменились; повторите просмотр и подтверждение")
        data = json.loads(jp.read_text(encoding="utf-8"))
        audio = data.get("file")
        if not audio or not Path(audio).is_file():
            raise CoreError("аудиофайл недоступен")
        if _voice_hash(audio) != snapshot["audio_hash"]:
            raise CoreError("аудиофайл изменился; повторите просмотр и подтверждение")
        args = ["--learn-only", str(jp), "--voice-source-hash", snapshot["source_hash"],
                "--voice-profile-hash", snapshot["profile_hash"], "--voice-audio-hash", snapshot["audio_hash"],
                "--voice-group", group]
        return {"job_id": launch("worker.py", args, audio, "learn"),
                "hint": "Ход — transcription_status(job_id)"}


def revoke_voice_source(snapshot):
    """Отозвать вклад источника после подтверждения; другие записи не затрагиваются."""
    group, jp = snapshot["group"], Path(snapshot["json"])
    path = paths.voices_path(group)
    with settings.group_voice_lock(group):
        if _voice_hash(jp) != snapshot["source_hash"] or _voice_hash(path) != snapshot["profile_hash"]:
            raise CoreError("стенограмма или профиль изменились; повторите предварительный просмотр и подтверждение")
        if not path.exists():
            raise CoreError("профиль отсутствует")
        data = json.loads(jp.read_text(encoding="utf-8"))
        key = str(Path(data["file"]).resolve())
        import voices
        try:
            profile = voices.load_profile(path, convert=False)
        except voices.VoiceProfileError as ex:
            raise CoreError(f"голосовой профиль группы несовместим или повреждён: {ex}. "
                            "Отзыв не выполнен; перенесите профиль отдельно.") from ex
        names = sorted(n for n, s in profile.get("speakers", {}).items() if key in s.get("parts", {}))
        if not names or names != snapshot["report"]["existing_contributions"]:
            raise CoreError("вклады изменились или отсутствуют; повторите просмотр")
        backup = backup_voice_profile(path)
        for name in names:
            profile["speakers"][name]["parts"].pop(key)
        profile.get("confusions", {}).pop(key, None)
        profile.get("sources", {}).pop(str(jp.resolve()), None)
        profile.get("source_selection_versions", {}).pop(str(jp.resolve()), None)
        profile["revision"] = int(profile.get("revision", 0)) + 1
        profile.setdefault("history", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   "from": jp.name, "kind": "source_revoked", "speakers": names})
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            json.dump(profile, f, ensure_ascii=False)
            tmp = f.name
        os.replace(tmp, path)
        return {"group": group, "revoked": names, "revision": profile["revision"], "backup": backup}


def set_voice_auto_learn(enabled, group=""):
    if type(enabled) is not bool:
        raise CoreError("значение должно быть true или false")
    if not group:
        try:
            for existing in paths.list_groups():
                with settings.group_voice_lock(existing):
                    settings.group_voice_settings(existing, initialize=True)
        except (ValueError, OSError) as ex:
            raise CoreError(f"не удалось сохранить настройки существующих групп: {ex}") from ex
        settings.update(explicit=("voice_auto_learn_default",), voice_auto_learn_default=enabled)
        return {"default_for_new_groups": enabled}
    if group not in paths.list_groups():
        raise CoreError("группа не найдена")
    try:
        with settings.group_voice_lock(group):
            current = settings.group_voice_settings(group)
            data = {k: v for k, v in current.items() if k != "audio_quality_policy_source"}
            data.update(auto_learn=enabled, reason="manual")
            return {"group": group, **settings.save_group_voice_settings(group, data)}
    except (ValueError, OSError) as ex:
        raise CoreError(str(ex)) from ex


def voice_profiles():
    """Сводка по голосовым профилям групп; сами отпечатки не выводятся."""
    import voices
    out = []
    for p in sorted(paths.GROUPS.glob("*/voices.json")):
        try:
            d = voices.load_profile(p, convert=False)
        except voices.VoiceProfileError as ex:
            out.append({"group": p.parent.name, "error": str(ex), "profile_error": str(ex)})
            continue
        conf = {}
        for pairs in (d.get("confusions") or {}).values():
            for k, v in pairs.items():
                conf[k.replace("|", " → ")] = conf.get(k.replace("|", " → "), 0) + v
        try:
            policy = settings.group_voice_settings(p.parent.name)
        except ValueError:
            policy = {"auto_learn": False, "reason": "settings_error"}
        vocab_file = p.parent / "vocab.json"
        try:
            vocab = json.loads(vocab_file.read_text(encoding="utf-8"))
            expected = [x["name"] for x in vocab.get("participants", []) if x.get("name")]
        except (OSError, ValueError, TypeError):
            expected = []
        out.append({"group": p.parent.name, "revision": d.get("revision"),
                    "auto_learn": policy, "sufficiency": voices.profile_sufficiency(d, expected),
                    "weight_diagnostics": voices.voice_weight_diagnostics(d),
                    "audio_quality_policy": policy.get("audio_quality_policy", "warn"),
                    "audio_quality_policy_source": policy.get("audio_quality_policy_source", "default"),
                    "speakers": {n: _speaker_voice_summary(s) for n, s in d.get("speakers", {}).items()},
                    "corrected_pairs": conf,
                    "warnings": (d.get("warnings") or [])[-5:],
                    "quality_summary": {"measured_sources": len(d.get("audio_quality") or {}),
                                        "clipping_sources": sum(
                                            1 for q in (d.get("audio_quality") or {}).values()
                                            if "clipping" in ((q or {}).get("warnings") or []))},
                    "quarantine": [{"name": n, "recording": Path(a).name, **{k: v for k, v in q.items() if k != "json"}}
                                   for a, qa in (d.get("quarantine") or {}).items() for n, q in qa.items()],
                    "identity": {Path(a).name: v for a, v in (d.get("identity") or {}).items()},
                    "sources": list(d.get("sources", {}))})
    return {"profiles": out, "groups": paths.list_groups(),
            "default_group": settings.load_config().get("default_group")}


def _speaker_voice_summary(speaker):
    """Длительность и вес раздельно: старый `sec` не подменяет неизмеренную длительность."""
    seconds = weighted = human = 0.0
    measured = unmeasured = 0
    for part in (speaker.get("parts") or {}).values():
        sec = part.get("sec")
        if isinstance(sec, (int, float)) and not isinstance(sec, bool) and sec >= 0:
            weighted += float(sec)
        a, h = part.get("auto_sec"), part.get("human_sec")
        if (isinstance(a, (int, float)) and not isinstance(a, bool)
                and isinstance(h, (int, float)) and not isinstance(h, bool) and a >= 0 and h >= 0):
            seconds += float(a) + float(h)
            human += float(h)
            measured += 1
        else:
            unmeasured += 1
    return {"records": len(speaker.get("parts") or {}),
            "seconds": round(seconds, 1),
            "weighted_seconds": round(weighted, 1),
            "human_seconds": round(human, 1),
            "measured_records": measured,
            "unmeasured_records": unmeasured}


# ---------- данные: выполнение (решение человека принимает слой управления) ----------
def do_migrate(dry_run=True):
    out = {"data_dir": str(paths.DATA_DIR), "old_data_dirs": [str(p) for p in paths.old_default_dirs()],
           "env": {k: os.environ.get(k) for k in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "RUS_TRANSCRIBER_DATA")},
           "home": str(Path.home())}
    try:
        out["result"] = migrate.run(dry=dry_run)
    except Exception as ex:
        out["error"] = f"{type(ex).__name__}: {ex}"
    try:
        out["log_tail"] = migrate.LOG.read_text(encoding="utf-8").splitlines()[-12:]
    except OSError:
        out["log_tail"] = []
    return out


def _data(fn, *a):
    try:
        return fn(*a)
    except RuntimeError as ex:
        raise CoreError(str(ex)) from ex


def do_export(group="", dest_dir="", with_jobs=False):
    return _data(data_io.export_data, dest_dir or None, group or None, with_jobs)


def do_import(zip_path, mode="merge", apply=False):
    return _data(data_io.import_data, zip_path, mode, apply)


def do_delete_group(group, confirm="", permanent=False):
    return _data(data_io.delete_group, group, confirm, permanent)


def do_cleanup(days=30, apply=False):
    return data_io.cleanup_jobs(days, apply)


# ---------- описание действия для человека (без выполнения) ----------
def describe_export(group="", dest_dir="", with_jobs=False):
    groups = [group] if group else paths.list_groups()
    return ["Выгрузка архива с ГОЛОСОВЫМИ ОТПЕЧАТКАМИ (биометрия) и словарями.",
            f"Группы: {', '.join(groups) or '—'}",
            f"Статусы задач: {'да' if with_jobs else 'нет'}",
            f"Куда: {dest_dir or str(paths.DATA_DIR / 'exports')}"]


def describe_import(zip_path, mode="merge"):
    rep = do_import(zip_path, mode, False)            # только отчёт, ничего не меняется
    lines = [f"Загрузка архива {Path(zip_path).name}, режим: {mode}."]
    for g, r in rep.get("groups", {}).items():
        lines.append(f"Группа «{g}»: {r.get('action')}; словарь: {r.get('vocab', '—')}; голоса: {r.get('voices', '—')}")
    if mode == "replace":
        lines.append("Прежняя версия заменяемой группы уйдёт в резервную копию.")
    return lines


def describe_delete(group, permanent=False):
    g = paths.safe_group(group)
    gd = paths.group_dir(g)
    if not gd.is_dir():
        raise CoreError(f"Группа «{group}» не найдена (есть: {', '.join(paths.list_groups())})")
    n = sum(1 for _ in gd.rglob("*") if _.is_file())
    return [f"Удаление группы «{g}» (файлов: {n}).",
            "НАВСЕГДА: восстановить будет нельзя." if permanent else "Папка будет перенесена в _deleted (можно вернуть)."]


def describe_cleanup(days=30):
    r = do_cleanup(days, False)
    return [f"Удаление статусов и логов задач старше {days} дн.: файлов {r['files']}, {r['bytes'] // 1024} КБ."]


def describe_migrate():
    r = do_migrate(True)
    res = r.get("result")
    return ["Перенос данных в папку данных из прежних мест.", f"Папка данных: {r['data_dir']}",
            f"План: {json.dumps(res, ensure_ascii=False)[:300] if res is not None else r.get('error', '—')}"]
