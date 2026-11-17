"""Схема ограниченного модельного задания и проверка ссылок на слова."""


def build(recording_id, source_revision, words, goal="", review_revision=1):
    """Сформировать задание только из слов и их устойчивых идентификаторов."""
    if not isinstance(recording_id, str) or not recording_id:
        raise ValueError("требуется идентификатор записи")
    if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
        raise ValueError("требуется ревизия источника")
    if type(review_revision) is not int or review_revision < 1:
        raise ValueError("требуется ревизия сомнений")
    if not isinstance(words, list):
        raise ValueError("слова должны быть списком")
    if not isinstance(goal, str) or len(goal) > 2000:
        raise ValueError("некорректная цель")
    result, seen = [], set()
    for word in words:
        if (not isinstance(word, dict) or not {"word_id", "text"} <= set(word)
                or set(word) - {"word_id", "text", "uncertain"}
                or not isinstance(word.get("uncertain", False), bool)):
            raise ValueError("слово должно содержать только ID, текст и признак сомнения")
        ident, text = word["word_id"], word["text"]
        if not isinstance(ident, str) or not ident or ident in seen or not isinstance(text, str):
            raise ValueError("некорректный или повторный ID слова")
        seen.add(ident)
        result.append({"word_id": ident, "text": text, "uncertain": word.get("uncertain", False)})
    return {"kind": "conspect", "recording_id": recording_id,
            "source_revision": source_revision, "review_revision": review_revision, "goal": goal, "words": result}


def validate(task, response, *, current_revision):
    """Проверить ссылки и ревизию; совпадение ссылок не доказывает смысл ответа."""
    if not isinstance(task, dict):
        raise ValueError("некорректное задание")
    checked = build(task.get("recording_id"), task.get("source_revision"), task.get("words"),
                    task.get("goal"), task.get("review_revision"))
    if task != checked:
        raise ValueError("неизвестные поля задания")
    if type(current_revision) is not int or current_revision != checked["source_revision"]:
        raise ValueError("ревизия источника изменилась")
    if not isinstance(response, dict) or set(response) != {"source_revision", "sections"}:
        raise ValueError("неверный формат ответа")
    if (type(response["source_revision"]) is not int or response["source_revision"] != current_revision
            or not isinstance(response["sections"], list)):
        raise ValueError("неверная ревизия или разделы")
    known = {word["word_id"] for word in checked["words"]}
    if not response["sections"]:
        raise ValueError("ответ не содержит разделов")
    sections, covered = [], set()
    for section in response["sections"]:
        if not isinstance(section, dict) or set(section) != {"text", "evidence"}:
            raise ValueError("неверный формат раздела")
        text, evidence = section["text"], section["evidence"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("пустой раздел")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(i, str) or i not in known for i in evidence)
                or len(set(evidence)) != len(evidence)):
            raise ValueError("неверные ссылки на слова")
        covered.update(evidence)
        sections.append({"text": text, "evidence": list(evidence)})
    if not known or covered != known:
        raise ValueError("не все слова источника охвачены ссылками ответа")
    return {"source_revision": current_revision, "sections": sections,
            "actor_kind": "model", "human_verification": "none"}
