"""Словарь группы: участники (имя, пол), имена и названия, явные замены.

Поиск словаря ограничен явным путём, sidecar в папке записи и двух предках, точной папкой
известного результата и прямой ссылкой quality.vocab. DATA_DIR — только fallback для
однозначной группы. Несколько разных подходящих файлов или противоречие групп требуют
явного решения; порядок обхода не выбирает словарь. Произвольные вложенные папки не обходятся.
Голосовые профили в словарь не входят (см. voices.py): они хранятся только на этом компьютере.

Формат:
{
  "group": "имя-группы",                      — к нему привязаны голосовые профили
  "participants": [{"name": "Анна", "gender": "ж", "role": "ведущая"}, ...],
  "terms": ["Вереск", "Лисицын", ...],     — имена и названия: близкие по звучанию искажения
                                                исправляются автоматически
  "replace": {"Борька": "Борис", ...},         — явные замены целого слова или фразы (всегда)
  "replace_if": [                              — замены с условием: для слов, которые бывают и обычными
    {"from": "весна", "to": "Весна",             (слово «весна» остаётся, название — после предлога)
     "after": ["в", "на", "про", ...],         — только после одного из этих слов
     "not_before": ["богу"],                   — не перед этими словами
     "case": true}                             — с учётом регистра («Весна», но не «весна»)
  ]
}
"""
import difflib
import json
import re
from pathlib import Path

FILE_NAME = "rus-transcriber.vocab.json"
WORD = re.compile(r"[А-Яа-яЁёA-Za-z]+(?:-[А-Яа-яЁё]+)*")
_PH = str.maketrans({"ё": "и", "е": "и", "я": "и", "ы": "и", "э": "и", "й": "и", "о": "а", "ю": "у",
                     "б": "п", "в": "ф", "г": "к", "д": "т", "ж": "ш", "з": "с", "ь": None, "ъ": None})


class ResolutionError(RuntimeError):
    """Отказ выбора словаря с ограниченным отчётом, не содержащим его содержимое."""

    def __init__(self, message, report):
        self.report = report
        details = []
        for item in report.get("candidates", [])[:8]:
            source = item.get("source")
            if isinstance(source, list):
                source = ", ".join(source)
            part = f"{source}: {item.get('status')} — {item.get('path') or 'путь некорректен'}"
            if item.get("group"):
                part += f" (группа {item['group']})"
            details.append(part)
        binding = report.get("binding") or {}
        if binding.get("conflict"):
            details.append(f"group={binding.get('embedded_group')}, "
                           f"quality.vocab group={binding.get('vocab_group')} — {binding.get('vocab_path')}")
        elif binding.get("vocab_path") and binding.get("vocab_status") != "valid":
            details.append(f"quality.vocab: {binding['vocab_status']} — {binding['vocab_path']}")
        super().__init__(message + (": " + "; ".join(details) if details else ""))


def _group_key(group):
    if not isinstance(group, str) or not group.strip():
        return None
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in group.strip())


def _path_key(path):
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        p = p.absolute()
    return str(p).casefold()


def validate(data):
    """Проверяет внешнюю форму словаря, не возвращая его содержимое в диагностике."""
    if not isinstance(data, dict):
        return "root_not_object"
    if "group" in data and (not isinstance(data["group"], str) or not data["group"].strip()):
        return "invalid_group"
    if not isinstance(data.get("participants", []), list):
        return "invalid_participants"
    for participant in data.get("participants", []):
        if not isinstance(participant, dict) or (
                "name" in participant and not isinstance(participant["name"], str)) or (
                "gender" in participant and not isinstance(participant["gender"], str)):
            return "invalid_participant"
    if not isinstance(data.get("terms", []), list) or any(
            not isinstance(term, str) for term in data.get("terms", [])):
        return "invalid_terms"
    if not isinstance(data.get("replace", {}), dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in data.get("replace", {}).items()):
        return "invalid_replace"
    if not isinstance(data.get("replace_if", []), list) or any(
            not isinstance(rule, dict) for rule in data.get("replace_if", [])):
        return "invalid_replace_if"
    return None


