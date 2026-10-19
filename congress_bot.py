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


def process_with_ai(title, status_en, summary_en, subjects=None):
    if not GROQ_API_KEY:
        return title, status_en, "Помилка: Ключ GROQ_API_KEY не знайдено.", ["OTHER"]

    subjects_text = ", ".join(subjects or []) or "Офіційні тематичні рубрики відсутні."

    model_name = get_best_model()

    ai_prompt = f"""Ти — професійний політичний аналітик. Переклади та поясни законопроєкт Конгресу США.
Назва: {title}
Статус: {status_en}
Офіційне резюме: {summary_en if summary_en else 'Відсутнє.'}
Офіційні тематичні рубрики Congress.gov: {subjects_text}

Поверни об'єкт JSON з чотирма ключами:
1. "title_uk" - точний переклад назви.
2. "status_uk" - точний переклад статусу.
3. "summary_uk" - коротке пояснення українською (1-2 речення). Якщо офіційне резюме відсутнє, не вигадуй деталей.
4. "categories" - масив із однієї або кількох категорій: "MILITARY", "INTERNATIONAL", "ECONOMICS", "OTHER". MILITARY: оборонний бюджет, збройні сили, зброя, боєприпаси, військові технології, оборонна промисловість, військові закупівлі та дослідження. INTERNATIONAL: зовнішня політика, дипломатія, договори, санкції, міжнародні організації, відносини з іншими державами, міжнародна допомога, зовнішня торгівля та обмеження. ECONOMICS: федеральний бюджет і борг, податки, внутрішня економічна політика, банки, фінанси, енергетика, промисловість, праця та внутрішні ринки. OTHER: решта тем. Дозволено кілька категорій, якщо є реальний змістовний перетин. Не класифікуй як INTERNATIONAL лише через географічну назву або іноземне походження людини. Відповідай лише валідним JSON."""

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
        return requests.post(url, headers=headers, json=payload, timeout=60)

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
                    valid_categories = {"MILITARY", "INTERNATIONAL", "ECONOMICS", "OTHER"}
                    categories = result.get("categories", ["OTHER"])
                    if not isinstance(categories, list):
                        categories = ["OTHER"]
                    categories = [x for x in categories if x in valid_categories]
                    if not categories:
                        categories = ["OTHER"]
                    if "OTHER" in categories and len(categories) > 1:
                        categories.remove("OTHER")
                    return (
                        result.get("title_uk", title),
                        result.get("status_uk", status_en),
                        result.get("summary_uk", "Опис відсутній."),
                        categories
                    )
                except json.JSONDecodeError:
                    pass
            return title, status_en, title, ["OTHER"]
        else:
            err_msg = res.json().get('error', {}).get('message', res.text)
            return title, status_en, f"Помилка API ({res.status_code}): {err_msg}", ["OTHER"]

    except Exception as e:
        return title, status_en, f"Збій з'єднання: {e}", ["OTHER"]


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
    subjects = []

    url = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}?api_key={API_KEY}"
    res = requests.get(url, timeout=20)
    if res.status_code == 200:
        data = res.json().get("bill", {})
        sponsors = data.get("sponsors", [])
        if sponsors:
            sponsor = sponsors[0].get("fullName", "Невідомо")

        action = data.get("latestAction", {}).get("text", "")
        if action:
            status_en = action

    url_sum = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}/summaries?api_key={API_KEY}"
    res_sum = requests.get(url_sum, timeout=20)
    if res_sum.status_code == 200:
        summaries = res_sum.json().get("summaries", [])
        if summaries:
            summary_en = summaries[0].get("text", "")
            summary_en = re.sub(r'<[^>]+>', '', summary_en)

    url_subjects = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}/subjects?api_key={API_KEY}"
    try:
        res_subjects = requests.get(url_subjects, timeout=15)
        if res_subjects.status_code == 200:
            subject_data = res_subjects.json().get("subjects", {})
            subjects = [x.get("name", "") for x in subject_data.get("legislativeSubjects", []) if x.get("name")]
            policy_area = subject_data.get("policyArea", {})
            if isinstance(policy_area, dict) and policy_area.get("name"):
                subjects.append(policy_area["name"])
    except Exception as e:
        print(f"Не вдалося отримати теми для {congress}/{b_type}/{number}: {e}")

    return sponsor, status_en, summary_en, subjects


