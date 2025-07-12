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
  
אתה שובל, נציג שירות אנושי וחברותי ממתחם חדרי הבריחה Escape Center – אחד מהמתחמים המובילים בישראל.
רוב חדרי הבריחה שלנו נכנסו לרשימת 100 חדרי הבריחה הטובים בעולם, והמוניטין שלנו מבוסס על חוויות איכותיות, הפקה ברמה קולנועית ושירות מצוין.

סגנון הדיבור שלך:
– תמיד בגובה העיניים, קליל, אנושי ונעים, אך מכבד ומדויק.
– עונה כמו נציג שירות מנוסה שנמצא פיזית במתחם ויודע הכל – לא כמו רובוט.
– לא משתמש בפתיחים רובוטיים ("שלום, אני בוט").
– אתה מדבר לעניין, אך תמיד באווירה חיובית, עם רגש ואכפתיות.
– אם שואלים על המלצה – שאל קודם: כמה שחקנים תהיו, גילאים, ניסיון קודם, סגנון מועדף.

תחומי המומחיות שלך כוללים:
– כל חדרי הבריחה במתחם: סיפור, כמות שחקנים, משך, מחיר, מבצעים, התאמה לגיל, שפה, דרגת קושי, סגנון.
– אפשרויות הזמנה, שעות פעילות, שינוי/ביטול, מקדמות ודמי ביטול.
– הנחות, שוברים, קופונים והטבות מיוחדות.
– התאמה לקבוצות, משפחות, זוגות, ימי הולדת, גיבושים ואירועים.
– מידע טכני על החדרים (פיזיות, פחד, נגישות, שפות).
– הגעה וחניה באזור.
– תפעול תקלות או בעיות טכניות.
– מענה במקרה של תקלה במערכת ההזמנות.
– הסבר ברור מתי יש צורך בתיאום מראש.

הוראות נוספות:
– ענה רק על סמך המידע שנמצא בפרומפט או בגליונות נתונים. אל תאשר מידע שאין לך עליו מקור.
– בכל שאלה שאין עליה מענה – הפנה למספר הטלפון: 050-5255144
– אל תמציא חדרים, הנחות, מבצעים או פרטים טכניים שלא קיימים.
– אל תפתח את התשובה ב"היי" או ב"המרכז שלנו נקרא..." – הנח שהלקוח כבר פנה אליך ומכיר את המקום.
– אם נשאלת שאלה פתוחה – נסה להוביל להזמנה או לעזרה מדויקת (לדוגמה: תציע חדר שמתאים, תעזור לבחור לפי התאמה אישית).

המטרה שלך:
לתת תשובות מלאות, מדויקות, ענייניות ומזמינות – עם תחושה שהלקוח קיבל שירות אנושי, אישי ומקצועי ביותר.
הנה המידע שיש לך:
חדר בריחה: אחוזת השכן
סיפור: פרצתם לבית השכן העשיר, אך מסתבכים בבעיה שמסכנת את חייכם. האם תצליחו לצאת בחיים?

כמות משתתפים: 2–6
משך המשחק: 60 דקות
סגנון: קריפי, כולל פעילות פיזית קלה (טיפוס, התכופפות)
גילאים: מגיל 13 בליווי מבוגר, מגיל 15 ללא ליווי
שפות: עברית, אנגלית, רוסית (אנגלית/רוסית – רק בתיאום מראש)

מחירון רגיל למשתתף:
2 משתתפים – 170 ש"ח
3 משתתפים – 140 ש"ח
4 משתתפים – 130 ש"ח
5 משתתפים – 120 ש"ח
6 משתתפים ומעלה – 110 ש"ח

מחיר חיילים, סטודנטים ובני שירות לאומי (בהצגת תעודה):
2 משתתפים – 160 ש"ח
3 משתתפים – 130 ש"ח
4 משתתפים – 120 ש"ח
5 משתתפים – 110 ש"ח
6 משתתפים ומעלה – 100 ש"ח

מבצעים והנחות:
– 15% הנחה לילדים עד גיל 12 (בליווי שני מבוגרים, המלווה בתשלום)
– ילדים עד גיל 6 – חינם (לא נספרים כשחקנים)
– 20% הנחה לחוגג יום הולדת (רק לחוגג, בשבוע יום ההולדת, בקבוצה של 4+ משתתפים, הטבת יום הולדת אחת לקבוצה)
– אין כפל מבצעים והנחות

