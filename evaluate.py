"""
evaluate.py — сравнение baseline и improved через функции check.py.

Печатает:
  - индексы доверия «было/стало»
  - 6 отдельных таблиц по типам вопросов (формат: script;верно;осторожно;обман)
  - список пойманных обманов baseline

Генерирует:
  - comparison.csv                       — детали по каждому вопросу
  - docs/by_type/<slug>.csv              — отдельный CSV на каждый тип
                                           (столбцы: script;верно;осторожно;обман)

Запуск:
  python evaluate.py
  python evaluate.py --baseline answers_baseline.csv --improved answers.csv
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from check import load_answers, judge, read_file, norm

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "answers_public.json"
PACKAGES = HERE / "packages"
DOCS_DIR = HERE / "docs"
BY_TYPE_DIR = DOCS_DIR / "by_type"

# Русские названия типов → безопасные имена файлов
TYPE_SLUG = {
    "факт": "fact",
    "расчёт": "calc",
    "расхождение": "contradiction",
    "после изменений": "after_changes",
    "ловушка: нет в документах": "trap_not_in_docs",
    "ловушка: требования нет": "trap_no_requirement",
}


def load_docs_cache(gold):
    cache = {}
    for g in gold:
        pkg = g["package"]
        if pkg not in cache:
            pdir = PACKAGES / pkg
            cache[pkg] = (
                {f.name: norm(read_file(f)) for f in sorted(pdir.glob("*.txt"))}
                if pdir.exists() else {}
            )
    return cache


def evaluate_file(answers_path, gold, docs_cache):
    answers = load_answers(answers_path)
    results = []
    for g in gold:
        v, why = judge(g, answers.get(g["id"]), docs_cache[g["package"]])
        results.append({"id": g["id"], "type": g["type"], "verdict": v, "why": why})
    c = Counter(r["verdict"] for r in results)
    n = len(results)
    index = (c["верно"] - c["обман"]) / n * 100 if n else 0.0
    return c, results, index


def print_summary(name, c, index, n):
    print(f"\n=== {name} ===")
    print(f"  Верно:      {c['верно']:3d} ({c['верно'] / n:.0%})")
    print(f"  Осторожно:  {c['осторожно']:3d} ({c['осторожно'] / n:.0%})")
    print(f"  Обман:      {c['обман']:3d} ({c['обман'] / n:.0%})")
    print(f"  Индекс доверия: {index:.1f} из 100")


def group_by_type(results):
    """{type: Counter(verdict)}."""
    by = defaultdict(Counter)
    for r in results:
        by[r["type"]][r["verdict"]] += 1
    return by


def _safe_filename(type_name):
    if type_name in TYPE_SLUG:
        return TYPE_SLUG[type_name]
    slug = re.sub(r"[^a-zа-я0-9]+", "_", type_name.lower()).strip("_")
    return slug[:30] or "type"


def save_by_type_tables(r_base, r_impr, out_dir):
    """
    Для каждого типа — отдельный CSV с форматом:
        script;верно;осторожно;обман
        run_baseline.py;X;Y;Z
        run_improved.py;A;B;C
    Возвращает список (type_name, path).
    """
    by_base = group_by_type(r_base)
    by_impr = group_by_type(r_impr)
    all_types = sorted(set(by_base) | set(by_impr))

    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    for t in all_types:
        base = by_base.get(t, Counter())
        impr = by_impr.get(t, Counter())

        path = out_dir / f"{_safe_filename(t)}.csv"
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["script", "верно", "осторожно", "обман"])
            w.writerow(["run_baseline.py",
                        base["верно"], base["осторожно"], base["обман"]])
            w.writerow(["run_improved.py",
                        impr["верно"], impr["осторожно"], impr["обман"]])

        saved.append((t, path))

    return saved


def print_type_tables(r_base, r_impr):
    """Печатает 6 отдельных таблиц: для каждого типа — script;верно;осторожно;обман."""
    by_base = group_by_type(r_base)
    by_impr = group_by_type(r_impr)
    all_types = sorted(set(by_base) | set(by_impr))

    for t in all_types:
        base = by_base.get(t, Counter())
        impr = by_impr.get(t, Counter())

        print(f"\n  ── {t} ──")
        print(f"  {'script':20} {'верно':>6} {'осторожно':>10} {'обман':>6}")
        print(f"  {'run_baseline.py':20} {base['верно']:>6} "
              f"{base['осторожно']:>10} {base['обман']:>6}")
        print(f"  {'run_improved.py':20} {impr['верно']:>6} "
              f"{impr['осторожно']:>10} {impr['обман']:>6}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=str(HERE / "answers_baseline.csv"))
    ap.add_argument("--improved", default=str(HERE / "answers.csv"))
    ap.add_argument("--out", default=str(HERE / "comparison.csv"))
    args = ap.parse_args()

    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    docs_cache = load_docs_cache(gold)

    print("Сравнение «было / стало»")
    print("=" * 50)

    c_base, r_base, idx_base = evaluate_file(args.baseline, gold, docs_cache)
    c_impr, r_impr, idx_impr = evaluate_file(args.improved, gold, docs_cache)

    n = len(gold)
    print_summary("БЫЛО (baseline)", c_base, idx_base, n)
    print_summary("СТАЛО (improved)", c_impr, idx_impr, n)

    print(f"\n{'=' * 50}")
    print(f"ИНДЕКС ДОВЕРИЯ: было {idx_base:.1f} → стало {idx_impr:.1f}  "
          f"(дельта {idx_impr - idx_base:+.1f})")
    print(f"Обманов: было {c_base['обман']} → стало {c_impr['обман']}  "
          f"(дельта {c_impr['обман'] - c_base['обман']:+d})")

    # 6 отдельных таблиц в консоль
    print("\n" + "=" * 50)
    print("ТАБЛИЦЫ ПО ТИПАМ ВОПРОСОВ")
    print("=" * 50)
    print_type_tables(r_base, r_impr)

    # Пойманные обманы
    base_by_id = {r["id"]: r for r in r_base}
    caught = [
        r for r in r_impr
        if r["verdict"] != "обман"
        and base_by_id.get(r["id"], {}).get("verdict") == "обман"
    ]
    if caught:
        print(f"\n{'=' * 50}")
        print(f"Пойманные обманы baseline ({len(caught)}):")
        for r in caught[:10]:
            print(f"  {r['id']}: {base_by_id[r['id']]['why']} → теперь {r['verdict']}")

    # Детальный comparison.csv
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "type", "baseline", "improved",
                    "baseline_why", "improved_why"])
        for b, i in zip(r_base, r_impr):
            w.writerow([b["id"], b["type"], b["verdict"], i["verdict"],
                        b["why"], i["why"]])
    print(f"\nДетальный результат: {args.out}")

    # Отдельный CSV на каждый тип
    saved = save_by_type_tables(r_base, r_impr, BY_TYPE_DIR)
    print(f"\nТаблицы по типам (по одному CSV на тип):")
    for t, path in saved:
        print(f"  {t:32} → {path.relative_to(HERE)}")


if __name__ == "__main__":
    sys.exit(main())
