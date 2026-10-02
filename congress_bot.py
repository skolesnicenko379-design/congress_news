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
    """Використовує Groq для перекладу. Жорстко очищає форматування від Llama."""
    if not GROQ_API_KEY:
        print("Попередження: Немає GROQ_API_KEY! Передаємо оригінали.")
        return title, status_en, "Переклад неможливий (немає ключа)."
        
    ai_prompt = f"""Ти — професійний політичний аналітик. Переклади та поясни законопроєкт Конгресу США.
Назва: {title}
Статус: {status_en}
Офіційне резюме: {summary_en if summary_en else 'Відсутнє.'}

Поверни ВИКЛЮЧНО валідний JSON об'єкт з трьома ключами:
1. "title_uk" - точний переклад назви.
2. "status_uk" - точний переклад статусу.
3. "summary_uk" - коротке і чітке пояснення українською (1-2 речення), про що цей закон. Якщо "Офіційне резюме" відсутнє, зроби логічний висновок самостійно на основі назви.

БЕЗ жодних вступних слів, маркдауну чи коментарів. Тільки JSON."""

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
            
            # ЖОРСТКА ОЧИСТКА: видаляємо всі можливі маркдаун-теги від Llama
            data_clean = data.replace('```json', '').replace('
