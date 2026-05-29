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
from groq import Groq
from google import genai
from google.genai import types
import base64

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID  = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DATABASE_URL       = os.getenv("DATABASE_URL")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
DATA_FILE          = "lottery_data.json"

# ==================== GEMINI MULTI-KEY ROTATION + TOKEN TRACKING ====================
GEMINI_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),  os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),  os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),  os.getenv("GEMINI_API_KEY_6"),
    os.getenv("GEMINI_API_KEY_7"),  os.getenv("GEMINI_API_KEY_8"),
    os.getenv("GEMINI_API_KEY_9"),  os.getenv("GEMINI_API_KEY_10"),
]
GEMINI_KEYS = [k for k in GEMINI_KEYS if k]
gemini_key_index = 0

gemini_token_usage: dict = {}
gemini_total_tokens: int = 0
GEMINI_KEY_LIMIT = 500_000

def get_next_gemini_key() -> str:
    global gemini_key_index
    for _ in range(len(GEMINI_KEYS)):
        key         = GEMINI_KEYS[gemini_key_index % len(GEMINI_KEYS)]
        gemini_key_index += 1
        key_preview = key[:8] + "..."
        usage       = gemini_token_usage.get(key_preview, {})
        if usage.get("total", 0) < GEMINI_KEY_LIMIT:
            return key
    key = GEMINI_KEYS[gemini_key_index % len(GEMINI_KEYS)]
    gemini_key_index += 1
    return key

def track_token_usage(key_preview: str, input_tokens: int, output_tokens: int):
    global gemini_total_tokens
    if key_preview not in gemini_token_usage:
        gemini_token_usage[key_preview] = {"input": 0, "output": 0, "total": 0, "calls": 0}
    gemini_token_usage[key_preview]["input"]  += input_tokens
    gemini_token_usage[key_preview]["output"] += output_tokens
    gemini_token_usage[key_preview]["total"]  += input_tokens + output_tokens
    gemini_token_usage[key_preview]["calls"]  += 1
    gemini_total_tokens += input_tokens + output_tokens

def build_token_report() -> str:
    if not gemini_token_usage:
        return "📊 እስካሁን token አልተጠቀሰም።"
    lines = [f"📊 Gemini Token Usage Report\n{'='*30}"]
    grand_total = 0
    for key_preview, usage in gemini_token_usage.items():
        total    = usage["total"]
        pct      = (total / GEMINI_KEY_LIMIT) * 100
        bar      = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        status   = "✅" if pct < 80 else ("⚠️" if pct < 95 else "❌")
        lines.append(
            f"\n🔑 Key: {key_preview}\n"
            f"  {status} [{bar}] {pct:.1f}%\n"
            f"  📥 Input:  {usage['input']:,} tokens\n"
            f"  📤 Output: {usage['output']:,} tokens\n"
            f"  📊 Total:  {total:,} / {GEMINI_KEY_LIMIT:,}\n"
            f"  📞 Calls:  {usage['calls']}"
        )
        grand_total += total
    lines.append(f"\n{'='*30}")
    lines.append(f"🔢 ጠቅላላ tokens: {grand_total:,}")
    lines.append(f"🔑 Active keys:  {len(GEMINI_KEYS)}")
    return "\n".join(lines)

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

