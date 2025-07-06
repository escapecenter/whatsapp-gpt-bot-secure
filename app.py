# ✅ שדרוג: fallback ל"מידע כללי", זיהוי שאלות כלליות, חישוב טוקנים לפי שיחה מלאה (12345)

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

app = Flask(__name__)

# === Redis Setup ===
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD"),
    ssl=os.getenv("REDIS_SSL", "false").lower() == "true",
    decode_responses=True
)

# === Local Cache Setup ===
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
GENERIC_KEYWORDS = ["איך", "טלפון", "מזמינים", "הנחה", "פתוח", "מגיעים"]

# === OpenAI Setup ===
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# === Redis + Cache Chat Management ===
def get_chat_history(user_id: str) -> list:
    if user_id in chat_cache:
        return chat_cache[user_id]
    raw = redis_client.get(f"chat:{user_id}")
    history = json.loads(raw) if raw else []
    chat_cache[user_id] = history
    return history

def save_chat_history(user_id: str, history: list):
    chat_cache[user_id] = history
    redis_client.setex(f"chat:{user_id}", 3600, json.dumps(history))

# === גיליון אחרון שנשלח ===
def get_last_used_sheet(user_id: str) -> str:
    return redis_client.get(f"last_sheet:{user_id}") or DEFAULT_SHEET

def set_last_used_sheet(user_id: str, sheet_name: str):
    redis_client.setex(f"last_sheet:{user_id}", 3600, sheet_name)

# === Count Tokens ===
def count_tokens(messages: list) -> int:
    enc = tiktoken.encoding_for_model("gpt-3.5-turbo")
    total = 0
    for msg in messages:
        total += 4
        total += len(enc.encode(msg.get("content", "")))
    return total

# === Prompt Template ===
def build_system_prompt(sheet_data: str) -> str:
    return f"""
אתה נציג שירות בשם שובל – עובד אמיתי במתחם חדרי בריחה.
ענה תמיד בצורה אנושית, שירותית, קלילה, בגובה העיניים – לא רשמית ולא רובוטית.
...
הנה המידע שיש לך:
{sheet_data}
"""

# === GPT Call ===
def ask_gpt(user_id: str, user_question: str, sheet_data: str) -> str:
    system_prompt = build_system_prompt(sheet_data)
    history = get_chat_history(user_id)
    history.append({"role": "user", "content": user_question})
    messages = [{"role": "system", "content": system_prompt}] + history

    token_count = count_tokens(messages)
    redis_client.setex(f"tokens:{user_id}", 3600, str(token_count))
    print(f"🔢 Token count: {token_count}")

    if token_count <= 4096:
        model_name = "gpt-3.5-turbo"
    elif token_count <= 16384:
        model_name = "gpt-3.5-turbo-16k"
    else:
        return "שגיאה: הבקשה חורגת ממגבלת טוקנים."

    response = openai_client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.6,
        max_tokens=500
    )

    answer = response.choices[0].message.content.strip()
    answer = re.sub(r"(?<!\\S)/(.*?)(?<!\\s)/", r"\1", answer)
    answer = re.sub(r"איך אני יכול לעזור.*?$", "", answer).strip()

    history.append({"role": "assistant", "content": answer})
    save_chat_history(user_id, history)
    return answer

# === Detect Relevant Sheets ===
def detect_relevant_sheets(user_id: str, question: str) -> list:
    sheets = [room for room in ROOMS if room in question]
    if sheets:
        set_last_used_sheet(user_id, sheets[0])
        return sheets
    if any(keyword in question for keyword in GENERIC_KEYWORDS):
        return [DEFAULT_SHEET]
    return [get_last_used_sheet(user_id)]

# === Sheet Caching ===
def get_sheet_data(sheet_name: str) -> str:
    if sheet_name in sheet_cache:
        return sheet_cache[sheet_name]
    try:
        ws = sheet.worksheet(sheet_name)
        rows = ws.get_all_values()
        data = f"-- {sheet_name} --\n" + "\n".join([" | ".join(r) for r in rows])
        sheet_cache[sheet_name] = data
        return data
    except Exception as e:
        print(f"⚠️ שגיאה בגליון {sheet_name}: {e}")
        return ""

# === Handle Messages ===
def handle_user_message(user_id: str, user_question: str) -> str:
    if user_question.strip() == "12345":
        tokens = redis_client.get(f"tokens:{user_id}") or "0"
        return f"כמות הטוקנים שצרכנו עד כה: {tokens}"

    last = redis_client.get(f"last_msg:{user_id}")
    if last == user_question:
        return "רגע אחד... נראה שכבר עניתי על זה 😊"
    redis_client.setex(f"last_msg:{user_id}", 600, user_question)

    sheets = detect_relevant_sheets(user_id, user_question)
    combined_data = [get_sheet_data(name) for name in sheets if name]
    combined_data = [d for d in combined_data if d.strip()]
    if not combined_data:
        return "שגיאה: לא הצלחנו לקרוא מידע רלוונטי."

    full_context = "\n\n".join(combined_data)
    return ask_gpt(user_id, user_question, full_context)

# === Routes ===
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        user_question = data.get("message")
        user_id = data.get("user_id")
        if not user_question or not user_id:
            return jsonify({"error": "Missing 'message' or 'user_id'"}), 400
        reply = handle_user_message(user_id, user_question)
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
