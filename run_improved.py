"""
v1: базовая improved-версия. Валидация цитат + ретраи + приоритет 05.
Индекс 83.1, обман 4.

Запуск:
  python run_improved.py
  python run_improved.py --packages P01 P02 --limit 5
"""
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

from llm_client import ask_llm, extract_json
from check import read_file, norm

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.csv"
PACKAGES = HERE / "packages"
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))

SYSTEM_PROMPT = """Ты — аккуратный аналитик документов госзакупок.
Твоя главная цель — НЕ ОБМАНУТЬ. Лучше честно сказать «не знаю» или
«нет в документах», чем выдумать ответ.

ПРАВИЛА О ЦИТАТАХ:
1. Поле quote — ДОСЛОВНАЯ копия фрагмента из указанного файла.
2. Не перефразируй, не сокращай слова, не меняй знаки препинания.
3. Если не можешь найти точную цитату — верни status="нет в документах"
   или status="не знаю", но НЕ выдумывай цитату.

ПРИОРИТЕТ ДОКУМЕНТОВ:
- Если в документах есть файл с «изменени» в названии — его положения
  ЗАМЕНЯЮТ соответствующие пункты 01_izveshchenie.txt.
- Если значение есть и в 01, и в 05 — верное берётся из 05.
  Это НЕ расхождение, это новая редакция.

ПРИЗНАКИ РАСХОЖДЕНИЯ (status="расхождение"):
- Два разных файла содержат РАЗНЫЕ значения одного и того же параметра,
  и это НЕ перекрыто изменениями.
- В answer укажи ОБА значения через "и".

ПРИЗНАКИ «НЕТ В ДОКУМЕНТАХ» (status="нет в документах"):
- Вопрос про параметр, которого ВООБЩЕ НЕТ ни в одном документе.
- answer и quote оставь пустыми.

ОТЛИЧИЕ «НЕТ ТРЕБОВАНИЯ» ОТ «НЕТ В ДОКУМЕНТАХ»:
- Если в документе ПРЯМО НАПИСАНО «требование не установлено»,
  «обеспечение не требуется», «присутствие не требуется» —
  это ОТВЕТ. status="ответ", answer="нет" или "не требуется".

ВОПРОСЫ С НЕСКОЛЬКИМИ ЧИСЛАМИ:
- Если вопрос требует НЕСКОЛЬКО значений, приведи ВСЕ.
  Пример: «Сколько контрактов нужно И какая цена каждого?» —
  в answer должно быть ДВА числа.

ПРОЧЕЕ:
- Если ответ есть, но ты не уверен — верни status="не знаю".
"""