def category_labels(categories):
    labels = {"MILITARY": "#Оборона", "INTERNATIONAL": "#МіжнародніВідносини", "ECONOMICS": "#Економіка", "OTHER": "#Інше"}
    return " ".join(labels.get(category, "#Інше") for category in categories)


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
        json.dump(list(dict.fromkeys(processed)), f, ensure_ascii=False, indent=2)


def parse_update_date(bill):
    """Повертає дату оновлення в UTC або None, якщо дата відсутня/некоректна."""
    value = bill.get("updateDate")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_recent_bills():
    if not API_KEY:
        raise RuntimeError("Не задано секрет CONGRESS_API_KEY.")

    current_congress = get_current_congress()
    url = f"https://api.congress.gov/v3/bill/{current_congress}"
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    # Congress.gov документує fromDateTime/toDateTime для фільтрації за updateDate.
    # Відступ на добу потрібен, бо частина відповідей може містити updateDate
    # лише як YYYY-MM-DD, без часу, інакше можна втратити записи на межі доби.
    api_from = (cutoff - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    recent_bills = []
    seen_ids = set()
    limit = 250
    offset = 0
    page = 0

    print(
        f"Congress.gov: скликання {current_congress}; "
        f"шукаємо документи після {cutoff.isoformat()} "
        f"(API-вікно від {api_from.isoformat()} до {now.isoformat()})."
    )

    while True:
        params = {
            "api_key": API_KEY,
            "limit": limit,
            "offset": offset,
            "fromDateTime": api_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "toDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort": "updateDate+desc",
        }

        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError(f"Помилка з'єднання з Congress.gov API: {exc}") from exc

        if response.status_code != 200:
            excerpt = response.text[:500].replace("\\n", " ")
            raise RuntimeError(
                f"Congress.gov API повернув HTTP {response.status_code}: {excerpt}"
            )

        payload = response.json()
        bills = payload.get("bills", [])
        page += 1
        print(
            f"Congress.gov сторінка {page} (offset={offset}): "
            f"HTTP {response.status_code}; отримано документів: {len(bills)}."
        )

        if not bills:
            break

        page_dates = [parse_update_date(bill) for bill in bills]
        valid_dates = [value for value in page_dates if value is not None]
        if valid_dates:
            print(
                f"Діапазон updateDate на сторінці {page}: "
                f"{min(valid_dates).isoformat()} — {max(valid_dates).isoformat()}; "
                f"дат без розпізнавання: {len(bills) - len(valid_dates)}."
            )
        else:
            print(
                f"Увага: на сторінці {page} немає жодної розпізнаної updateDate."
            )

        for bill, bill_date in zip(bills, page_dates):
            raw_date = str(bill.get("updateDate", "")).strip()
            if bill_date is None:
                print(
                    "Пропущено запис без коректної updateDate: "
                    f"{bill.get('congress')}_{bill.get('type')}_{bill.get('number')}"
                )
                continue

            # Якщо API повернув повний timestamp, фільтруємо точно за 24 годинами.
            # Якщо повернулася лише календарна дата, точний час невідомий, тому
            # беремо попередню календарну дату як захисний запас від пропусків.
            if len(raw_date) <= 10:
                if bill_date.date() < (cutoff.date() - timedelta(days=1)):
                    continue
            elif bill_date < cutoff:
                continue

            bill_id = (
                f"{bill.get('congress')}_{bill.get('type')}_{bill.get('number')}"
            )
            if bill_id not in seen_ids:
                recent_bills.append(bill)
                seen_ids.add(bill_id)

        # Не зупиняємося за мінімальною датою сторінки: пагінація продовжується,
        # доки API повертає записи в межах запитаного діапазону.
        if len(bills) < limit:
            break
        offset += limit

    # Захисне сортування на стороні клієнта, незалежно від порядку API.
    recent_bills.sort(
        key=lambda bill: parse_update_date(bill) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    print(
        f"Congress.gov: знайдено документів у часовому вікні: {len(recent_bills)}."
    )
    if not recent_bills:
        print(
            "У заданому часовому вікні документів не знайдено. "
            "Перевір параметри fromDateTime/toDateTime, сортування та API-відповідь."
        )

    return recent_bills

def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Помилка з'єднання з Telegram API: {exc}") from exc

    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.status_code != 200 or not result.get("ok"):
        description = result.get("description", response.text[:500])
        raise RuntimeError(f"Telegram API не підтвердив публікацію (HTTP {response.status_code}): {description}")
    return True


if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Відсутні TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")
    if not API_KEY:
        raise RuntimeError("Відсутній CONGRESS_API_KEY.")

    processed = load_processed()
    print(f"У базі вже оброблених документів: {len(processed)}.")
    recent_bills = fetch_recent_bills()
    sent_count = 0
    already_processed = 0
    technical_count = 0
    failed_count = 0

    for bill in recent_bills:
        bill_id = f"{bill['congress']}_{bill['type']}_{bill['number']}"

        if bill_id in processed:
            already_processed += 1
            print(
                f"УЖЕ ОБРОБЛЕНО: {bill_id} | "
                f"оновлено={bill.get('updateDate', 'невідомо')} | "
                f"title={bill.get('title', '')}"
            )
            continue

        if is_technical(bill.get("title", "")) or is_technical(bill.get("latestAction", {}).get("text", "")):
            technical_count += 1
            print(f"Пропущено як технічний документ: {bill_id} | {bill.get('title', '')}")
            processed.append(bill_id)
            continue

        print(f"Обробка закону: {bill_id} | оновлено: {bill.get('updateDate', 'невідомо')}")

        try:
            sponsor, status_en, summary_en, subjects = get_bill_details(
                bill["congress"], bill["type"], bill["number"]
            )
            title_uk, status_uk, desc_uk, categories = process_with_ai(
                bill["title"], status_en, summary_en, subjects
            )

            public_url = get_public_url(bill["congress"], bill["type"], bill["number"])
            categories_text = category_labels(categories)
            message = (
                f"🇺🇸 <b>Конгрес США ({bill['congress']}-е скликання)</b>\n\n"
                f"📌 <b>{bill['type'].upper()} {bill['number']}</b>\n"
                f"<b>{escape_html(title_uk)}</b>\n"
                f"🏷 <b>Категорії:</b> {escape_html(categories_text)}\n\n"
                f"👤 <b>Ініціатор:</b> {escape_html(sponsor)}\n"
                f"📜 <b>Статус:</b> {escape_html(status_uk)}\n\n"
                f"💡 <b>Суть:</b> {escape_html(desc_uk)}\n\n"
                f"🔗 <a href='{public_url}'>Оригінал документа</a>"
            )

            send_telegram(message)
            # Фіксуємо ID лише після підтвердження успішної публікації.
            processed.append(bill_id)
            sent_count += 1
            print(f"Telegram підтвердив публікацію: {bill_id}")
            time.sleep(3)
        except Exception as exc:
            failed_count += 1
            print(f"ПОМИЛКА для {bill_id}: {exc}")
            # Невдалий документ не потрапляє до processed і може бути повторений.
            continue

    save_processed(processed)
    print(
        "ПІДСУМОК: "
        f"знайдено={len(recent_bills)}; "
        f"вже оброблено={already_processed}; "
        f"технічні пропуски={technical_count}; "
        f"успішно опубліковано={sent_count}; "
        f"помилки={failed_count}."
    )
    if failed_count:
        raise RuntimeError(f"Не вдалося обробити/опублікувати {failed_count} документ(ів); див. журнал вище.")
