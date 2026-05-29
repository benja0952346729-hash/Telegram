import os
import re
import json
import threading
import requests
import psycopg2
import psycopg2.extras
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from google import genai
from google.genai import types

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID  = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DATABASE_URL       = os.getenv("DATABASE_URL")
DATA_FILE          = "lottery_data.json"

# ==================== GEMINI MULTI-KEY ROTATION ====================
GEMINI_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),  os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),  os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),  os.getenv("GEMINI_API_KEY_6"),
    os.getenv("GEMINI_API_KEY_7"),  os.getenv("GEMINI_API_KEY_8"),
    os.getenv("GEMINI_API_KEY_9"),  os.getenv("GEMINI_API_KEY_10"),
]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]
gemini_key_index = 0

def get_next_gemini_key() -> str:
    global gemini_key_index
    key = GEMINI_KEYS[gemini_key_index % len(GEMINI_KEYS)]
    gemini_key_index += 1
    return key

# ==================== LOTTERY TEMPLATE ====================
LOTTERY_TEMPLATE = """በ 400 ብር 5 ቁጥሮችን በተከታታይ በመያዝ እድሎን ይሞክሩ ለ 20 ሰው ብቻ ፈጣን ዕድል መልካም ዕድል

መደብ 👉በ 4️⃣0️⃣0️⃣ ብር 
       👉ግማሽ 2️⃣0️⃣0️⃣ ብር 

1ኛ 🥇5️⃣,0️⃣0️⃣0️⃣ ብር 
2ኛ 🥈1000
3ኛ 🥇400

{numbers}

CBE 1000641057146 biniyam dawit
አዋሽ  01335630641400
ዳሽን  5389857825011
ቴሌ ብር 0952346729"""

