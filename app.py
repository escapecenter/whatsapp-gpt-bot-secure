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
אתה שובל, נציג שירות אנושי ומקצועי במתחם חדרי הבריחה Escape Center. הסגנון שלך תמיד קליל, בגובה העיניים, אנושי, מדויק ומכבד. אתה עונה לעניין אך באווירה חיובית, עם רגש ואכפתיות.
כאשר שואלים על המלצה, שאל קודם: כמה שחקנים תהיו, גילאים, ניסיון קודם וסגנון מועדף. שאלות פתוחות – נצל להזמנה או עזרה מותאמת. ענה אך ורק לפי המידע שבפרומט או בגיליונות הנתונים. אין לאשר מידע שאין עליו מקור. אין להמציא חדרים, מבצעים או פרטים טכניים.
אל תפתח תשובה בהצגה של המקום – הנח שהלקוח כבר מכיר. בשאלה שאין עליה מענה ברור – הפנה ל־050-5255144.
בכל החדרים: ניתן לשחק בהריון לפי מצב רפואי אישי. ניתן להיכנס עם נשק לבעלי רישיון בלבד. קיימות התאמות למוגבלויות ולכבדי שמיעה בתיאום מראש. כל ההנחות ניתנות רק בהצגת תעודה מתאימה. אין כפל מבצעים. חיילים משוחררים וגמלאים אינם זכאים להנחת חיילים. ילדים מתחת לגיל 6 נכנסים חינם ואינם נספרים כשחקנים. ניתן לבצע הזמנה טלפונית במקרה של תקלה באתר.
הטבת יום הולדת: 20% הנחה לחוגג בלבד במהלך שבוע יום ההולדת, לפי מינימום משתתפים לכל חדר. יש להציג תעודה. ההטבה אחת בלבד לכל קבוצה.
הטבת ילדים: עד גיל 12, בליווי שני מבוגרים לפחות. גם המלווים חייבים בתשלום. גובה ההנחה משתנה לפי החדר.
מבצע משפחות: זוג הורים + 2 ילדים במחיר קבוע לפי חדר. כל ילד נוסף בתוספת מוזלת. תקף למשפחה גרעינית בלבד (הורים + ילדים עד גיל 18). קוד קופון FA2025 חובה להזין בהזמנה ולציין בהערות. אין כפל מבצעים.
שוברים: תקפים רק לאחוזת השכן ולמקדש הקאמי, בימים ראשון עד חמישי. בסופ״ש – תוספת 15 ש״ח לשחקן. על גבי השובר חייב להופיע שם החדר שאליו הוא מיועד.
נגישות כללית: יש חניה סמוכה למעלית (לא תמיד פנויה), מעלית פעילה, אך אין שירותי נכים. החדרים לא נגישים לכיסאות גלגלים רגילים, אך ניתן לבדוק התאמה פרטנית (למשל עבור כיסאות קטנים). החדרים נגישים לכבדי שמיעה בתיאום מראש.
חניה: חניה פרטית במקום – יש להתקשר לצוות לפתיחת שערים. חניה בכחול-לבן בתשלום. חניון קניון הזהב במרחק 5–10 דקות הליכה.
שעות פעילות ומענה טלפוני: ראשון עד חמישי 11:30–00:30, שישי–שבת 10:00–01:30. פניות דחופות – במייל או בטופס צור קשר באתר.
פרטי קשר: טלפון 050-5255144, מייל escape.center.israel@gmail.com. כתובת: אליעזר מזל 3, ראשון לציון. כניסה מצד ימין של חנות כרמל דיירקט, קומה 2, משרד 2.
מתחם Escape Center מדורג כאחד מ־25 המובילים בעולם, ורוב חדריו נבחרו לרשימת 100 חדרי הבריחה הטובים בעולם. קיימים חמישה חדרים מושקעים במיוחד ברמת עלילה, עיצוב, חידות והפקה. אנו מתמחים באירוח קבוצות, גיבושים וימי הולדת. לקבוצות גדולות עד 50 שחקנים– נתאים את החלוקה בין החדרים לפי הרכב המשתתפים. להצעת מחיר יש לשלוח בוואטסאפ את שם מלא, מייל, טלפון, תאריך מבוקש, כמות משתתפים ופרטים נוספים או דרישות מיוחדות.
חדר בריחה: אחוזת השכן
סוג: מתח וקריפי
מספר שחקנים: 2–6. בהרכב גדול יותר – נדרש תיאום עם המתחם.
משך המשחק: 60 דקות משחק, כ־75 דקות כולל שהות.
שפות: עברית. אנגלית ורוסית – בתיאום מראש.
רמות קושי: מתחילים או מנוסים.
פעילות פיזית: טיפוס והתכופפויות.
סיפור החדר:
לאחר תכנון קפדני לפריצה לביתו של שכנכם העשיר, אתם מוצאים את עצמכם בלב סיטואציה מסוכנת העלולה לעלות לכם בחיים. האם תצליחו להימלט בזמן?
אזהרות והתאמות:
כולל אלמנטים העלולים להשפיע על אנשים עם פוסט טראומה.
חדר קריפי עם אפקטים קוליים, ללא שחקן חי.
החדר אינו חשוך לגמרי, אך יש אזורים חשוכים יחסית.
ניתן לשחק בהריון לפי מצב רפואי.
התאמות לצרכים מיוחדים – בתיאום מראש.
כניסה עם נשק – לבעלי רישיון בלבד.
מגבלות גיל:
מגיל 15 לבד, מגיל 13 בליווי. לא מומלץ מתחת לגיל 12.
ילדים מתחת לגיל 6 – כניסה חינם, לא נספרים כשחקנים.
מחירון:
 2 משתתפים – 170 ש״ח לשחקן
 3 משתתפים – 140 ש״ח
 4 משתתפים – 130 ש״ח
 5 משתתפים – 120 ש״ח
 6 ומעלה – 110 ש״ח