# ==================== DATABASE ====================

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
            CREATE TABLE IF NOT EXISTS payments (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT,
                user_name   TEXT,
                ref         TEXT UNIQUE,
                amount      FLOAT,
                bank        TEXT,
                photo_ok    BOOLEAN DEFAULT FALSE,
                sms_ok      BOOLEAN DEFAULT FALSE,
                status      TEXT DEFAULT 'pending',
                slot_number INT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS used_refs (
                id         SERIAL PRIMARY KEY,
                ref        TEXT UNIQUE NOT NULL,
                user_id    BIGINT,
                used_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS group_commands (
                id         SERIAL PRIMARY KEY,
                command    TEXT NOT NULL,
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
    except Exception as e:
        print(f"❌ delete_all_admin_rules error: {e}")

def delete_specific_rule(rule_text: str):
    """ተቃርኖ ያለው rule ን ይሰርዛል"""
    try:
        conn = get_db()
        cur  = conn.cursor()
        # ትክክለኛ match ሞክር
        cur.execute("DELETE FROM admin_rules WHERE rule = %s", (rule_text,))
        if cur.rowcount == 0:
            # partial match ሞክር
            cur.execute("DELETE FROM admin_rules WHERE rule ILIKE %s", (f"%{rule_text[:30]}%",))
        conn.commit()
        deleted = cur.rowcount
        cur.close()
        conn.close()
        print(f"🗑️ Deleted rule (matched {deleted}): {rule_text[:50]}")
    except Exception as e:
        print(f"❌ delete_specific_rule error: {e}")

def build_admin_rules_text() -> str:
    rules = load_admin_rules()
    if not rules:
        return ""
    lines = "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
    return f"\n========= Admin ያስተማረኝ ህጎች (እነዚህ ሁሉንም ነገር OVERRIDE ያደርጋሉ) =========\n{lines}\n"

# ==================== PAYMENT DB HELPERS ====================

def is_ref_used(ref: str) -> bool:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM used_refs WHERE ref=%s", (ref,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"❌ is_ref_used error: {e}")
        return False

def mark_ref_used(ref: str, user_id: int):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("INSERT INTO used_refs (ref, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (ref, user_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ mark_ref_used error: {e}")

def get_payment_by_ref(ref: str) -> dict:
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM payments WHERE ref=%s", (ref,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"❌ get_payment_by_ref error: {e}")
        return None

def upsert_payment(ref: str, user_id: int, user_name: str, amount: float,
                   bank: str, photo_ok: bool = False, sms_ok: bool = False,
                   slot_number: int = None):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO payments (ref, user_id, user_name, amount, bank, photo_ok, sms_ok, slot_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ref) DO UPDATE SET
                photo_ok    = payments.photo_ok OR EXCLUDED.photo_ok,
                sms_ok      = payments.sms_ok   OR EXCLUDED.sms_ok,
                amount      = CASE WHEN EXCLUDED.sms_ok THEN EXCLUDED.amount ELSE payments.amount END,
                slot_number = COALESCE(EXCLUDED.slot_number, payments.slot_number)
        """, (ref, user_id, user_name, amount, bank, photo_ok, sms_ok, slot_number))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ upsert_payment error: {e}")

def set_payment_approved(ref: str):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("UPDATE payments SET status='approved' WHERE ref=%s", (ref,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ set_payment_approved error: {e}")

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

def get_slot_by_user(user_id: int, data: dict):
    results = []
    for slot_id, slot in data["slots"].items():
        if slot["p1_id"] == user_id or slot["p2_id"] == user_id:
            results.append((slot_id, slot))
    return results

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
    state   = summary + "\n".join(lines)

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT 50")
        payments = cur.fetchall()
        cur.close()
        conn.close()

        if payments:
            total_approved = sum(p["amount"] for p in payments if p["status"] == "approved" and p["amount"])
            total_pending  = sum(p["amount"] for p in payments if p["status"] == "pending" and p["amount"])
            pay_lines = [
                f"\n--- Payment Summary ---",
                f"✅ Approved ጠቅላላ: {total_approved:.0f} ብር",
                f"⏳ Pending ጠቅላላ: {total_pending:.0f} ብር",
                f"\n--- Payment Details (ቅርብ 50) ---"
            ]
            for p in payments:
                pay_lines.append(
                    f"User:{p['user_name']} (ID:{p['user_id']}) | Ref:{p['ref']} | "
                    f"{p['amount']}ብር | {p['bank']} | {p['status']} | "
                    f"photo:{'✅' if p['photo_ok'] else '❌'} sms:{'✅' if p['sms_ok'] else '❌'}"
                )
            state += "\n" + "\n".join(pay_lines)
    except Exception as e:
        print(f"❌ payment state error: {e}")

    return state

# ==================== GEMINI AI CALL ====================

def gemini_call(prompt: str, max_tokens: int = 800, temperature: float = 0.2) -> str:
    for attempt in range(len(GEMINI_KEYS) * 2):
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
            try:
                usage      = response.usage_metadata
                input_tok  = usage.prompt_token_count     or 0
                output_tok = usage.candidates_token_count or 0
                track_token_usage(key_preview, input_tok, output_tok)
                print(f"✅ Gemini OK (key: {key_preview}) | in={input_tok} out={output_tok}")
            except Exception as te:
                print(f"✅ Gemini OK (key: {key_preview}) | token err: {te}")
            return response.text.strip()
        except Exception as e:
            err = str(e)
            if "API_KEY_INVALID" in err or "API key not valid" in err:
                reason = "❌ API Key ትክክል አይደለም"
            elif "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                reason = "❌ Quota ተጠቀሰ"
                if key_preview not in gemini_token_usage:
                    gemini_token_usage[key_preview] = {"input": 0, "output": 0, "total": 0, "calls": 0}
                gemini_token_usage[key_preview]["total"] = GEMINI_KEY_LIMIT
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

# ==================== GROQ PAYMENT EXTRACTION ====================

def groq_extract_payment_from_text(text: str) -> dict:
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""Extract ALL transaction reference IDs from this SMS/text. Return JSON only, no markdown.

Text: "{text}"

Rules:
- refs: LIST of ALL reference/transaction IDs found.
- amount: ETB value (number only)
- bank: "CBE", "TELEBIRR", "AWASH", or "DASHEN"

Return: {{"refs":["FT26149R63JM"],"amount":50.0,"bank":"CBE"}}
If not a payment: {{"refs":[],"amount":null,"bank":null}}

JSON only:"""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.1,
        )
        raw   = response.choices[0].message.content.strip()
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        result = json.loads(clean)
        if "ref" in result and "refs" not in result:
            result["refs"] = [result["ref"]] if result.get("ref") else []
        return result
    except Exception as e:
        print(f"❌ groq_extract_payment_from_text error: {e}")
        return {"refs": [], "amount": None, "bank": None}

def groq_extract_payment_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    try:
        client    = Groq(api_key=GROQ_API_KEY)
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        prompt    = """Extract payment info from this receipt/screenshot. Return JSON only, no markdown.

Rules:
- ref: transaction/receipt/reference ID or number
- amount: number only (ETB value sent)
- bank: "CBE", "TELEBIRR", "AWASH", or "DASHEN"

Return: {"ref":"...","amount":50.0,"bank":"CBE"}
If not a payment: {"ref":null,"amount":null,"bank":null}

JSON only:"""

        response = client.chat.completions.create(
            model="llama-4-scout-17b-16e-instruct",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}},
                    {"type": "text", "text": prompt}
                ]
            }],
            max_tokens=100,
            temperature=0.1,
        )
        raw   = response.choices[0].message.content.strip()
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        return json.loads(clean)
    except Exception as e:
        print(f"❌ groq_extract_payment_from_image error: {e}")
        return {"ref": None, "amount": None, "bank": None}