def inspect(path, source):
    """Возвращает метаданные кандидата и данные отдельно; в отчёт словарь не попадает."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        return {"path": None, "source": source, "status": "invalid",
                "group": None, "reason": "invalid_path"}, None
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        pass
    item = {"path": str(p), "source": source, "status": "missing", "group": None}
    try:
        if not p.is_file():
            return item, None
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        item.update(status="invalid", reason="invalid_json")
        return item, None
    except (OSError, UnicodeError):
        item.update(status="invalid", reason="unreadable")
        return item, None
    problem = validate(data)
    if problem:
        item.update(status="invalid", reason=problem)
        return item, None
    item.update(status="valid", group=data.get("group"))
    return item, data


def resolve_candidates(candidates, expected_group=None, allow_ungrouped=False):
    """Однозначно разрешает список точечных кандидатов; никогда не выбирает первый.

    candidates — пары (путь, источник). Одинаковые нормализованные пути объединяются.
    """
    report = {"candidates": [], "selected": None, "group": None, "selection_reason": None}
    unique = {}
    for path, source in candidates:
        key = _path_key(path)
        if key not in unique:
            unique[key] = [Path(path).expanduser(), []]
        if source not in unique[key][1]:
            unique[key][1].append(source)

    inspected = []
    for path, sources in unique.values():
        item, data = inspect(path, sources[0] if len(sources) == 1 else sources)
        report["candidates"].append(item)
        if item["status"] == "valid":
            inspected.append((item, data))
    report["expected_group"] = expected_group

    invalid = [item for item in report["candidates"] if item["status"] == "invalid"]
    if invalid:
        report["selection_reason"] = "invalid_candidate"
        raise ResolutionError("Найден некорректный словарь; выбор остановлен", report)

    if expected_group:
        expected_key = _group_key(expected_group)
        conflicts = [item for item, _ in inspected
                     if item.get("group") and _group_key(item["group"]) != expected_key]
        if conflicts:
            report["selection_reason"] = "group_conflict"
            raise ResolutionError("Группа словаря не совпадает с выбранной группой", report)
        unknown = [item for item, _ in inspected if not item.get("group")]
        if unknown and not allow_ungrouped:
            report["selection_reason"] = "unknown_group"
            raise ResolutionError("Группа одного из словарей неизвестна; требуется явный выбор", report)
        group = expected_group
    else:
        groups = {_group_key(item["group"]) for item, _ in inspected if item.get("group")}
        if len(groups) > 1:
            report["selection_reason"] = "group_conflict"
            raise ResolutionError("Найдены словари разных групп; требуется явный выбор", report)
        if any(not item.get("group") for item, _ in inspected) and not allow_ungrouped:
            report["selection_reason"] = "unknown_group"
            raise ResolutionError("Группа словаря неизвестна; требуется явный выбор", report)
        group = next((item["group"] for item, _ in inspected if item.get("group")), None)

    if len(inspected) > 1:
        report["selection_reason"] = "multiple_candidates"
        raise ResolutionError("Найдено несколько словарей; укажите словарь или группу явно", report)
    if not inspected:
        report["group"] = group
        report["selection_reason"] = "no_dictionary"
        return None, {}, group, report

    item, data = inspected[0]
    if not item.get("group") and not allow_ungrouped:
        report["selection_reason"] = "unknown_group"
        raise ResolutionError("Группа словаря неизвестна; требуется явный выбор", report)
    report["selected"] = item["path"]
    report["group"] = group or item.get("group")
    report["selection_reason"] = "explicit_candidate" if allow_ungrouped else "unique_group_candidate"
    return Path(item["path"]), data, report["group"], report


def transcript_binding(data):
    """Сверяет встроенную группу и прямую ссылку quality.vocab без поиска по дереву."""
    if not isinstance(data, dict):
        raise ValueError("Расшифровка должна быть объектом")
    embedded = data.get("group")
    if embedded is not None and _group_key(embedded) is None:
        raise ValueError("Некорректная группа в расшифровке")
    quality = data.get("quality") or {}
    if not isinstance(quality, dict):
        raise ValueError("Некорректное поле quality в расшифровке")
    ref = quality.get("vocab")
    result = {"group": embedded, "embedded_group": embedded, "vocab_path": None,
              "vocab_group": None, "vocab_status": "not_referenced", "conflict": False}
    if not ref:
        return result
    if not isinstance(ref, (str, Path)) or not str(ref).strip():
        result.update(vocab_status="invalid", vocab_reason="invalid_path")
        return result
    item, vocab = inspect(ref, "quality.vocab")
    result.update(vocab_path=item["path"], vocab_status=item["status"],
                  vocab_group=item.get("group"), vocab_reason=item.get("reason"))
    if item["status"] == "valid":
        referenced_group = item.get("group")
        if embedded and referenced_group and _group_key(embedded) != _group_key(referenced_group):
            result["conflict"] = True
        else:
            result["group"] = embedded or referenced_group
    return result


def related_candidates(audio_path=None, transcript_path=None, quality_path=None):
    """Точечные известные словари без обхода произвольных вложенных каталогов."""
    candidates = []
    if quality_path:
        candidates.append((Path(quality_path), "quality.vocab"))
    if audio_path:
        audio = Path(audio_path).expanduser()
        directory = audio.resolve().parent
        candidates.extend((parent / FILE_NAME, "audio_sidecar")
                          for parent in [directory, *list(directory.parents)[:2]])
        candidates.append((directory / (audio.stem + ".rus-transcriber") / FILE_NAME,
                           "known_output_sidecar"))
    if transcript_path:
        candidates.append((Path(transcript_path).expanduser().resolve().parent / FILE_NAME,
                           "transcript_sidecar"))
    return candidates


def resolve_group_vocab(audio_path, transcript_path, transcript_data, group, group_vocab_path):
    """Строго разрешает канонический vocab выбранной группы с проверкой всех известных ссылок."""
    binding = transcript_binding(transcript_data)
    if binding["conflict"]:
        raise ResolutionError("Группа расшифровки противоречит quality.vocab",
                              {"binding": binding, "candidates": []})
    if binding["vocab_status"] == "invalid":
        raise ResolutionError("Ссылка quality.vocab повреждена или невалидна",
                              {"binding": binding, "candidates": []})
    if binding["group"] and _group_key(binding["group"]) != _group_key(group):
        raise ResolutionError("Выбранная группа противоречит привязке расшифровки",
                              {"binding": binding, "candidates": []})

    candidates = related_candidates(audio_path, transcript_path, binding["vocab_path"])
    candidates.append((group_vocab_path, "selected_group_vocab"))
    path, data, resolved_group, report = resolve_candidates(candidates, expected_group=group)
    report["binding"] = binding
    canonical_key = _path_key(group_vocab_path)
    if path is None:
        report["selection_reason"] = "selected_group_vocab_missing"
        raise ResolutionError("Словарь выбранной группы отсутствует или не найден", report)
    if _path_key(path) != canonical_key:
        report["selection_reason"] = "selected_group_vocab_not_canonical"
        raise ResolutionError("Для ручного выбора требуется канонический словарь группы", report)
    if not data.get("group") or _group_key(data["group"]) != _group_key(group):
        report["selection_reason"] = "selected_group_vocab_mismatch"
        raise ResolutionError("Канонический словарь не подтверждает выбранную группу", report)
    report["group"] = resolved_group
    report["selection_reason"] = "selected_group_vocab"
    return path, data, resolved_group, report, binding


def find(audio_path=None, explicit=None, default_file=None):
    """Возвращает (путь, данные) или (None, {}); неоднозначность не разрешается порядком обхода."""
    if explicit:
        candidates = [(Path(explicit).expanduser(), "explicit")]
        path, data, _, _ = resolve_candidates(candidates, allow_ungrouped=True)
        return path, data
    candidates = []
    if audio_path:
        directory = Path(audio_path).resolve().parent
        candidates.extend((parent / FILE_NAME, "audio_sidecar")
                          for parent in [directory, *list(directory.parents)[:2]])
    if default_file:
        candidates.append((Path(default_file), "default_file"))
    path, data, _, _ = resolve_candidates(candidates)
    return path, data


def genders(vocab):
    """{имя: 'f'|'m'} из participants."""
    out = {}
    for p in vocab.get("participants", []):
        g = str(p.get("gender", "")).strip().lower()[:1]
        if g in ("ж", "f", "w"):
            out[p["name"]] = "f"
        elif g in ("м", "m"):
            out[p["name"]] = "m"
    return out


def _ph(w):
    s = w.lower().translate(_PH)
    return re.sub(r"(.)\1+", r"\1", s)


def _low(w):
    return str(w).lower().replace("ё", "е").strip()


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def _lastword(t):
    m = WORD.findall(str(t or ""))
    return m[-1] if m else None


def _firstword(t):
    m = WORD.findall(str(t or ""))
    return m[0] if m else None


class Fixer:
    def __init__(self, vocab):
        self.replace = []
        for k, v in (vocab.get("replace") or {}).items():
            k = str(k).strip()
            if k:
                body = re.sub(r"[её]", "[её]", re.escape(k), flags=re.I)
                pat = re.compile(r"(?<![А-Яа-яЁёA-Za-z])" + body + r"(?![А-Яа-яЁёA-Za-z])", re.I)
                self.replace.append((pat, str(v), k))
        self.cond = []
        for r in vocab.get("replace_if") or []:
            if r.get("from") and r.get("to"):
                self.cond.append({"from": str(r["from"]).strip(), "to": str(r["to"]),
                                  "case": bool(r.get("case")),
                                  "after": {_low(x) for x in r.get("after") or []},
                                  "not_after": {_low(x) for x in r.get("not_after") or []},
                                  "not_before": {_low(x) for x in r.get("not_before") or []}})
        terms = list(vocab.get("terms") or [])
        terms += [p["name"] for p in vocab.get("participants", []) if p.get("name")]
        self.terms = [(t, _ph(t)) for t in dict.fromkeys(terms) if " " not in t and len(t) >= 4]

    @staticmethod
    def _stem(term):
        return term[:-1] if len(term) >= 4 and term[-1].lower() in "аяоеиыйь" else term

    def _best(self, tok):
        """Лучшая замена для слова или None.

        Правила (подобраны на расшифровках 05.11 и 06.11, чтобы не трогать обычные слова):
          * падежная форма термина («Никитой», «Эврики», «Сень») не меняется, только регистр;
          * слово со строчной буквы сравнивается только целиком, от 6 букв, порог 0.92;
          * с заглавной — порог 0.85, допускается окончание до 3 букв;
          * кроме звучания сверяются и сами буквы (≥0.7), иначе «Берег» превращался бы в «Борис».
        """
        if len(tok) < 4:
            return None
        cap = tok[0].isupper()
        low = tok.lower()
        nt = _ph(tok)
        best = None
        for term, nterm in self.terms:
            stem = self._stem(term).lower()
            if low.startswith(stem) and len(low) - len(stem) <= 3:
                # та же основа: меняем только регистр, если термин — имя собственное
                if term[0].isupper() and not cap and low.startswith(term.lower()):
                    rep = term[0] + tok[1:]
                    if rep != tok:
                        return (1.0, rep)
                return None
            if nt[:1] != nterm[:1]:
                continue
            if not cap and len(tok) < 6:
                continue
            r1 = _ratio(nt, nterm) if _ratio(low, term.lower()) >= 0.7 else 0.0
            r2 = 0.0
            extra = len(tok) - len(term)
            if cap and 0 < extra <= 3 and _ratio(low[:len(term)], term.lower()) >= 0.7:
                r2 = _ratio(_ph(tok[:len(term)]), nterm)
            r = max(r1, r2)
            if r >= (0.85 if cap else 0.92) and (best is None or r > best[0]):
                best = (r, term if r1 >= r2 else term + tok[len(term):])
        if best and best[1] != tok:
            return best
        return None

    def _conditional(self, tok, prev, nxt):
        for c in self.cond:
            if not (tok == c["from"] if c["case"] else _low(tok) == _low(c["from"])):
                continue
            p, n = _low(prev or ""), _low(nxt or "")
            if c["after"] and p not in c["after"]:
                continue
            if p in c["not_after"] or n in c["not_before"]:
                continue
            return c["to"]
        return None

    def _guarded(self, tok):
        return any(_low(tok) == _low(c["from"]) for c in self.cond)

    def apply(self, text, prev=None, nxt=None):
        """-> (новый текст, [(было, стало), ...]). prev / nxt — соседние слова, когда text — одно слово
        (для замен с условием)."""
        fixes = []
        for pat, val, key in self.replace:
            def sub(m):
                fixes.append((m.group(0), val))
                return val
            text = pat.sub(sub, text)
        out, pos = [], 0
        ms = list(WORD.finditer(text))
        for k, m in enumerate(ms):
            tok = m.group(0)
            c = self._conditional(tok, ms[k - 1].group(0) if k else _lastword(prev),
                                  ms[k + 1].group(0) if k + 1 < len(ms) else _firstword(nxt)) \
                if self.cond else None
            # слово из замены с условием меняется только по условию: похожесть на термин его не трогает
            b = (1.0, c) if c else None if self._guarded(tok) else self._best(tok)
            out.append(text[pos:m.start()])
            if b:
                out.append(b[1])
                fixes.append((tok, b[1]))
            else:
                out.append(tok)
            pos = m.end()
        out.append(text[pos:])
        return "".join(out), fixes