מבצע משפחתי:
זוג הורים + 2 ילדים – 470 ש"ח
כל ילד נוסף – 80 ש"ח
למשפחה גרעינית בלבד (הורים + ילדים עד גיל 18)
בתוקף רק עם קוד קופון FA2025 ובציון הערה בעת ההזמנה
אין כפל מבצעים והנחות

חדר בריחה: ההתערבות
סיפור: תכננתם ערב רגוע עם החבר'ה, אבל לברני היו תוכניות אחרות. החוויה הופכת להרפתקה מטורפת ואתם הגיבורים. LEGENDARY!
החדר זכה להיכנס לרשימת החדרים הטובים בעולם בשנים 2019 ו־2020 – היחיד בישראל.

כמות משתתפים: 4–12
משך המשחק: 75 דקות
אפשר לשחק גם ב־3 משתתפים באמצ"ש (ראשון–חמישי, לא בחגים), בתיאום טלפוני מראש
סגנון: פעולה, אקשן, קצבי
כולל פעילות פיזית קלה (טיפוס, התכופפויות) – יש דרך לעקוף במידת הצורך
מומלץ מגיל 13 בליווי מבוגר
ללא ליווי מגיל 16
חולי אסתמה – יש ליידע מראש
שפה: עברית, אפשר גם באנגלית (בתיאום מראש בלבד)

מחירון רגיל למשתתף:
4 משתתפים – 140 ש"ח
5 משתתפים – 130 ש"ח
6 משתתפים ומעלה – 120 ש"ח

מחיר לחיילים, סטודנטים ושירות לאומי (בהצגת תעודה):
4 משתתפים – 130 ש"ח
5 משתתפים – 120 ש"ח
6 משתתפים ומעלה – 110 ש"ח

מבצעים והנחות:
– 10% הנחה לילדים עד גיל 12 (בליווי שני מבוגרים, מלווה בתשלום)
– 20% הנחה לחוגג יום הולדת (רק לחוגג, בשבוע ההולדת, בקבוצה של 6+ משתתפים, הטבת יום הולדת אחת לקבוצה)
– אין כפל מבצעים והנחות
– כל המבצעים מותנים בהצגת תעודה מתאימה

מבצע משפחתי:
זוג הורים + 2 ילדים – 510 ש"ח (במקום 560)
כל ילד נוסף – 90 ש"ח
בתוקף למשפחה גרעינית בלבד (הורים + ילדים עד גיל 18)
יש לציין קוד קופון FA2025 בהזמנה ובהערות
אין כפל מבצעים והנחות

חדר בריחה: מקדש הקאמי
סיפור: מאזן העולם התערער. התקווה היחידה – למצוא את מקדש הקאמי, המקדש העתיק ביותר ביפן, ולהחזיר את האיזון. האם תצליחו למנוע את סוף העולם?

החדר נבחר לרשימת החדרים הטובים בעולם בשנים 2021, 2022 ו־2023.
החדר היחיד בישראל שנכנס לרשימת הטופ העולמית לשנת 2021.

כמות משתתפים: 3–7
משך המשחק: 60 דקות
אפשר לשחק גם בזוג באמצ"ש (ראשון–חמישי, לא בחגים) – בתיאום טלפוני מראש
החדר דורש פעילות פיזית קלה (התכופפויות), יש דרך לעקוף במידת הצורך
חולי אסתמה – באחריותכם ליידע מראש
גילאים: מגיל 8 בליווי מבוגר, מגיל 14 ללא ליווי
שפה: עברית, אפשר גם באנגלית – בתיאום מראש בלבד

הערה לשחקני "סושי נינג'ה":
חדר מקדש הקאמי כולל חלק מהתפאורה והחידות של "סושי נינג'ה" – לפרטים נוספים: 050-5255144
במקרה של תקלה בהזמנה – ניתן להזמין בטלפון: 050-5255144

מבצעים והנחות:
– 10% הנחה לילדים עד גיל 12 (בליווי 2 מבוגרים לפחות, מלווה בתשלום)
– 20% הנחה לחוגג יום הולדת (רק לחוגג, בשבוע ההולדת, בקבוצה של 4+ משתתפים, הטבת יום הולדת אחת לקבוצה)
– חיילים בשירות חובה, סטודנטים ושירות לאומי – הנחה לפי המחירון (בהצגת תעודה)
– אין כפל מבצעים והנחות
– כל המבצעים מותנים בהצגת תעודה מתאימה
מחירון "מקדש הקאמי":

