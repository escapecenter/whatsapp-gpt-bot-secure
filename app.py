from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json
import redis
import tiktoken
from cachetools import TTLCache
from datetime import datetime
import traceback
from difflib import get_close_matches

app = Flask(__name__)

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD"),
    ssl=os.getenv("REDIS_SSL", "false").lower() == "true",
    decode_responses=True
)

chat_cache = TTLCache(maxsize=1000, ttl=300)
sheet_cache = TTLCache(maxsize=100, ttl=300)

FAQ_MATCH_THRESHOLD = 0.65
faq_data = []

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not creds_json:
    raise EnvironmentError("❌ חסר GOOGLE_APPLICATION_CREDENTIALS_JSON")

creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_url("https://docs.google.com/spreadsheets/d/17e13cqXTMQ0aq6-EUpZmgvOKs0sM6OblxM3Wi1V3-FE/edit")
log_worksheet = sheet.worksheet("שיחות")
faq_worksheet = sheet.worksheet("מידע כללי")

ROOMS = ["אחוזת השכן", "ההתערבות", "מקדש הקאמי", "אינפיניטי", "נרקוס"]
DEFAULT_SHEET = "מידע כללי"
GENERAL_KEYWORDS = ["טלפון", "הנחה", "פתוח", "איך מגיעים", "איך מזמינים", "שעות", "נכים", "חניה"]

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

PRICING = {
    "gpt-3.5-turbo": {"input": 0.0015, "output": 0.002},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
}
ILS_CONVERSION = 3.7
MAX_TOKENS_GPT3 = 16000
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
אתה שובל, נציג שירות אנושי ומקצועי במתחם חדרי הבריחה Escape Center. הסגנון שלך קליל, אנושי ונעים, אך תמיד מדויק, ברור ומכבד. אתה עונה בגובה העיניים, בצורה ישירה וממוקדת, אך עם רגש, סבלנות ואכפתיות אמיתית.

כאשר לקוח מבקש המלצה, שאל תמיד תחילה: כמה שחקנים תהיו, מה הגילאים, האם יש לכם ניסיון קודם ומה הסגנון המועדף עליכם. בשאלות פתוחות, נסה להוביל להזמנה בפועל או לעזרה מותאמת שתסייע ללקוח לבחור חדר שמתאים לו בדיוק.

ענה אך ורק לפי מידע שנמצא בפרומט הזה או בגליונות הנתונים. אם אין לך מקור מוסמך למידע – אל תאשר. לעולם אל תמציא חדרים, הנחות, מבצעים או פרטים טכניים שלא מופיעים במידע שקיבלת.

אל תפתח תשובה בהצגת המקום – הנח שהלקוח כבר יודע לאן פנה. אם שואלים שאלה שאין עליה תשובה ברורה, הפנה ישירות למספר הטלפון: 050-5255144.

שמור תמיד על עברית תקנית, ניסוח הגיוני ומשפטים ברורים וזורמים. חשוב מאוד שכל לקוח יבין אותך בקלות וירגיש שהוא מקבל שירות אנושי, איכותי ומקצועי.