# ==================== PAYMENT APPROVAL LOGIC ====================

async def handle_payment_match(ref: str, payment: dict, bot: Bot, data: dict):
    user_id  = payment["user_id"]
    amount   = payment["amount"]
    bank     = payment["bank"]
    slot_num = payment["slot_number"]

    if is_ref_used(ref):
        await bot.send_message(chat_id=user_id, text=f"❌ ይህ ref ({ref}) አስቀድሞ ተጠቅሟል!")
        return

    if slot_num:
        slot_id, slot = get_slot_by_number(slot_num, data)
    else:
        user_slots = get_slot_by_user(user_id, data)
        if not user_slots:
            await bot.send_message(chat_id=user_id, text="❌ ያዝከው slot አልተገኘም።")
            return
        slot_id, slot = user_slots[0]
        slot_num = slot["numbers"][0]

    expected = 400.0 if slot["type"] == "full" else 200.0
    if amount < expected:
        await bot.send_message(
            chat_id=user_id,
            text=f"❌ ክፍያ አይበቃም! የላካችሁት {amount} ብር ነው። ያስፈልጋል {expected} ብር።"
        )
        return

    if slot["p1_id"] == user_id:
        data["slots"][slot_id]["p1_paid"] = True
    elif slot["p2_id"] == user_id:
        data["slots"][slot_id]["p2_paid"] = True

    set_payment_approved(ref)
    mark_ref_used(ref, user_id)
    save_data(data)

    await bot.send_message(
        chat_id=user_id,
        text=f"✅ ክፍያ ተረጋግጧል! {amount} ብር ({bank})\nRef: {ref}\n🎉 መልካም ዕድል!"
    )
    await bot.send_message(
        chat_id=ADMIN_TELEGRAM_ID,
        text=f"✅ AUTO APPROVED\nUser: {payment['user_name']} (ID:{user_id})\nRef: {ref}\nብር: {amount} ({bank})\nSlot: {slot_num}"
    )
    await update_lottery_message(bot, data)
    print(f"✅ Auto approved: ref={ref}, user={user_id}, amount={amount}")

# ==================== AI BRAIN ====================

