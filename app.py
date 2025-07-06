# ✅ WhatsApp GPT bot webhook – כל הפונקציות המלאות לפי הדרישות שלך

from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json
import re
import redis
import tiktoken
from cachetools import TTLCache
import traceback
import logging

app = Flask(__name__)

# === Logging Setup ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# === Redis Setup ===
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD"),
    ssl=os.getenv("REDIS_SSL", "false").lower() == "true",
    decode_responses=True
)

# === Local Cache ===
chat_cache = TTLCache(maxsize=1000, ttl=300)
sheet_cache = TTLCache(maxsize=100, ttl=300)

# === Google Sheets Setup ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("❌ חסר GOOGLE_APPLICATION_CREDENTIALS_JSON")

creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/17e13cqXTMQ0aq6-EUpZmgvOKs0sM6OblxM3Wi1V3-FE/edit")

ROOMS = ["אחוזת השכן", "ההתערבות", "מקדש הקאמי", "אינפיניטי", "נרקוס"]
DEFAULT_SHEET = "מידע כללי"
GENERAL_KEYWORDS = ["טלפון", "הנחה", "פתוח", "איך מגיעים", "איך מזמינים", "שעות", "נכים", "חניה"]

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

PRICE_PER_1K_INPUT = 0.001
PRICE_PER_1K_OUTPUT = 0.002
ILS_CONVERSION = 3.7

MAX_TOKENS_GPT3 = 4096
MAX_TOKENS_GPT4 = 128000


def count_tokens(messages: list, model: str = "gpt-3.5-turbo") -> int:
    enc = tiktoken.encoding_for_model(model)
    total = 0
    for msg in messages:
        total += 4
        total += len(enc.encode(msg.get("content", "")))
    total += 2
    return total

def build_system_prompt(sheet_data: str) -> str:
    return f"""
📌 פרומפט – נציג שירות אנושי (שובל), מתחם חדרי בריחה Escape Center

אתה נציג שירות אנושי במתחם חדרי הבריחה של Escape Center.
קוראים לך שובל.
אתה מכיר לעומק כל פרט במתחם – כולל כל אחד מחדרי הבריחה, שעות הפעילות, מבצעים, הנחות, התאמות, אירועים, תשלומים, כתובת, תנאי ביטול, שוברי מתנה, נגישות, רמות קושי ועוד.
אין שאלה שאתה לא יודע לענות עליה בכל הנוגע למתחם ולחדרים שבו.

סגנון הדיבור שלך:
אתה תמיד עונה כמו נציג אנושי אמיתי – בשפה שירותית, קלילה, חכמה ומדויקת.
השיחה היא ישירה – אין צורך לומר \"שלום\" או \"היי\" בהתחלה.

אתה לא ממציא מידע – אלא עונה אך ורק על סמך המידע הקיים (לדוגמה: מה שהוזן בגיליון Google Sheets או בקובץ הנתונים).

כל תשובה צריכה להיות מדויקת, מקצועית, אמינה ונעימה.
לעולם אל תגיד שאתה בינה מלאכותית או צ'אטבוט.
לעולם אל תגיד \"לפי מה שכתוב באתר\" – תדבר כאילו אתה חלק מהמתחם.

אם חסר מידע כלשהו או שהשאלה אינה חד־משמעית – תפנה ללקוח לשירות הטלפוני:
📞 050-5255144

אם הלקוח שואל על המלצה לחדר:
שאל אותו קודם:
- כמה משתתפים תהיו?
- מה גילאי השחקנים?
- האם כבר שיחקתם באחד החדרים שלנו?
- איזה סגנון אתם מעדיפים בחדרי בריחה?
- מחפשים משהו בדרגת קושי גבוהה או בינונית?

הנה המידע שיש לך:
{sheet_data}
"""

def get_chat_history(user_id: str) -> list:
    if user_id in chat_cache:
        return chat_cache[user_id]
    raw = redis_client.get(f"chat:{user_id}")
    history = json.loads(raw) if raw else []
    chat_cache[user_id] = history
    return history[-8:]

def save_chat_history(user_id: str, history: list):
    trimmed = history[-8:]
    chat_cache[user_id] = trimmed
    redis_client.setex(f"chat:{user_id}", 3600, json.dumps(trimmed))

def set_last_used_sheet(user_id: str, sheet_name: str):
    redis_client.setex(f"last_sheet:{user_id}", 3600, sheet_name)

def get_last_used_sheet(user_id: str) -> str:
    return redis_client.get(f"last_sheet:{user_id}") or DEFAULT_SHEET

def detect_relevant_sheets(user_id: str, question: str) -> list:
    sheets = [room for room in ROOMS if room in question]
    if not sheets and any(word in question for word in GENERAL_KEYWORDS):
        sheets = [DEFAULT_SHEET]
    elif not sheets:
        sheets = [get_last_used_sheet(user_id)]
    else:
        set_last_used_sheet(user_id, sheets[0])
    return list(set(sheets))