def load_questions(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def load_package_docs(package):
    pdir = PACKAGES / package
    if not pdir.exists():
        return {}
    return {f.name: read_file(f) for f in sorted(pdir.glob("*.txt"))}


def build_user_prompt(question, docs):
    def sort_key(name):
        return (0 if "izmenen" in name.lower() else 1, name)

    docs_text = "\n\n".join(
        f"=== ФАЙЛ: {name} ===\n{docs[name]}"
        for name in sorted(docs, key=sort_key)
    )
    has_changes = any("izmenen" in n.lower() for n in docs)
    changes_note = (
        "\n⚠ В документах есть файл изменений. Его положения ЗАМЕНЯЮТ "
        "пункты 01_izveshchenie.txt (это НЕ расхождение).\n"
        if has_changes else ""
    )
    return f"""{changes_note}ДОКУМЕНТЫ:
{docs_text}

ВОПРОС: {question}

Верни ТОЛЬКО JSON без markdown:
{{"status": "ответ" | "расхождение" | "нет в документах" | "не знаю",
  "answer": "значение (для расхождения — оба через 'и', для нескольких чисел — все)",
  "quote": "дословная цитата из указанного файла",
  "file": "имя_файла.txt"}}"""


STOPWORDS = set("""
какой какая какие какого каком в на по для от до не и или а но что это
сколько рублей процент процента процентов размер цена контракта заявки
установлен установлено установлена требуется требование есть ли
ответьте да нет
""".split())


def keywords_from_question(question):
    words = re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", question.lower())
    return [w for w in words if w not in STOPWORDS]


def precheck_not_in_docs(question, docs_norm):
    kws = keywords_from_question(question)
    if not kws:
        return False
    joined = " ".join(docs_norm.values())
    hits = sum(1 for kw in kws if (kw[:6] if len(kw) > 6 else kw) in joined)
    return hits == 0


def validate_answer(ans, docs_norm):
    status = ans.get("status", "")
    if status not in ("ответ", "расхождение", "нет в документах", "не знаю"):
        return False, f'Неверный status={status!r}.'
    if status in ("нет в документах", "не знаю"):
        return True, ""

    quote = (ans.get("quote") or "").strip()
    file = (ans.get("file") or "").strip()
    answer = (ans.get("answer") or "").strip()

    if not answer:
        return False, "Пустое поле answer."
    if not quote:
        return False, "Нет поля quote."
    if not file:
        return False, "Не указан файл в поле file."

    q_norm = norm(quote)
    if len(q_norm) < 8:
        return False, "Цитата короче 8 символов."
    if file not in docs_norm:
        return False, f"Файл {file!r} не найден. Доступные: {list(docs_norm.keys())}."
    if q_norm not in docs_norm[file]:
        return False, f"Цитата НЕ НАЙДЕНА в файле {file}."

    if status == "расхождение":
        nums = re.findall(r"\d+(?:[.,]\d+)?", answer)
        if len(nums) < 2:
            return False, "Статус 'расхождение', но одно значение."

    return True, ""


def normalize_answer(ans):
    if not isinstance(ans, dict):
        return {"status": "не знаю", "answer": "", "quote": "", "file": ""}
    status = str(ans.get("status", "")).strip().lower()
    if status not in ("ответ", "расхождение", "нет в документах", "не знаю"):
        status = "не знаю"
    return {
        "status": status,
        "answer": str(ans.get("answer") or ""),
        "quote": str(ans.get("quote") or ""),
        "file": str(ans.get("file") or ""),
    }


def solve_question(question, docs):
    docs_norm = {name: norm(text) for name, text in docs.items()}

    if not docs:
        return {"status": "нет в документах", "answer": "", "quote": "", "file": ""}

    if precheck_not_in_docs(question, docs_norm):
        print("    пре-чек: ключевые слова не найдены → нет в документах")
        return {"status": "нет в документах", "answer": "", "quote": "", "file": ""}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(question, docs)},
    ]

    for attempt in range(MAX_ATTEMPTS):
        try:
            raw = ask_llm(messages)
        except Exception as e:
            print(f"    LLM ошибка: {e}")
            return {"status": "не знаю", "answer": "", "quote": "", "file": ""}

        messages.append({"role": "assistant", "content": raw})
        ans = extract_json(raw)
        if ans is None:
            messages.append({
                "role": "user",
                "content": "Не смог разобрать JSON. Верни СТРОГО JSON без markdown.",
            })
            continue

        ans = normalize_answer(ans)
        ok, err = validate_answer(ans, docs_norm)
        if not ok:
            print(f"    попытка {attempt + 1}: {err[:120]}")
            messages.append({
                "role": "user",
                "content": f"Проблема с твоим ответом: {err}\nИсправь и верни корректный JSON.",
            })
            continue

        return ans

    return {"status": "не знаю", "answer": "", "quote": "", "file": ""}


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
    ap.add_argument("--out", default=str(HERE / "answers_improved.csv"))
    args = ap.parse_args()

    questions = load_questions(QUESTIONS)
    if args.packages:
        questions = [q for q in questions if q["package"] in args.packages]
    if args.limit:
        questions = questions[: args.limit]

    print(f"Вопросов: {len(questions)}. MAX_ATTEMPTS={MAX_ATTEMPTS}")
    rows = []
    docs_cache = {}

    for i, q in enumerate(questions, 1):
        pkg = q["package"]
        if pkg not in docs_cache:
            docs_cache[pkg] = load_package_docs(pkg)
        docs = docs_cache[pkg]

        print(f"[{i}/{len(questions)}] {q['id']}: {q['question'][:60]}...")
        ans = solve_question(q["question"], docs)
        rows.append({"id": q["id"], **ans})
        print(f"  → {ans['status']}: {ans['answer'][:80]}")

    write_answers(rows, args.out)
    print(f"\nСохранено: {args.out}")
    print(f"Дальше: python check.py {args.out}")


if __name__ == "__main__":
    sys.exit(main())
