"""
v3: лучший рабочий вариант на GigaChat-2. Индекс 90.1, обман 2.

Что нового против v2:
  - _unit_from_question / _value_matches_unit: проверка единицы (месяцы ≠ дни)
  - _regex_contradiction_backstop: программный поиск, если модель не справилась
  - override_no_requirement: «нет в документах» → «не требуется»
  - recalc_contract_security_rub: NMCK × %, приоритет 05, в 05 последний %
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
CONTRADICTION_TRIGGERS = ("срок", "гарант")

SYSTEM_PROMPT = """Ты — аккуратный аналитик документов госзакупок.
Твоя главная цель — НЕ ОБМАНУТЬ.

ПРАВИЛА О ЦИТАТАХ:
1. Поле quote — ДОСЛОВНАЯ копия фрагмента из указанного файла.
2. Не перефразируй, не сокращай слова.
3. Если не можешь найти цитату — верни status="нет в документах" или "не знаю".

ПРИОРИТЕТ ДОКУМЕНТОВ:
- Файл с «изменени» в названии ЗАМЕНЯЕТ соответствующие пункты 01.
- Если значение есть и в 01, и в 05 — верное из 05. Это НЕ расхождение.

РАСХОЖДЕНИЕ (status="расхождение"):
- Два файла без переопределения дают РАЗНЫЕ значения одного параметра.
- В answer — ОБА значения через "и".

НЕТ В ДОКУМЕНТАХ:
- Параметра НЕТ ни в одном файле → status="нет в документах", answer="".

ОТЛИЧИЕ «НЕТ ТРЕБОВАНИЯ»:
- «требование не установлено», «не требуется» в тексте → status="ответ",
  answer="не требуется".

ВОПРОСЫ С НЕСКОЛЬКИМИ ЧИСЛАМИ:
- Если вопрос требует НЕСКОЛЬКО значений, приведи ВСЕ (например, «3 и 250000»).

ПРОЧЕЕ:
- Не уверен — верни status="не знаю".
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
        "\n⚠ Есть файл изменений. Его положения ЗАМЕНЯЮТ 01 (это НЕ расхождение).\n"
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
        return False, f"Файл {file!r} не найден."
    if q_norm not in docs_norm[file]:
        return False, f"Цитата НЕ НАЙДЕНА в файле {file}."

    if status == "расхождение":
        nums = re.findall(r"\d+(?:[.,]\d+)?", answer)
        if len(nums) < 2:
            return False, "Статус 'расхождение', но одно значение."

    return True, ""


# ---------- Утилиты: единицы ----------

def _unit_from_question(question):
    q = question.lower()
    if "месяц" in q:
        return r"месяц"
    if "календарн" in q or "дн" in q:
        return r"(?:календарн\w*\s+)?дн"
    if "год" in q or "лет" in q:
        return r"(?:год|лет)"
    if "процент" in q or "%" in q:
        return r"(?:%|процент)"
    return None


def _unit_kind(question):
    q = question.lower()
    if "месяц" in q:
        return "months"
    if "календарн" in q or "дн" in q:
        return "days"
    if "год" in q or "лет" in q:
        return "years"
    if "процент" in q or "%" in q:
        return "percent"
    return None


def _value_matches_unit(doc_text_norm, value_norm, unit_pattern):
    if not unit_pattern:
        return True
    pattern = re.escape(value_norm) + r"[^\d]{0,80}(?:" + unit_pattern + r")"
    return bool(re.search(pattern, doc_text_norm))


# ---------- Критик расхождений ----------

FIND_CONTRADICTIONS_PROMPT = """Задача: найти РАСХОЖДЕНИЯ между файлами.
ВОПРОС: {question}
Для КАЖДОГО файла найди значение параметра. Верни JSON, где ключ — имя
файла, значение — найденное значение (например "60") или "нет".
Пример: {{"01.txt": "60", "04.txt": "75", "02.txt": "нет"}}
ФАЙЛЫ:
{docs}
"""