המטרה שלך ברורה: לעזור ללקוח להזמין משחק באחד מהחדרים המעולים שלנו – בעזרת תקשורת מדויקת, מכוונת מטרה ועם תחושת ביטחון שהוא בידיים טובות.
תענה לפי המידע המצורף בגוגל שייטס בגליונות 
    {sheet_data}
    """

def load_faq_data():
    global faq_data
    if not faq_data:
        faq_data = faq_worksheet.get_all_records()

def match_faq(user_question: str, threshold: float = FAQ_MATCH_THRESHOLD):
    load_faq_data()
    questions = [row["שאלה"] for row in faq_data]
    matches = get_close_matches(user_question, questions, n=1, cutoff=threshold)
    if matches:
        match = matches[0]
        for row in faq_data:
            if row["שאלה"] == match:
                return row["תשובה"], match
    return None

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

def try_load_valid_sheets(user_id: str, question: str):
    candidates = detect_relevant_sheets(user_id, question)
    valid, combined = [], []
    for sheet_name in candidates:
        data = get_sheet_data(sheet_name)
        if any(word in data for word in question.split()):
            valid.append(sheet_name)
            combined.append(data)
    if not valid:
        data = get_sheet_data(DEFAULT_SHEET)
        return [DEFAULT_SHEET], data
    return valid, "\n\n".join(combined)

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

def detect_relevant_sheets(user_id: str, question: str) -> list:
    sheets = [room for room in ROOMS if room in question]
    if not sheets and any(word in question for word in GENERAL_KEYWORDS):
        return [DEFAULT_SHEET]
    if not sheets:
        sheets = [redis_client.get(f"last_sheet:{user_id}") or DEFAULT_SHEET]
    redis_client.setex(f"last_sheet:{user_id}", 3600, sheets[0])
    return sheets

def log_to_sheet(user_id, model, q, a, tokens, price_ils, sheet_name, source="GPT", match=""):
    try:
        log_worksheet.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            user_id, model,
            q[:300].replace("\n", " "),
            a.replace("\n", " "),
            tokens,
            f"₪{price_ils}",
            sheet_name, source, match
        ])
    except Exception as e:
        print(f"⚠️ שגיאה בלוג לגיליון: {e}")

def ask_gpt(user_id: str, user_question: str, sheet_data: str, sheet_names: list) -> str:
    history = get_chat_history(user_id)
    history.append({"role": "user", "content": user_question})
    messages = [{"role": "system", "content": build_system_prompt(sheet_data)}] + history

    prompt_tokens = count_tokens(messages)
    completion_tokens = 1200
    total_tokens = prompt_tokens + completion_tokens

    model_name = "gpt-3.5-turbo"
    if total_tokens > MAX_TOKENS_GPT3:
        model_name = "gpt-4-turbo"
        prompt_tokens = count_tokens(messages, model=model_name)
        total_tokens = prompt_tokens + completion_tokens

    if total_tokens > MAX_TOKENS_GPT4:
        return "⚠️ השאלה וההקשר ארוכים מדי ל-GPT-4-Turbo. נסה לקצר."

    try:
        response = openai_client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.6,
            max_tokens=completion_tokens
        )
    except Exception as e:
        return f"⚠️ שגיאה מהשרת של OpenAI: {str(e)}"

    answer = response.choices[0].message.content.strip().replace('"', '').replace('\n', ' ').replace('\r', ' ').strip()
    history.append({"role": "assistant", "content": answer})
    save_chat_history(user_id, history)

    redis_client.incrby(f"token_sum:{user_id}", total_tokens)
    redis_client.incrby(f"token_input:{user_id}", prompt_tokens)
    redis_client.incrby(f"token_output:{user_id}", completion_tokens)

    usd = (prompt_tokens * PRICING[model_name]["input"] + completion_tokens * PRICING[model_name]["output"]) / 1000
    price_ils = round(usd * ILS_CONVERSION, 2)
    log_to_sheet(user_id, model_name, user_question, answer, total_tokens, price_ils, ', '.join(sheet_names))
    return answer

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        user_question = data.get("message")
        user_id = data.get("user_id")

        if not user_question or not user_id:
            return jsonify({"error": "Missing 'message' or 'user_id'"}), 200

        if user_question.strip().lower() == "סיים שיחה":
            redis_client.delete(f"chat:{user_id}", f"token_sum:{user_id}", f"token_input:{user_id}", f"token_output:{user_id}")
            return jsonify({"reply": "השיחה אופסה בהצלחה ✅"})

        if user_question.strip() == "12345":
            try:
                total = int(redis_client.get(f"token_sum:{user_id}") or 0)
                input_toks = int(redis_client.get(f"token_input:{user_id}") or 0)
                output_toks = int(redis_client.get(f"token_output:{user_id}") or 0)
                model = "gpt-4-turbo" if total > MAX_TOKENS_GPT3 else "gpt-3.5-turbo"
                usd = ((input_toks * PRICING[model]["input"] + output_toks * PRICING[model]["output"]) / 1000)
                ils = round(usd * ILS_CONVERSION, 2)
            except Exception as e:
                total, ils = 0, 0
            return jsonify({"reply": f"🔢 סך הטוקנים: {total}\n💰 עלות משוערת: ₪{ils}"})

        match = match_faq(user_question)
        if match:
            answer, matched_question = match
            log_to_sheet(user_id, "FAQ", user_question, answer, 0, 0, DEFAULT_SHEET, source="FAQ", match=matched_question)
            return jsonify({"reply": answer})

        sheets, full_context = try_load_valid_sheets(user_id, user_question)
        if not full_context:
            return jsonify({"reply": "שגיאה: לא הצלחנו לקרוא מידע רלוונטי."})

        reply = ask_gpt(user_id, user_question, full_context, sheets)
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"error": "Internal Server Error", "details": str(e)}), 200

@app.route("/", methods=["GET"])
def index():
    return "✅ WhatsApp GPT bot is alive"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
