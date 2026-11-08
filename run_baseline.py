"""
v1-baseline: наивный прогон «как есть».

Задание: «Задайте модели вопросы "как есть", одним запросом».
Здесь нет ни роли, ни инструкций про цитаты, ни правил про типы status —
модель получает документы, вопрос и минимальную схему JSON. Это честная
точка «было», на которой видно, сколько ИИ обманывает без инженерии.

Результат: answers_baseline.csv (формат как у check.py).

Запуск:
  python run_baseline.py
  python run_baseline.py --limit 5
  python run_baseline.py --packages P01 P02
"""
import argparse
import csv
import sys
from pathlib import Path

from llm_client import ask_llm, extract_json
from check import read_file

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.csv"
PACKAGES = HERE / "packages"


def load_questions(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def load_package_docs(package):
    pdir = PACKAGES / package
    if not pdir.exists():
        return {}
    return {f.name: read_file(f) for f in sorted(pdir.glob("*.txt"))}


def build_prompt(question, docs):
    """Минимальный промпт: только документы, вопрос и схема JSON."""
    docs_text = "\n\n".join(
        f"=== ФАЙЛ: {name} ===\n{text}" for name, text in docs.items()
    )
    return f"""{docs_text}

ВОПРОС: {question}

Ответь в формате JSON:
{{"status": "...", "answer": "...", "quote": "...", "file": "..."}}
"""


def normalize_answer(ans):
    """
    Приводит ответ модели к 4 полям.
    Если status нет или он не по-русски — но answer непустой,
    считаем это ответом: baseline не умеет различать типы, но ответ модели
    должен быть учтён. Иначе замер врёт.
    """
    if not isinstance(ans, dict):
        return {"status": "не знаю", "answer": "", "quote": "", "file": ""}

    answer = str(ans.get("answer") or "").strip()
    status = str(ans.get("status", "")).strip().lower()

    # Английские и «сырые» варианты → русские
    status_map = {
        "answer": "ответ", "yes": "ответ", "ok": "ответ",
        "response": "ответ", "found": "ответ",
        "discrepancy": "расхождение", "contradiction": "расхождение",
        "not_in_documents": "нет в документах",
        "not found": "нет в документах", "absent": "нет в документах",
        "unknown": "не знаю", "idk": "не знаю", "": "не знаю",
    }
    status = status_map.get(status, status)

    # Fallback: если status невалидный, но answer есть — это ответ
    if status not in ("ответ", "расхождение", "нет в документах", "не знаю"):
        if answer:
            status = "ответ"
        else:
            status = "не знаю"

    return {
        "status": status,
        "answer": answer,
        "quote": str(ans.get("quote") or ""),
        "file": str(ans.get("file") or ""),
    }


def write_answers(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "status", "answer", "quote", "file"])
        for r in rows:
            w.writerow([r["id"], r["status"], r["answer"], r["quote"], r["file"]])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--packages", nargs="*", default=None)
    ap.add_argument("--out", default=str(HERE / "answers_baseline.csv"))
    args = ap.parse_args()

    questions = load_questions(QUESTIONS)
    if args.packages:
        questions = [q for q in questions if q["package"] in args.packages]
    if args.limit:
        questions = questions[: args.limit]

    print(f"Вопросов к обработке: {len(questions)}")
    rows = []
    docs_cache = {}

    for i, q in enumerate(questions, 1):
        pkg = q["package"]
        if pkg not in docs_cache:
            docs_cache[pkg] = load_package_docs(pkg)
        docs = docs_cache[pkg]

        print(f"[{i}/{len(questions)}] {q['id']}: {q['question'][:60]}...")
        try:
            raw = ask_llm([
                {"role": "user", "content": build_prompt(q["question"], docs)},
            ])
            ans = extract_json(raw)
            row = {"id": q["id"], **normalize_answer(ans)}
        except Exception as e:
            print(f"  ОШИБКА: {e}")
            row = {"id": q["id"], "status": "не знаю",
                   "answer": "", "quote": "", "file": ""}

        rows.append(row)
        print(f"  → {row['status']}: {row['answer'][:80]}")

    write_answers(rows, args.out)
    print(f"\nСохранено: {args.out}")
    print(f"Дальше: python check.py {args.out}")


if __name__ == "__main__":
    sys.exit(main())