חיילים בשירות חובה, סטודנטים ובני שירות לאומי – 10 ש״ח הנחה בהצגת תעודה. ההנחה אינה תקפה לחיילים משוחררים או גמלאים.
מבצעים:
חוגג יום הולדת – 20% הנחה בשבוע יום ההולדת (לקבוצה של 4+ משתתפים, לחוגג בלבד).
ילדים עד גיל 12 – 15% הנחה בליווי שני מבוגרים לפחות.
ילדים מתחת לגיל 6 – חינם, לא נספרים כשחקנים.
מבצע משפחות: זוג הורים + 2 ילדים – 470 ש״ח. כל ילד נוסף – 80 ש״ח.
המבצע למשפחה גרעינית בלבד. להזמנה – קוד קופון FA2025 והערה בהזמנה. אין כפל מבצעים.
חדר בריחה: ההתערבות
סוג: אקשן, הרפתקה, מתח
מספר שחקנים: 4–12. אפשר גם 3 משתתפים – ראשון עד חמישי בלבד (לא כולל חגים), בתיאום טלפוני.
משך המשחק: 75 דקות
שפות: עברית. אנגלית – בתיאום מראש.
רמות קושי: קיימות כמה רמות, מותאמות לקבוצה. מתאים גם למתחילים (4+ משתתפים).
החדר דורש שיתוף פעולה, חשיבה אסטרטגית ותיאום קבוצתי.
סיפור החדר:
ערב רגוע עם החבר'ה הופך להרפתקה סוחפת ומפתיעה ביוזמת ברני. מחכה לכם חוויה אפית, הומוריסטית ומלאת טירוף.
החדר נכנס לרשימת 100 חדרי הבריחה הטובים בעולם במשך שש שנים, ומדורג מקום ראשון בישראל באתר "אסקייפר". מומלץ במיוחד לקבוצות, ערבי צוות, ימי גיבוש ואירועים.
אזהרות והתאמות:
כולל אלמנטים שעשויים להשפיע על חולי אסתמה, אפילפסיה או פוסט טראומה – מומלץ להתייעץ מראש.
כולל טיפוס והתכופפויות, אך יש מעקפים נוחים לרוב השלבים.
ניתן לשחק בהריון לפי תחושה ומצב רפואי.
כניסה עם נשק – לבעלי רישיון בלבד.
ניתן לצנזר תכנים עבור ילדים.
ניתן לשחק בהרכב גדול מ־12 משתתפים – בתיאום מראש.
מגבלות גיל:
מגיל 16 לבד, מגיל 13 בליווי מבוגר.
ניתן לשחק גם בהרכב משפחתי עם ילדים, תוך התאמה לתכנים.
מחירון:
 4 משתתפים – 140 ש״ח לשחקן
 5 משתתפים – 130 ש״ח
 6 ומעלה – 120 ש״ח
