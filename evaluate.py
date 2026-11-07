"""
evaluate.py — сравнение baseline и improved через функции check.py.

Печатает:
  - индексы доверия «было/стало»
  - разбивку по типам вопросов для обоих прогонов
  - список пойманных обманов baseline (где improved сработал лучше)
  - сохраняет comparison.csv с деталями

Запуск:
  python evaluate.py
  python evaluate.py --baseline answers_baseline.csv --improved answers.csv
  python evaluate.py --out docs/comparison.csv
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from check import load_answers, judge, read_file, norm

HERE = Path(__file__).resolve().parent
GOLD_PATH = HERE / "answers_public.json"
PACKAGES = HERE / "packages"


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


def print_by_type(name, results):
    by = defaultdict(Counter)
    for r in results:
        by[r["type"]][r["verdict"]] += 1
    print(f"\n  {name}: по типам вопросов")
    print(f"  {'Тип':32} {'всего':>5} {'верно':>5} {'осторож':>8} {'обман':>5}")
    for t, cc in sorted(by.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {t:32} {sum(cc.values()):>5} {cc['верно']:>5} "
              f"{cc['осторожно']:>8} {cc['обман']:>5}")


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

    print_by_type("БЫЛО", r_base)
    print_by_type("СТАЛО", r_impr)

    base_by_id = {r["id"]: r for r in r_base}
    caught = [
        r for r in r_impr
        if r["verdict"] != "обман"
        and base_by_id.get(r["id"], {}).get("verdict") == "обман"
    ]
    if caught:
        print(f"\nПойманные обманы baseline ({len(caught)}):")
        for r in caught[:10]:
            print(f"  {r['id']}: {base_by_id[r['id']]['why']} → теперь {r['verdict']}")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "type", "baseline", "improved",
                    "baseline_why", "improved_why"])
        for b, i in zip(r_base, r_impr):
            w.writerow([b["id"], b["type"], b["verdict"], i["verdict"],
                        b["why"], i["why"]])
    print(f"\nДетальный результат: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