def ai_brain(user_message: str, user_id: int, user_name: str, full_state: str, is_admin_in_group: bool = False) -> dict:
    admin_rules = build_admin_rules_text()

    if is_admin_in_group:
        prompt = f"""አንተ የሎተሪ ስርዓት AI brain ነህ። ADMIN group ላይ አዛዥ ነው — ማንኛውም ትዕዛዝ ቀጥታ ፈጽም።

{admin_rules if admin_rules else ""}

========= የአሁን ሎተሪ ሁኔታ =========
{full_state}

========= Admin ትዕዛዝ =========
Admin Name: {user_name}
Admin ID: {user_id}
ትዕዛዝ: "{user_message}"

========= ACTION ህጎች =========
Admin group ላይ ሲጽፍ ትዕዛዙን ቀጥታ ፈጽም:
- "X ቁጥር ለ Y ስጥ / ያዝ" → book action
- "X ቁጥር ሰርዝ" → cancel
- "X ከፍሏል" → mark_paid
- "ሎተሪ message አዘምን" → action: refresh
- ሌላ ማንኛውም ትዕዛዝ → ፈጽም ወይም reply

ምሳሌ book_multiple: "86 31 21 ያዝ" →
{{"action":"book_multiple","bookings":[{{"number":86,"type":"full"}},{{"number":31,"type":"full"}},{{"number":21,"type":"full"}}],"name":"{user_name}","reply":"✅ ተያዘ!"}}

JSON ብቻ (markdown የለ):
{{"action":"book_full","number":6,"name":"...","reply":"..."}}
{{"action":"book_multiple","bookings":[{{"number":10,"type":"full"}},{{"number":21,"type":"half"}}],"name":"...","reply":"..."}}
{{"action":"cancel","number":6,"reply":"..."}}
{{"action":"mark_paid","number":6,"which":1,"reply":"..."}}
{{"action":"update_rule","rule":"...","reply":"✅ ተቀምጧል!"}}
{{"action":"reply","reply":"..."}}

JSON ብቻ:"""

    else:
        prompt = f"""አንተ የሎተሪ ስርዓት AI brain ነህ። Bot worker ነው የሚያስፈጽመው።

{admin_rules if admin_rules else ""}

========= የሎተሪ ህጎች =========
- 20 slots (1-20), እያንዳንዱ slot 5 ቁጥሮች (slot1=1-5, slot2=6-10, ... slot20=96-100)
- ሙሉ = 400ብር (አንድ ሰው), ግማሽ = 200ብር (ሁለት ሰዎች)
- ሽልማት: 1ኛ=5000ብር, 2ኛ=1000ብር, 3ኛ=400ብር
- ክፍያ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ 0952346729

========= ቁጥር መያዝ ምልክቶች =========
ሙሉ (default): "06", "36ሙሉ", "36 full"
ግማሽ: "21+", "21ግማሽ", "21half", "21 200"
ብዙ ቁጥር: "10 16 21ግማሽ" → 10=ሙሉ, 16=ሙሉ, 21=ግማሽ

ምሳሌ book_multiple:
"86 31 21 ያዝ" → {{"action":"book_multiple","bookings":[{{"number":86,"type":"full"}},{{"number":31,"type":"full"}},{{"number":21,"type":"full"}}],"name":"{user_name}","reply":"እሺ ቤተሰብ ✅"}}

========= BOOKING KEYWORDS =========
"yaz","ያዝ","book","hold","ale","አለ","alew","አለው","register",
"ያዝልኝ","ያዝልን","እያዝኩ","ምዝገባ","እፈልጋለሁ","እፈልጋለን","give me",
"wanna","want","need","gimme","take","እወስዳለሁ","እወስዳለን"

CRITICAL RULE: ቁጥር + ማንኛውም ቃል = ቀጥታ book። አትጠይቅ።

========= የአሁን ሎተሪ ሁኔታ =========
{full_state}

========= ተጠቃሚ =========
User ID: {user_id}
User Name: {user_name}
መልእክት: "{user_message}"

========= ACTION ህጎች =========
1. ቁጥር ሲጽፍ → ቀጥታ book (admin rules style ተጠቀም)
2. ብዙ ቁጥሮች → book_multiple (ሁሉንም ያዝ)
3. የተያዘ slot → "ተቀድመሃል ቤተሰብ 🙏"
4. ቀድሞ የያዘ → "ይዥሄልሃለው ቤተሰብ 🙏"
5. ክፍያ ጥያቄ → "screenshot ወይም SMS forward ልካልን ✅"
6. mark_paid → admin ብቻ

JSON ብቻ (markdown የለ):
{{"action":"book_full","number":6,"name":"...","reply":"..."}}
{{"action":"book_half_p1","number":21,"name":"...","reply":"..."}}
{{"action":"book_half_p2","number":21,"name":"...","reply":"..."}}
{{"action":"book_multiple","bookings":[{{"number":10,"type":"full"}},{{"number":21,"type":"half"}}],"name":"...","reply":"..."}}
{{"action":"cancel","number":6,"reply":"..."}}
{{"action":"cancel_and_rebook","cancel_number":6,"book_number":11,"book_type":"full","name":"...","reply":"..."}}
{{"action":"reply","reply":"..."}}
{{"action":"ask","reply":"..."}}

JSON ብቻ:"""

    raw = gemini_call(prompt, max_tokens=800, temperature=0.1)
    print(f"🧠 AI Brain raw: {raw}")
    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"❌ AI Brain parse error: {e}")
    return {"action": "reply", "reply": "❌ ጊዜያዊ ችግር አለ። ደግመህ ሞክር።"}

# ==================== TEACH MODE AI — ሙሉ ኃይሉን ይጠቀማል ====================

