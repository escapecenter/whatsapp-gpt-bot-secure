
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
from datetime import datetime
import traceback

app = Flask(__name__)

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
log_worksheet = sheet.worksheet("שיחות")

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
        total += 4 + len(enc.encode(msg.get("content", "")))
    total += 2
    return total

def build_system_prompt(sheet_data: str) -> str:
    return f"""
📌 פרומפט – נציג שירות אנושי (שובל), מתחם חדרי בריחה Escape Center

אתה נציג שירות אנושי במתחם חדרי הבריחה של Escape Center.
קוראים לך שובל.
אתה מכיר לעומק כל פרט במתחם – כולל כל אחד מחדרי הבריחה, שעות הפעילות, מבצעים, הנחות, התאמות, אירועים, תשלומים, כתובת, תנאי ביטול, שוברי מתנה, נגישות, רמות קושי ועוד.

סגנון הדיבור שלך:
אתה תמיד עונה כמו נציג אנושי אמיתי – בשפה שירותית, קלילה, חכמה ומדויקת.
אל תאמר "שלום" – זה כבר נאמר קודם. אל תזכיר שאתה בינה מלאכותית.
אם אין מידע – תפנה לשירות הטלפוני 📞 050-5255144.

אם מבקשים המלצה – שאל קודם:
- כמה משתתפים?
- גילאים?
- שיחקו כבר אצלנו?
- מה הסגנון המועדף?
- דרגת קושי?

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

def get_last_message(user_id: str) -> str:
    return redis_client.get(f"last_msg:{user_id}")

def set_last_message(user_id: str, message: str):
    redis_client.setex(f"last_msg:{user_id}", 10, message)

def get_last_used_sheet(user_id: str) -> str:
    return redis_client.get(f"last_sheet:{user_id}") or DEFAULT_SHEET

def set_last_used_sheet(user_id: str, sheet_name: str):
    redis_client.setex(f"last_sheet:{user_id}", 3600, sheet_name)

def detect_relevant_sheets(user_id: str, question: str) -> list:
    sheets = [room for room in ROOMS if room in question]
    if not sheets and any(word in question for word in GENERAL_KEYWORDS):
        sheets = [DEFAULT_SHEET]
    elif not sheets:
        sheets = [get_last_used_sheet(user_id)]
    else:
        set_last_used_sheet(user_id, sheets[0])
    return list(set(sheets))

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

def log_to_sheet(user_id: str, model: str, q: str, a: str, tokens: int):
    try:
        log_worksheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            user_id, model, q[:300], a[:300], tokens
        ])
    except Exception as e:
        print(f"⚠️ שגיאה בלוג לגיליון: {e}")

def ask_gpt(user_id: str, user_question: str, sheet_data: str) -> str:
    history = get_chat_history(user_id)
    history.append({"role": "user", "content": user_question})
    system_prompt = build_system_prompt(sheet_data)
    messages = [{"role": "system", "content": system_prompt}] + history

    prompt_tokens = count_tokens(messages)
    completion_tokens = 500
    total_tokens = prompt_tokens + completion_tokens

    model_name = "gpt-3.5-turbo"
    max_allowed = MAX_TOKENS_GPT3
    if total_tokens > MAX_TOKENS_GPT3:
        model_name = "gpt-4-turbo"
        max_allowed = MAX_TOKENS_GPT4
        prompt_tokens = count_tokens(messages, model=model_name)
        total_tokens = prompt_tokens + completion_tokens

    if total_tokens > max_allowed:
        return "⚠️ השאלה וההקשר ארוכים מדי גם ל-GPT-4-Turbo. נסה לקצר."

    redis_client.incrby(f"token_sum:{user_id}", total_tokens)
    redis_client.incrby(f"token_input:{user_id}", prompt_tokens)
    redis_client.incrby(f"token_output:{user_id}", completion_tokens)

    response = openai_client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.6,
        max_tokens=completion_tokens
    )

    answer = response.choices[0].message.content.strip()
    answer = re.sub(r"(?<!\\S)/(.*?)(?<!\\s)/", r"\1", answer)
    answer = re.sub(r"איך אני יכול לעזור.*?$", "", answer).strip()

    history.append({"role": "assistant", "content": answer})
    save_chat_history(user_id, history)
    log_to_sheet(user_id, model_name, user_question, answer, total_tokens)
    return answer

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        user_question = data.get("message")
        user_id = data.get("user_id")

        if not user_question or not user_id:
            return jsonify({"error": "Missing 'message' or 'user_id'"}), 400

        if user_question.strip().lower() == "סיים שיחה":
            redis_client.delete(f"chat:{user_id}", f"token_sum:{user_id}", f"token_input:{user_id}", f"token_output:{user_id}")
            return jsonify({"reply": "השיחה אופסה בהצלחה ✅"})

        if user_question.strip() == "12345":
            try:
                total = int(redis_client.get(f"token_sum:{user_id}") or 0)
                input_toks = int(redis_client.get(f"token_input:{user_id}") or 0)
                output_toks = int(redis_client.get(f"token_output:{user_id}") or 0)
                usd = ((input_toks * PRICE_PER_1K_INPUT) + (output_toks * PRICE_PER_1K_OUTPUT))
                ils = round(usd * ILS_CONVERSION, 2)
            except Exception as e:
                print(f"⚠️ שגיאה בחישוב עלות: {e}")
                total, ils = 0, 0
            return jsonify({"reply": f"🔢 סך הטוקנים: {total}\n💰 עלות משוערת: ₪{ils}"})

        if get_last_message(user_id) == user_question:
            return jsonify({"reply": "רגע אחד... נראה שכבר עניתי על זה 😊"})
        set_last_message(user_id, user_question)

        sheets = detect_relevant_sheets(user_id, user_question)
        combined_data = [get_sheet_data(name) for name in sheets if name]
        full_context = "\n\n".join([d for d in combined_data if d.strip()])

        if not full_context:
            return jsonify({"reply": "שגיאה: לא הצלחנו לקרוא מידע רלוונטי."})

        reply = ask_gpt(user_id, user_question, full_context)
        return jsonify({"reply": reply})

    except Exception as e:
        print("❌ שגיאה כללית:", traceback.format_exc())
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
