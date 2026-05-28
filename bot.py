import os
import re
import json
import base64
import threading
import time
import requests
import psutil
import psycopg2
import psycopg2.extras
from psycopg2 import pool
from datetime import datetime
from flask import Flask, request as flask_request, jsonify
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# ==================== DATABASE ====================

db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    ref TEXT UNIQUE NOT NULL,
                    amount FLOAT DEFAULT 0,
                    sender TEXT,
                    used BOOLEAN DEFAULT FALSE,
                    used_by BIGINT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_payments (
                    user_id BIGINT PRIMARY KEY,
                    slot_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    booking_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_screenshots (
                    ref TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    slot_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    booking_type TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lottery_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_last_numbers (
                    user_id BIGINT PRIMARY KEY,
                    numbers TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS resource_usage (
                    id SERIAL PRIMARY KEY,
                    day DATE NOT NULL DEFAULT CURRENT_DATE,
                    gemini_calls INTEGER DEFAULT 0,
                    groq_calls INTEGER DEFAULT 0,
                    db_queries INTEGER DEFAULT 0,
                    messages_handled INTEGER DEFAULT 0,
                    photos_handled INTEGER DEFAULT 0,
                    sms_received INTEGER DEFAULT 0,
                    auto_approved INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    UNIQUE(day)
                )
            """)
        conn.commit()
        print("✅ Database tables ready")
    finally:
        release_conn(conn)

# ==================== RESOURCE TRACKING ====================

def increment_counter(column: str, amount: int = 1):
    def _inc():
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO resource_usage (day, {column})
                    VALUES (CURRENT_DATE, %s)
                    ON CONFLICT (day) DO UPDATE
                    SET {column} = resource_usage.{column} + %s
                """, (amount, amount))
            conn.commit()
        except Exception as e:
            print(f"❌ Counter error ({column}): {e}")
        finally:
            release_conn(conn)
    threading.Thread(target=_inc, daemon=True).start()

def get_usage_last_5_days() -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    day, gemini_calls, groq_calls, db_queries,
                    messages_handled, photos_handled,
                    sms_received, auto_approved, errors
                FROM resource_usage
                ORDER BY day DESC
                LIMIT 5
            """)
            return cur.fetchall()
    finally:
        release_conn(conn)

def get_system_metrics() -> dict:
    try:
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / 1024 / 1024
        cpu_pct = process.cpu_percent(interval=0.1)
        sys_ram = psutil.virtual_memory()
        return {
            "bot_ram_mb": round(ram_mb, 1),
            "bot_cpu_pct": round(cpu_pct, 1),
            "sys_ram_pct": round(sys_ram.percent, 1),
            "sys_ram_used": round(sys_ram.used / 1024 / 1024, 1),
            "sys_ram_total": round(sys_ram.total / 1024 / 1024, 1),
        }
    except Exception as e:
        print(f"❌ Metrics error: {e}")
        return {}

def build_progress_bar(percent: float, width: int = 10) -> str:
    filled = round((percent / 100) * width)
    bar = "█" * filled + "░" * (width - filled)
    emoji = "🟢" if percent < 60 else "🟡" if percent < 85 else "🔴"
    return f"{emoji} [{bar}] {percent}%"

def build_804_report() -> str:
    rows = get_usage_last_5_days()
    metrics = get_system_metrics()

    lines = ["📊 *Bot Resource Report*", ""]

    if metrics:
        lines.append("🖥 *System — አሁን*")
        lines.append(f"  Bot RAM:  `{metrics.get('bot_ram_mb')} MB`")
        lines.append(f"  Bot CPU:  `{metrics.get('bot_cpu_pct')}%`")
        lines.append(f"  Sys RAM:  `{metrics.get('sys_ram_used')} / {metrics.get('sys_ram_total')} MB ({metrics.get('sys_ram_pct')}%)`")
        lines.append("")

    if rows:
        today = rows[0]
        groq_today = today[2] or 0
        lines.append("⚡ *Groq API — ዛሬ*")
        lines.append(f"  ጥቅም: `{groq_today}` calls")
        lines.append("")

    lines.append("📅 *የ 5 ቀን Usage*")
    lines.append("```")
    lines.append(f"{'ቀን':<10} {'Groq':>5} {'Msg':>5} {'📷':>4} {'SMS':>4} {'✅':>4} {'❌':>4}")
    lines.append("─" * 42)

    for row in rows:
        day, gemini, groq, db_q, msgs, photos, sms, auto_app, errors = row
        day_str = day.strftime("%m/%d") if hasattr(day, "strftime") else str(day)
        lines.append(
            f"{day_str:<10} {groq or 0:>5} "
            f"{msgs or 0:>5} {photos or 0:>4} {sms or 0:>4} "
            f"{auto_app or 0:>4} {errors or 0:>4}"
        )

    if not rows:
        lines.append("  (ምንም data የለም ገና)")

    lines.append("```")
    lines.append("")
    lines.append("_Groq=Groq calls | Msg=Messages | 📷=Photos_")
    lines.append("_SMS=SMS | ✅=AutoApproved | ❌=Errors_")
    lines.append("")
    lines.append(f"_🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

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

# ==================== PAYMENTS DATA (PostgreSQL) ====================

def save_verified_payment(ref: str, amount: float, sender: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO payments (ref, amount, sender)
                VALUES (%s, %s, %s)
                ON CONFLICT (ref) DO NOTHING
            """, (ref, amount, sender))
            inserted = cur.rowcount > 0
        conn.commit()
        increment_counter("db_queries")
        if inserted:
            print(f"✅ Payment saved: ref={ref}, amount={amount}")
        return inserted
    finally:
        release_conn(conn)

def find_verified_payment(ref: str) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM payments WHERE ref = %s", (ref,))
            row = cur.fetchone()
            increment_counter("db_queries")
            return dict(row) if row else None
    finally:
        release_conn(conn)

def mark_payment_used(ref: str, user_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE payments SET used = TRUE, used_by = %s WHERE ref = %s
            """, (user_id, ref))
        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

def save_pending_payment(user_id: int, slot_id: str, amount: int, booking_type: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_payments (user_id, slot_id, amount, booking_type)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    slot_id = EXCLUDED.slot_id,
                    amount = EXCLUDED.amount,
                    booking_type = EXCLUDED.booking_type,
                    created_at = NOW()
            """, (user_id, slot_id, amount, booking_type))
        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

def get_pending_payment(user_id: int) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pending_payments WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            increment_counter("db_queries")
            return dict(row) if row else None
    finally:
        release_conn(conn)

def clear_pending_payment(user_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_payments WHERE user_id = %s", (user_id,))
        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

def save_pending_screenshot(ref: str, user_id: int, slot_id: str, amount: int, booking_type: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pending_screenshots (ref, user_id, slot_id, amount, booking_type)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (ref) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    slot_id = EXCLUDED.slot_id,
                    amount = EXCLUDED.amount,
                    booking_type = EXCLUDED.booking_type,
                    created_at = NOW()
            """, (ref, user_id, slot_id, amount, booking_type))
        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

def get_pending_screenshot(ref: str) -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM pending_screenshots
                WHERE ref = %s AND created_at > NOW() - INTERVAL '24 hours'
            """, (ref,))
            row = cur.fetchone()
            increment_counter("db_queries")
            return dict(row) if row else None
    finally:
        release_conn(conn)

def clear_pending_screenshot(ref: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_screenshots WHERE ref = %s", (ref,))
        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

# ==================== DATA MANAGEMENT ====================

def make_empty_slot(i: int) -> dict:
    start = (i - 1) * 5 + 1
    return {
        "numbers": list(range(start, start + 5)),
        "type": None,
        "p1_id": None, "p1_name": None, "p1_paid": False,
        "p2_id": None, "p2_name": None, "p2_paid": False,
    }

def load_data() -> dict:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT key, value FROM lottery_state")
            rows = {r["key"]: r["value"] for r in cur.fetchall()}

            slots_json = rows.get("slots")
            if slots_json:
                slots = json.loads(slots_json)
            else:
                slots = {str(i): make_empty_slot(i) for i in range(1, 21)}

            cur.execute("SELECT user_id, numbers FROM user_last_numbers")
            last_numbers = {str(r["user_id"]): json.loads(r["numbers"]) for r in cur.fetchall()}

            increment_counter("db_queries")
            return {
                "slots": slots,
                "lottery_message_id": int(rows["lottery_message_id"]) if rows.get("lottery_message_id") else None,
                "chat_id": int(rows["chat_id"]) if rows.get("chat_id") else None,
                "last_numbers_per_user": last_numbers
            }
    finally:
        release_conn(conn)

def save_data(data: dict):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO lottery_state (key, value) VALUES ('slots', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (json.dumps(data["slots"], ensure_ascii=False),))

            if data.get("lottery_message_id"):
                cur.execute("""
                    INSERT INTO lottery_state (key, value) VALUES ('lottery_message_id', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (str(data["lottery_message_id"]),))

            if data.get("chat_id"):
                cur.execute("""
                    INSERT INTO lottery_state (key, value) VALUES ('chat_id', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """, (str(data["chat_id"]),))

            for uid, nums in data.get("last_numbers_per_user", {}).items():
                cur.execute("""
                    INSERT INTO user_last_numbers (user_id, numbers) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET numbers = EXCLUDED.numbers
                """, (int(uid), json.dumps(nums)))

        conn.commit()
        increment_counter("db_queries")
    finally:
        release_conn(conn)

def is_slot_free(slot: dict) -> bool:
    return slot["type"] is None

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

# ==================== CBE PAYMENT HELPERS ====================

def extract_cbe_link(text: str) -> str:
    match = re.search(r'https?://[Mm]breciept\.cbe\.com\.et/\S+', text)
    return match.group(0).strip() if match else None

def extract_amount_from_sms(text: str) -> float:
    match = re.search(r'ETB\s*([\d,]+\.?\d*)', text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None

def extract_sender_from_sms(text: str) -> str:
    match = re.search(r'from\s+account\s+\S+\s+\(([^)]+)\)', text, re.IGNORECASE)
    return match.group(1).strip() if match else None

def fetch_ref_from_cbe_link(link: str) -> str:
    try:
        print(f"🔗 Fetching CBE link: {link}")
        resp = requests.get(
            link,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"},
            stream=True
        )
        content_type = resp.headers.get("content-type", "image/jpeg")
        image_data = resp.content
        print(f"📄 Content-Type: {content_type}, Size: {len(image_data)} bytes")

        ref = extract_ref_with_groq(image_data, content_type.split(";")[0].strip())
        return ref
    except Exception as e:
        print(f"❌ CBE link fetch error: {e}")
        increment_counter("errors")
        return None

def extract_ref_with_groq(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": """ይህ CBE (Commercial Bank of Ethiopia) receipt ነው።
Reference No. ወይም VAT Receipt No. ያለውን code ብቻ አውጣ።
FT የሚጀምር code ነው። ምሳሌ: FT26147TDW1K
የ reference code ብቻ ፃፍ። ምንም ሌላ ቃል አትጨምር።"""
                            }
                        ]
                    }
                ],
                "max_tokens": 50
            },
            timeout=20
        )
        result = response.json()
        text = result["choices"][0]["message"]["content"].strip()
        print(f"🔍 Groq extracted: {text}")
        increment_counter("groq_calls")
        match = re.search(r'[A-Z]{2}[A-Z0-9]{6,15}', text)
        return match.group(0) if match else None
    except Exception as e:
        print(f"❌ Groq Vision error: {e}")
        increment_counter("errors")
        return None

# ==================== GROQ AI BRAIN ====================

def groq_call(prompt: str, max_tokens: int = 500, temperature: float = 0.1) -> str:
    """Groq API call — ፈጣን፣ ነፃ፣ አማርኛ ይችላል"""
    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a JSON-only responder. You MUST respond with valid JSON only. No markdown, no backticks, no explanation. Only a raw JSON object."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "response_format": {"type": "json_object"}  # JSON mode — ስህተት ዜሮ!
            },
            timeout=20
        )
        result = response.json()

        if response.status_code != 200:
            print(f"❌ Groq error {response.status_code}: {result}")
            increment_counter("errors")
            return ""

        text = result["choices"][0]["message"]["content"].strip()
        print(f"⚡ Groq response: {text[:100]}")
        increment_counter("groq_calls")
        return text

    except Exception as e:
        print(f"❌ Groq call error: {e}")
        increment_counter("errors")
        return ""


def build_context_info(data: dict) -> str:
    filled = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
    free_slots = [s for s in data["slots"].values() if s["type"] is None]
    half_open  = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]

    lines = [
        f"አጠቃላይ: {filled}/20 slots ሞልቷል",
        f"ነፃ slots: {len(free_slots)} | ግማሽ ክፍት: {len(half_open)}",
        f"ነፃ ቁጥሮች ብዛት: {len(free_slots) * 5}",
        "",
        "=== እያንዳንዱ slot ሁኔታ ===",
    ]
    for slot_id, slot in data["slots"].items():
        nums = f"{slot['numbers'][0]}-{slot['numbers'][-1]}"
        if slot["type"] is None:
            lines.append(f"slot{slot_id} [{nums}]: ነፃ ✅")
        elif slot["type"] == "full":
            paid = "✅ ከፍሏል" if slot["p1_paid"] else "⏳ ያልከፈለ"
            lines.append(f"slot{slot_id} [{nums}]: ሙሉ — {slot['p1_name']} ({paid})")
        elif slot["type"] == "half":
            p1 = f"{slot['p1_name']} ({'✅' if slot['p1_paid'] else '⏳'})"
            if slot["p2_id"] is None:
                lines.append(f"slot{slot_id} [{nums}]: ግማሽ — {p1} | ሁለተኛ ሰው ክፍት 🟡")
            else:
                p2 = f"{slot['p2_name']} ({'✅' if slot['p2_paid'] else '⏳'})"
                lines.append(f"slot{slot_id} [{nums}]: ግማሽ ሙሉ — {p1} + {p2}")
    if filled < 20:
        lines.append("\n⚠️ ሎተሪ ገና አልሞላም — ነፃ slots አሉ!")
    else:
        lines.append("\n✅ ሁሉም slots ተሞልቷል!")
    return "\n".join(lines)


def ai_brain(raw_text: str, sender_first_name: str, context_info: str,
             last_numbers: list = None, free_numbers: list = None) -> dict:

    prompt = f"""አንተ ልምድ ያለው የሎተሪ ረዳት ነህ። JSON ONLY። ምንም ሌላ text አትጻፍ።

══════════════════════════════
🎰 ጨዋታው
══════════════════════════════
- 20 slots አሉ። እያንዳንዱ slot 5 ቁጥሮች (1-5, 6-10, ... 96-100)
- ሙሉ slot = 1 ሰው 400ብር፣ 5 ቁጥሮች ሙሉ
- ግማሽ slot = 2 ሰው ይካፈላሉ፣ እያንዳንዱ 200ብር
- ሽልማቶች: 1ኛ=5000ብር | 2ኛ=1000ብር | 3ኛ=400ብር
- ክፍያ: CBE 1000641057146 | አዋሽ 01335630641400 | ዳሽን 5389857825011 | ቴሌ ብር 0952346729

══════════════════════════════
🌐 Latin አማርኛ slang — ሁሉ ተረዳ
══════════════════════════════
yaz/yazlgn = ያዝልኝ | srez/sirez = ሰርዝ | gmash/grmash/begmash = ግማሽ
nefta ale = ነፃ አለ? | sint bir = ስንት ብር? | yemeta = የቀረ?
tekayelgn = ተካልኝ | endet = እንዴት | hulum = ሁሉም | kefelku = ከፈልኩ

══════════════════════════════
⚡ Actions
══════════════════════════════
1. book — ቁጥር ሲያዝ
   - ቁጥር ብቻ → is_half=false
   - ቁጥር+ ወይም gmash/200/ግማሽ → is_half=true
   - "yazlgn/ያዝልኝ/አዎ/እሺ" ቁጥር ሳይኖር → last_numbers ተጠቀም: {last_numbers or []}
   - "hulum/ቀሪውን ያዝልኝ" → free_numbers ሁሉ: {free_numbers or []}
   - ሌላ ሰው ስም ካለ → name field ሙላ

2. cancel — ሲሰርዝ
   - "ሰርዝ/srez/cancel" + ቁጥር

3. change_type — slot አይነት መቀየር
   - "X ወደ ግማሽ ቀይር" → new_type="half"
   - "X ወደ ሙሉ ቀይር" → new_type="full"

4. swap — ሰርዞ ሌላ ሲያዝ
   - "X ሰርዘህ Y ያዝልኝ" / "X ተካልኝ Y"

══════════════════════════════
💬 ጥያቄ ሲሆን — reply field ሙላ
══════════════════════════════
"ቁጥሮች አሉ?" → context_info ውስጥ ነፃ ቁጥሮች ብዛት ተጠቀም
"ዋጋ ስንት?" → "ሙሉ=400ብር | ግማሽ=200ብር"
"ሽልማት ስንት?" → "1ኛ=5000ብር 🥇 | 2ኛ=1000ብር 🥈 | 3ኛ=400ብር 🥉"
"ሰላም/hi/hello" → ሙቅ አቀባበል + ነፃ slots ብዛት ጠቅስ
"እንዴት?" → አጭር ማብራሪያ: ቁጥር ምረጥ → ክፈል → screenshot ላክ → ዕጣ ጠብቅ
reply ሁልጊዜ አማርኛ ብቻ። ቢበዛ 2 ዓረፍተነገር።

══════════════════════════════
📊 አሁናዊ ሁኔታ
══════════════════════════════
{context_info}

══════════════════════════════
👤 ላኪ
══════════════════════════════
ስም: {sender_first_name}
መልእክት: "{raw_text}"

══════════════════════════════
📋 JSON format — ይህን ብቻ ተጠቀም
══════════════════════════════
Action ሲሆን:
{{"actions": [{{"intent": "book", "number": 21, "is_half": false, "name": null}}], "valid": true, "reply": null}}

swap ሲሆን:
{{"actions": [{{"intent": "swap", "cancel_number": 41, "book_numbers": [31], "is_half": false, "name": null}}], "valid": true, "reply": null}}

change_type ሲሆን:
{{"actions": [{{"intent": "change_type", "number": 21, "new_type": "half"}}], "valid": true, "reply": null}}

ጥያቄ/ማብራሪያ ሲሆን:
{{"actions": [], "valid": false, "reply": "አጭር አማርኛ መልስ"}}

CRITICAL: JSON ብቻ። valid=true ሲሆን actions ባዶ መሆን የለበትም።"""

    result = groq_call(prompt, max_tokens=500, temperature=0.1)
    print(f"🧠 AI brain raw: {result}")

    if not result:
        # Groq ከሳተ → fallback
        nums = extract_numbers_from_text(raw_text)
        is_half = detect_half_booking(raw_text)
        if nums:
            return {
                "actions": [{"intent": "book", "number": n, "is_half": is_half, "name": None} for n, h in nums],
                "valid": True,
                "reply": None
            }
        return {"actions": [], "valid": False, "reply": "❓ ቁጥር ፃፍ ወይም ጥያቄ ጠይቅ።"}

    try:
        # response_format json_object ስለተጠቀምን clean ማድረግ አያስፈልግም
        # ነገር ግን safety net አለ
        clean = re.sub(r'```(?:json)?', '', result).strip().rstrip('`').strip()
        match = re.search(r'\{.*\}', clean, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return parsed
    except Exception as e:
        print(f"❌ ai_brain parse error: {e}")
        increment_counter("errors")

    nums = extract_numbers_from_text(raw_text)
    is_half = detect_half_booking(raw_text)
    if nums:
        return {
            "actions": [{"intent": "book", "number": n, "is_half": is_half, "name": None} for n, h in nums],
            "valid": True,
            "reply": None
        }
    return {"actions": [], "valid": False, "reply": "❓ ቁጥር ፃፍ ወይም ጥያቄ ጠይቅ።"}


def extract_numbers_from_text(text: str) -> list:
    matches = re.finditer(r'(?<!\d)(\d{1,3})(\+?)(?!\d)', text)
    seen = set()
    result = []
    for m in matches:
        num = int(m.group(1))
        is_half = m.group(2) == '+'
        if 1 <= num <= 100 and num not in seen:
            seen.add(num)
            result.append((num, is_half))
    return result

def detect_half_booking(raw_text: str) -> bool:
    lower = raw_text.lower()
    half_keywords = ["ግማሽ", "half", "begmash", "grmash", "gmash", "200"]
    if any(k in lower for k in half_keywords):
        return True
    if re.search(r'\d+\s*\+', raw_text):
        return True
    return False

def has_text(raw_text: str) -> bool:
    cleaned = re.sub(r'[\d\s\+\&\,፣#\.\/\*\-]|ብር|birr', '', raw_text, flags=re.IGNORECASE).strip()
    return len(cleaned) > 0

# ==================== BOT HANDLERS ====================

async def start_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return
    data = load_data()
    data["slots"] = {str(i): make_empty_slot(i) for i in range(1, 21)}
    sent = await update.message.reply_text(build_full_message(data))
    data["lottery_message_id"] = sent.message_id
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    await update.message.reply_text("✅ ሎተሪ ጀምሯል!")

async def mark_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("አጠቃቀም:\n/paid <ቁጥር>\n/paid <ቁጥር> 2")
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

    data = load_data()
    slot_id, slot = get_slot_by_number(number, data)
    if not slot:
        await update.message.reply_text("❌ ቁጥር አልተገኘም")
        return
    if slot["type"] is None:
        await update.message.reply_text("❌ ይህ slot ገና አልተያዘም")
        return

    if which == 2:
        if slot["type"] != "half" or slot["p2_id"] is None:
            await update.message.reply_text("❌ ሁለተኛ ሰው የለም")
            return
        data["slots"][slot_id]["p2_paid"] = True
        name = slot["p2_name"]
    else:
        data["slots"][slot_id]["p1_paid"] = True
        name = slot["p1_name"]

    save_data(data)
    await update_lottery_message(context.bot, data)
    await update.message.reply_text(f"✅ {name} ክፍያ ተረጋግጧል!")

async def admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return

    await update.message.reply_text("⏳ Report እየተሰራ ነው...")

    try:
        report = build_804_report()
        await update.message.reply_text(report, parse_mode="Markdown")
    except Exception as e:
        print(f"❌ /804 report error: {e}")
        increment_counter("errors")
        await update.message.reply_text(f"❌ Report ሲሰራ ስህተት: {e}")

async def update_lottery_message(bot: Bot, data: dict):
    msg_id = data.get("lottery_message_id")
    chat_id = data.get("chat_id")
    if msg_id and chat_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=build_full_message(data)
            )
        except Exception as e:
            print(f"❌ Message update error: {e}")
            increment_counter("errors")
            try:
                sent = await bot.send_message(chat_id=chat_id, text=build_full_message(data))
                data["lottery_message_id"] = sent.message_id
                save_data(data)
            except Exception as e2:
                print(f"❌ Send new message error: {e2}")
                increment_counter("errors")

# ==================== PAYMENT: SCREENSHOT HANDLER ====================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    increment_counter("photos_handled")

    pending = get_pending_payment(user_id)
    if not pending:
        await update.message.reply_text("❓ መጀመሪያ ቁጥር ይያዙ፣ ከዚያ screenshot ይላኩ።")
        return

    await update.message.reply_text("⏳ ክፍያ እየተረጋገጠ ነው...")

    photos = update.message.photo
    largest = photos[-1]
    file = await context.bot.get_file(largest.file_id)
    image_bytes = await file.download_as_bytearray()

    ref = extract_ref_with_groq(bytes(image_bytes), "image/jpeg")

    if not ref:
        increment_counter("errors")
        await update.message.reply_text(
            "❌ Reference number ማንበብ አልተቻለም።\n"
            "CBE receipt screenshot ትክክለኛ መሆኑን ያረጋግጡ።"
        )
        return

    print(f"📋 Extracted ref: {ref}")

    existing = find_verified_payment(ref)
    if existing and existing.get("used"):
        await update.message.reply_text(f"⚠️ ይህ ክፍያ ቀድሞ ጥቅም ላይ ውሏል!\n\nRef: {ref}")
        return

    if existing and not existing.get("used"):
        paid_amount = existing.get("amount", 0)
        expected_amount = pending["amount"]

        if paid_amount and paid_amount < expected_amount:
            await update.message.reply_text(
                f"❌ ክፍያ አይሆንም!\n\n"
                f"💰 የተከፈለ: ETB {paid_amount}\n"
                f"💰 የሚፈለግ: ETB {expected_amount}\n\n"
                f"ትክክለኛ መጠን ይክፈሉ።"
            )
            return

        data = load_data()
        slot_id = pending["slot_id"]
        slot = data["slots"].get(slot_id)

        if not slot:
            await update.message.reply_text("❌ Slot አልተገኘም።")
            return

        if slot["p1_id"] == user_id:
            data["slots"][slot_id]["p1_paid"] = True
        elif slot["p2_id"] == user_id:
            data["slots"][slot_id]["p2_paid"] = True

        save_data(data)
        mark_payment_used(ref, user_id)
        clear_pending_payment(user_id)
        clear_pending_screenshot(ref)
        increment_counter("auto_approved")

        await update_lottery_message(context.bot, data)

        sender = existing.get("sender", "Unknown")
        await update.message.reply_text(
            f"✅ ክፍያ ተረጋግጧል!\n\n"
            f"📋 Ref: {ref}\n"
            f"💰 ETB {paid_amount}\n"
            f"👤 {sender}\n\n"
            f"🎰 መልካም ዕድል!"
        )

        data = load_data()
        if sum(1 for s in data["slots"].values() if is_slot_full_booked(s)) == 20:
            await context.bot.send_message(chat_id, "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")

    else:
        save_pending_screenshot(ref, user_id, pending["slot_id"], pending["amount"], pending["booking_type"])
        await update.message.reply_text(
            f"⏳ Screenshot ተቀብሏል!\n\n"
            f"📋 Ref: {ref}\n\n"
            f"Admin SMS ሲደርስ ክፍያዎ ይረጋገጣል።\n"
            f"እንደገና screenshot መላክ አያስፈልግም። ✅"
        )

# ==================== PAYMENT: ADMIN SMS HANDLER ====================

async def handle_admin_sms(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    increment_counter("sms_received")

    await update.message.reply_text("⏳ CBE receipt እየተረጋገጠ ነው...")

    link = extract_cbe_link(text)
    amount = extract_amount_from_sms(text)
    sender = extract_sender_from_sms(text)

    if not link:
        await update.message.reply_text("❌ CBE link አልተገኘም።")
        return

    ref = fetch_ref_from_cbe_link(link)

    if not ref:
        await update.message.reply_text(
            "❌ Ref number ማወጣት አልተቻለም።\n"
            "CBE receipt page ለጊዜው ተዘግቷል ይሆናል።"
        )
        return

    already = save_verified_payment(ref, amount or 0, sender or "Unknown")

    if not already:
        await update.message.reply_text(f"⚠️ ይህ payment ቀድሞ ተመዝግቧል!\nRef: {ref}")
        return

    pending_sc = get_pending_screenshot(ref)
    if pending_sc:
        try:
            data = load_data()
            slot_id = pending_sc["slot_id"]
            slot = data["slots"].get(slot_id)
            sc_user_id = pending_sc["user_id"]
            expected_amount = pending_sc["amount"]

            if slot and (not amount or amount >= expected_amount):
                if slot.get("p1_id") == sc_user_id:
                    data["slots"][slot_id]["p1_paid"] = True
                elif slot.get("p2_id") == sc_user_id:
                    data["slots"][slot_id]["p2_paid"] = True

                save_data(data)
                mark_payment_used(ref, sc_user_id)
                clear_pending_payment(sc_user_id)
                clear_pending_screenshot(ref)
                increment_counter("auto_approved")

                await update_lottery_message(context.bot, data)

                await context.bot.send_message(
                    chat_id=sc_user_id,
                    text=(
                        f"✅ ክፍያዎ ተረጋግጧል!\n\n"
                        f"📋 Ref: {ref}\n"
                        f"💰 ETB {amount}\n"
                        f"👤 {sender or 'Unknown'}\n\n"
                        f"🎰 መልካም ዕድል!"
                    )
                )

                await update.message.reply_text(
                    f"✅ Payment ተመዝግቦ auto-approved!\n\n"
                    f"📋 Ref: {ref}\n"
                    f"💰 Amount: ETB {amount}\n"
                    f"👤 Sender: {sender or 'Unknown'}\n"
                    f"🤖 Screenshot ቀደም ተልኮ ነበር — ተረጋግጧል!"
                )

                data = load_data()
                if sum(1 for s in data["slots"].values() if is_slot_full_booked(s)) == 20:
                    chat_id = data.get("chat_id")
                    if chat_id:
                        await context.bot.send_message(chat_id, "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")
                return

        except Exception as e:
            print(f"❌ Auto-approve error: {e}")
            increment_counter("errors")

    await update.message.reply_text(
        f"✅ Payment ተመዝግቧል!\n\n"
        f"📋 Ref: {ref}\n"
        f"💰 Amount: ETB {amount}\n"
        f"👤 Sender: {sender or 'Unknown'}"
    )

# ==================== MAIN MESSAGE HANDLER ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text.strip()
    sender_first_name = update.effective_user.first_name or "ተጠቃሚ"
    user_id = update.effective_user.id
    data = load_data()

    increment_counter("messages_handled")

    # ── ADMIN: CBE SMS forward ──
    if user_id == ADMIN_TELEGRAM_ID and "Mbreciept.cbe.com.et" in raw_text:
        await handle_admin_sms(update, context, raw_text)
        return

    context_info = build_context_info(data)
    number_list = extract_numbers_from_text(raw_text)

    user_id_str = str(user_id)
    last_numbers = data.get("last_numbers_per_user", {}).get(user_id_str, [])
    free_numbers = [s["numbers"][0] for s in data["slots"].values() if s["type"] is None]

    if number_list:
        data.setdefault("last_numbers_per_user", {})[user_id_str] = [n for n, h in number_list]
        save_data(data)

    if has_text(raw_text):
        brain = ai_brain(raw_text, sender_first_name, context_info, last_numbers, free_numbers)
        valid = brain.get("valid", False)
        reply = brain.get("reply", None)

        if not valid or not brain.get("actions"):
            await update.message.reply_text(reply or "❓ ቁጥር ፃፍ ወይም ጥያቄ ጠይቅ።")
            return

        actions = brain.get("actions", [])
    else:
        if not number_list:
            await update.message.reply_text("❓ ቁጥር ፃፍ። ለምሳሌ: 21 ወይም 21+")
            return
        actions = [{"intent": "book", "number": n, "is_half": h, "name": None} for n, h in number_list]

    # ==================== ACTIONS ====================
    booked_full   = []
    booked_half   = []
    half_joined   = []
    already_taken = []
    cancelled     = []
    not_yours     = []
    changed       = False

    last_booked_slot_id = None
    last_booked_amount  = 0
    last_booked_type    = "full"

    for action in actions:
        intent      = action.get("intent", "book")
        number      = action.get("number")
        is_half     = action.get("is_half", False)
        ai_name     = action.get("name")
        display_name = ai_name if ai_name else sender_first_name

        if not number:
            continue

        slot_id, slot = get_slot_by_number(number, data)
        if slot is None:
            continue

        if intent == "book":
            if is_half:
                if slot["type"] is None:
                    data["slots"][slot_id].update({
                        "type": "half",
                        "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                    booked_half.append(number)
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 200
                    last_booked_type    = "half"
                    changed = True
                elif slot["type"] == "half" and slot["p2_id"] is None and slot["p1_id"] != user_id:
                    data["slots"][slot_id].update({
                        "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                    })
                    half_joined.append(number)
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 200
                    last_booked_type    = "half"
                    changed = True
                elif slot["type"] == "half" and slot["p1_id"] == user_id:
                    await update.message.reply_text(f"⚠️ {number}# ቀድሞ ይዘሃል!")
                else:
                    already_taken.append(number)
            else:
                if slot["type"] is None:
                    data["slots"][slot_id].update({
                        "type": "full",
                        "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                    booked_full.append(number)
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 400
                    last_booked_type    = "full"
                    changed = True
                elif slot["type"] == "half" and slot["p2_id"] is None and slot["p1_id"] != user_id:
                    data["slots"][slot_id].update({
                        "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                    })
                    half_joined.append(number)
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 200
                    last_booked_type    = "half"
                    changed = True
                elif slot["p1_id"] == user_id or slot["p2_id"] == user_id:
                    await update.message.reply_text(f"🙏 {number}# ቀድሞ ይዘሃል!")
                else:
                    already_taken.append(number)

        elif intent == "cancel":
            if slot["type"] is None:
                not_yours.append(number)
            elif slot["p1_id"] == user_id:
                if slot["type"] == "half" and slot["p2_id"] is not None:
                    data["slots"][slot_id].update({
                        "p1_id": slot["p2_id"], "p1_name": slot["p2_name"],
                        "p1_paid": slot["p2_paid"],
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                else:
                    saved_numbers = slot["numbers"]
                    data["slots"][slot_id] = make_empty_slot(int(slot_id))
                    data["slots"][slot_id]["numbers"] = saved_numbers
                cancelled.append(number)
                clear_pending_payment(user_id)
                changed = True
            elif slot["type"] == "half" and slot["p2_id"] == user_id:
                data["slots"][slot_id].update({"p2_id": None, "p2_name": None, "p2_paid": False})
                cancelled.append(number)
                clear_pending_payment(user_id)
                changed = True
            else:
                not_yours.append(number)

        elif intent == "change_type":
            new_type = action.get("new_type", "half")
            if slot["type"] is None:
                await update.message.reply_text(f"❌ {number}# ገና አልተያዘም።")
            elif slot["p1_id"] != user_id and slot["p2_id"] != user_id:
                await update.message.reply_text(f"❌ {number}# የእርስዎ አይደለም።")
            elif slot["type"] == new_type:
                await update.message.reply_text(f"🙏 {number}# ቀድሞ {'ግማሽ' if new_type=='half' else 'ሙሉ'} ነው!")
            else:
                if new_type == "half":
                    data["slots"][slot_id].update({
                        "type": "half",
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                    await update.message.reply_text(f"✅ {number}# ወደ ግማሽ ተቀይሯል። ገቢ 200ብር 🙏")
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 200
                    last_booked_type    = "half"
                else:
                    data["slots"][slot_id].update({
                        "type": "full",
                        "p2_id": None, "p2_name": None, "p2_paid": False,
                    })
                    await update.message.reply_text(f"✅ {number}# ወደ ሙሉ ተቀይሯል። ገቢ 400ብር 🙏")
                    last_booked_slot_id = slot_id
                    last_booked_amount  = 400
                    last_booked_type    = "full"
                changed = True

        elif intent == "swap":
            cancel_number = action.get("cancel_number")
            book_numbers  = action.get("book_numbers", [])
            swap_half     = action.get("is_half", False)

            if cancel_number:
                c_slot_id, c_slot = get_slot_by_number(cancel_number, data)
                if c_slot and (c_slot["p1_id"] == user_id or c_slot["p2_id"] == user_id):
                    if c_slot["type"] == "half" and c_slot["p2_id"] is not None and c_slot["p1_id"] == user_id:
                        data["slots"][c_slot_id].update({
                            "p1_id": c_slot["p2_id"], "p1_name": c_slot["p2_name"],
                            "p1_paid": c_slot["p2_paid"],
                            "p2_id": None, "p2_name": None, "p2_paid": False,
                        })
                    elif c_slot["p2_id"] == user_id:
                        data["slots"][c_slot_id].update({"p2_id": None, "p2_name": None, "p2_paid": False})
                    else:
                        saved_numbers = c_slot["numbers"]
                        data["slots"][c_slot_id] = make_empty_slot(int(c_slot_id))
                        data["slots"][c_slot_id]["numbers"] = saved_numbers
                    cancelled.append(cancel_number)
                    changed = True

            for bn in book_numbers:
                b_slot_id, b_slot = get_slot_by_number(bn, data)
                if b_slot is None:
                    continue
                if swap_half:
                    if b_slot["type"] is None:
                        data["slots"][b_slot_id].update({
                            "type": "half",
                            "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                            "p2_id": None, "p2_name": None, "p2_paid": False,
                        })
                        booked_half.append(bn)
                        last_booked_slot_id = b_slot_id
                        last_booked_amount  = 200
                        last_booked_type    = "half"
                        changed = True
                    elif b_slot["type"] == "half" and b_slot["p2_id"] is None and b_slot["p1_id"] != user_id:
                        data["slots"][b_slot_id].update({
                            "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                        })
                        half_joined.append(bn)
                        last_booked_slot_id = b_slot_id
                        last_booked_amount  = 200
                        last_booked_type    = "half"
                        changed = True
                    else:
                        already_taken.append(bn)
                else:
                    if b_slot["type"] is None:
                        data["slots"][b_slot_id].update({
                            "type": "full",
                            "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                            "p2_id": None, "p2_name": None, "p2_paid": False,
                        })
                        booked_full.append(bn)
                        last_booked_slot_id = b_slot_id
                        last_booked_amount  = 400
                        last_booked_type    = "full"
                        changed = True
                    else:
                        already_taken.append(bn)

    if changed:
        save_data(data)
        await update_lottery_message(context.bot, data)

    if booked_full:
        await update.message.reply_text(f"እሺ ገቢ {len(booked_full) * 400}ብር 🙏")
        if last_booked_slot_id:
            save_pending_payment(user_id, last_booked_slot_id, last_booked_amount, last_booked_type)

    if booked_half:
        await update.message.reply_text(f"እሺ ገቢ {len(booked_half) * 200}ብር 🙏")
        if last_booked_slot_id:
            save_pending_payment(user_id, last_booked_slot_id, last_booked_amount, last_booked_type)

    if half_joined:
        await update.message.reply_text(f"✅ ተቀላቅለሃል! slot ሙሉ ሆኗል 🎉 እሺ ገቢ 200ብር 🙏")
        if last_booked_slot_id:
            save_pending_payment(user_id, last_booked_slot_id, 200, "half")

    if cancelled:
        await update.message.reply_text("✅ ተሰርዟል።")

    if not_yours:
        await update.message.reply_text(f"❌ እነዚህ ቁጥሮች የእርስዎ አይደሉም: {not_yours}")

    if already_taken:
        await update.message.reply_text("🙏 ተቀደምክ! 🙏")

    data = load_data()
    if sum(1 for s in data["slots"].values() if is_slot_full_booked(s)) == 20:
        await update.message.reply_text("🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")

# ==================== FLASK SMS WEBHOOK ====================

flask_app = Flask(__name__)
_bot_app = None

@flask_app.route("/", methods=["GET"])
def health():
    return "Bot is running! ✅", 200

@flask_app.route("/sms", methods=["POST"])
def sms_webhook():
    try:
        if flask_request.is_json:
            payload = flask_request.get_json()
            sms_text = payload.get("message") or payload.get("text") or payload.get("body") or ""
        else:
            sms_text = (
                flask_request.form.get("message") or
                flask_request.form.get("text") or
                flask_request.form.get("body") or
                flask_request.data.decode("utf-8", errors="ignore")
            )

        print(f"📩 SMS received: {sms_text[:100]}")

        if "Mbreciept.cbe.com.et" in sms_text or "mbreciept.cbe.com.et" in sms_text:
            threading.Thread(
                target=lambda: _process_cbe_sms(sms_text),
                daemon=True
            ).start()
            return jsonify({"status": "processing"}), 200

        return jsonify({"status": "ignored", "reason": "not CBE SMS"}), 200

    except Exception as e:
        print(f"❌ SMS webhook error: {e}")
        increment_counter("errors")
        return jsonify({"status": "error", "message": str(e)}), 500

def _process_cbe_sms(sms_text: str):
    import asyncio

    increment_counter("sms_received")

    link = extract_cbe_link(sms_text)
    amount = extract_amount_from_sms(sms_text)
    sender = extract_sender_from_sms(sms_text)

    if not link:
        print("❌ No CBE link found in SMS")
        return

    ref = fetch_ref_from_cbe_link(link)
    if not ref:
        print("❌ Could not extract ref from CBE link")
        increment_counter("errors")
        return

    saved = save_verified_payment(ref, amount or 0, sender or "Unknown")

    pending_sc = get_pending_screenshot(ref)
    auto_approved = False

    if pending_sc:
        try:
            data = load_data()
            slot_id = pending_sc["slot_id"]
            slot = data["slots"].get(slot_id)
            sc_user_id = pending_sc["user_id"]
            expected_amount = pending_sc["amount"]

            if slot and (not amount or amount >= expected_amount):
                if slot.get("p1_id") == sc_user_id:
                    data["slots"][slot_id]["p1_paid"] = True
                elif slot.get("p2_id") == sc_user_id:
                    data["slots"][slot_id]["p2_paid"] = True

                save_data(data)
                mark_payment_used(ref, sc_user_id)
                clear_pending_payment(sc_user_id)
                clear_pending_screenshot(ref)
                auto_approved = True
                increment_counter("auto_approved")
                print(f"✅ Auto-approved ref={ref} for user_id={sc_user_id}")
        except Exception as e:
            print(f"❌ Auto-approve error: {e}")
            increment_counter("errors")

    if _bot_app and ADMIN_TELEGRAM_ID:
        async def notify():
            if auto_approved:
                admin_msg = (
                    f"✅ ክፍያ auto-approved!\n\n"
                    f"📋 Ref: {ref}\n"
                    f"💰 Amount: ETB {amount}\n"
                    f"👤 Sender: {sender or 'Unknown'}\n"
                    f"🤖 Screenshot ቀደም ተልኮ ነበር — ተረጋግጧል!"
                )
            elif saved:
                admin_msg = (
                    f"✅ አዲስ ክፍያ ተመዝግቧል!\n\n"
                    f"📋 Ref: {ref}\n"
                    f"💰 Amount: ETB {amount}\n"
                    f"👤 Sender: {sender or 'Unknown'}"
                )
            else:
                admin_msg = f"⚠️ ይህ payment ቀድሞ ተመዝግቧል!\nRef: {ref}"

            await _bot_app.bot.send_message(chat_id=ADMIN_TELEGRAM_ID, text=admin_msg)

            if auto_approved and pending_sc:
                user_msg = (
                    f"✅ ክፍያዎ ተረጋግጧል!\n\n"
                    f"📋 Ref: {ref}\n"
                    f"💰 ETB {amount}\n"
                    f"👤 {sender or 'Unknown'}\n\n"
                    f"🎰 መልካም ዕድል!"
                )
                await _bot_app.bot.send_message(
                    chat_id=pending_sc["user_id"],
                    text=user_msg
                )

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(notify())
        loop.close()

def run_flask():
    port = int(os.getenv("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ==================== MAIN ====================

def main():
    global _bot_app
    init_db()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start_lottery", start_lottery))
    app.add_handler(CommandHandler("paid", mark_paid))
    app.add_handler(CommandHandler("804", admin_report))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _bot_app = app

    print("✅ Groq AI Brain active — llama-3.3-70b-versatile")
    print("✅ Bot እየሰራ ነው...")
    print("✅ SMS Webhook: /sms endpoint ready")
    print("✅ /804 Resource tracking ready")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
