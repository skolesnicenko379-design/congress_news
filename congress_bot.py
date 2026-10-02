import os
import json
import requests
import re
from datetime import datetime, timezone, timedelta

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("CONGRESS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

STATE_FILE = "processed_bills.json"

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

def escape_md(text):
    if not text: return ""
    return str(text).replace('*', '').replace('_', '').replace('`', '').replace('[', '(').replace(']', ')')

def process_with_ai(title, status_en, summary_en):
    """Використовує Groq для перекладу. Стійкий до помилок формату."""
    if not GROQ_API_KEY:
        print("Попередження: Немає GROQ_API_KEY! Передаємо оригінали.")
        return title, status_en, "Переклад неможливий (немає ключа)."
        
    ai_prompt = f"""Ти — професійний політичний аналітик. Переклади та поясни законопроєкт Конгресу США.
Назва: {title}
Статус: {status_en}
Офіційне резюме: {summary_en if summary_en else 'Відсутнє.'}

Поверни виключно JSON об'єкт з трьома ключами:
1. "title_uk" - точний переклад назви.
2. "status_uk" - точний переклад статусу.
3. "summary_uk" - коротке і чітке пояснення українською (1-2 речення), про що цей закон. Якщо "Офіційне резюме" відсутнє, зроби висновок самостійно на основі назви.

Не пиши нічого крім JSON."""

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": ai_prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }

    try:
        res = requests.post(url, headers=headers, json=payload)
        if res.status_code == 200:
            data = res.json()["choices"][0]["message"]["content"]
            print(f"Відповідь ШІ: {data}") # Логуємо для дебагу
            result = json.loads(data)
            return result.get("title_uk", title), result.get("status_uk", status_en), result.get("summary_uk", "Опис відсутній.")
        else:
            print(f"Помилка Groq API ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Помилка обробки ШІ: {e}")
        
    return title, status_en, "Помилка генерації опису."

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
        print(f"Помилка API Конгресу: {response.status_code}")
        return []
        
    bills = response.json().get("bills", [])
    recent_bills = []
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=2) 
    
    for bill in bills:
        if not bill.get("updateDate"): continue
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
        print("Помилка: Немає Telegram токенів!")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    res = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True})
    if res.status_code != 200:
        print(f"Помилка Telegram API: {res.text}")

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
            
        print(f"Обробка нового закону через ШІ: {bill_id}")
        
        sponsor, status_en, summary_en = get_bill_details(bill["congress"], bill["type"], bill["number"])
        title_uk, status_uk, desc_uk = process_with_ai(bill["title"], status_en, summary_en)
        
        public_url = get_public_url(bill["congress"], bill["type"], bill["number"])
        
        message = (
            f"🇺🇸 *Конгрес США ({bill['congress']}-е скликання)*\n\n"
            f"📌 *{bill['type'].upper()} {bill['number']}*\n"
            f"*{escape_md(title_uk)}*\n\n"
            f"👤 *Ініціатор:* {escape_md(sponsor)}\n"
            f"📜 *Статус:* {escape_md(status_uk)}\n\n"
            f"💡 *Суть:* {escape_md(desc_uk)}\n\n"
            f"🔗 [Оригінал документа]({public_url})"
        )
        
        send_telegram(message)
        processed.append(bill_id)
        sent_count += 1
        
    save_processed(processed)
    print(f"Успішно оброблено та відправлено: {sent_count}")
