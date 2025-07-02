from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

app = Flask(__name__)

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("❌ חסר GOOGLE_APPLICATION_CREDENTIALS_JSON")

try:
    creds_dict = json.loads(creds_json)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ JSON שגוי: {e}")

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("escape_rooms_full_data").sheet1

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

def ask_gpt_with_context(user_question: str, sheet_data: str) -> str:
    prompt = f"""
    אתה נציג שירות לקוחות בעסק חדרי בריחה. ענה ללקוח בהתאם למידע הבא:

    מידע מתוך השיטס:
    {sheet_data}

    שאלה של הלקוח:
    {user_question}
    """

    response = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "אתה נציג שירות לקוחות מקצועי."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.6,
        max_tokens=300
    )
    return response.choices[0].message.content.strip()

def handle_user_message(user_question: str) -> str:
    rows = sheet.get_all_values()
    if not rows or len(rows) < 2:
        return "שגיאה: הטבלה ריקה רקה."

    header = rows[0]
    data_rows = rows[1:]

    sheet_data = "\n".join([
        " | ".join(row) for row in data_rows
    ])

    print("📄 Sheet data preview:", sheet_data[:500])

    return ask_gpt_with_context(user_question, sheet_data)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("📥 Received data:", data)

        if not data or "message" not in data:
            return jsonify({"error": "Missing 'message' key"}), 400

        user_question = data["message"]
        reply = handle_user_message(user_question)
        print("✅ Reply:", reply)
        return jsonify({"reply": reply})

    except Exception as e:
        print("❌ Error in /webhook:", e)
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