def _regex_contradiction_backstop(question, docs_norm):
    q = question.lower()
    if "гарант" in q:
        trigger_re = re.compile(r"гарант\w*")
    elif "срок" in q:
        trigger_re = re.compile(r"срок\w*")
    else:
        return {}

    unit_pattern = _unit_from_question(question)
    if not unit_pattern:
        return {}

    num_unit_re = re.compile(
        r"(\d+(?:[.,]\d+)?)[^\d]{0,30}(?:" + unit_pattern + r")"
    )
    values = {}
    for fname, text in docs_norm.items():
        for tm in trigger_re.finditer(text):
            window = text[tm.start():tm.start() + 250]
            for nm in num_unit_re.finditer(window):
                val = nm.group(1).replace(",", ".")
                values.setdefault(val, set()).add(fname)
    return values


def find_contradictions(question, docs, ans):
    q_lower = question.lower()
    triggers = [t for t in CONTRADICTION_TRIGGERS if t in q_lower]
    if not triggers or ans["status"] != "ответ":
        return ans

    docs_norm = {n: norm(t) for n, t in docs.items()}
    unit_pattern = _unit_from_question(question)

    docs_short = {n: t[:2500] for n, t in docs.items()}
    docs_text = "\n\n".join(f"=== ФАЙЛ: {n} ===\n{t}" for n, t in docs_short.items())
    prompt = FIND_CONTRADICTIONS_PROMPT.format(question=question, docs=docs_text)

    unique = {}
    try:
        raw = ask_llm([{"role": "user", "content": prompt}])
        result = extract_json(raw)
        if isinstance(result, dict):
            for fname, val in result.items():
                if not isinstance(val, str):
                    continue
                v_lower = val.lower().strip()
                if v_lower in ("нет", "не найдено", "отсутствует", "не установлено",
                               "не требуется", "none", ""):
                    continue
                if fname not in docs_norm:
                    continue
                for n in re.findall(r"\d+(?:[.,]\d+)?", val):
                    n_norm = norm(n)
                    if not _value_matches_unit(docs_norm[fname], n_norm, unit_pattern):
                        print(f"    find_contradictions: {n} в {fname} не в единице")
                        continue
                    if n_norm not in unique:
                        unique[n_norm] = fname
    except Exception as e:
        print(f"    find_contradictions: LLM ошибка ({e})")

    if len(unique) < 2:
        backstop = _regex_contradiction_backstop(question, docs_norm)
        all_files = set()
        for fs in backstop.values():
            all_files |= fs
        if len(backstop) >= 2 and len(all_files) >= 2:
            print(f"    find_contradictions: бэкстоп нашёл {sorted(backstop.keys())}")
            unique = {v: sorted(fs)[0] for v, fs in backstop.items()}

    if len(unique) >= 2:
        parts = [f"{v} (в {f})" for v, f in unique.items()]
        print(f"    find_contradictions: {len(unique)} значений → {' и '.join(parts)[:100]}")
        ans["status"] = "расхождение"
        ans["answer"] = " и ".join(parts)
    else:
        print(f"    find_contradictions: подтверждено {len(unique)} значение")
    return ans


# ---------- Пост-обработка: «не требуется» ----------

