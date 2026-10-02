import os
import json
import requests
import re
import time
from datetime import datetime, timezone, timedelta

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("CONGRESS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip(" .")

STATE_FILE = "processed_bills.json"

# Моделі, для яких ПІДТВЕРДЖЕНО підтримку response_format: json_object на Groq.
# Онови цей список, якщо Groq змінить набір доступних моделей:
# https://console.groq.com/docs/structured-outputs#supported-models
JSON_CAPABLE_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "gemma2-9b-it",
]

_CACHED_MODEL = None  # кеш вибраної моделі на весь прогін скрипта


def get_best_model():
    """Обирає модель один раз на запуск скрипта і кешує результат,
    щоб не робити зайвих запитів і не отримувати різні моделі для різних біллів."""
    global _CACHED_MODEL
    if _CACHED_MODEL:
        return _CACHED_MODEL

    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            available = {m["id"] for m in res.json().get("data", [])}
            print(f"Усі доступні моделі на сервері Groq: {sorted(available)}")

            # Перебираємо ТІЛЬКИ перевірені JSON-сумісні моделі, у порядку пріоритету
            for candidate in JSON_CAPABLE_MODELS:
                if candidate in available:
                    print(f"Обрано пріоритетну модель: {candidate}")
                    _CACHED_MODEL = candidate
                    return _CACHED_MODEL
    except Exception as e:
        print(f"Не вдалося отримати список моделей динамічно: {e}")

    # Залізобетонний фоллбек: перша модель зі списку, навіть якщо не вдалось
    # підтвердити її наявність через API — усе одно безпечніше за substring-угадування.
    print("Жодної підтвердженої моделі не знайдено в списку доступних, беремо дефолт.")
    _CACHED_MODEL = JSON_CAPABLE_MODELS[0]
    return _CACHED_MODEL


def get_current_congress():
    now = datetime.now()
    year = now.year
    if year % 2 != 0 and (now.month == 1 and now.day < 3):
        effective_year = year - 1
    else:
        effective_year = year
    return str((effective_year - 1789) // 2 + 1)


def is_technical(title):
    t = title.lower()
    stop_words = [
        "post office", "postal service", "technical correction",
        "express the sense", "expressing the sense", "medal",
        "mint coins", "commemorative", "designate the facility",
        "designate the building", "naming a", "renaming"
    ]
    return any(phrase in t for phrase in stop_words)


def escape_html(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def process_with_ai(title, status_en, summary_en):
    if not GROQ_API_KEY:
        return title, status_en, "Помилка: Ключ GROQ_API_KEY не знайдено."

    model_name = get_best_model()

    ai_prompt = f"""Ти — професійний політичний аналітик. Переклади та поясни законопроєкт Конгресу США.
Назва: {title}
Статус: {status_en}
Офіційне резюме: {summary_en if summary_en else 'Відсутнє.'}

Поверни об'єкт JSON з трьома ключами:
1. "title_uk" - точний переклад назви.
2. "status_uk" - точний переклад статусу.
3. "summary_uk" - коротке і чітке пояснення українською (1-2 речення), про що цей закон. Якщо "Офіційне резюме" відсутнє, зроби логічний висновок самостійно на основі назви."""

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    def call(use_json_mode):
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": ai_prompt}],
            "temperature": 0.2,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return requests.post(url, headers=headers, json=payload)

    try:
        res = call(use_json_mode=True)

        # Якщо саме ця модель не підтримує json-режим — пробуємо без нього
        # і покладаємось на regex-парсер нижче.
        if res.status_code == 400 and "json" in res.text.lower():
            print(f"Модель {model_name} не підтримує JSON mode, пробую без нього...")
            res = call(use_json_mode=False)

        if res.status_code == 200:
            data = res.json()["choices"][0]["message"]["content"]
            match = re.search(r'\{[\s\S]*\}', data)
            if match:
                try:
                    result = json.loads(match.group(0))
                    return (
                        result.get("title_uk", title),
                        result.get("status_uk", status_en),
                        result.get("summary_uk", "Опис відсутній.")
                    )
                except json.JSONDecodeError:
                    pass
            return title, status_en, title
        else:
            err_msg = res.json().get('error', {}).get('message', res.text)
            return title, status_en, f"Помилка API ({res.status_code}): {err_msg}"

    except Exception as e:
        return title, status_en, f"Збій з'єднання: {e}"


def get_public_url(congress, bill_type, number):
    type_map = {
        "hr": "house-bill", "s": "senate-bill",
        "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
        "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution",
        "hres": "house-resolution", "sres": "senate-resolution"
    }
    full_type = type_map.get(bill_type.lower(), "bill")
    return f"https://www.congress.gov/bill/{congress}th-congress/{full_type}/{number}"


def get_bill_details(congress, b_type, number):
    sponsor = "Невідомо"
    status_en = "Introduced"
    summary_en = ""

    url = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}?api_key={API_KEY}"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json().get("bill", {})
        sponsors = data.get("sponsors", [])
        if sponsors:
            sponsor = sponsors[0].get("fullName", "Невідомо")

        action = data.get("latestAction", {}).get("text", "")
        if action:
            status_en = action

    url_sum = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}/summaries?api_key={API_KEY}"
    res_sum = requests.get(url_sum)
    if res_sum.status_code == 200:
        summaries = res_sum.json().get("summaries", [])
        if summaries:
            summary_en = summaries[0].get("text", "")
            summary_en = re.sub(r'<[^>]+>', '', summary_en)

    return sponsor, status_en, summary_en


