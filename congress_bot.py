import os
import json
import requests
import re
import time
import hashlib
import unicodedata
from datetime import datetime, timezone, timedelta

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("CONGRESS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip(" .")

STATE_FILE = "processed_bills.json"
ACTION_STATE_FILE = "processed_actions.json"

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

_CACHED_MODEL = None
_CACHED_MODEL_ERROR = None


class AIProcessingError(RuntimeError):
    """AI-помилка, яку заборонено оформлювати як новину."""


def request_with_retry(
    method, url, *, timeout=20, retries=3, retry_connection_errors=True, **kwargs
):
    """Повторює тимчасові HTTP-помилки з експоненційною затримкою."""
    retryable_statuses = {429, 500, 502, 503, 504}
    for attempt in range(retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException:
            if not retry_connection_errors or attempt >= retries:
                raise
            wait_seconds = min(2 ** attempt, 8)
            print(
                f"Мережева помилка {method.upper()} {url}; "
                f"повтор {attempt + 1}/{retries} через {wait_seconds} с."
            )
            time.sleep(wait_seconds)
            continue

        if response.status_code not in retryable_statuses or attempt >= retries:
            return response

        retry_after = response.headers.get("Retry-After", "")
        try:
            wait_seconds = max(0, min(float(retry_after), 30))
        except (TypeError, ValueError):
            wait_seconds = min(2 ** attempt, 8)
        print(
            f"HTTP {response.status_code} {method.upper()} {url}; "
            f"повтор {attempt + 1}/{retries} через {wait_seconds:g} с."
        )
        time.sleep(wait_seconds)
    raise RuntimeError(f"Запит {method.upper()} {url} завершився без відповіді.")



def get_best_model():
    """Обирає одну доступну JSON-сумісну модель на весь запуск."""
    global _CACHED_MODEL, _CACHED_MODEL_ERROR
    if _CACHED_MODEL:
        return _CACHED_MODEL
    if _CACHED_MODEL_ERROR:
        raise AIProcessingError(_CACHED_MODEL_ERROR)

    url = "https://api.groq.com/openai/v1/models"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    try:
        response = request_with_retry("GET", url, headers=headers, timeout=10, retries=2)
        if response.status_code != 200:
            raise AIProcessingError(
                f"Список моделей Groq недоступний: HTTP {response.status_code}."
            )
        available = {item.get("id") for item in response.json().get("data", [])}
        print(f"Доступні моделі Groq: {sorted(item for item in available if item)}")
        for candidate in JSON_CAPABLE_MODELS:
            if candidate in available:
                _CACHED_MODEL = candidate
                print(f"Обрано модель Groq: {_CACHED_MODEL}")
                return _CACHED_MODEL
        raise AIProcessingError(
            "Серед доступних моделей немає перевіреної JSON-сумісної моделі."
        )
    except Exception as exc:
        _CACHED_MODEL_ERROR = f"Не вдалося вибрати модель Groq: {exc}"
        raise AIProcessingError(_CACHED_MODEL_ERROR) from exc


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
    """Перекладає документ або повертає контрольовану помилку без публікації."""
    if not GROQ_API_KEY:
        raise AIProcessingError("Не задано GROQ_API_KEY.")

    summary_compact = " ".join((summary_en or "").split())[:4000]
    subject_items = [
        " ".join(str(item).split())[:100]
        for item in (subjects or [])
        if str(item).strip()
    ][:15]
    subjects_text = ", ".join(subject_items) or "Офіційні тематичні рубрики відсутні."

    ai_prompt = f"""Ти — професійний політичний аналітик. Переклади та поясни законопроєкт Конгресу США.
Назва: {title}
Статус: {status_en}
Офіційне резюме: {summary_compact if summary_compact else 'Відсутнє.'}
Офіційні тематичні рубрики Congress.gov: {subjects_text}

Поверни об'єкт JSON з чотирма ключами:
1. "title_uk" — точний переклад назви.
2. "status_uk" — точний переклад статусу.
3. "summary_uk" — коротке пояснення українською (1–2 речення). Якщо офіційне резюме відсутнє, не вигадуй деталей.
4. "categories" — масив з однієї або кількох категорій: "MILITARY", "INTERNATIONAL", "ECONOMICS", "OTHER". MILITARY: оборонний бюджет, збройні сили, зброя, боєприпаси, військові технології, оборонна промисловість, військові закупівлі та дослідження. INTERNATIONAL: зовнішня політика, дипломатія, договори, санкції, міжнародні організації, відносини з іншими державами, міжнародна допомога, зовнішня торгівля та обмеження. ECONOMICS: федеральний бюджет і борг, податки, внутрішня економічна політика, банки, фінанси, енергетика, промисловість, праця та внутрішні ринки. OTHER: решта тем. Дозволено кілька категорій, якщо є реальний змістовний перетин. Не класифікуй як INTERNATIONAL лише через географічну назву або іноземне походження людини.
Відповідай лише валідним JSON без Markdown."""

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    model_name = get_best_model()

    def call(use_json_mode):
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": ai_prompt}],
            "temperature": 0.2,
            "max_completion_tokens": 500,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}
        return request_with_retry(
            "POST", url, headers=headers, json=payload, timeout=60, retries=3
        )

    try:
        response = call(use_json_mode=True)
        err_text = response.text.lower()
        json_mode_unsupported = (
            response.status_code == 400
            and "response_format" in err_text
            and ("not supported" in err_text or "unsupported" in err_text)
        )
        if json_mode_unsupported:
            print(f"Модель {model_name} відхилила JSON mode; повторюю без нього.")
            response = call(use_json_mode=False)

        if response.status_code != 200:
            try:
                err = response.json().get("error", {})
                message = err.get("message", response.text)
            except (ValueError, AttributeError):
                message = response.text
            raise AIProcessingError(
                f"Groq API повернув HTTP {response.status_code}: {str(message)[:300]}"
            )

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise AIProcessingError("Groq повернув відповідь без choices.")
        content = choices[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise AIProcessingError("Groq повернув порожню відповідь.")

        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{[\s\S]*\}", content)
            if not match:
                raise AIProcessingError("Groq повернув невалідний JSON.")
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise AIProcessingError("Groq повернув невалідний JSON.") from exc

        title_uk = result.get("title_uk")
        status_uk = result.get("status_uk")
        summary_uk = result.get("summary_uk")
        if not all(isinstance(value, str) and value.strip() for value in
                   (title_uk, status_uk, summary_uk)):
            raise AIProcessingError("У JSON Groq немає всіх обов'язкових текстових полів.")

        valid_categories = {"MILITARY", "INTERNATIONAL", "ECONOMICS", "OTHER"}
        categories = result.get("categories", ["OTHER"])
        if not isinstance(categories, list):
            categories = ["OTHER"]
        categories = [
            item for item in categories
            if isinstance(item, str) and item in valid_categories
        ]
        if not categories:
            categories = ["OTHER"]
        if "OTHER" in categories and len(categories) > 1:
            categories.remove("OTHER")

        usage = body.get("usage", {})
        if usage:
            print(
                f"Groq usage: input={usage.get('prompt_tokens', 'n/a')}, "
                f"output={usage.get('completion_tokens', 'n/a')}, "
                f"total={usage.get('total_tokens', 'n/a')}."
            )
        return (
            title_uk.strip(),
            status_uk.strip(),
            summary_uk.strip(),
            list(dict.fromkeys(categories)),
        )
    except AIProcessingError:
        raise
    except requests.RequestException as exc:
        raise AIProcessingError(f"Мережева помилка Groq: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise AIProcessingError(f"Некоректна відповідь Groq: {exc}") from exc


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

    base_url = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}"
    headers = {"X-API-Key": API_KEY}
    response = request_with_retry(
        "GET", base_url, params={"api_key": API_KEY}, headers=headers,
        timeout=20, retries=2
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Congress.gov не повернув документ {congress}/{b_type}/{number}: "
            f"HTTP {response.status_code}."
        )

    data = response.json().get("bill", {})
    sponsors = data.get("sponsors", [])
    if sponsors:
        sponsor = sponsors[0].get("fullName", "Невідомо")
    action = data.get("latestAction", {}).get("text", "")
    if action:
        status_en = action

    summary_response = request_with_retry(
        "GET", f"{base_url}/summaries", params={"api_key": API_KEY}, headers=headers,
        timeout=20, retries=2
    )
    if summary_response.status_code == 200:
        summaries = summary_response.json().get("summaries", [])
        if summaries:
            summary_en = summaries[0].get("text", "")
            summary_en = re.sub(r"<[^>]+>", " ", summary_en)
            summary_en = " ".join(summary_en.split())
    elif summary_response.status_code != 404:
        raise RuntimeError(
            f"Congress.gov не повернув резюме {congress}/{b_type}/{number}: "
            f"HTTP {summary_response.status_code}."
        )

    subjects_response = request_with_retry(
        "GET", f"{base_url}/subjects", params={"api_key": API_KEY}, headers=headers,
        timeout=15, retries=2
    )
    if subjects_response.status_code == 200:
        subject_data = subjects_response.json().get("subjects", {})
        subjects = [
            item.get("name", "")
            for item in subject_data.get("legislativeSubjects", [])
            if item.get("name")
        ]
        policy_area = subject_data.get("policyArea", {})
        if isinstance(policy_area, dict) and policy_area.get("name"):
            subjects.append(policy_area["name"])
    elif subjects_response.status_code != 404:
        print(
            f"Не вдалося отримати теми {congress}/{b_type}/{number}: "
            f"HTTP {subjects_response.status_code}."
        )

    return sponsor, status_en, summary_en, subjects


def category_labels(categories):
    labels = {"MILITARY": "#Оборона", "INTERNATIONAL": "#МіжнародніВідносини", "ECONOMICS": "#Економіка", "OTHER": "#Інше"}
    return " ".join(labels.get(category, "#Інше") for category in categories)


def load_processed():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, ValueError) as exc:
            print(f"Не вдалося прочитати {STATE_FILE}: {exc}.")
    return []


def save_processed(processed):
    temporary_path = f"{STATE_FILE}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(list(dict.fromkeys(processed)), handle, ensure_ascii=False, indent=2)
    os.replace(temporary_path, STATE_FILE)


def load_processed_actions():
    default_state = {"version": 1, "actions": [], "tracked_bills": []}
    if not os.path.exists(ACTION_STATE_FILE):
        return default_state
    try:
        with open(ACTION_STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("очікувався JSON-об'єкт")
        return {
            "version": 1,
            "actions": data.get("actions", []) if isinstance(data.get("actions"), list) else [],
            "tracked_bills": data.get("tracked_bills", [])
            if isinstance(data.get("tracked_bills"), list) else [],
        }
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"Не вдалося прочитати {ACTION_STATE_FILE}; зупиняюся, щоб не створити дублікати: {exc}"
        ) from exc


def save_processed_actions(actions, tracked_bills):
    state = {
        "version": 1,
        "actions": list(dict.fromkeys(actions))[-20000:],
        "tracked_bills": sorted(set(tracked_bills)),
    }
    temporary_path = f"{ACTION_STATE_FILE}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    os.replace(temporary_path, ACTION_STATE_FILE)


def parse_update_date(bill):
    value = bill.get("updateDate")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def get_activity_date_raw(bill):
    latest_action = bill.get("latestAction")
    if isinstance(latest_action, dict):
        value = latest_action.get("actionDate")
        if value:
            return str(value).strip()
    return str(bill.get("introducedDate") or "").strip()


def parse_activity_date(bill):
    value = get_activity_date_raw(bill)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def is_recent_activity(bill, now=None):
    """Строго відсікає дії старші за 24 години та дати з майбутнього.

    Для actionDate у форматі YYYY-MM-DD час відсутній. Щоб не видавати старі
    документи як нові, такі записи приймаються лише за поточну UTC-дату.
    """
    now = now or datetime.now(timezone.utc)
    raw_value = get_activity_date_raw(bill)
    activity_date = parse_activity_date(bill)
    if activity_date is None or activity_date > now:
        return False
    if len(raw_value) <= 10:
        return activity_date.date() == now.date()
    return activity_date >= now - timedelta(hours=24)


def get_action_fingerprint(bill):
    """Хешує конкретну дію; один законопроєкт може мати кілька різних дій."""
    bill_id = f"{bill.get('congress')}_{bill.get('type')}_{bill.get('number')}"
    latest_action = bill.get("latestAction")
    action_text = latest_action.get("text", "") if isinstance(latest_action, dict) else ""
    normalized_action = " ".join(
        unicodedata.normalize("NFKC", str(action_text)).casefold().split()
    ) or "introduced"
    raw_key = f"{bill_id}|{get_activity_date_raw(bill)}|{normalized_action}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def fetch_recent_bills():
    if not API_KEY:
        raise RuntimeError("Не задано секрет CONGRESS_API_KEY.")

    current_congress = get_current_congress()
    url = f"https://api.congress.gov/v3/bill/{current_congress}"
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    api_from = (cutoff - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    recent_bills = []
    seen_ids = set()
    stale_count = 0
    missing_activity_count = 0
    invalid_update_count = 0
    limit = 250
    offset = 0
    page = 0

    print(
        f"Congress.gov: скликання {current_congress}; відбір законодавчих дій "
        f"за останні 24 години до {now.isoformat()}."
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
        response = request_with_retry("GET", url, params=params, timeout=30, retries=3)
        if response.status_code != 200:
            excerpt = response.text[:300].replace("\n", " ")
            raise RuntimeError(
                f"Congress.gov API повернув HTTP {response.status_code}: {excerpt}"
            )

        bills = response.json().get("bills", [])
        page += 1
        print(
            f"Congress.gov сторінка {page} (offset={offset}): "
            f"отримано документів={len(bills)}."
        )
        if not bills:
            break

        page_dates = [parse_update_date(bill) for bill in bills]
        for bill, update_date in zip(bills, page_dates):
            raw_update = str(bill.get("updateDate", "")).strip()
            if update_date is None:
                invalid_update_count += 1
                continue
            # API-вікно ширше за 24 години; фактичну свіжість визначає actionDate нижче.
            if len(raw_update) <= 10:
                if update_date.date() < api_from.date():
                    continue
            elif update_date < api_from or update_date > now + timedelta(minutes=5):
                continue

            activity_raw = get_activity_date_raw(bill)
            if not activity_raw:
                missing_activity_count += 1
                continue
            if not is_recent_activity(bill, now):
                stale_count += 1
                continue

            bill_id = f"{bill.get('congress')}_{bill.get('type')}_{bill.get('number')}"
            if bill_id not in seen_ids:
                recent_bills.append(bill)
                seen_ids.add(bill_id)

        if len(bills) < limit:
            break
        offset += limit

    recent_bills.sort(
        key=lambda bill: parse_activity_date(bill)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    print(
        f"Congress.gov: свіжих дій={len(recent_bills)}; "
        f"відсіяно старих/непідтверджених={stale_count}; "
        f"без дати дії={missing_activity_count}; "
        f"без коректної updateDate={invalid_update_count}."
    )
    if not recent_bills:
        print("За суворим правилом останніх 24 годин законодавчих дій не знайдено.")
    return recent_bills


def send_telegram(message_text):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Не задано TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    # Не повторюємо POST після мережевого exception: невідомо, чи Telegram уже
    # прийняв повідомлення. HTTP-відповіді 429/5xx безпечно обробляємо retry.
    response = request_with_retry(
        "POST",
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
        retries=2,
        retry_connection_errors=False,
    )
    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.status_code != 200 or not result.get("ok"):
        description = result.get("description", response.text[:300])
        raise RuntimeError(
            f"Telegram API не підтвердив публікацію "
            f"(HTTP {response.status_code}): {description}"
        )
    return True



if __name__ == "__main__":
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Відсутні TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID.")
    if not API_KEY:
        raise RuntimeError("Відсутній CONGRESS_API_KEY.")

    processed = load_processed()
    action_state = load_processed_actions()
    processed_actions = set(action_state["actions"])
    tracked_bills = set(action_state["tracked_bills"])
    print(f"Legacy ID документів: {len(processed)}.")
    print(f"Fingerprint законодавчих дій: {len(processed_actions)}.")

    recent_bills = fetch_recent_bills()
    sent_count = 0
    already_processed = 0
    legacy_baselined = 0
    technical_count = 0
    failed_count = 0

    for bill in recent_bills:
        bill_id = f"{bill['congress']}_{bill['type']}_{bill['number']}"
        action_key = get_action_fingerprint(bill)

        if action_key in processed_actions:
            already_processed += 1
            print(f"УЖЕ ОПУБЛІКОВАНА ЦЯ ДІЯ: {bill_id}")
            continue

        # Разово базуємо старі ID, щоб не повторити пости після оновлення.
        # Наступна дія цього документа матиме інший fingerprint і буде опублікована.
        if bill_id in processed and bill_id not in tracked_bills:
            processed_actions.add(action_key)
            tracked_bills.add(bill_id)
            legacy_baselined += 1
            print(f"Створено початковий fingerprint старого документа: {bill_id}")
            continue

        title_en = bill.get("title", "")
        latest_action = bill.get("latestAction", {})
        latest_action_text = (
            latest_action.get("text", "") if isinstance(latest_action, dict) else ""
        )
        if is_technical(title_en) or is_technical(latest_action_text):
            technical_count += 1
            processed_actions.add(action_key)
            tracked_bills.add(bill_id)
            if bill_id not in processed:
                processed.append(bill_id)
            print(f"Пропущено як технічний документ: {bill_id} | {title_en}")
            continue

        print(
            f"Обробка свіжої законодавчої дії: {bill_id}; "
            f"actionDate={get_activity_date_raw(bill)}"
        )
        try:
            sponsor, status_en, summary_en, subjects = get_bill_details(
                bill["congress"], bill["type"], bill["number"]
            )
            # process_with_ai кидає помилку; вона ніколи не піде в текст новини.
            title_uk, status_uk, desc_uk, categories = process_with_ai(
                title_en, status_en, summary_en, subjects
            )
            public_url = get_public_url(
                bill["congress"], bill["type"], bill["number"]
            )
            categories_text = category_labels(categories)
            action_date = get_activity_date_raw(bill) or "невідомо"
            introduced_date = str(bill.get("introducedDate") or "").strip()

            message_lines = [
                f"🇺🇸 <b>Конгрес США ({bill['congress']}-е скликання)</b>",
                "",
                f"📌 <b>{bill['type'].upper()} {bill['number']}</b>",
                f"<b>{escape_html(title_uk)}</b>",
                f"🏷 <b>Категорії:</b> {escape_html(categories_text)}",
                f"📅 <b>Дата законодавчої дії:</b> {escape_html(action_date)}",
            ]
            if introduced_date and introduced_date != action_date:
                message_lines.append(
                    f"🗂 <b>Дата внесення:</b> {escape_html(introduced_date)}"
                )
            message_lines.extend([
                "",
                f"👤 <b>Ініціатор:</b> {escape_html(sponsor)}",
                f"📜 <b>Статус / дія:</b> {escape_html(status_uk)}",
                "",
                f"💡 <b>Суть:</b> {escape_html(desc_uk)}",
                "",
                f"🔗 <a href='{public_url}'>Оригінал документа</a>",
            ])
            send_telegram("\n".join(message_lines))

            # Зберігаємо fingerprint тільки після підтвердженої публікації.
            processed_actions.add(action_key)
            tracked_bills.add(bill_id)
            if bill_id not in processed:
                processed.append(bill_id)
            sent_count += 1
            print(f"Telegram підтвердив публікацію: {bill_id}")
            time.sleep(3)
        except Exception as exc:
            failed_count += 1
            print(f"ПОМИЛКА для {bill_id}; дія залишена для повторної спроби: {exc}")
            continue

    save_processed(processed)
    save_processed_actions(processed_actions, tracked_bills)
    print(
        "ПІДСУМОК: "
        f"свіжих дій={len(recent_bills)}; "
        f"повторів/старих fingerprints={already_processed}; "
        f"базових fingerprints legacy={legacy_baselined}; "
        f"технічних пропусків={technical_count}; "
        f"успішних публікацій={sent_count}; помилок={failed_count}."
    )
    if failed_count:
        raise RuntimeError(
            f"Не вдалося обробити/опублікувати {failed_count} документ(ів); див. журнал вище."
        )
