# app.py
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json
import re
import redis
import tiktoken

app = Flask(__name__)

# === Redis Setup ===
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

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

# === OpenAI Setup ===
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# === Redis-based History Management ===
def get_chat_history(user_id: str) -> list:
    raw = redis_client.get(f"chat:{user_id}")
    return json.loads(raw) if raw else []

def save_chat_history(user_id: str, history: list):
    redis_client.setex(f"chat:{user_id}", 3600, json.dumps(history))

def get_last_message(user_id: str) -> str:
    return redis_client.get(f"last_msg:{user_id}")

def set_last_message(user_id: str, message: str):
    redis_client.setex(f"last_msg:{user_id}", 600, message)

# === Prompt Template ===
def build_system_prompt(sheet_data: str) -> str:
    return f"""
אתה נציג שירות בשם שובל – עובד אמיתי במתחם חדרי בריחה.
ענה תמיד בצורה אנושית, שירותית, קלילה, בגובה העיניים – לא רשמית ולא רובוטית.
אל תפתח את השיחה ב"היי" ואל תציין את שם המקום – זה כבר נאמר ללקוח.

אם מבקשים המלצה על חדר, אל תענה לפני ששאלת (אם לא נאמר כבר):
תגיד קודם משפט מנומס כמו:
"בשמחה! כדי להמליץ לכם בצורה הכי טובה, רק צריך שאדע כמה פרטים קטנים 😊"
ואז תשאל:
1. כמה שחקנים תהיו?
2. מה גילאי המשתתפים?
3. שיחקתם כבר אצלנו? אם כן – באיזה חדר?
4. איזה סגנון חדר אתם הכי אוהבים? (אימה, אקשן, מצחיק, דרמטי וכו')

ענה רק לפי המידע שנתן. אל תמציא.
אם אין לך תשובה – כתוב בנימוס:
"אני לא בטוח בזה – הכי טוב לפנות אלינו ישירות למתחם ולשאול 📞 050-5255144"
תמיד תהיה חייכן, מקצועי וסבלני – כאילו אתה באמת נמצא במתחם ומדבר עם הלקוח.

הנה המידע שיש לך:
{sheet_data}
"""

# === Count Tokens ===
def count_tokens(messages: list) -> int:
    enc = tiktoken.encoding_for_model("gpt-3.5-turbo")
    total = 0
    for msg in messages:
        total += 4  # tokens per message overhead
        total += len(enc.encode(msg.get("content", "")))
    return total

# === GPT Call with Context ===
def ask_gpt(user_id: str, user_question: str, sheet_data: str) -> str:
    system_prompt = build_system_prompt(sheet_data)
    history = get_chat_history(user_id)
    history.append({"role": "user", "content": user_question})
    messages = [{"role": "system", "content": system_prompt}] + history

    token_count = count_tokens(messages)
    print(f"🔢 Token count: {token_count}")

    if token_count <= 4096:
        model_name = "gpt-3.5-turbo"
    elif token_count <= 16384:
        model_name = "gpt-3.5-turbo-16k"
    else:
        return "שגיאה: הבקשה חורגת ממגבלת טוקנים."

    print(f"🤖 Using model: {model_name}")

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

# === Detect relevant sheets ===
def detect_relevant_sheets(question: str) -> list:
    sheets = [room for room in ROOMS if room in question]
    return sheets or [DEFAULT_SHEET]

# === Handle User Input ===
def handle_user_message(user_id: str, user_question: str) -> str:
    if get_last_message(user_id) == user_question:
        return "רגע אחד... נראה שכבר עניתי על זה 😊"
    set_last_message(user_id, user_question)

    relevant_sheets = detect_relevant_sheets(user_question)
    print(f"📌 Relevant sheets: {relevant_sheets}")

    combined_data = []
    for name in relevant_sheets:
        try:
            ws = sheet.worksheet(name)
            rows = ws.get_all_values()
            if rows:
                combined_data.append(f"-- {name} --\n" + "\n".join([" | ".join(r) for r in rows]))
        except Exception as e:
            print(f"⚠️ שגיאה בגליון {name}: {e}")

    if not combined_data:
        return "שגיאה: לא הצלחנו לקרוא מידע רלוונטי."

    full_context = "\n\n".join(combined_data)
    print(f"📄 Sheet Preview: {full_context[:300]}")
    return ask_gpt(user_id, user_question, full_context)

# === Routes ===
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("📥 Received:", data)

        user_question = data.get("message")
        user_id = data.get("user_id")

        print("📞 user_id:", user_id)
        print("💬 message:", user_question)

        if not user_question or not user_id:
            return jsonify({"error": "Missing 'message' or 'user_id'"}), 400

        reply = handle_user_message(user_id, user_question)
        print("✅ Reply:", reply)
        return jsonify({"reply": reply})

    except Exception as e:
        print("❌ Error:", e)
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

