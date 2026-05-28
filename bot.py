import os
import re
import json
import base64
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATA_FILE = "lottery_data.json"
PAYMENTS_FILE = "payments.json"

ADDIS_AI_KEYS = [
    os.getenv("ADDIS_AI_API_KEY_1"),
    os.getenv("ADDIS_AI_API_KEY_2"),
    os.getenv("ADDIS_AI_API_KEY_3"),
    os.getenv("ADDIS_AI_API_KEY_4"),
    os.getenv("ADDIS_AI_API_KEY_5"),
    os.getenv("ADDIS_AI_API_KEY_6"),
    os.getenv("ADDIS_AI_API_KEY_7"),
    os.getenv("ADDIS_AI_API_KEY_8"),
    os.getenv("ADDIS_AI_API_KEY_9"),
    os.getenv("ADDIS_AI_API_KEY_10"),
]
ADDIS_AI_KEYS = [k for k in ADDIS_AI_KEYS if k]
current_key_index = 0

def get_next_key() -> str:
    global current_key_index
    key = ADDIS_AI_KEYS[current_key_index % len(ADDIS_AI_KEYS)]
    current_key_index += 1
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

# ==================== PAYMENTS DATA ====================

def load_payments() -> dict:
    if os.path.exists(PAYMENTS_FILE):
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"verified": {}, "pending": {}}