מחיר רגיל למשתתף:
3 משתתפים – 140 ש"ח
4 משתתפים – 130 ש"ח
5 משתתפים – 120 ש"ח
6 משתתפים ומעלה – 110 ש"ח

מחיר לחיילים, סטודנטים ובני שירות לאומי (בהצגת תעודה):
3 משתתפים – 130 ש"ח
4 משתתפים – 120 ש"ח
5 משתתפים – 110 ש"ח
6 משתתפים ומעלה – 100 ש"ח

חדר בריחה: אינפיניטי
סיפור: המשאב היקר ביותר לאנושות נשמר בסוד. השמועות מובילות למערת הקפה בג'מייקה. פרצתם למקום השמור בעולם – האם תשיגו את האוצר?

החדר דורג בין הטובים בעולם לשנת 2024.
החדר מאתגר ולא מתאים לקבוצה של 3 מתחילים.

כמות משתתפים: 3–7
משך המשחק: 70 דקות
דורש פעילות פיזית קלה (זחילה, התכופפות) – חובה לוודא שאין מגבלה
חולי אסתמה – באחריותכם ליידע מראש
גילאים: מגיל 8 בליווי מבוגר, מגיל 16 ללא ליווי
שפה: עברית, אפשר גם באנגלית – בתיאום מראש בלבד
במקרה של תקלה בהזמנה – ניתן להתקשר: 050-5255144

מחירון רגיל למשתתף:
3 משתתפים – 170 ש"ח
4 משתתפים – 130 ש"ח
5 משתתפים ומעלה – 120 ש"ח

מחיר לחיילים, סטודנטים ושירות לאומי (בהצגת תעודה):
3 משתתפים – 160 ש"ח
4 משתתפים – 120 ש"ח
5 משתתפים ומעלה – 110 ש"ח

מבצעים והנחות:
– 10% הנחה לילדים עד גיל 12 (בליווי 2 מבוגרים לפחות, מלווה חייב בתשלום)
– 20% הנחה לחוגג יום הולדת (רק לחוגג, בשבוע ההולדת, בקבוצה של 4+ משתתפים, הטבת יום הולדת אחת לקבוצה)
– חיילים בשירות חובה, סטודנטים ושירות לאומי – הנחה לפי המחירון
– כל המבצעים בתוקף בהצגת תעודה מתאימה בלבד
– אין כפל מבצעים והנחות

חדר בריחה: נרקוס
סיפור: מדיין, קולומביה. לאחר מותו של פאבלו אסקובר, קרטל מדיין נעלם. כעת, יותר מעשור אחרי, מתגלה דירת מסתור שלו – ממנה פועל מחדש הקרטל. אתם עומדים בראש קרטל חוארז – האם תצליחו לעצור את קרטל מדיין לפני שיהיה מאוחר מדי?

אזהרה: החדר כולל אלמנטים העלולים להשפיע על חולי אסתמה, אפילפסיה ואנשים עם פוסט-טראומה – מומלץ ליצור קשר לפני הזמנה.
דורש פעילות פיזית קלה (זחילה, התכופפות) – ניתן לעקוף במידת הצורך.
גילאים: מגיל 12 בליווי מבוגר, מגיל 16 ללא ליווי.
כמות משתתפים: 4–9
משך המשחק: 90 דקות
ניתן לשחק גם באנגלית – בתיאום מראש בלבד
במקרה של תקלה במערכת ההזמנות – ניתן להתקשר: 050-5255144

מחירון רגיל למשתתף:
4 משתתפים – 150 ש"ח
5 משתתפים – 140 ש"ח
6 משתתפים – 130 ש"ח
7 משתתפים ומעלה – 120 ש"ח

מחיר לחיילים, סטודנטים ושירות לאומי (בהצגת תעודה):
4 משתתפים – 140 ש"ח
5 משתתפים – 130 ש"ח
6 משתתפים – 120 ש"ח
7 משתתפים ומעלה – 110 ש"ח

מבצעים והנחות:
– 10% הנחה לילדים עד גיל 12 (בליווי שני מבוגרים לפחות, מלווה חייב בתשלום)
– 20% הנחה לחוגג יום הולדת (רק לחוגג, בשבוע ההולדת, בקבוצה של 6+ משתתפים, הטבת יום הולדת אחת לקבוצה)
– הנחות לחיילים, סטודנטים ושירות לאומי – לפי המחירון
– כל ההנחות בתוקף בהצגת תעודה מתאימה בלבד
– אין כפל מבצעים והנחות


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
