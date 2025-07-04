# app.py
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json
import re

app = Flask(__name__)

# === Google Sheets Setup ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("❌ חסר GOOGLE_APPLICATION_CREDENTIALS_JSON")

creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/17e13cqXTMQ0aq6-EUpZmgvOKs0sM6OblxM3Wi1V3-FE/edit").sheet1

# === OpenAI Setup ===
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# === In-memory chat history and deduplication ===
chat_history = {}
last_message = {}

# === Prompt Template ===
def build_system_prompt(sheet_data: str) -> str:
    return f"""
אתה נציג שירות בשם שובל – עובד אמיתי במתחם חדרי בריחה.
ענה תמיד בצורה אנושית, שירותית, קלילה, בגובה העיניים – לא רשמית ולא רובוטית.
אל תפתח את השיחה בהיי" ואל תציין את שם המקום – זה כבר נאמר ללקוח.

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

# === GPT Call with Context ===
def ask_gpt(user_id: str, user_question: str, sheet_data: str) -> str:
    system_prompt = build_system_prompt(sheet_data)
    history = chat_history.get(user_id, [])
    history.append({"role": "user", "content": user_question})
    messages = [{"role": "system", "content": system_prompt}] + history

    response = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        temperature=0.6,
        max_tokens=500
    )

    answer = response.choices[0].message.content.strip()
    # ניקוי סלאשים מסביב לשמות חדרים (בתחילת או סוף מילה בלבד)
    answer = re.sub(r"(?<!\\S)/(.*?)(?<!\\s)/", r"\1", answer)
    # הסרה של משפט סיום קבוע לא רצוי
    answer = re.sub(r"איך אני יכול לעזור.*?$", "", answer).strip()

    history.append({"role": "assistant", "content": answer})
    chat_history[user_id] = history
    return answer

# === Handle User Input ===
def handle_user_message(user_id: str, user_question: str) -> str:
    if last_message.get(user_id) == user_question:
        return "רגע אחד... נראה שכבר עניתי על זה 😊"
    last_message[user_id] = user_question

    rows = sheet.get_all_values()
    if not rows or len(rows) < 2:
        return "שגיאה: אין מידע בטבלה."

    sheet_data = "\n".join([" | ".join(row) for row in rows])
    print(f"📄 Sheet Preview: {sheet_data[:300]}")
    return ask_gpt(user_id, user_question, sheet_data)

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
