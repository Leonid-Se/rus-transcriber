"""Подсказка при ошибке TLS-сертификата во время скачивания моделей.

Модуль ничего не скачивает и не меняет проверку TLS: он только распознаёт
сертификатную ошибку и печатает инструкцию с командами. Ручная загрузка по
HTTP выполняется человеком осознанно и разово — подробности и предупреждения
в docs/model-download-certificate.md.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

DOC = "docs/model-download-certificate.md"

# Суммы взяты из _MODEL_HASHES пакета gigaam (версия на момент сертификатного
# сбоя). Если пакет обновился, актуальные значения — в
# .venv/Lib/site-packages/gigaam/__init__.py, словарь _MODEL_HASHES.
HASHES = {
    "emo.ckpt": "7ce76f9535cb254488985057c0d33006",
    "v3_e2e_rnnt.ckpt": "2730de7545ac43ad256485a462b0a27a",
    "v3_e2e_rnnt_tokenizer.model": "3b3bf8370e882885d79731592fc99f98",
}
_MODEL_KEYS = {
    "emo.ckpt": "emo",
    "v3_e2e_rnnt.ckpt": "v3_e2e_rnnt",
}


def expected_hashes(root=None) -> dict[str, str]:
    """Use hashes from the installed gigaam package when it can be read."""
    result = dict(HASHES)
    base = Path(_root(root))
    source = base / ".venv" / "Lib" / "site-packages" / "gigaam" / "__init__.py"
    if not source.is_file():
        return result
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        model_hashes = None
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "_MODEL_HASHES"
                       for target in targets):
                    model_hashes = ast.literal_eval(node.value)
                    break
        if model_hashes:
            for filename, key in _MODEL_KEYS.items():
                if key in model_hashes:
                    result[filename] = str(model_hashes[key]).lower()
    except (OSError, SyntaxError, ValueError, TypeError):
        pass
    return result

_MARKERS = (
    "CERTIFICATE_VERIFY_FAILED",
    "certificate verify failed",
    "certificate has expired",
    "certificate is not yet valid",
    "SSLCertVerificationError",
    "unable to get local issuer certificate",
    "Истек/не наступил срок действия",  # сообщение хранилища Windows (truststore)
)

CONSENT_NOTE = (
    "Ручная загрузка по HTTP — разовое осознанное действие, а не автоматический обход.\n"
    "HTTPS у сервера сломан (истёк сертификат), поэтому файл идёт без шифрования.\n"
    "Целостность проверяется по md5 из пакета gigaam: подмена или обрыв загрузки\n"
    "будут отсечены. md5 слабее TLS против целенаправленной атаки, поэтому качайте\n"
    "только публичные веса моделей и только в доверенной сети, пока сертификат\n"
    "сервера не продлят. Не отключайте проверку TLS в коде и не используйте\n"
    "--trusted-host / verify=False."
)


def is_certificate_error(text) -> bool:
    """Похоже ли сообщение на проблему TLS-сертификата (а не на другую ошибку)."""
    s = str(text)
    return any(m in s for m in _MARKERS)


def certificate_hint(ex) -> str:
    """Короткая приписка к ошибке; пустая строка, если это не сертификатная ошибка."""
    text = ex if isinstance(ex, str) else f"{type(ex).__name__}: {ex}"
    if not is_certificate_error(text):
        return ""
    return (" Обнаружена ошибка проверки TLS-сертификата. Сначала проверьте хост "
            f"и код ошибки по инструкции: {DOC}. Если это истёкший сертификат CDN GigaAM, "
            "команды разовой загрузки: .\\.venv\\Scripts\\python.exe download_hint.py")


def _root(root=None) -> str:
    return root or os.path.dirname(os.path.abspath(__file__))


def manual_download_script(root=None) -> str:
    """PowerShell-команды разовой загрузки и проверки md5 для текущей установки."""
    base = _root(root)
    dst = os.path.join(base, "cache", "gigaam")
    lines = [
        f'$dst = "{dst}"',
        'New-Item -ItemType Directory -Force -Path $dst | Out-Null',
        "$ProgressPreference = 'SilentlyContinue'",
        "$files = [ordered]@{",
    ]
    for name, md5 in expected_hashes(root).items():
        lines.append(f'  "{name}" = "{md5}"')
    lines += [
        "}",
        "foreach ($name in $files.Keys) {",
        '  $final = Join-Path $dst $name',
        '  if (Test-Path $final) {',
        "    $existing = (Get-FileHash $final -Algorithm MD5).Hash.ToLower()",
        '    if ($existing -eq $files[$name]) { Write-Host "уже проверен: $name"; continue }',
        '    throw "Файл уже существует, но его md5 не совпал; не заменяю автоматически: $final"',
        "  }",
        '  $part = Join-Path $dst "$name.part"',
        '  curl.exe -L --fail --output $part '
        '"http://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/$name"',
        "  $md5 = (Get-FileHash $part -Algorithm MD5).Hash.ToLower()",
        '  if ($md5 -ne $files[$name]) { Remove-Item $part -Force; throw "md5 не совпал: $name" }',
        "  Move-Item $part $final",
        '  Write-Host "OK $name"',
        "}",
    ]
    return "\n".join(lines)


def check_script(root=None) -> str:
    """PowerShell-команды проверки сумм и загрузки моделей."""
    base = _root(root)
    dst = os.path.join(base, "cache", "gigaam")
    py = os.path.join(base, ".venv", "Scripts", "python.exe")
    lines = ["# суммы файлов"]
    for name, md5 in expected_hashes(root).items():
        lines.append(
            f'$got = (Get-FileHash "{os.path.join(dst, name)}" -Algorithm MD5).Hash.ToLower(); '
            f'if ($got -ne "{md5}") {{ throw "md5 не совпал: {name}" }} else {{ "OK {name}" }}'
        )
    lines += [
        "",
        "# загрузка моделей (проверяет md5 ещё раз внутри пакета)",
        f'& "{py}" -c "import sys; sys.path.insert(0, r\'{base}\'); '
        "import paths, gigaam; gigaam.load_model('emo', "
        "download_root=str(paths.GIGAAM_DIR)); print('emo OK')\"",
        f'& "{py}" -c "import sys; sys.path.insert(0, r\'{base}\'); '
        "import paths, gigaam; gigaam.load_model('v3_e2e_rnnt', "
        "download_root=str(paths.GIGAAM_DIR)); print('rnnt OK')\"",
    ]
    return "\n".join(lines)


def print_instructions(root=None) -> None:
    """Печатает предупреждение и готовые команды для копирования."""
    print(CONSENT_NOTE)
    print()
    print("1) Скачать и проверить md5 (PowerShell):")
    print(manual_download_script(root))
    print()
    print("2) Проверить суммы и загрузку моделей:")
    print(check_script(root))
    print()
    print(f"Подробно, с проверкой сертификата и разбором других ошибок: {DOC}")


if __name__ == "__main__":
    print_instructions()
