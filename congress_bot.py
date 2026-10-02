import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ТЕПЕР ЗМІННІ ЗБІГАЮТЬСЯ З WORKFLOW.YML
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("CONGRESS_API_KEY")

STATE_FILE = "processed_bills.json"

def get_current_congress():
    """Математично точне визначення Конгресу (з урахуванням зміни 3 січня непарних років)"""
    now = datetime.now()
    year = now.year
    
    # Якщо це непарний рік, але ще не настало 3 січня — працює попереднє скликання
    if year % 2 != 0 and (now.month == 1 and now.day < 3):
        effective_year = year - 1
    else:
        effective_year = year
        
    # Формула: (Поточний рік - Рік першого Конгресу) // 2 + 1
    congress_number = (effective_year - 1789) // 2 + 1
    
    return str(congress_number)

def load_processed():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_processed(processed):
    # Зберігаємо останні 100 унікальних ID, щоб файл не роздувався
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(processed[-100:], f, ensure_ascii=False, indent=2)

def fetch_recent_bills():
    current_congress = get_current_congress()
    print(f"Поточний визначений Конгрес: {current_congress}")
    
    # Вшиваємо сортування прямо в URL, щоб уникнути багів з кодуванням знака "+"
    url = f"https://api.congress.gov/v3/bill/{current_congress}?sort=updateDate+desc"
    
    params = {
        "api_key": API_KEY,
        "limit": 25
    }
    
    response = requests.get(url, params=params)
    
    if response.status_code != 200:
        print(f"Помилка API Конгресу: {response.status_code}")
        return []
        
    data = response.json()
    bills = data.get("bills", [])
    
    recent_bills = []
    # Залишаємо 30 днів для перевірки. Коли переконаєшся, що все працює — зміни на hours=24
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=30)
    
    print(f"Отримано {len(bills)} законопроєктів від API. Аналізуємо дати...")
    
    for bill in bills:
        update_date_str = bill.get("updateDate")
        if not update_date_str:
            continue
            
        try:
            # Парсимо дату
            bill_date = datetime.fromisoformat(update_date_str.replace("Z", "+00:00"))
            
            # Якщо API повернуло "наївну" дату без часового поясу, примусово робимо її UTC
            if bill_date.tzinfo is None:
                bill_date = bill_date.replace(tzinfo=timezone.utc)
                
            # Виводимо в логи GitHub кожну отриману дату для діагностики
            print(f"Перевірка: {bill['type']} {bill['number']} | Оновлено: {update_date_str}")
                
        except Exception:
            continue
            
        # Суворий фільтр
        if bill_date >= one_day_ago:
            bill_id = f"{bill['congress']}_{bill['type']}_{bill['number']}"
            recent_bills.append({
                "id": bill_id,
                "congress": bill["congress"],
                "type": bill["type"].upper(),
                "number": bill["number"],
                "title": bill.get("title", "Без назви"),
                "url": bill.get("url", ""),
                "updateDate": update_date_str
            })
            
    return recent_bills

def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram токен або ID каналу не налаштовані!")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload)

if __name__ == "__main__":
    processed = load_processed()
    new_bills = fetch_recent_bills()
    
    sent_count = 0
    for bill in new_bills:
        if bill["id"] not in processed:
            message = (
                f"🇺🇸 *Оновлення в Конгресі США ({bill['congress']}-е скликання)*\n\n"
                f"📌 *{bill['type']} {bill['number']}*\n"
                f"{bill['title']}\n\n"
                f"🕒 Оновлено: `{bill['updateDate']}`"
            )
            send_telegram(message)
            processed.append(bill["id"])
            sent_count += 1
            
    save_processed(processed)
    print(f"Успішно оброблено нових законопроєктів: {sent_count}")
