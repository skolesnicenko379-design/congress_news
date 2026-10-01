import json
import os
import time
from deep_translator import GoogleTranslator
import requests

# --- КОНФІГУРАЦІЯ ---
CONGRESS_API_KEY = os.getenv("CONGRESS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([CONGRESS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    raise ValueError("Помилка: Не всі секретні ключі знайдені у змінних оточення!")

DB_FILE = "processed_bills.json"
BASE_URL = "https://api.congress.gov/v3"

# Ініціалізація перекладача (з англійської на українську)
translator = GoogleTranslator(source="en", target="uk")


def load_processed_bills():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_processed_bills(processed_set):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(processed_set), f, ensure_ascii=False, indent=2)


def is_target_bill(title):
    """Фільтрує законопроєкти за ключовими словами (відсікає сміття)."""
    if not title:
        return False

    title_lower = title.lower()

    stop_words = [
        "postal service",
        "post office",
        "commemorative coin",
        "to designate the facility",
        "to rename",
        "for the relief of",
        "to redesignate",
        "mint coins",
    ]
    if any(sw in title_lower for sw in stop_words):
        return False

    return True


def get_bill_details(congress, bill_type, bill_number):
    url = f"{BASE_URL}/bill/{congress}/{bill_type.lower()}/{bill_number}"
    params = {"api_key": CONGRESS_API_KEY, "format": "json"}
    try:
        res = requests.get(url, params=params, timeout=15)
        if res.status_code == 200:
            return res.json().get("bill", {})
    except Exception as e:
        print(f"Помилка отримання деталей {bill_type}{bill_number}: {e}")
    return {}


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=15)
    return response.status_code == 200


def run_tracker():
    print("Запуск перевірки Congress API...")
    processed_bills = load_processed_bills()

    params = {
        "api_key": CONGRESS_API_KEY,
        "format": "json",
        "limit": 15,
        "sort": "updateDate+desc",
    }

    try:
        response = requests.get(f"{BASE_URL}/bill", params=params, timeout=15)
        if response.status_code != 200:
            print(f"Помилка Congress API: {response.status_code}")
            return

        data = response.json()
        bills = data.get("bills", [])

        for item in reversed(bills):
            bill_type = item.get("type", "").upper()

            if bill_type not in ["HR", "S"]:
                continue

            congress = item.get("congress")
            number = item.get("number")
            bill_id = f"{congress}_{bill_type}_{number}"

            if bill_id in processed_bills:
                continue

            details = get_bill_details(congress, bill_type, number)
            title = details.get("title") or item.get("title", "Без назви")

            # Фільтруємо сміття
            if not is_target_bill(title):
                processed_bills.add(bill_id)
                save_processed_bills(processed_bills)
                print(f"Відфільтровано: {bill_id}")
                continue

            sponsors = details.get("sponsors", [])
            sponsor_name = (
                f"{sponsors[0].get('fullName')} ({sponsors[0].get('party')}-{sponsors[0].get('state')})"
                if sponsors
                else "Не вказано"
            )

            latest_action = details.get("latestAction", {})
            action_text = latest_action.get("text", "Внесено")
            action_date = latest_action.get(
                "actionDate", item.get("updateDate", "Невідомо")
            )

            # Переклад англійських текстів українською
            try:
                uk_title = translator.translate(title) or title
            except Exception:
                uk_title = title

            try:
                uk_action_text = translator.translate(action_text) or action_text
            except Exception:
                uk_action_text = action_text

            chamber_path = (
                "house-bill" if bill_type == "HR" else "senate-bill"
            )
            bill_url = f"https://www.congress.gov/bill/{congress}th-congress/{chamber_path}/{number}"
            chamber_name = (
                "Палата представників" if bill_type == "HR" else "Сенат"
            )

            post = (
                f"🇺🇸 <b>Новий законопроєкт</b>\n\n"
                f"<b>Документ:</b> {bill_type}.{number} ({chamber_name})\n"
                f"<b>Спонсор:</b> {sponsor_name}\n"
                f"<b>Статус ({action_date}):</b> {uk_action_text}\n\n"
                f"<b>Суть:</b> {uk_title}\n\n"
                f"🔗 <a href='{bill_url}'>Посилання на документ</a>"
            )

            if send_telegram_message(post):
                print(f"Опубліковано: {bill_id}")
                processed_bills.add(bill_id)
                save_processed_bills(processed_bills)
                time.sleep(2)

    except Exception as e:
        print(f"Помилка: {e}")


if __name__ == "__main__":
    run_tracker()
