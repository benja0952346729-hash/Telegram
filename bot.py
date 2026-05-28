import os
import re
import json
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DATA_FILE = "lottery_data.json"

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
    tokens = re.split(r'[\s\*\/\&\,፣#\-]+', text)
    seen = set()
    unique = []
    for token in tokens:
        token = token.strip().rstrip('+')
        try:
            num = int(token)
            if 1 <= num <= 100 and num not in seen:
                seen.add(num)
                unique.append(num)
        except ValueError:
            continue
    return unique


def translate_to_amharic(text: str) -> str:
    has_amharic = any('\u1200' <= c <= '\u137F' for c in text)
    if has_amharic:
        return text

    prompt = f"""ይህ አማርኛ በ Latin ፊደል የተጻፈ ሊሆን ይችላል (Ethiopic transliteration)።
ወደ አማርኛ ፊደል ቀይረህ ስጠኝ። ትርጉም አያስፈልግም — ፊደሉን ብቻ ቀይር።
English ቃላት እና ቁጥሮች እንዳሉ ተው።

መልእክት: "{text}"

አማርኛ ፊደል ብቻ ስጥ። ምንም ማብራሪያ አታክል።"""

    result = addis_call(prompt, max_tokens=150, temperature=0.1)
    if result:
        print(f"🔄 Translated: '{text}' → '{result}'")
        return result
    return text


def detect_half_booking(raw_text: str) -> bool:
    lower = raw_text.lower()
    half_keywords = ["ግማሽ", "half", "begmash", "grmash", "gmash", "200"]
    if any(k in lower for k in half_keywords):
        return True
    if re.search(r'\d+\s*\+', raw_text):
        return True
    return False


def parse_user_intent(amharic_text: str, raw_text: str, sender_first_name: str) -> dict:
    is_half = detect_half_booking(raw_text) or detect_half_booking(amharic_text)

    prompt = f"""ከዚህ Telegram መልእክት ውስጥ intent፣ numbers፣ name extract አድርግ።
JSON ብቻ መልስ። ምንም ሌላ ቃል አታክል።

መልእክት: "{amharic_text}"
ላኪ ስም: "{sender_first_name}"

Rules:
- intent = "book"     ← ሰው ቁጥር/ቁጥሮች ሊይዝ ከፈለገ
- intent = "cancel"   ← ሰው ቁጥር/ቁጥሮች ሊሰርዝ/ሊለቅ ከፈለገ
- intent = "question" ← ጥያቄ ከሆነ
- intent = "other"    ← ሌላ
- numbers = ሁሉም ቁጥሮች array (1-100)
- name = በመልእክቱ የተጠቀሰ ስም፣ ከሌለ null

JSON format ብቻ:
{{"intent": "book", "numbers": [10, 15, 36], "name": null}}"""

    result = addis_call(prompt, max_tokens=120, temperature=0.1)

    try:
        match = re.search(r'\{.*?\}', result, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            numbers = parsed.get("numbers", [])
            if not numbers and parsed.get("number"):
                numbers = [parsed["number"]]
            return {
                "intent": parsed.get("intent", "other"),
                "numbers": [n for n in numbers if 1 <= n <= 100],
                "name": parsed.get("name", None),
                "is_half": is_half,
            }
    except Exception as e:
        print(f"❌ parse_user_intent error: {e} | raw: {result}")

    # Fallback
    nums = extract_numbers_from_text(raw_text)
    if nums:
        return {"intent": "book", "numbers": nums, "name": None, "is_half": is_half}

    return {"intent": "other", "numbers": [], "name": None, "is_half": is_half}


def build_context_info(data: dict) -> str:
    """
    Addis AI ሎተሪው ያለበትን ሁኔታ በዝርዝር ያሳውቃል።
    ይህ context Addis AI "ሁሉም ሞልቷል" ብሎ እንዳይሳሳት ይከላከላል።
    """
    filled = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
    free_slots = [s for s in data["slots"].values() if s["type"] is None]
    half_open  = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]

    free_nums = [s["numbers"][0] for s in free_slots[:5]]
    half_nums = [s["numbers"][0] for s in half_open[:3]]

    lines = [
        f"ሞልቷል: {filled}/20 slots",
        f"ነፃ slots (ሙሉ ሊያዙ የሚችሉ): {len(free_slots)} ቀርቷል → ናሙና ቁጥሮች: {free_nums if free_nums else 'የሉም'}",
        f"ግማሽ ክፍት slots (ሌላ ሰው ሊቀላቀል): {len(half_open)} → ናሙና ቁጥሮች: {half_nums if half_nums else 'የሉም'}",
    ]
    if filled < 20:
        lines.append("⚠️ ሎተሪ ገና አልሞላም — ነፃ slots አሉ!")
    else:
        lines.append("✅ ሁሉም slots ተሞልቷል!")
    return "\n".join(lines)