def load_processed():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_processed(processed):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(processed[-100:], f, ensure_ascii=False, indent=2)


def fetch_recent_bills():
    current_congress = get_current_congress()
    url = f"https://api.congress.gov/v3/bill/{current_congress}?sort=updateDate+desc"
    params = {"api_key": API_KEY, "limit": 10}

    response = requests.get(url, params=params)
    if response.status_code != 200:
        return []

    bills = response.json().get("bills", [])
    recent_bills = []
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=2)

    for bill in bills:
        if not bill.get("updateDate"):
            continue
        try:
            bill_date = datetime.fromisoformat(bill["updateDate"].replace("Z", "+00:00"))
            if bill_date.tzinfo is None:
                bill_date = bill_date.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if bill_date >= one_day_ago:
            recent_bills.append(bill)

    return recent_bills


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })


if __name__ == "__main__":
    processed = load_processed()
    recent_bills = fetch_recent_bills()
    sent_count = 0

    for bill in recent_bills:
        bill_id = f"{bill['congress']}_{bill['type']}_{bill['number']}"

        if bill_id in processed:
            continue

        if is_technical(bill.get("title", "")) or is_technical(bill.get("latestAction", {}).get("text", "")):
            processed.append(bill_id)
            continue

        print(f"Обробка закону: {bill_id}")

        sponsor, status_en, summary_en = get_bill_details(bill["congress"], bill["type"], bill["number"])
        title_uk, status_uk, desc_uk = process_with_ai(bill["title"], status_en, summary_en)

        public_url = get_public_url(bill["congress"], bill["type"], bill["number"])

        message = (
            f"🇺🇸 <b>Конгрес США ({bill['congress']}-е скликання)</b>\n\n"
            f"📌 <b>{bill['type'].upper()} {bill['number']}</b>\n"
            f"<b>{escape_html(title_uk)}</b>\n\n"
            f"👤 <b>Ініціатор:</b> {escape_html(sponsor)}\n"
            f"📜 <b>Статус:</b> {escape_html(status_uk)}\n\n"
            f"💡 <b>Суть:</b> {escape_html(desc_uk)}\n\n"
            f"🔗 <a href='{public_url}'>Оригінал документа</a>"
        )

        send_telegram(message)
        processed.append(bill_id)
        sent_count += 1

        time.sleep(3)

    save_processed(processed)
    print(f"Успішно оброблено та відправлено: {sent_count}")