def override_no_requirement(question, docs, ans):
    if ans["status"] != "нет в документах":
        return ans
    q_lower = question.lower()
    if "обеспечен" in q_lower and "заявк" in q_lower:
        primary, context = "обеспечени", "заявк"
    elif "обеспечен" in q_lower and "исполнен" in q_lower:
        primary, context = "обеспечени", "исполнени"
    elif "гарантийн" in q_lower and "обеспечен" in q_lower:
        primary, context = "гарантийн", "обеспечен"
    elif "лицензи" in q_lower and "фсб" in q_lower:
        primary, context = "лицензи", "фсб"
    else:
        return ans

    neg_phrases = ["не установлен", "не требуется", "не предусмотрен"]

    for fname, text in docs.items():
        text_lower = text.lower()
        for m in re.finditer(primary, text_lower):
            window = text_lower[max(0, m.start() - 30):m.start() + 350]
            if context not in window:
                continue
            if not any(p in window for p in neg_phrases):
                continue
            sent_start = text.rfind(".", 0, m.start())
            if sent_start == -1 or m.start() - sent_start > 300:
                sent_start = max(0, m.start() - 50)
            else:
                sent_start += 1
            while sent_start < len(text) and text[sent_start].isspace():
                sent_start += 1
            sent_end = text.find(".", m.start())
            if sent_end == -1 or sent_end - sent_start > 300:
                sent_end = m.start() + 200
            else:
                sent_end += 1
            quote = text[sent_start:sent_end].strip()
            print(f"    override: «нет в документах» → «не требуется» ({fname})")
            return {"status": "ответ", "answer": "не требуется",
                    "quote": quote, "file": fname}
    return ans


# ---------- Пост-обработка: пересчёт обеспечения ----------

def recalc_contract_security_rub(question, docs, ans):
    q_lower = question.lower()
    if not ("обеспечен" in q_lower and "исполнен" in q_lower):
        return ans
    if "руб" not in q_lower and "составит" not in q_lower:
        return ans
    if ans["status"] != "ответ":
        return ans

    nmck = None
    for text in docs.values():
        m = re.search(
            r"Начальная\s*\(максимальная\)\s*цена\s*контракта[:\s]*([\d ]+[.,]?\d*)",
            text, re.IGNORECASE,
        )
        if m:
            try:
                nmck = float(re.sub(r"\s", "", m.group(1)).replace(",", "."))
                break
            except ValueError:
                pass
    if not nmck:
        return ans

    ordered = sorted(
        docs.items(),
        key=lambda kv: (0 if "izmenen" in kv[0].lower() else 1, kv[0]),
    )
    percent, percent_file = None, None
    for fname, text in ordered:
        is_change = "izmenen" in fname.lower()
        found = False
        for m in re.finditer(
            r"обеспечени[ея]\s+исполнени[яя]\s+контракта", text, re.IGNORECASE,
        ):
            window = text[m.start():m.start() + 500]
            all_pct = re.findall(r"(\d+(?:[.,]\d+)?)\s*(?:%|процент)", window)
            if not all_pct:
                continue
            chosen = all_pct[-1] if is_change else all_pct[0]
            try:
                percent = float(chosen.replace(",", "."))
                percent_file = fname
                found = True
                break
            except ValueError:
                continue
        if found:
            break

    if percent is None:
        return ans

    correct = nmck * percent / 100
    correct_str = str(int(correct)) if correct == int(correct) else str(correct)
    print(f"    recalc: NMCK={nmck}, %={percent} ({percent_file}) → {correct_str}")
    ans["answer"] = correct_str

    if percent_file:
        for m in re.finditer(
            r"обеспечени[ея]\s+исполнени[яя]\s+контракта",
            docs[percent_file], re.IGNORECASE,
        ):
            start = m.start()
            end = docs[percent_file].find(".", start)
            if end == -1 or end - start > 300:
                end = start + 200
            else:
                end += 1
            ans["quote"] = docs[percent_file][start:end].strip()
            ans["file"] = percent_file
            break

    return ans


# ---------- Основной цикл ----------

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

        ans = find_contradictions(q["question"], docs, ans)
        ans = override_no_requirement(q["question"], docs, ans)
        ans = recalc_contract_security_rub(q["question"], docs, ans)

        rows.append({"id": q["id"], **ans})
        print(f"  → {ans['status']}: {ans['answer'][:80]}")

    write_answers(rows, args.out)
    print(f"\nСохранено: {args.out}")
    print(f"Дальше: python check.py {args.out}")


if __name__ == "__main__":
    sys.exit(main())