def save_payments(data: dict):
    with open(PAYMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_verified_payment(ref: str, amount: float, sender: str):
    """Admin SMS forward ሲመጣ ref ያስቀምጣል"""
    payments = load_payments()
    if ref not in payments["verified"]:
        payments["verified"][ref] = {
            "amount": amount,
            "sender": sender,
            "used": False,
            "used_by": None
        }
        save_payments(payments)
        print(f"✅ Payment saved: ref={ref}, amount={amount}")
        return True
    return False  # Already exists

def find_verified_payment(ref: str) -> dict:
    payments = load_payments()
    return payments["verified"].get(ref)

def mark_payment_used(ref: str, user_id: int):
    payments = load_payments()
    if ref in payments["verified"]:
        payments["verified"][ref]["used"] = True
        payments["verified"][ref]["used_by"] = user_id
        save_payments(payments)

def save_pending_payment(user_id: int, slot_id: str, amount: int, booking_type: str):
    """User ቁጥር ሲይዝ pending payment ያስቀምጣል"""
    payments = load_payments()
    payments["pending"][str(user_id)] = {
        "slot_id": slot_id,
        "amount": amount,
        "type": booking_type
    }
    save_payments(payments)

def get_pending_payment(user_id: int) -> dict:
    payments = load_payments()
    return payments["pending"].get(str(user_id))

def clear_pending_payment(user_id: int):
    payments = load_payments()
    if str(user_id) in payments["pending"]:
        del payments["pending"][str(user_id)]
        save_payments(payments)

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
            d = json.load(f)
        if "last_numbers_per_user" not in d:
            d["last_numbers_per_user"] = {}
        return d
    slots = {str(i): make_empty_slot(i) for i in range(1, 21)}
    return {"slots": slots, "lottery_message_id": None, "chat_id": None, "last_numbers_per_user": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    """CBE link → image/page → Groq Vision → ref"""
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

        # Groq Vision ይጠቀማል
        ref = extract_ref_with_groq(image_data, content_type.split(";")[0].strip())
        return ref
    except Exception as e:
        print(f"❌ CBE link fetch error: {e}")
        return None

def extract_ref_with_groq(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Groq Vision → CBE receipt ላይ ref number ያወጣል"""
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

        # FT... format ያወጣል
        match = re.search(r'[A-Z]{2}[A-Z0-9]{6,15}', text)
        return match.group(0) if match else None
    except Exception as e:
        print(f"❌ Groq Vision error: {e}")
        return None

# ==================== ADDIS AI HELPERS ====================

def addis_call(prompt: str, max_tokens: int = 300, temperature: float = 0.3) -> str:
    try:
        response = requests.post(
            "https://api.addisassistant.com/api/v1/chat_generate",
            headers={"x-api-key": get_next_key(), "Content-Type": "application/json"},
            json={
                "model": "Addis-፩-አሌፍ",
                "prompt": prompt,
                "target_language": "am",
                "generation_config": {"temperature": temperature, "maxOutputTokens": max_tokens}
            },
            timeout=20
        )
        data = response.json()
        inner = data.get("data", data)
        return inner.get("response_text", "").strip()
    except Exception as e:
        print(f"❌ Addis AI call error: {e}")
        return ""

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

def build_context_info(data: dict) -> str:
    filled = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
    free_slots = [s for s in data["slots"].values() if s["type"] is None]
    half_open  = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]

    lines = [
        f"አጠቃላይ: {filled}/20 slots ሞልቷል",
        f"ነፃ slots: {len(free_slots)} | ግማሽ ክፍት: {len(half_open)}",
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
    prompt = f"""አንተ የሎተሪ bot brain ነህ። JSON ብቻ መልስ። ምንም ሌላ ቃል አታክል።

=== ዋጋ ===
ሙሉ slot=400ብር | ግማሽ slot=200ብር | ሌላ ዋጋ የለም።

=== Latin አማርኛ ===
ሰዎች አማርኛን በ Latin ፊደል ይፅፋሉ (Ethiopic transliteration)።
Latin ቃል ሲመጣ አማርኛ ነው — ሙሉ ፍቺውን ተረድተህ ስራ።

=== Intents ===
1. book — ቁጥር መያዝ (number, is_half, name)
2. cancel — ቁጥር መሰረዝ (number)
3. change_type — slot አይነት መቀየር (number, new_type)
4. swap — ቁጥሮች መቀያየር (cancel_number, book_numbers, is_half)

=== አሁናዊ ሁኔታ ===
{context_info}

last_numbers: {last_numbers if last_numbers else "የሉም"}
free_numbers: {free_numbers if free_numbers else "የሉም"}

=== ላኪ ===
ስም: {sender_first_name}
መልእክት: "{raw_text}"

=== JSON format ===
{{
  "actions": [
    {{"intent": "book", "number": 21, "is_half": false, "name": null}}
  ],
  "valid": true,
  "reply": null
}}

valid=false → reply=አማርኛ | valid=true → reply=null
JSON ብቻ።"""

    result = addis_call(prompt, max_tokens=300, temperature=0.1)
    print(f"🧠 AI brain raw: {result}")

    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"❌ ai_brain parse error: {e}")

    nums = extract_numbers_from_text(raw_text)
    is_half = detect_half_booking(raw_text)
    if nums:
        return {
            "actions": [{"intent": "book", "number": n, "is_half": is_half, "name": None} for n, h in nums],
            "valid": True,
            "reply": None
        }
    return {"actions": [], "valid": False, "reply": "❓ ልረዳህ አልቻልኩም። ቁጥር ፃፍ ወይም ጥያቄ ጠይቅ።"}

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
    """Admin manually paid ሊያደርግ ከፈለገ"""
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
            try:
                sent = await bot.send_message(chat_id=chat_id, text=build_full_message(data))
                data["lottery_message_id"] = sent.message_id
                save_data(data)
            except Exception as e2:
                print(f"❌ Send new message error: {e2}")

# ==================== PAYMENT: SCREENSHOT HANDLER ====================

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User screenshot ሲልክ — CBE ref ያረጋግጣል"""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Pending payment አለ?
    pending = get_pending_payment(user_id)
    if not pending:
        await update.message.reply_text(
            "❓ መጀመሪያ ቁጥር ይያዙ፣ ከዚያ screenshot ይላኩ።"
        )
        return

    await update.message.reply_text("⏳ ክፍያ እየተረጋገጠ ነው...")

    # Photo ያወርዳል
    photos = update.message.photo
    largest = photos[-1]
    file = await context.bot.get_file(largest.file_id)
    image_bytes = await file.download_as_bytearray()

    # Groq Vision → ref
    ref = extract_ref_with_groq(bytes(image_bytes), "image/jpeg")

    if not ref:
        await update.message.reply_text(
            "❌ Reference number ማንበብ አልተቻለም።\n"
            "CBE receipt screenshot ትክክለኛ መሆኑን ያረጋግጡ።"
        )
        return

    print(f"📋 Extracted ref: {ref}")

    # DB ውስጥ ይፈልጋል
    payment = find_verified_payment(ref)

    if not payment:
        await update.message.reply_text(
            f"❌ ይህ ክፍያ አልተረጋገጠም!\n\n"
            f"📋 Ref: {ref}\n\n"
            f"Admin ገና SMS አልደረሰውም። ትንሽ ቆይተው እንደገና ይሞክሩ።"
        )
        return

    if payment["used"]:
        await update.message.reply_text(
            f"⚠️ ይህ ክፍያ ቀድሞ ጥቅም ላይ ውሏል!\n\nRef: {ref}"
        )
        return

    # Amount ይፈትሻል
    expected_amount = pending["amount"]
    paid_amount = payment.get("amount", 0)

    if paid_amount and paid_amount < expected_amount:
        await update.message.reply_text(
            f"❌ ክፍያ አይሆንም!\n\n"
            f"💰 የተከፈለ: ETB {paid_amount}\n"
            f"💰 የሚፈለግ: ETB {expected_amount}\n\n"
            f"ትክክለኛ መጠን ይክፈሉ።"
        )
        return

    # ✅ ሁሉም ተሟልቷል — slot paid ያደርጋል
    data = load_data()
    slot_id = pending["slot_id"]
    slot = data["slots"].get(slot_id)

    if not slot:
        await update.message.reply_text("❌ Slot አልተገኘም።")
        return

    # p1 ወይም p2 paid ያደርጋል
    if slot["p1_id"] == user_id:
        data["slots"][slot_id]["p1_paid"] = True
    elif slot["p2_id"] == user_id:
        data["slots"][slot_id]["p2_paid"] = True

    save_data(data)
    mark_payment_used(ref, user_id)
    clear_pending_payment(user_id)

    await update_lottery_message(context.bot, data)

    sender = payment.get("sender", "Unknown")
    await update.message.reply_text(
        f"✅ ክፍያ ተረጋግጧል!\n\n"
        f"📋 Ref: {ref}\n"
        f"💰 ETB {paid_amount}\n"
        f"👤 {sender}\n\n"
        f"🎰 መልካም ዕድል!"
    )

    # ሁሉም ሞልቷል?
    data = load_data()
    if sum(1 for s in data["slots"].values() if is_slot_full_booked(s)) == 20:
        await context.bot.send_message(chat_id, "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")

# ==================== PAYMENT: ADMIN SMS HANDLER ====================

async def handle_admin_sms(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Admin CBE SMS forward ሲልክ"""
    chat_id = update.effective_chat.id
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
            await update.message.reply_text(reply or "❓ ልረዳህ አልቻልኩም።")
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

    # ── ለ pending payment slot_id ──
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

        # ── BOOK ──
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

        # ── CANCEL ──
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

        # ── CHANGE TYPE ──
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

        # ── SWAP ──
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

    # ── REPLIES ──
    if booked_full:
        total = len(booked_full) * 400
        await update.message.reply_text(
            f"እሺ ገቢ {total}ብር 🙏\n\n"
            f"💳 CBE: 1000641057146\n"
            f"💳 አዋሽ: 01335630641400\n"
            f"💳 ዳሽን: 5389857825011\n"
            f"📱 ቴሌ ብር: 0952346729\n\n"
            f"✅ ከፈሉ በኋላ screenshot ይላኩ!"
        )
        if last_booked_slot_id:
            save_pending_payment(user_id, last_booked_slot_id, last_booked_amount, last_booked_type)

    if booked_half:
        total = len(booked_half) * 200
        await update.message.reply_text(
            f"እሺ ገቢ {total}ብር 🙏\n\n"
            f"💳 CBE: 1000641057146\n"
            f"💳 አዋሽ: 01335630641400\n"
            f"💳 ዳሽን: 5389857825011\n"
            f"📱 ቴሌ ብር: 0952346729\n\n"
            f"✅ ከፈሉ በኋላ screenshot ይላኩ!"
        )
        if last_booked_slot_id:
            save_pending_payment(user_id, last_booked_slot_id, last_booked_amount, last_booked_type)

    if half_joined:
        await update.message.reply_text(
            f"✅ ተቀላቅለሃል! slot ሙሉ ሆኗል 🎉 እሺ ገቢ 200ብር 🙏\n\n"
            f"💳 CBE: 1000641057146\n\n"
            f"✅ ከፈሉ በኋላ screenshot ይላኩ!"
        )
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
    thread = threading.Thread(target=run_server)
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

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start_lottery", start_lottery))
    app.add_handler(CommandHandler("paid", mark_paid))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"✅ {len(ADDIS_AI_KEYS)} Addis AI keys loaded")
    print("✅ Bot እየሰራ ነው...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