def ai_teach_brain(history: list, new_message: str, existing_rules: list) -> dict:
    """
    ልክ እንደ Claude/Gemini app — ሙሉ AI ኃይሉን ተጠቅሞ ያወያያል።
    ቀጥታ አይቀምጥም — ያስቀድሞ ይረዳል፣ ይጠይቃል፣ ተቃርኖ ካለ ያሳውቃል።
    """
    history_text = "\n".join(
        f"{'Admin' if m['role']=='user' else 'Bot'}: {m['content']}"
        for m in history
    )
    existing_rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(existing_rules)) if existing_rules else "ምንም የለም"

    prompt = f"""አንተ AI assistant ነህ — ሙሉ ኃይልህን ተጠቀም ልክ እንደ Gemini/Claude app።
Admin ከዚህ session ሊያስተምርህ ነው። 

======================================================
🧠 አስፈላጊ መርሆዎች — እነዚህ ሁሌ ይሰሩ:
======================================================

1. ቀጥታ አትቀበልና አትቀምጥ — UNDERSTAND ካረግህ በኋላ ብቻ save
2. Admin ሲናገር → ምን INTENT አለበት? ምን ለማለት ፈለገ?
3. ያልገባህ ካለ → ምሳሌ ጠይቅ፣ አሳጥረህ ጠይቅ
4. ያለው rules ጋር CONFLICT አለ? → ግልጽ አድርጎ ጠይቅ፣ ምን ያረጋል?
5. ህግ ከመቀመጡ በፊት → ለAdmin ተረጋጋጥ (summarize ና confirm)
6. Conflict ሲኖር → ቀደምቱ rule ትክክል ወይም አዲሱ? ጠይቅ
7. "ok/አዎ/awo" ካለ → confirmed, save
8. "አይ/no" ካለ → clarify ጠይቅ

======================================================
🚦 CONFLICT DETECTION — ዋናው ሥራ:
======================================================
አዲስ ህግ ሲሰጥህ ያሉ rules ሁሉ scan አድርግ:
- ተቃርኖ አለ? → "ቀደም ሲል '{ቀደምቱ ህግ}' ብለህ ነበር። አሁን '{አዲሱ}' ትላለህ — የቱ ትክክል?" ብለህ ጠይቅ
- ወደፊት ሊደናቀፍ የሚችል? → ተናገር
- Overlap አለ? → አዋህዶ ጻፍ

======================================================
📋 አሁን ያሉ ህጎች:
======================================================
{existing_rules_text}

======================================================
💬 ውይይት ታሪክ:
======================================================
{history_text}
Admin: "{new_message}"

======================================================
📝 Response style:
======================================================
- አማርኛ — casual, ሰው-like, ወዳጃዊ
- ህግ ሲቀበል → ዓረፍተ ነገሩን በራስህ ቃል summarize ና ጠይቅ
- አጭር ወይም ረዥም — ለ context
- Conflict ካለ → ቀጥታ ተናገር: "ትንሽ ግር ብሎኛል — ቀደም..."
- ሳይገባህ → "ምሳሌ ስጠኝ ደግሞ ሰምቻለሁ"

======================================================
🔑 STATUS definitions:
======================================================
- "discussing"  → ህግ አይደለም፣ ጨዋታ/ወይይት ብቻ
- "clarifying"  → ያልገባ አለ — ይጠይቃል
- "conflict"    → ተቃርኖ አለ — admin ይምረጥ
- "confirming"  → ህጉ ገብቶታል — ያረጋግጣል (ยัง save አይደለም)
- "saved"       → admin confirmed → save
- "done"        → ጨርሰናል

IMPORTANT rules array:
- "confirming" ላይ → rules = [ሊቀመጠው ህግ] (ብዙ detail ጋር)
- "saved" ላይ → rules = [የተረጋገጠው ህግ]
- "conflict" ላይ → deleted_rules = [ያሮጌው conflicting rule text ትክክለኛ] 
- ሌሎች → rules=[], deleted_rules=[]

JSON ብቻ (markdown የለ):
{{"status":"discussing","deleted_rules":[],"rules":[],"reply":"..."}}
{{"status":"clarifying","deleted_rules":[],"rules":[],"reply":"..."}}
{{"status":"conflict","deleted_rules":["ያሮጌው ህግ exact text"],"rules":[],"reply":"ቀደም ሲል X ብለህ ነበር፣ አሁን Y ትላለህ — የቱ ትክክል?"}}
{{"status":"confirming","deleted_rules":[],"rules":["ሊቀመጠው ህግ ሙሉ detail ጋር"],"reply":"ትክክል ነው? ✅ ካልህ ይቀመጣል"}}
{{"status":"saved","deleted_rules":["ያሮጌ conflict"],"rules":["አዲሱ ህግ"],"reply":"✅ ተቀምጧል!"}}
{{"status":"done","deleted_rules":[],"rules":[],"reply":"✅ ሁሉም ተቀምጧል! ሌላ?"}}

JSON ብቻ:"""

    raw = gemini_call(prompt, max_tokens=1000, temperature=0.3)
    print(f"🎓 Teach AI raw: {raw}")
    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"❌ Teach AI parse error: {e}")
    return {"status": "discussing", "deleted_rules": [], "rules": [], "reply": "ገባኝ! ሌሎ?"}

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

    elif action == "update_rule":
        rule = action_data.get("rule", "")
        if rule:
            save_admin_rule(rule)
            print(f"📚 Group rule saved: {rule}")

    return {"data": data, "reply": reply, "changed": changed}

