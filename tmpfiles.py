"""Временные папки программы: создаются через scratch(), удаляются при выходе процесса, а остатки от
принудительно завершённых процессов (taskkill /F, отключение питания) убирает sweep_stale().

Раньше mkdtemp() без очистки оставлял в %TEMP% папки emo_*, bench_* (с копией всего звука), gigaam_*, vr_*.
"""
import atexit
import shutil
import tempfile
import time
from pathlib import Path

# Только префиксы, которые создаёт сама программа. Папки данных (rt-data-*) сюда не входят.
PREFIXES = ("emo_", "bench_", "gigaam_", "vr_")
_made = {}
_ACTIVE_LOCK = ".rus-transcriber-active.lock"


def _rm(p):
    shutil.rmtree(p, ignore_errors=True)


def scratch(prefix):
    """Новая временная папка; удаляется при завершении процесса (и вручную через drop())."""
    if prefix not in PREFIXES:
        raise ValueError(f"неизвестный префикс временной папки: {prefix}")
    p = Path(tempfile.mkdtemp(prefix=prefix))
    import jobslots
    lock = (p / _ACTIVE_LOCK).open("a+b")
    if not jobslots._try_lock(lock):
        lock.close()
        _rm(p)
        raise RuntimeError("не удалось пометить временную папку активной")
    _made[str(p.resolve())] = lock
    return p


def drop(p):
    lock = _made.pop(str(Path(p).resolve()), None)
    if lock is not None:
        lock.close()
    _rm(p)


@atexit.register
def _cleanup_all():
    for name, lock in list(_made.items()):
        lock.close()
        _rm(Path(name))
    _made.clear()


def sweep_stale(max_age_hours=12, now=None):
    """Удаляет папки программы в %TEMP% старше max_age_hours. Возвращает число удалённых папок.
    Берёт только папки с префиксами программы; задачи длиннее 12 часов не бывает (слот тяжёлых задач один)."""
    now = time.time() if now is None else now
    n = 0
    try:
        base = Path(tempfile.gettempdir())
        for d in base.iterdir():
            if d.is_dir() and d.name.startswith(PREFIXES):
                lock = None
                try:
                    resolved = str(d.resolve())
                    if resolved in _made:
                        continue
                    directory_mtime = d.stat().st_mtime
                    lock_path = d / _ACTIVE_LOCK
                    lock = lock_path.open("a+b")
                    import jobslots
                    if not jobslots._try_lock(lock):
                        continue
                    latest_mtime = directory_mtime
                    for entry in d.rglob("*"):
                        if entry == lock_path:
                            continue
                        latest_mtime = max(latest_mtime, entry.stat().st_mtime)
                    lock.close()
                    lock = None
                    if now - latest_mtime > max_age_hours * 3600:
                        _rm(d)
                        if not d.exists():
                            n += 1
                except OSError:
                    pass
                finally:
                    if lock is not None:
                        lock.close()
    except OSError:
        pass
    return n