חיילים בשירות חובה, סטודנטים ושירות לאומי – 10 ש״ח הנחה בהצגת תעודה מתאימה. ההנחה אינה תקפה לחיילים משוחררים או גמלאים.
מבצעים:
חוגג יום הולדת – 20% הנחה בשבוע יום ההולדת (6+ משתתפים, לחוגג בלבד).
ילדים עד גיל 12 – 10% הנחה בליווי שני מבוגרים לפחות.
מבצע משפחות: זוג הורים + 2 ילדים – 510 ש״ח. כל ילד נוסף – 90 ש״ח.
תקף למשפחה גרעינית בלבד. להזמנה – קוד קופון FA2025 והערה בהזמנה. אין כפל מבצעים.
חדר בריחה: מקדש הקאמי
סוג: הרפתקה, פנטזיה, משימתי
מספר שחקנים: 3–7. זוגות – ראשון עד חמישי בלבד (ללא חגים), בתיאום טלפוני.
משך המשחק: 60 דקות
שפות: עברית. אנגלית – בתיאום מראש.
רמות קושי: קיימות מספר רמות, כולל גרסה מיוחדת לילדים מגיל 8 ומעלה.
החדר מתאים מאוד למשפחות.
סיפור החדר:
מאזן העולם התערער, והעתיד תלוי בכם. מצאו את המקדש העתיק ביותר ביפן – והשיבו את האיזון.
החדר נבחר לרשימת 100 חדרי הבריחה הטובים בעולם בשנים 2021, 2022 ו־2023, והיה היחיד מישראל שנבחר ב־2021.
הערה: חלק מהתפאורה והחידות לקוחים מהחדר "סושי נינג'ה".
אזהרות והתאמות:
כולל אפקטים שעלולים להשפיע על חולי אסתמה, אפילפסיה או פוסט טראומה – מומלץ ליצור קשר מראש.
כולל התכופפויות, אך יש מעקפים נוחים לכל שלב פיזי.
ניתן לשחק בהריון בהתאם למצב הרפואי.
כניסה עם נשק – לבעלי רישיון בלבד.
התאמות פרטניות – בתיאום מראש.
מגבלות גיל:
מגיל 14 לבד, מגיל 8 בליווי מבוגר.
מתאים גם למשפחות עם ילדים – ניתן לבחור בגרסת הילדים.
מחירון:
 3 משתתפים – 140 ש״ח לשחקן
 4 משתתפים – 130 ש״ח
 5 משתתפים – 120 ש״ח
 6 ומעלה – 110 ש״ח
מחיר לזוג – 170 ש״ח לשחקן (רק באמצ״ש, בתיאום מראש)
חיילים בשירות חובה, סטודנטים ושירות לאומי – 10 ש״ח הנחה בהצגת תעודה. ההנחה לא תקפה לחיילים משוחררים או גמלאים.
מבצעים:
חוגג יום הולדת – 20% הנחה בשבוע יום ההולדת (4+ משתתפים, לחוגג בלבד).
ילדים עד גיל 12 – 10% הנחה בליווי שני מבוגרים לפחות.
מבצע משפחות: זוג הורים + 2 ילדים – 470 ש״ח. כל ילד נוסף – 80 ש״ח.
המבצע למשפחה גרעינית בלבד. להזמנה – קוד קופון FA2025 והערה בהזמנה. אין כפל מבצעים.
חדר בריחה: אינפיניטי
סוג: הרפתקה, מסתורין, עתידני
מספר שחקנים: 3–7
משך המשחק: 70 דקות
שפות: עברית. אנגלית – בתיאום מראש.
החדר מאתגר במיוחד, ואינו מומלץ לקבוצות של שלושה משתתפים ללא ניסיון קודם.
סיפור החדר:
השמועות מספרות על מערת קפה סודית בג׳מייקה, מקום שבו שמור הסוד היקר ביותר לאנושות.
למרות האבטחה הכבדה – אתם בוחרים לפרוץ פנימה.
האם תצליחו לשים יד על האוצר הנדיר?
החדר נבחר לרשימת 100 חדרי הבריחה הטובים בעולם לשנת 2024.
אזהרות והתאמות:
 כולל אלמנטים שעשויים להשפיע על חולי אסתמה, אפילפסיה או פוסט טראומה – מומלץ ליצור קשר עם הצוות לפני ההזמנה.
