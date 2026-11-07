"""
Дисковый кэш для LLM-ответов. Гарантирует воспроизводимость прогонов.

Первый прогон спрашивает модель, второй — берёт из кэша.
Ключ — SHA256 от messages + model + temperature + провайдер.

Файл llm_cache.pkl в .gitignore — не коммитится.
"""
import hashlib
import json
import pickle
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parent / "llm_cache.pkl"

_cache = None
_dirty = False


def _load():
    global _cache
    if _cache is not None:
        return _cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "rb") as f:
                _cache = pickle.load(f)
                return _cache
        except Exception:
            pass
    _cache = {}
    return _cache


def _save():
    global _dirty
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(_cache, f)
    _dirty = False


def make_key(messages, **extra):
    payload = {"messages": messages, **extra}
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def cached_call(fn, messages, **kwargs):
    """
    fn(messages, **kwargs) → результат.
    Если те же messages уже встречались — возвращаем из кэша.
    """
    global _dirty
    _load()
    key = make_key(messages, **{k: v for k, v in kwargs.items() if k != "max_retries"})
    if key in _cache:
        return _cache[key]
    result = fn(messages, **kwargs)
    _cache[key] = result
    _dirty = True
    if len(_cache) % 10 == 0:
        _save()
    return result


def flush():
    global _dirty
    if _dirty:
        _save()


def clear():
    global _cache, _dirty
    _cache = {}
    _dirty = True
    _save()


def stats():
    _load()
    return {"entries": len(_cache), "file": str(CACHE_FILE)}