# ==================== ADMIN TEACH MODE ====================
admin_teach_sessions: dict = {}

async def teach_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return

    user_id = update.effective_user.id

    if not context.args:
        # ========== /mkr ብቻ → teach mode ጀምር ==========
        admin_teach_sessions[user_id] = {
            "active": True,
            "history": [],
            "pending_rules": [],       # confirmed ግን ยัง saved አይደሉ
            "pending_deletes": [],     # conflict ተለይቶ delete ሊሆኑ
        }
        existing_rules = load_admin_rules()
        rules_preview = ""
        if existing_rules:
            shown = existing_rules[-5:]
            rules_preview = f"\n\n📌 አሁን ያሉ ህጎች ({len(existing_rules)}):\n" + "\n".join(f"• {r}" for r in shown)
            if len(existing_rules) > 5:
                rules_preview += f"\n... እና {len(existing_rules)-5} ተጨማሪ (/mkr list ለማየት)"
        await update.message.reply_text(
            f"📚 ዝግጁ ነኝ! አወራኝ 🤝\n"
            f"ሁሉንም ነገር ልትነግረኝ ትችላለህ — reply style, አዲስ features, behavior...\n"
            f"ሳይገባኝ አልቀምጥም — እጠይቃለሁ 😊\n"
            f"(ስትጨርስ \"ጨረስኩ\" በል){rules_preview}"
        )
        return

    # ========== /mkr list ==========
    if context.args[0] == "list":
        rules = load_admin_rules()
        if not rules:
            await update.message.reply_text("📚 እስካሁን ምንም ህግ አልተመዘገበም።")
        else:
            text = "📚 የተመዘገቡ ህጎች:\n\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules))
            await update.message.reply_text(text)
        return

    # ========== /mkr tokens ==========
    if context.args[0] == "tokens":
        await update.message.reply_text(build_token_report())
        return

    # ========== /mkr reset ==========
    if context.args[0] == "reset":
        if len(context.args) == 1 or context.args[1].lower() == "all":
            delete_all_admin_rules()
            await update.message.reply_text("🗑️ ሁሉም ህጎች ተሰርዘዋል።")
            return
        try:
            indices = [int(x) for x in context.args[1:]]
        except ValueError:
            await update.message.reply_text("❌ ቁጥሮች ብቻ ጻፍ። ምሳሌ: /mkr reset 1 3 5")
            return
        rules = load_admin_rules()
        to_delete = []
        for idx in indices:
            if 1 <= idx <= len(rules):
                to_delete.append(rules[idx - 1])
            else:
                await update.message.reply_text(f"❌ {idx} የለም። /mkr list ተመልከት።")
                return
        try:
            conn = get_db()
            cur  = conn.cursor()
            for rule in to_delete:
                cur.execute("DELETE FROM admin_rules WHERE rule=%s", (rule,))
            conn.commit()
            cur.close()
            conn.close()
            deleted = "\n".join(f"• {r}" for r in to_delete)
            await update.message.reply_text(f"🗑️ ተሰርዘዋል:\n{deleted}")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        return

    # ========== /mkr <direct rule> ==========
    rule = " ".join(context.args)
    save_admin_rule(rule)
    await update.message.reply_text(f"✅ ተማርኩ!\n📌 \"{rule}\"")

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
    if update.effective_chat.type != "private":
        return
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

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    user_id   = update.effective_user.id
    user_name = update.effective_user.first_name or "ተጠቃሚ"
    is_private = update.effective_chat.type == "private"

    # ========== TEACH MODE PHOTO ==========
    if is_private and user_id == ADMIN_TELEGRAM_ID and user_id in admin_teach_sessions and admin_teach_sessions[user_id]["active"]:
        try:
            photo     = update.message.photo[-1]
            file      = await context.bot.get_file(photo.file_id)
            img_bytes = await file.download_as_bytearray()

            session        = admin_teach_sessions[user_id]
            existing_rules = load_admin_rules()
            history_text   = "\n".join(
                f"{'Admin' if m['role']=='user' else 'Bot'}: {m['content']}"
                for m in session["history"]
            )
            existing_rules_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(existing_rules)) if existing_rules else "ምንም የለም"

            key    = get_next_gemini_key()
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=[
                    types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg"),
                    f"""አንተ AI assistant ነህ — ሙሉ ኃይልህን ተጠቀም። Admin photo ልኮልሃል።

ፎቶውን ተመልከትና ምን እንደሚያሳይ ተናገር።
ህግ ካለ → rules ውስጥ አስቀምጥ — ህግ ካልሆነ rules=[]።

ያሉ ህጎች:
{existing_rules_text}

ውይይት:
{history_text}

JSON ብቻ:
{{"status":"discussing","deleted_rules":[],"rules":[],"reply":"..."}}
{{"status":"confirming","deleted_rules":[],"rules":["ህጉ"],"reply":"ትክክል ነው?"}}

JSON ብቻ:"""
                ],
                config=types.GenerateContentConfig(max_output_tokens=600, temperature=0.3)
            )
            raw   = response.text.strip()
            clean = re.sub(r'```(?:json)?', '', raw).strip()
            match = re.search(r'\{.*?\}', clean, re.DOTALL)
            result = json.loads(match.group()) if match else {"status": "discussing", "rules": [], "deleted_rules": [], "reply": raw}

            reply        = result.get("reply", "ፎቶ ደረሰኝ 👍")
            new_rules    = result.get("rules", [])
            del_rules    = result.get("deleted_rules", [])
            status       = result.get("status", "discussing")

            session["history"].append({"role": "user", "content": "[photo]"})
            session["history"].append({"role": "assistant", "content": reply})

            # Only save on "saved" status
            if status == "saved":
                for r in del_rules:
                    if r:
                        delete_specific_rule(r)
                for r in new_rules:
                    if r:
                        save_admin_rule(r)
            elif status == "confirming":
                session["pending_rules"]  = new_rules
                session["pending_deletes"] = del_rules

            await update.message.reply_text(reply)
        except Exception as e:
            print(f"❌ teach photo error: {e}")
            await update.message.reply_text("❌ ፎቶ ማንበብ አልተቻለም።")
        return

    # ========== PAYMENT PHOTO ==========
    await update.message.reply_text("⏳ Screenshot እየተመረመረ ነው...")
    try:
        photo     = update.message.photo[-1]
        file      = await context.bot.get_file(photo.file_id)
        img_bytes = await file.download_as_bytearray()

        info = groq_extract_payment_from_image(bytes(img_bytes))
        print(f"📸 Photo payment info: {info}")

        if not info.get("ref"):
            await update.message.reply_text("❌ ከ screenshot ክፍያ መረጃ ማግኘት አልተቻለም። ግልጽ screenshot ይላኩ።")
            return

        ref    = info["ref"]
        amount = info.get("amount") or 0.0
        bank   = info.get("bank") or "UNKNOWN"

        if is_ref_used(ref):
            await update.message.reply_text(f"❌ ይህ ref ({ref}) አስቀድሞ ተጠቅሟል!")
            return

        data       = load_data()
        user_slots = get_slot_by_user(user_id, data)
        slot_num   = user_slots[0][1]["numbers"][0] if user_slots else None

        upsert_payment(ref, user_id, user_name, amount, bank, photo_ok=True, slot_number=slot_num)

        payment = get_payment_by_ref(ref)
        if payment and payment["photo_ok"] and payment["sms_ok"]:
            await handle_payment_match(ref, payment, context.bot, data)
        else:
            await update.message.reply_text(
                f"✅ Screenshot ተቀብዬአለሁ!\nRef: {ref} | {bank}\n⏳ SMS confirmation እየጠበቅን ነው..."
            )
    except Exception as e:
        print(f"❌ handle_photo error: {e}")
        await update.message.reply_text("❌ ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።")