def ask_addis_ai(amharic_message: str, context_info: str) -> str:
    prompt = f"""አንተ የሎተሪ bot ነህ። አማርኛ ብቻ ተናገር። አጭር እና ግልጽ መልስ ስጥ። emoji ተጠቀም። markdown formatting አትጠቀም።

=== ጨዋታው እንዴት እንደሚሰራ ===
- ሎተሪ ለ 20 ሰው ብቻ ነው (20 slots)
- እያንዳንዱ slot 5 ቁጥሮች አሉት (slot 1=1-5, slot 2=6-10, ... slot 19=91-95, slot 20=96-100)
- ቁጥር ለመያዝ → ከዚያ slot ውስጥ ማንኛውንም ቁጥር ፃፍ (ለምሳሌ 91 ወይም 95)
- ዋጋ: 400 ብር (ሙሉ slot) ወይም 200 ብር (ግማሽ slot — 2 ሰው ይካፈላሉ)
- ሽልማት: 1ኛ=5000ብር 🥇  2ኛ=1000ብር 🥈  3ኛ=400ብር 🥉
- ክፍያ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729

=== አስፈላጊ ህጎች — ፈጽሞ አትጣስ ===
1. context ውስጥ "ነፃ slots አሉ" ካለ → "ሁሉም ሞልቷል" ብለህ አትናገር
2. ቁጥር ተልኮ slot ክፍት ከሆነ → "✅ ተይዟል! ክፍያ ፈፅም" ብል
3. ቁጥር ተልኮ slot ተይዟል → "⚠️ ቁጥሩ ተይዟል። ሌላ ምረጥ: [ነፃ ቁጥሮች]" ብል
4. context ሳታምን ምንም አትናገር

=== አሁናዊ የሎተሪ ሁኔታ ===
{context_info}

ተጠቃሚ መልእክት: {amharic_message}"""

    result = addis_call(prompt, max_tokens=300, temperature=0.5)
    return result if result else "❌ AI አገልግሎት ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።"

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
        await update.message.reply_text("አጠቃቀም:\n/paid <ቁጥር>      → ሙሉ ወይም ግማሽ p1\n/paid <ቁጥር> 2   → ግማሽ p2")
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
    print(f"🔄 update_lottery_message: chat_id={chat_id}, message_id={msg_id}")
    if msg_id and chat_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=build_full_message(data)
            )
            print("✅ Lottery message updated successfully")
        except Exception as e:
            print(f"❌ Message update error: {e}")
            # ✅ FIX: edit fail ከሆነ አዲስ message ልካ
            try:
                sent = await bot.send_message(chat_id=chat_id, text=build_full_message(data))
                data["lottery_message_id"] = sent.message_id
                save_data(data)
                print(f"✅ Sent new lottery message id={sent.message_id}")
            except Exception as e2:
                print(f"❌ Send new message error: {e2}")
    else:
        print("⚠️ No lottery_message_id or chat_id — cannot update")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    raw_text = update.message.text.strip()
    sender_first_name = update.effective_user.first_name or "ተጠቃሚ"
    user_id = update.effective_user.id
    data = load_data()

    # ✅ FIX: ዝርዝር context_info ለ Addis AI
    context_info = build_context_info(data)

    # Step 1: Latin → አማርኛ
    amharic_text = translate_to_amharic(raw_text)

    # Step 2: Intent parse
    parsed = parse_user_intent(amharic_text, raw_text, sender_first_name)
    intent      = parsed.get("intent", "other")
    numbers     = parsed.get("numbers", [])
    is_half     = parsed.get("is_half", False)
    ai_name     = parsed.get("name", None)
    display_name = ai_name if ai_name else sender_first_name

    print(f"📩 '{raw_text}' → '{amharic_text}' | intent={intent} | numbers={numbers} | half={is_half} | name={display_name}")

    # ==================== BOOK ====================
    if intent == "book" and numbers:
        booked_full  = []
        booked_half  = []
        already_full = []
        half_joined  = []
        not_found    = []

        for number in numbers:
            slot_id, slot = get_slot_by_number(number, data)
            if slot is None:
                not_found.append(number)
                continue

            if is_half:
                if slot["type"] is None:
                    data["slots"][slot_id].update({
                        "type": "half",
                        "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                        "p2_id": None,    "p2_name": None,          "p2_paid": False,
                    })
                    booked_half.append(number)

                elif slot["type"] == "half" and slot["p2_id"] is None:
                    if slot["p1_id"] == user_id:
                        await update.message.reply_text(f"⚠️ {number}# ቁጥር ቀድሞ ግማሽ ይዘሃል።")
                        continue
                    data["slots"][slot_id].update({
                        "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                    })
                    half_joined.append(number)

                else:
                    name = slot["p1_name"]
                    already_full.append((number, name))

            else:
                if slot["type"] is None:
                    data["slots"][slot_id].update({
                        "type": "full",
                        "p1_id": user_id, "p1_name": display_name, "p1_paid": False,
                        "p2_id": None,    "p2_name": None,          "p2_paid": False,
                    })
                    booked_full.append(number)
                elif slot["p1_id"] == user_id or slot["p2_id"] == user_id:
                    # ✅ FIX: ተጠቃሚ ቀድሞ ይህን slot ይዟል
                    await update.message.reply_text(f"⚠️ {number}# ቁጥር ቀድሞ ይዘሃል!")
                else:
                    name = slot["p1_name"]
                    already_full.append((number, name))

        if booked_full or booked_half or half_joined:
            save_data(data)
            await update_lottery_message(context.bot, data)

        if booked_full:
            await update.message.reply_text(f"እሺ ገቢ {len(booked_full) * 400}ብር 🙏")

        if booked_half:
            await update.message.reply_text("እሺ ገቢ 200ብር 🙏")

        if half_joined:
            nums_str = ", ".join(str(n) for n in half_joined)
            await update.message.reply_text(
                f"✅ {nums_str}# ተቀላቅለሃል! ሁለቱም 200 ብር ፣ slot ሙሉ ሆኗል 🎉"
            )

        # ✅ FIX: already_full → Addis AI ሳይጠራ ቀጥታ መልስ
        if already_full:
            taken_info = ", ".join([f"{n}# (በ{name})" for n, name in already_full])
            free_slots = [s for s in data["slots"].values() if s["type"] is None]
            half_open  = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]
            free_nums  = [s["numbers"][0] for s in free_slots[:5]]
            half_nums  = [s["numbers"][0] for s in half_open[:3]]

            msg = f"⚠️ {taken_info} ቀድሞ ተይዟል።\n\n"
            if free_nums:
                msg += f"🟢 ነፃ ቁጥሮች (400ብር): {free_nums}\n"
            if half_nums:
                msg += f"🟡 ግማሽ ክፍት (200ብር): {half_nums}\n"
            if not free_nums and not half_nums:
                msg += "😔 ሁሉም slots ተሞልቷል!"
            await update.message.reply_text(msg)

        if not_found:
            await update.message.reply_text(f"❌ ቁጥሮቹ አልተገኙም: {not_found}")

        data = load_data()
        filled_now = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
        if filled_now == 20:
            await update.message.reply_text("🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! 🎰")

    # ==================== CANCEL ====================
    elif intent == "cancel" and numbers:
        cancelled = []
        not_yours = []

        for number in numbers:
            slot_id, slot = get_slot_by_number(number, data)
            if slot is None or slot["type"] is None:
                not_yours.append(number)
                continue

            if slot["p1_id"] == user_id:
                if slot["type"] == "half" and slot["p2_id"] is not None:
                    data["slots"][slot_id].update({
                        "p1_id":   slot["p2_id"],   "p1_name":  slot["p2_name"],
                        "p1_paid": slot["p2_paid"],
                        "p2_id":   None,             "p2_name":  None, "p2_paid": False,
                    })
                else:
                    data["slots"][slot_id] = make_empty_slot(int(slot_id))
                    data["slots"][slot_id]["numbers"] = slot["numbers"]
                cancelled.append(number)

            elif slot["type"] == "half" and slot["p2_id"] == user_id:
                data["slots"][slot_id].update({
                    "p2_id": None, "p2_name": None, "p2_paid": False
                })
                cancelled.append(number)
            else:
                not_yours.append(number)

        if cancelled:
            save_data(data)
            await update_lottery_message(context.bot, data)
            remaining_full = sum(1 for s in data["slots"].values() if s["p1_id"] == user_id and s["type"] == "full")
            remaining_half = sum(1 for s in data["slots"].values() if (s["p1_id"] == user_id or s["p2_id"] == user_id) and s["type"] == "half")
            total = remaining_full * 400 + remaining_half * 200
            if total > 0:
                await update.message.reply_text(f"✅ ተሰርዟል። ቀሪ ሂሳብ: {total} ብር 🙏")
            else:
                await update.message.reply_text("✅ ተሰርዟል።")

        if not_yours:
            await update.message.reply_text(f"❌ እነዚህ ቁጥሮች የእርስዎ አይደሉም: {not_yours}")

    # ==================== BOOK without numbers ====================
    elif intent == "book" and not numbers:
        free_slots = [s for s in data["slots"].values() if s["type"] is None]
        half_open  = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]
        free_nums  = [s["numbers"][0] for s in free_slots[:5]]
        half_nums  = [s["numbers"][0] for s in half_open[:3]]
        msg = f"🎯 ቁጥር ይምረጡ!\n\nሙሉ (400ብር) ነፃ: {free_nums}\nግማሽ (200ብር) ክፍት: {half_nums}\n\nሙሉ ለ: 76\nግማሽ ለ: 76+"
        await update.message.reply_text(msg)

    # ==================== OTHER / QUESTION ====================
    else:
        # ✅ FIX: ዝርዝር context_info ከ build_context_info() ይጠቀማል
        reply = ask_addis_ai(amharic_text, context_info)
        await update.message.reply_text(reply)

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"✅ {len(ADDIS_AI_KEYS)} Addis AI keys loaded:")
    for i, key in enumerate(ADDIS_AI_KEYS):
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "SHORT_KEY"
        print(f"  Key {i+1}: {masked}")
    print("✅ Bot እየሰራ ነው...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
