import os
import json
import requests
import re
from datetime import datetime, timezone, timedelta
from deep_translator import GoogleTranslator

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("CONGRESS_API_KEY")

STATE_FILE = "processed_bills.json"
translator = GoogleTranslator(source='en', target='uk')

def get_current_congress():
    now = datetime.now()
    year = now.year
    if year % 2 != 0 and (now.month == 1 and now.day < 3):
        effective_year = year - 1
    else:
        effective_year = year
    return str((effective_year - 1789) // 2 + 1)

def is_technical(title):
    """Фільтрує технічні закони та дрібні резолюції"""
    t = title.lower()
    stop_words = [
        "post office", "postal service", "technical correction",
        "express the sense", "expressing the sense", "medal",
        "mint coins", "commemorative", "designate the facility",
        "designate the building", "naming a", "renaming"
    ]
    return any(phrase in t for phrase in stop_words)

def escape_md(text):
    """Очищає текст від спецсимволів, щоб не зламати розмітку Telegram"""
    if not text: return ""
    return text.replace('*', '').replace('_', '').replace('`', '').replace('[', '(').replace(']', ')')

def get_public_url(congress, bill_type, number):
    """Генерує пряме посилання на сайт Конгресу (а не на API)"""
    type_map = {
        "hr": "house-bill", "s": "senate-bill",
        "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
        "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution",
        "hres": "house-resolution", "sres": "senate-resolution"
    }
    full_type = type_map.get(bill_type.lower(), "bill")
    return f"https://www.congress.gov/bill/{congress}th-congress/{full_type}/{number}"

def get_bill_details(congress, b_type, number):
    """Робить додаткові запити, щоб отримати ініціатора, статус та резюме"""
    sponsor = "Невідомо"
    status_en = "Introduced"
    summary_en = ""
    
    # 1. Отримуємо ініціатора та статус
    url = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}?api_key={API_KEY}"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json().get("bill", {})
        sponsors = data.get("sponsors", [])
        if sponsors:
            s = sponsors[0]
            name = s.get("fullName", "")
            party = s.get("party", "")
            state = s.get("state", "")
            sponsor = f"{name} ({party}-{state})" if party else name
                
        action = data.get("latestAction", {}).get("text", "")
        if action:
            status_en = action
            
    # 2. Отримуємо офіційне резюме (якщо воно вже є)
    url_sum = f"https://api.congress.gov/v3/bill/{congress}/{b_type.lower()}/{number}/summaries?api_key={API_KEY}"
    res_sum = requests.get(url_sum)
    if res_sum.status_code == 200:
        summaries = res_sum.json().get("summaries", [])
        if summaries:
            summary_en = summaries[0].get("text", "")
            
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
    params = {"api_key": API_KEY, "limit": 20}
    
    response = requests.get(url, params=params)
    if response.status_code != 200:
        return []
        
    bills = response.json().get("bills", [])
    recent_bills = []
    
    # Змінив на 2 дні (48 годин), щоб ти точно побачив свіжі закони, але не витратив ліміт API
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
    if not TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True})

if __name__ == "__main__":
    processed = load_processed()
    recent_bills = fetch_recent_bills()
    sent_count = 0
    
    for bill in recent_bills:
        bill_id = f"{bill['congress']}_{bill['type']}_{bill['number']}"
        
        # Якщо вже відправляли — пропускаємо
        if bill_id in processed:
            continue
            
        # Якщо це технічний закон про пошту чи монети — позначаємо як оброблений і пропускаємо
        if is_technical(bill.get("title", "")):
            processed.append(bill_id)
            continue
            
        print(f"Обробка нового закону: {bill_id}")
        
        # Отримуємо деталі: ініціатора, статус, текст
        sponsor, status_en, summary_en = get_bill_details(bill["congress"], bill["type"], bill["number"])
        
        # Робимо переклад
        try:
            title_uk = escape_md(translator.translate(bill["title"]))
            status_uk = escape_md(translator.translate(status_en))
            
            if summary_en:
                # Очищаємо від HTML-тегів (наприклад <p>)
                clean_sum = re.sub(r'<[^>]+>', '', summary_en)
                # Беремо перші два речення для короткого викладу
                sentences = clean_sum.split('. ')
                short_sum = '. '.join(sentences[:2]) + '.' if len(sentences) > 1 else clean_sum
                desc_uk = escape_md(translator.translate(short_sum))
            else:
                desc_uk = "Офіційне резюме ще не опубліковано Конгресом (зазвичай з'являється через кілька днів після внесення)."
                
        except Exception as e:
            print(f"Помилка перекладу: {e}")
            title_uk = escape_md(bill["title"])
            status_uk = escape_md(status_en)
            desc_uk = "Переклад тимчасово недоступний."
            
        public_url = get_public_url(bill["congress"], bill["type"], bill["number"])
        
        message = (
            f"🇺🇸 *Конгрес США ({bill['congress']}-е скликання)*\n\n"
            f"📌 *{bill['type'].upper()} {bill['number']}*\n"
            f"*{title_uk}*\n\n"
            f"👤 *Ініціатор:* {sponsor}\n"
            f"📜 *Статус:* {status_uk}\n\n"
            f"💡 *Суть:* {desc_uk}\n\n"
            f"🔗 [Оригінал документа]({public_url})"
        )
        
        send_telegram(message)
        processed.append(bill_id)
        sent_count += 1
        
    save_processed(processed)
    print(f"Успішно оброблено та відправлено: {sent_count}")
