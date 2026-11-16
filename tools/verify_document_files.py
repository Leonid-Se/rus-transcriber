"""Проверить зарегистрированные файлы документов без изменений."""
import argparse
import json
import sys

import documents
import storage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="путь к SQLite базе прототипа")
    parser.add_argument("--root-id", required=True, help="UUID зарегистрированного корня")
    parser.add_argument("--root", required=True, help="абсолютный путь этого корня")
    args = parser.parse_args(argv)

    store = None
    try:
        store = storage.open_store_readonly(args.db)
        report = documents.verify_files(store, args.root_id, args.root)
    except (storage.StorageError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