async def handle_sms_webhook(sms_text: str, bot: Bot):
    print(f"📱 SMS received: {sms_text[:100]}")
    info   = groq_extract_payment_from_text(sms_text)
    print(f"📱 SMS payment info: {info}")
    refs   = info.get("refs") or []
    amount = info.get("amount") or 0.0
    bank   = info.get("bank") or "UNKNOWN"
    if not refs:
        print("❌ SMS: ref ማግኘት አልተቻለም")
        return
    matched_ref     = None
    matched_payment = None
    data            = load_data()
    for ref in refs:
        if is_ref_used(ref):
            continue
        existing  = get_payment_by_ref(ref)
        user_id   = existing["user_id"]     if existing else None
        user_name = existing["user_name"]   if existing else "Unknown"
        slot_num  = existing["slot_number"] if existing else None
        upsert_payment(ref, user_id or 0, user_name, amount, bank, sms_ok=True, slot_number=slot_num)
        payment = get_payment_by_ref(ref)
        if payment and payment["photo_ok"] and payment["sms_ok"]:
            matched_ref     = ref
            matched_payment = payment
            break
    if matched_ref and matched_payment:
        await handle_payment_match(matched_ref, matched_payment, bot, data)
    else:
        for ref in refs:
            existing = get_payment_by_ref(ref)
            if existing and existing.get("user_id"):
                await bot.send_message(
                    chat_id=existing["user_id"],
                    text="📱 SMS ተቀብዬአለሁ!\n⏳ Screenshot እስካልከ ድረስ እጠብቃለሁ።"
                )
                break

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text   = update.message.text.strip()
    user_id    = update.effective_user.id
    user_name  = update.effective_user.first_name or "ተጠቃሚ"
    is_private = update.effective_chat.type == "private"
    is_group   = update.effective_chat.type in ("group", "supergroup")

    # ==================== TEACH MODE (DM only) ====================
    if is_private and user_id == ADMIN_TELEGRAM_ID and user_id in admin_teach_sessions and admin_teach_sessions[user_id]["active"]:
        session = admin_teach_sessions[user_id]
        session["history"].append({"role": "user", "content": raw_text})
        existing_rules = load_admin_rules()

        result        = ai_teach_brain(session["history"], raw_text, existing_rules)
        reply         = result.get("reply", "ገባኝ!")
        status        = result.get("status", "discussing")
        new_rules     = result.get("rules", [])
        del_rules     = result.get("deleted_rules", [])

        session["history"].append({"role": "assistant", "content": reply})

        # ====== Status ላይ በመመርኮዝ ምን ይደረጋል ======
        if status == "confirming":
            # ህጉ ገብቷል ግን ยัง save አይደለም — pending ያድርግ
            session["pending_rules"]   = new_rules
            session["pending_deletes"] = del_rules
            print(f"📝 Pending rules: {new_rules}")

        elif status == "saved":
            # Admin confirmed (ok/አዎ/yes) → አሁን save
            # ከ pending ወይም ከ result — ሁለቱንም check
            to_save   = new_rules or session.get("pending_rules", [])
            to_delete = del_rules or session.get("pending_deletes", [])

            for r in to_delete:
                if r:
                    delete_specific_rule(r)
                    print(f"🗑️ Deleted conflicting rule: {r[:60]}")

            for r in to_save:
                if r:
                    save_admin_rule(r)
                    print(f"✅ Saved rule: {r[:60]}")

            session["pending_rules"]   = []
            session["pending_deletes"] = []

        elif status == "conflict":
            # Conflict ተለይቷል — pending delete ያድርግ
            session["pending_deletes"] = del_rules
            print(f"⚠️ Conflict detected: {del_rules}")

        elif status == "done":
            # Session አለቀ
            to_save   = new_rules or session.get("pending_rules", [])
            to_delete = del_rules or session.get("pending_deletes", [])
            for r in to_delete:
                if r:
                    delete_specific_rule(r)
            for r in to_save:
                if r:
                    save_admin_rule(r)
            admin_teach_sessions[user_id]["active"] = False
            print(f"📚 Teaching session done.")

        await update.message.reply_text(reply)
        return

    # ==================== GROUP: ADMIN = FULL POWER ====================
    if is_group and user_id == ADMIN_TELEGRAM_ID:
        data       = load_data()
        full_state = build_full_state_for_ai(data)
        print(f"👑 Admin in group: '{raw_text}'")

        action_data = ai_brain(raw_text, user_id, user_name, full_state, is_admin_in_group=True)
        print(f"🧠 Admin Group Action: {action_data}")

        if action_data.get("action") == "ask":
            await update.message.reply_text(action_data.get("reply", "❓"))
            return

        result = execute_action(action_data, user_id, data)
        if result["changed"]:
            save_data(result["data"])
            await update_lottery_message(context.bot, result["data"])
        if result["reply"]:
            await update.message.reply_text(result["reply"])
        return

    # ==================== REGULAR USER ====================
    data       = load_data()
    full_state = build_full_state_for_ai(data)
    print(f"📩 {user_name} ({user_id}): '{raw_text}'")

    action_data = ai_brain(raw_text, user_id, user_name, full_state, is_admin_in_group=False)
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