# ==================== DATABASE (NEON POSTGRES) ====================

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_rules (
                id         SERIAL PRIMARY KEY,
                rule       TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_chat_history (
                id         SERIAL PRIMARY KEY,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ DB initialized")
    except Exception as e:
        print(f"❌ DB init error: {e}")

def load_admin_rules() -> list:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT rule FROM admin_rules ORDER BY id ASC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        print(f"❌ load_admin_rules error: {e}")
        return []

def save_admin_rule(rule: str):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("INSERT INTO admin_rules (rule) VALUES (%s)", (rule,))
        conn.commit()
        cur.close()
        conn.close()
        print(f"✅ Rule saved: {rule}")
    except Exception as e:
        print(f"❌ save_admin_rule error: {e}")

def delete_all_admin_rules():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("DELETE FROM admin_rules")
        conn.commit()
        cur.close()
        conn.close()
        print("✅ All rules deleted")
    except Exception as e:
        print(f"❌ delete_all_admin_rules error: {e}")

def load_admin_chat_history(limit: int = 30) -> list:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT role, content FROM admin_chat_history ORDER BY id DESC LIMIT %s",
            (limit,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception as e:
        print(f"❌ load_admin_chat_history error: {e}")
        return []

def save_admin_chat_message(role: str, content: str):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO admin_chat_history (role, content) VALUES (%s, %s)",
            (role, content)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ save_admin_chat_message error: {e}")

def clear_admin_chat_history():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("DELETE FROM admin_chat_history")
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Chat history cleared")
    except Exception as e:
        print(f"❌ clear_admin_chat_history error: {e}")

def build_admin_rules_text() -> str:
    rules = load_admin_rules()
    if not rules:
        return ""
    lines = "\n".join(f"- {r}" for r in rules)
    return f"\n========= Admin ያስተማረኝ ህጎች =========\n{lines}\n"

# ==================== DATA MANAGEMENT ====================

def make_empty_slot(i: int) -> dict:
    start = (i - 1) * 5 + 1
    return {
        "numbers": list(range(start, start + 5)),
        "type": None,
        "p1_id": None, "p1_name": None, "p1_paid": False,
        "p2_id": None, "p2_name": None, "p2_paid": False,
    }

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    slots = {str(i): make_empty_slot(i) for i in range(1, 21)}
    return {"slots": slots, "lottery_message_id": None, "chat_id": None}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_slot_full_booked(slot: dict) -> bool:
    if slot["type"] == "full":
        return True
    if slot["type"] == "half" and slot["p2_id"] is not None:
        return True
    return False

def get_slot_by_number(number: int, data: dict):
    for slot_id, slot in data["slots"].items():
        if number in slot["numbers"]:
            return slot_id, slot
    return None, None

def build_numbers_text(data: dict) -> str:
    groups = []
    for slot_id, slot in data["slots"].items():
        lines = []
        for idx, num in enumerate(slot["numbers"]):
            if idx == 0:
                lines.append(_format_first_line(num, slot))
            else:
                lines.append(f"{num:02d}#")
        groups.append("\n".join(lines))
    return "\n\n".join(groups)

def _format_first_line(num: int, slot: dict) -> str:
    n = f"{num:02d}#"
    if slot["type"] is None:
        return n
    if slot["type"] == "full":
        mark = "✅" if slot["p1_paid"] else "⏳"
        return f"{n} {slot['p1_name']} {mark}"
    p1_name = slot["p1_name"] or ""
    p1_mark = "✅" if slot["p1_paid"] else "⏳"
    if slot["p2_id"] is None:
        return f"{n} {p1_name}+ {p1_mark}"
    else:
        p2_name = slot["p2_name"] or ""
        p2_mark = "✅" if slot["p2_paid"] else "⏳"
        if slot["p1_paid"] and slot["p2_paid"]:
            return f"{n} {p1_name}+{p2_name} ✅"
        return f"{n} {p1_name}{p1_mark}+{p2_name}{p2_mark}"

def build_full_message(data: dict) -> str:
    return LOTTERY_TEMPLATE.format(numbers=build_numbers_text(data))

def build_full_state_for_ai(data: dict) -> str:
    lines = []
    for slot_id, slot in data["slots"].items():
        nums = slot["numbers"]
        t    = slot["type"]
        if t is None:
            lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ነፃ")
        elif t == "full":
            paid = "ከፍሏል ✅" if slot["p1_paid"] else "ገና አልከፈለም ⏳"
            lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ሙሉ | {slot['p1_name']} (ID:{slot['p1_id']}) | {paid}")
        elif t == "half":
            p1_paid = "✅" if slot["p1_paid"] else "⏳"
            if slot["p2_id"] is None:
                lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ግማሽ | p1={slot['p1_name']} (ID:{slot['p1_id']}) {p1_paid} | p2=ክፍት")
            else:
                p2_paid = "✅" if slot["p2_paid"] else "⏳"
                lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ግማሽ ሙሉ | p1={slot['p1_name']} (ID:{slot['p1_id']}) {p1_paid} | p2={slot['p2_name']} (ID:{slot['p2_id']}) {p2_paid}")
    filled  = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
    summary = f"\n--- ጠቅላላ: {filled}/20 slots ሞልቷል ---\n"
    return summary + "\n".join(lines)

# ==================== GEMINI AI CALL ====================

def gemini_call(prompt: str, max_tokens: int = 500, temperature: float = 0.2) -> str:
    for attempt in range(2):
        key         = get_next_gemini_key()
        key_preview = key[:8] + "..." if key else "None"
        try:
            client   = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                )
            )
            print(f"✅ Gemini OK (key: {key_preview})")
            return response.text.strip()
        except Exception as e:
            err = str(e)
            if "API_KEY_INVALID" in err or "API key not valid" in err:
                reason = "❌ API Key ትክክል አይደለም"
            elif "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                reason = "❌ Quota ተጠቀሰ"
            elif "PERMISSION_DENIED" in err:
                reason = "❌ Permission የለም"
            elif "UNAVAILABLE" in err or "503" in err:
                reason = "❌ Gemini server አይሰራም"
            elif "timeout" in err.lower():
                reason = "❌ Timeout"
            else:
                reason = f"❌ Error: {err}"
            print(f"⚠️ Gemini attempt {attempt+1} (key: {key_preview}): {reason}")
    print("🔴 Gemini ሙሉ በሙሉ አልሰራም")
    return ""

# ==================== ADMIN PRIVATE GEMINI CHAT ====================

def admin_gemini_chat(user_message: str, data: dict) -> str:
    """Admin DM ውስጥ ሙሉ context ያለው Gemini ውይይት"""
    full_state   = build_full_state_for_ai(data)
    admin_rules  = build_admin_rules_text()
    history      = load_admin_chat_history(limit=30)

    history_text = "\n".join(
        f"{'Admin' if m['role']=='user' else 'Gemini'}: {m['content']}"
        for m in history
    ) if history else "(ምንም ታሪክ የለም)"

    prompt = f"""አንተ ሙሉ የሎተሪ ስርዓት Gemini ነህ። Admin ጋር በ private ታወራለህ።
ሁሉንም ታወቃለህ — slots፣ ተጫዋቾች፣ ክፍያ፣ ህጎች።

========= የሎተሪ ሙሉ ሁኔታ =========
{full_state}

========= የተመዘገቡ ህጎች =========
{admin_rules if admin_rules else "ምንም ህግ አልተመዘገበም"}

========= የውይይት ታሪክ =========
{history_text}

========= Admin አዲስ መልእክት =========
{user_message}

========= መመሪያ =========
- አማርኛ ብቻ መልስ
- Admin ህግ ሲጨምር → [SAVE_RULE: ህጉን እዚህ ፃፍ] format ጨምር
- Admin ህጎች እንዲሰረዙ ከፈለገ → [DELETE_RULES] ፃፍ  
- ሁሉንም ጥያቄ ከ data ጋር መልስ
- አጭር፣ ግልጽ፣ ጠቃሚ መልስ ስጥ"""

    return gemini_call(prompt, max_tokens=600, temperature=0.3)


def process_admin_gemini_response(response: str) -> tuple[str, list, bool]:
    """Gemini response ውስጥ SAVE_RULE እና DELETE_RULES ያውጣ"""
    new_rules   = []
    delete_all  = False
    clean_reply = response

    # SAVE_RULE ፈልግ
    save_matches = re.findall(r'\[SAVE_RULE:\s*(.+?)\]', response)
    for rule in save_matches:
        rule = rule.strip()
        if rule:
            new_rules.append(rule)
    clean_reply = re.sub(r'\[SAVE_RULE:\s*.+?\]', '', clean_reply).strip()

    # DELETE_RULES ፈልግ
    if '[DELETE_RULES]' in response:
        delete_all  = True
        clean_reply = clean_reply.replace('[DELETE_RULES]', '').strip()

    return clean_reply, new_rules, delete_all

# ==================== GROUP AI BRAIN ====================

def ai_brain(user_message: str, user_id: int, user_name: str, full_state: str) -> dict:
    admin_rules = build_admin_rules_text()
    prompt = f"""አንተ የሎተሪ ስርዓት AI brain ነህ። Bot worker ነው የሚያስፈጽመው።
ተጫዋቾች ብቻ ናቸው የሚናገሩህ — group ውስጥ።

========= የሎተሪ ህጎች =========
- 20 slots (1-20), እያንዳንዱ slot 5 ቁጥሮች (slot1=1-5, slot2=6-10, ... slot20=96-100)
- ሙሉ = 400ብር (አንድ ሰው), ግማሽ = 200ብር (ሁለት ሰዎች)
- ሽልማት: 1ኛ=5000ብር, 2ኛ=1000ብር, 3ኛ=400ብር
- ክፍያ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ 0952346729
- ተጫዋች የራሱን ቁጥር ብቻ ሰርዝ/ቀይር ይችላል

========= ቁጥር መያዝ ምልክቶች =========
ሙሉ (default): "06", "36ሙሉ"
ግማሽ: "21+", "21ግማሽ", "21half", "21 200"
ብዙ ቁጥር: "10 16 21ግማሽ" → 10=ሙሉ, 16=ሙሉ, 21=ግማሽ

========= የአሁን ሎተሪ ሁኔታ =========
{full_state}
{admin_rules}
========= ተጠቃሚ =========
User ID: {user_id}
User Name: {user_name}
መልእክት: "{user_message}"

========= ACTION ህጎች =========
1. ቁጥር ሲጽፍ → ቀጥታ book → reply: "እሺ ገቢ 🙏"
2. ውስብስብ/ግልጽ ካልሆነ → ጥያቄ ጠይቅ
3. የተያዘ slot ሌላ ሰው ሲጠራ → reply: "ተቀድመሃል ቤተሰብ 🙏"
4. ሰው ቀድሞ የያዘውን እንደገና ሲጠራ → reply: "ይዥሄልሃለው ቤተሰብ 🙏"
5. ሰው "ያዝኩ" ቢል ግን ያልያዘ → "አይደለም፣ [ስም] [slot] ይዞታል — ከላይ ተመልከት"
6. ቁጥር አውጣ → cancel (የራሱን ብቻ)
7. ቁጥር ቀይር (X በ Y) → cancel_and_rebook (የራሱን ብቻ)
8. ክፍያ ማስረጃ → reply: "ተቀብዬአለሁ ✅ Admin ያረጋግጣል"
9. ሎተሪ ጥያቄ → AI ይመልሳል
10. Admin actions (mark_paid ወዘተ) → ተጫዋች ሊያደርግ አይችልም

========= SECURITY =========
- ተጫዋች የሌላ ሰው data ሊቀይር አይችልም
- JSON format ሳይሰብር ሁሌ ትክክለኛ action ብቻ

========= OUTPUT FORMAT (JSON ብቻ) =========
{{"action":"book_full","number":6,"name":"አበበ","reply":"እሺ ገቢ 🙏"}}
{{"action":"book_half_p1","number":21,"name":"አበበ","reply":"እሺ ገቢ 🙏"}}
{{"action":"book_half_p2","number":21,"name":"አበበ","reply":"እሺ ገቢ 🙏"}}
{{"action":"book_multiple","bookings":[{{"number":10,"type":"full"}},{{"number":21,"type":"half"}}],"name":"አበበ","reply":"እሺ ገቢ 🙏"}}
{{"action":"cancel","number":6,"reply":"✅ ተሰርዟል።"}}
{{"action":"cancel_and_rebook","cancel_number":6,"book_number":11,"book_type":"full","name":"አበበ","reply":"✅ ተቀይሯል።"}}
{{"action":"mark_paid","number":6,"which":1,"reply":"✅ ክፍያ ተረጋግጧል!"}}
{{"action":"reply","reply":"..."}}
{{"action":"ask","reply":"..."}}

አሁን JSON ብቻ:"""

    raw = gemini_call(prompt, max_tokens=400, temperature=0.1)
    print(f"🧠 AI Brain raw: {raw}")
    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        match = re.search(r'\{.*?\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"❌ AI Brain parse error: {e}")
    return {"action": "reply", "reply": "❌ ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።"}

# ==================== BOT EXECUTOR ====================

def execute_action(action_data: dict, user_id: int, data: dict) -> dict:
    action  = action_data.get("action", "reply")
    reply   = action_data.get("reply", "")
    number  = action_data.get("number")
    name    = action_data.get("name", "ተጠቃሚ")
    which   = action_data.get("which", 1)
    changed = False

    if action == "book_full" and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id and slot["type"] is None:
            data["slots"][slot_id].update({
                "type": "full",
                "p1_id": user_id, "p1_name": name, "p1_paid": False,
                "p2_id": None, "p2_name": None, "p2_paid": False,
            })
            changed = True

    elif action == "book_half_p1" and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id and slot["type"] is None:
            data["slots"][slot_id].update({
                "type": "half",
                "p1_id": user_id, "p1_name": name, "p1_paid": False,
                "p2_id": None, "p2_name": None, "p2_paid": False,
            })
            changed = True

    elif action == "book_half_p2" and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id and slot["type"] == "half" and slot["p2_id"] is None:
            data["slots"][slot_id].update({
                "p2_id": user_id, "p2_name": name, "p2_paid": False,
            })
            changed = True

    elif action == "book_multiple":
        bookings = action_data.get("bookings", [])
        for b in bookings:
            num   = b.get("number")
            btype = b.get("type", "full")
            if not num:
                continue
            slot_id, slot = get_slot_by_number(num, data)
            if slot_id and slot["type"] is None:
                if btype == "half":
                    data["slots"][slot_id].update({
                        "type": "half",
                        "p1_id": user_id, "p1_name": name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                else:
                    data["slots"][slot_id].update({
                        "type": "full",
                        "p1_id": user_id, "p1_name": name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                changed = True

    elif action == "cancel" and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id and slot["type"] is not None:
            if slot["p1_id"] == user_id:
                if slot["type"] == "half" and slot["p2_id"] is not None:
                    data["slots"][slot_id].update({
                        "p1_id": slot["p2_id"], "p1_name": slot["p2_name"],
                        "p1_paid": slot["p2_paid"],
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                else:
                    nums = slot["numbers"]
                    data["slots"][slot_id] = make_empty_slot(int(slot_id))
                    data["slots"][slot_id]["numbers"] = nums
                changed = True
            elif slot["type"] == "half" and slot["p2_id"] == user_id:
                data["slots"][slot_id].update({
                    "p2_id": None, "p2_name": None, "p2_paid": False
                })
                changed = True

    elif action == "cancel_and_rebook":
        cancel_num = action_data.get("cancel_number")
        book_num   = action_data.get("book_number")
        book_type  = action_data.get("book_type", "full")
        if cancel_num:
            slot_id, slot = get_slot_by_number(cancel_num, data)
            if slot_id and (slot["p1_id"] == user_id or slot["p2_id"] == user_id):
                if slot["p1_id"] == user_id:
                    if slot["type"] == "half" and slot["p2_id"] is not None:
                        data["slots"][slot_id].update({
                            "p1_id": slot["p2_id"], "p1_name": slot["p2_name"],
                            "p1_paid": slot["p2_paid"],
                            "p2_id": None, "p2_name": None, "p2_paid": False,
                        })
                    else:
                        nums = slot["numbers"]
                        data["slots"][slot_id] = make_empty_slot(int(slot_id))
                        data["slots"][slot_id]["numbers"] = nums
                else:
                    data["slots"][slot_id].update({
                        "p2_id": None, "p2_name": None, "p2_paid": False
                    })
                changed = True
        if book_num:
            slot_id2, slot2 = get_slot_by_number(book_num, data)
            if slot_id2 and slot2["type"] is None:
                if book_type == "half":
                    data["slots"][slot_id2].update({
                        "type": "half",
                        "p1_id": user_id, "p1_name": name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                else:
                    data["slots"][slot_id2].update({
                        "type": "full",
                        "p1_id": user_id, "p1_name": name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                changed = True

    elif action == "mark_paid" and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id and slot["type"] is not None:
            if which == 2 and slot["p2_id"] is not None:
                data["slots"][slot_id]["p2_paid"] = True
            else:
                data["slots"][slot_id]["p1_paid"] = True
            changed = True

    return {"data": data, "reply": reply, "changed": changed}

# ==================== BOT HANDLERS ====================

async def start_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return
    data = load_data()
    data["slots"] = {str(i): make_empty_slot(i) for i in range(1, 21)}
    sent = await update.message.reply_text(build_full_message(data))
    data["lottery_message_id"] = sent.message_id
    data["chat_id"]            = update.effective_chat.id
    save_data(data)
    await update.message.reply_text("✅ ሎተሪ ጀምሯል!")


async def mark_paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("አጠቃቀም: /paid <ቁጥር> [2]")
        return
    try:
        number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ቁጥር ብቻ ፃፍ")
        return
    which = 1
    if len(context.args) >= 2:
        try:
            which = int(context.args[1])
        except ValueError:
            pass
    data        = load_data()
    action_data = {"action": "mark_paid", "number": number, "which": which, "reply": ""}
    result      = execute_action(action_data, update.effective_user.id, data)
    if result["changed"]:
        save_data(result["data"])
        await update_lottery_message(context.bot, result["data"])
        slot_id, slot = get_slot_by_number(number, result["data"])
        name = slot["p2_name"] if which == 2 else slot["p1_name"]
        await update.message.reply_text(f"✅ {name} ክፍያ ተረጋግጧል!")
    else:
        await update.message.reply_text("❌ Slot አልተገኘም")


async def clear_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    clear_admin_chat_history()
    await update.message.reply_text("🗑️ የውይይት ታሪክ ተሰርዟል።")


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    rules = load_admin_rules()
    if not rules:
        await update.message.reply_text("📚 እስካሁን ምንም ህግ አልተመዘገበም።")
    else:
        text = "📚 የተመዘገቡ ህጎች:\n\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
        await update.message.reply_text(text)


async def update_lottery_message(bot: Bot, data: dict):
    if data.get("lottery_message_id") and data.get("chat_id"):
        try:
            await bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["lottery_message_id"],
                text=build_full_message(data)
            )
        except Exception as e:
            print(f"Message update error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text  = update.message.text.strip()
    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "ተጠቃሚ"
    chat_type = update.effective_chat.type  # "private" or "group"/"supergroup"

    # ==================== ADMIN PRIVATE → GEMINI FULL CHAT ====================
    if user_id == ADMIN_TELEGRAM_ID and chat_type == "private":
        data = load_data()
        print(f"🔐 Admin private: '{raw_text}'")

        # ታሪክ አስቀምጥ
        save_admin_chat_message("user", raw_text)

        # Gemini ጥራ
        response = admin_gemini_chat(raw_text, data)
        if not response:
            await update.message.reply_text("❌ Gemini አልተናገረም። ቆይተህ ሞክር።")
            return

        # SAVE_RULE / DELETE_RULES process
        clean_reply, new_rules, delete_all = process_admin_gemini_response(response)

        if delete_all:
            delete_all_admin_rules()

        for rule in new_rules:
            save_admin_rule(rule)

        if new_rules:
            clean_reply += f"\n\n📌 {len(new_rules)} ህግ ተመዝግቧል።"
        if delete_all:
            clean_reply += "\n🗑️ ሁሉም ህጎች ተሰርዘዋል።"

        # Gemini reply ታሪክ አስቀምጥ
        save_admin_chat_message("assistant", clean_reply)

        await update.message.reply_text(clean_reply)
        return

    # ==================== GROUP → NORMAL BOT MODE ====================
    data       = load_data()
    full_state = build_full_state_for_ai(data)
    print(f"📩 {user_name} ({user_id}): '{raw_text}'")

    action_data = ai_brain(raw_text, user_id, user_name, full_state)
    print(f"🧠 Action: {action_data}")

    if action_data.get("action") == "ask":
        await update.message.reply_text(action_data.get("reply", "❓"))
        return

    result = execute_action(action_data, user_id, data)

    if result["changed"]:
        save_data(result["data"])
        await update_lottery_message(context.bot, result["data"])
        filled = sum(1 for s in result["data"]["slots"].values() if is_slot_full_booked(s))
        if filled == 20:
            await update.message.reply_text("🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")

    await update.message.reply_text(result["reply"])


# ==================== KEEP ALIVE ====================

class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.getenv("PORT", 10000))
    HTTPServer(("0.0.0.0", port), KeepAlive).serve_forever()

# ==================== MAIN ====================

def main():
    if not GEMINI_KEYS:
        print("❌ ምንም Gemini API key አልተገኘም!")
        return

    init_db()

    thread        = threading.Thread(target=run_server)
    thread.daemon = True
    thread.start()

    import asyncio
    import telegram as tg

    async def close_others():
        try:
            bot = tg.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.delete_webhook(drop_pending_updates=True)
            await bot.close()
        except Exception as e:
            print(f"Close others: {e}")

    asyncio.run(close_others())

    async def error_handler(update, context):
        print(f"🔴 ERROR: {context.error}")
        import traceback
        traceback.print_exc()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start_lottery", start_lottery))
    app.add_handler(CommandHandler("paid",          mark_paid_cmd))
    app.add_handler(CommandHandler("rules",         rules_cmd))
    app.add_handler(CommandHandler("clear",         clear_history_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print(f"✅ {len(GEMINI_KEYS)} Gemini API keys loaded")
    print("✅ Bot እየሰራ ነው... (Neon DB + Gemini Full Context)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