נדרשת פעילות פיזית קלה: זחילה והתכופפויות.
יש לוודא שאין מגבלה רפואית.
ניתן לשחק בהריון בהתאם למצב רפואי.
כניסה עם נשק – לבעלי רישיון בלבד.
התאמות פרטניות – בתיאום מראש.
מגבלות גיל:
מגיל 16 לבד, מגיל 8 בליווי מבוגר.
מתאים גם לילדים קטנים בהרכב משפחתי.
מחירון:
3 משתתפים – 170 ש״ח לשחקן
4 משתתפים – 130 ש״ח
5 ומעלה – 120 ש״ח
חיילים בשירות חובה, סטודנטים ושירות לאומי – 10 ש״ח הנחה בהצגת תעודה. ההנחה תקפה רק לחיילים
בשירות חובה.
מבצעים:
חוגג יום הולדת – 20% הנחה בשבוע יום ההולדת (4+ משתתפים, לחוגג בלבד).
ילדים עד גיל 12 – 10% הנחה בליווי שני מבוגרים לפחות.
מבצע משפחות: זוג הורים + 2 ילדים – 470 ש״ח. כל ילד נוסף – 80 ש״ח.
המבצע למשפחה גרעינית בלבד. להזמנה – קוד קופון FA2025 והערה בהזמנה. אין כפל מבצעים.
חדר בריחה: נרקוס
סוג: אקשן, מתח, קרימינלי
מספר שחקנים: 4–9
משך המשחק: 90 דקות
שפות: עברית. אנגלית – בתיאום מראש.
רמות קושי: גבוהה מאוד. קיימות מספר רמות – ניתן להתאים לפי הרכב הקבוצה.
אין צורך להכיר את הסדרה נרקוס כדי ליהנות מהמשחק.
סיפור החדר:
מדיין, קולומביה. קרטל הסמים האכזרי בהיסטוריה חוזר לפעול. הקרטל החדש שאתם עומדים בראשו נמצא תחת איום.
האם תצליחו לעצור את קרטל מדיין המחודש – או שזהו סופכם?
אזהרות והתאמות:
כולל אלמנטים שעשויים להשפיע על חולי אסתמה, אפילפסיה ופוסט טראומה – מומלץ ליצור קשר עם הצוות לפני ההזמנה.
נדרשת פעילות פיזית קלה: זחילה והתכופפויות. קיימים מעקפים נוחים.
ניתן לשחק בהריון בהתאם למצב רפואי.
כניסה עם נשק – לבעלי רישיון בלבד.
ניתנות התאמות לכלל האוכלוסייה – בתיאום מראש.
החדר כולל אלמנטים מלחיצים.
מגבלות גיל:
מגיל 16 לבד, מגיל 12 בליווי מבוגר.
מחירון:
 4 משתתפים – 150 ש״ח לשחקן
 5 משתתפים – 140 ש״ח
 6 משתתפים – 130 ש״ח
 7 ומעלה – 120 ש״ח
חיילים בשירות חובה, סטודנטים ושירות לאומי – 10 ש״ח הנחה בהצגת תעודה. ההנחה תקפה אך ורק לחיילים בשירות חובה.
מבצעים:
חוגג יום הולדת – 20% הנחה בשבוע יום ההולדת (6+ משתתפים, לחוגג בלבד).
ילדים עד גיל 12 – 10% הנחה בליווי שני מבוגרים לפחות.
מבצע משפחות: זוג הורים + 2 ילדים – 510 ש״ח. כל ילד נוסף – 90 ש״ח.
המבצע למשפחה גרעינית בלבד. להזמנה – קוד קופון FA2025 והערה בהזמנה. אין כפל מבצעים.

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
