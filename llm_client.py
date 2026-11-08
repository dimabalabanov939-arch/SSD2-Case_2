"""
Клиент для LLM API (GigaChat + опционально Groq/OpenRouter).
Без внешних зависимостей (только stdlib).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import ssl

# ---------- Конфигурация провайдеров ----------
PROVIDERS = {
    "gigachat": {
        "chat_url": "https://api.giga.chat/v1/chat/completions",
        "auth_url": "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        "default_model": "GigaChat-2-Pro",          # быстрая модель для старта
        "key_required": False,                   # авторизация через OAuth
        "scope": "GIGACHAT_API_PERS",            # для физических лиц
    },
}


def _get_config():
    provider = os.getenv("LLM_PROVIDER", "gigachat").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"Неизвестный LLM_PROVIDER={provider}. Доступно: {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    api_key = os.getenv("LLM_API_KEY", "")
    if cfg["key_required"] and not api_key:
        raise ValueError(f"Для {provider} нужен LLM_API_KEY")
    model = os.getenv("LLM_MODEL") or cfg["default_model"]
    return provider, cfg, api_key, model


# ---------- Кэш токена GigaChat ----------
_gigachat_token_cache = {"access_token": None, "expires_at": 0.0}


def _get_gigachat_token(auth_key: str, auth_url: str, scope: str) -> str:
    """Возвращает валидный access_token, при необходимости обновляет."""
    now = time.time()
    # Обновляем за 60 секунд до истечения, чтобы избежать 401 на границе
    if _gigachat_token_cache["access_token"] and now < _gigachat_token_cache["expires_at"] - 60:
        return _gigachat_token_cache["access_token"]

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {auth_key}",
    }
    data = urllib.parse.urlencode({"scope": scope}).encode("utf-8")
    req = urllib.request.Request(auth_url, data=data, headers=headers, method="POST")

        # Отключаем проверку SSL для запроса токена (сертификаты НУЦ Минцифры)
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=30, context=context) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    _gigachat_token_cache["access_token"] = payload["access_token"]
    # expires_at в ответе — это Unix timestamp; если его нет, ставим now + 30 мин
    _gigachat_token_cache["expires_at"] = payload.get("expires_at", now + 1800)
    return _gigachat_token_cache["access_token"]


# ---------- HTTP ----------
def _post(url, payload, headers, timeout=180):
    headers = {
        **headers,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
    # Для GigaChat (и других сервисов с сертификатами НУЦ Минцифры)
    # временно отключаем проверку. Не используйте в продакшене!
    context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return json.loads(resp.read().decode("utf-8"))

# ---------- Публичная функция ----------
def ask_llm(messages, temperature=0.0, max_retries=4):
    provider, cfg, api_key, model = _get_config()

    if provider == "gigachat":
        auth_key = os.getenv("LLM_API_KEY") or os.getenv("GIGACHAT_AUTH_KEY")
        if not auth_key:
            raise ValueError(
                "Для GigaChat нужен LLM_API_KEY (Authorization Key в Base64). "
                "Можно также использовать переменную GIGACHAT_AUTH_KEY."
            )
        token = _get_gigachat_token(auth_key, cfg["auth_url"], cfg["scope"])
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        url = cfg["chat_url"]
    else:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = cfg["chat_url"]

    payload = {"model": model, "messages": messages, "temperature": temperature}

    last_err = None
    for attempt in range(max_retries):
        try:
            resp = _post(url, payload, headers)
            return resp["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (401,) and provider == "gigachat":
                # токен мог истечь — сбрасываем кэш и пробуем ещё раз
                _gigachat_token_cache["access_token"] = None
                continue
            if e.code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt * 3
                print(f"  [llm] HTTP {e.code}, жду {wait}с (попытка {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt * 2
            print(f"  [llm] сетевая ошибка ({e}), жду {wait}с")
            time.sleep(wait)
    raise RuntimeError(f"LLM не ответил за {max_retries} попыток: {last_err}")


# ---------- JSON-парсер (без изменений) ----------
def extract_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return None
