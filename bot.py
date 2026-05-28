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
    free_slots  = [s for s in data["slots"].values() if s["type"] is None]
    half_open   = [s for s in data["slots"].values() if s["type"] == "half" and s["p2_id"] is None]

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


def ai_brain(raw_text: str, sender_first_name: str, context_info: str) -> dict:
    """
    AI Brain — ቁጥር book/cancel ብቻ valid=true።
    ሌላ ሁሉም (ጥያቄ፣ ሰላምታ፣ check፣ ወዘተ) → valid=false + reply።
    """
    prompt = f"""አንተ የሎተሪ bot brain ነህ። JSON ብቻ መልስ። ምንም ሌላ ቃል አታክል።

=== ህግ — ጥብቅ ===
valid=true የሚሆነው ሰው ቁጥር ሊይዝ ወይም ሊሰርዝ ሲፈልግ ብቻ ነው።
ሌላ ሁሉም → valid=false + reply (አማርኛ፣ አጭር፣ ትክክለኛ መልስ)።

=== valid=true ምሳሌዎች ===
- "21" → {{"intent":"book","number":21,"is_half":false,"name":null}}
- "21+" → {{"intent":"book","number":21,"is_half":true,"name":null}}
- "21 ግማሽ" / "21 half" → is_half=true
- "10 እና 16" → ሁለት actions
- "7 8 9" → ሶስት actions
- "cancel 21" / "ሰርዝ 10" / "ልቀቅ 5" → intent=cancel
- "21 አበበ ብለህ ያዝልኝ" → number=21, name="አበበ"
- "16 ለእናቴ ያዝልኝ ስሟ ሳራ ነው" → number=16, name="ሳራ"
- "10 ለጓደኛዬ ለዮሐንስ" → number=10, name="ዮሐንስ"
- "21+ አበበ" → number=21, is_half=true, name="አበበ"
- "ያዝልኝ 33" → number=33, name=null (sender name ይጠቀማል)

name rule: መልእክቱ ውስጥ ሌላ ሰው ስም ካለ → name ስጥ። ከሌለ → name=null።

=== valid=false ምሳሌዎች ===
- "51 አለ ወይ?" → slot ሁኔታ ንገረው (ነፃ ወይም ተይዟል)
- "ስንት ቀርቷል?" → ቀሪ slots ብዛት ንገረው
- "ዋጋው ስንት ነው?" → 400ብር ሙሉ፣ 200ብር ግማሽ ንገረው
- "መቼ ነው ዕጣው?" → አታውቅም ብል
- "ሰላም" / "ሃሎ" → ሰላምታ ልስጥ
- "ሽልማቱ ስንት ነው?" → 1ኛ=5000፣ 2ኛ=1000፣ 3ኛ=400 ንገረው
- ቁጥር ያለው ግን booking/cancel አይደለም → valid=false

=== አሁናዊ ሁኔታ ===
{context_info}

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

valid=false ከሆነ reply=አማርኛ መልስ፣ valid=true ከሆነ reply=null።
JSON ብቻ። ምንም ማብራሪያ አታክል።"""

    result = addis_call(prompt, max_tokens=300, temperature=0.1)
    print(f"🧠 AI brain raw: {result}")

    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return parsed
    except Exception as e:
        print(f"❌ ai_brain parse error: {e} | raw: {result}")

    # Fallback — ቁጥሮች ካሉ book
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
    context_info = build_context_info(data)

    number_list = extract_numbers_from_text(raw_text)

    if has_text(raw_text):
        # AI Brain — ቁጥር book/cancel ብቻ valid=true፣ ሌላ ሁሉም false
        brain = ai_brain(raw_text, sender_first_name, context_info)
        valid  = brain.get("valid", False)
        reply  = brain.get("reply", None)
        print(f"🧠 brain={brain}")

        if not valid or not brain.get("actions"):
            await update.message.reply_text(reply or "❓ ልረዳህ አልቻልኩም።")
            return

        actions = brain.get("actions", [])
    else:
        # Bot ቀጥታ — ቁጥሮች ብቻ
        if not number_list:
            await update.message.reply_text("❓ ቁጥር ፃፍ። ለምሳሌ: 21 ወይም 21+")
            return
        actions = [{"intent": "book", "number": n, "is_half": h, "name": None} for n, h in number_list]

    # ==================== ACTIONS ====================
    booked_full  = []
    booked_half  = []
    half_joined  = []
    already_taken = []
    cancelled    = []
    not_yours    = []

    changed = False

    for action in actions:
        intent  = action.get("intent", "book")
        number  = action.get("number")
        is_half = action.get("is_half", False)
        ai_name = action.get("name")
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
                        "p2_id": None,    "p2_name": None,          "p2_paid": False,
                    })
                    booked_half.append(number)
                    changed = True
                elif slot["type"] == "half" and slot["p2_id"] is None and slot["p1_id"] != user_id:
                    data["slots"][slot_id].update({
                        "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                    })
                    half_joined.append(number)
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
                        "p2_id": None,    "p2_name": None,          "p2_paid": False,
                    })
                    booked_full.append(number)
                    changed = True
                elif slot["type"] == "half" and slot["p2_id"] is None and slot["p1_id"] != user_id:
                    # ግማሽ ክፍት እያለ ሙሉ ቢፅፍ → ግማሽ ያደርብለታል
                    data["slots"][slot_id].update({
                        "p2_id": user_id, "p2_name": display_name, "p2_paid": False
                    })
                    half_joined.append(number)
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
                changed = True
            elif slot["type"] == "half" and slot["p2_id"] == user_id:
                data["slots"][slot_id].update({"p2_id": None, "p2_name": None, "p2_paid": False})
                cancelled.append(number)
                changed = True
            else:
                not_yours.append(number)

    if changed:
        save_data(data)
        await update_lottery_message(context.bot, data)

    # ── REPLIES ──
    if booked_full:
        await update.message.reply_text(f"እሺ ገቢ {len(booked_full) * 400}ብር 🙏")

    if booked_half:
        await update.message.reply_text(f"እሺ ገቢ {len(booked_half) * 200}ብር 🙏")

    if half_joined:
        await update.message.reply_text(f"✅ ተቀላቅለሃል! slot ሙሉ ሆኗል 🎉 እሺ ገቢ 200ብር 🙏")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print(f"✅ {len(ADDIS_AI_KEYS)} Addis AI keys loaded:")
    for i, key in enumerate(ADDIS_AI_KEYS):
        masked = key[:8] + "..." + key[-4:] if len(key) > 12 else "SHORT_KEY"
        print(f"  Key {i+1}: {masked}")
    print("✅ Bot እየሰራ ነው...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
