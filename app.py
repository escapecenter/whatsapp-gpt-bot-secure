from flask import Flask, request, jsonify
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json

app = Flask(__name__)

# התחברות לגוגל שיטס (באמצעות Environment Variable)
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("Missing GOOGLE_APPLICATION_CREDENTIALS_JSON environment variable")

try:
    creds_dict = json.loads(creds_json)
except json.JSONDecodeError as e:
    raise ValueError(f"Invalid JSON in GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")

creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("escape_rooms_full_data").sheet1

# קבלת מפתח מ-ENV
openai.api_key = os.environ.get("OPENAI_API_KEY")


def get_answer_from_sheet(user_question: str) -> str:
    records = sheet.get_all_records()
    for row in records:
        question = row.get("שאלה", "").strip()
        answer = row.get("תשובה", "").strip()
        if question and question in user_question:
            return answer
    return None


def ask_gpt_with_context(user_question: str, sheet_data: str) -> str:
    prompt = f"""
    אתה נציג שירות לקוחות בעסק חדרי בריחה. ענה ללקוח בהתאם למידע הבא:

    מידע מתוך הקובץ:
    {sheet_data}

    שאלה של הלקוח:
    {user_question}
    """

    response = openai.ChatCompletion.create(
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
    direct_answer = get_answer_from_sheet(user_question)
    if direct_answer:
        return direct_answer

    sheet_data = "\n".join([
        f"שאלה: {row['שאלה']} תשובה: {row['תשובה']}"
        for row in sheet.get_all_records()
    ])
    return ask_gpt_with_context(user_question, sheet_data)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    message = data.get("message")
    if not message:
        return jsonify({"error": "Missing message"}), 400

    reply = handle_user_message(message)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