# ==================== SMS WEBHOOK SERVER ====================

class SMSWebhookHandler(BaseHTTPRequestHandler):
    bot_instance = None

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_POST(self):
        try:
            length   = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8", errors="ignore")
            print(f"📥 Webhook POST: {raw_body[:200]}")
            sms_text = raw_body
            try:
                parsed = json.loads(raw_body)
                sms_text = parsed.get("sms") or parsed.get("text") or parsed.get("message") or raw_body
            except Exception:
                pass
            if sms_text and SMSWebhookHandler.bot_instance:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    handle_sms_webhook(sms_text, SMSWebhookHandler.bot_instance),
                    loop=asyncio.get_event_loop()
                )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print(f"❌ Webhook error: {e}")
            self.send_response(500)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server(bot_instance):
    SMSWebhookHandler.bot_instance = bot_instance
    port = int(os.getenv("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SMSWebhookHandler).serve_forever()

# ==================== MAIN ====================

def main():
    if not GEMINI_KEYS:
        print("❌ ምንም Gemini API key አልተገኘም!")
        return

    init_db()

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
    app.add_handler(CommandHandler("mkr",           teach_cmd))
    app.add_handler(CommandHandler("805",           teach_cmd))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    thread = threading.Thread(target=run_server, args=(app.bot,))
    thread.daemon = True
    thread.start()

    print(f"✅ {len(GEMINI_KEYS)} Gemini API keys loaded")
    print(f"✅ Groq API: {'✅' if GROQ_API_KEY else '❌ Missing'}")
    print("✅ Bot እየሰራ ነው... (Neon DB + Gemini AI + Groq Vision + SMS Webhook)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
