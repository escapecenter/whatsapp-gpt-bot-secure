from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

app = Flask(__name__)

# Define Google Sheets access scope
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# Load Google credentials from environment
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("❌ חסר GOOGLE_APPLICATION_CREDENTIALS_JSON")

try:
    creds_dict = json.loads(creds_json)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ JSON שגוי: {e}")

# Authorize Google Sheets access
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/17e13cqXTMQ0aq6-EUpZmgvOKs0sM6OblxM3Wi1V3-FE/edit").sheet1

# Setup OpenAI
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def ask_gpt_with_context(user_question: str, sheet_data: str) -> str:
    prompt = f"""
    אתה מייצג את אסקייפ סנטר - נציג שירות מקצועי, ברור, אנושי ומסביר פנים.
    תענה לשאלות הלקוח במדויק, בגובה העיניים, בלי להיות רובוט, בלי להזכיר שאתה GPT.

    מידע מתוך הטבלה:
    {sheet_data}

    שאלה:
    {user_question}
    """

    response = openai_client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "אתה נציג שירות אנושי ואדיב של מתחם חדרי הבריחה Escape Center."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5,
        max_tokens=700
    )
    return response.choices[0].message.content.strip()

def handle_user_message(user_question: str) -> str:
    rows = sheet.get_all_values()
    if not rows or len(rows) < 2:
        return "שגיאה: אין מידע זמין כרגע בטבלה."

    # Limit context to avoid token overflow
    limited_rows = rows[:10]  # ← אתה יכול להגדיל לפי הצורך
    sheet_data = "\n".join([" | ".join(row) for row in limited_rows])

    return ask_gpt_with_context(user_question, sheet_data)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data or "message" not in data:
            return jsonify({"error": "Missing 'message' key"}), 400

        user_question = data["message"]
        reply = handle_user_message(user_question)
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
