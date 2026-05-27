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

# 10 Addis AI API Keys rotation
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
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    slots = {}
    for i in range(1, 21):
        start = (i - 1) * 5 + 1
        slots[str(i)] = {
            "numbers": list(range(start, start + 5)),
            "owner": None,
            "first_name": None,
            "paid": False
        }
    return {
        "slots": slots,
        "lottery_message_id": None,
        "chat_id": None
    }

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
            if idx == 0 and slot["owner"]:
                paid_mark = "✅" if slot["paid"] else "⏳"
                lines.append(f"{num:02d}# {slot['first_name']} {paid_mark}")
            else:
                lines.append(f"{num:02d}#")
        groups.append("\n".join(lines))
    return "\n\n".join(groups)

def build_full_message(data: dict) -> str:
    numbers_text = build_numbers_text(data)
    return LOTTERY_TEMPLATE.format(numbers=numbers_text)

# ==================== ADDIS AI HELPERS ====================

def addis_call(prompt: str, max_tokens: int = 300, temperature: float = 0.3) -> str:
    """ሁሉም Addis AI call አንድ ቦታ ያልፋል"""
    try:
        response = requests.post(
            "https://api.addisassistant.com/api/v1/chat_generate",
            headers={
                "x-api-key": get_next_key(),
                "Content-Type": "application/json"
            },
            json={
                "model": "Addis-፩-አሌፍ",
                "prompt": prompt,
                "target_language": "am",
                "generation_config": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens
                }
            },
            timeout=20
        )
        data = response.json()
        inner = data.get("data", data)
        result = inner.get("response_text", "").strip()
        return result
    except Exception as e:
        print(f"❌ Addis AI call error: {e}")
        return ""


def translate_to_amharic(text: str) -> str:
    """
    Latin ፊደል አማርኛ (Ethiopic transliteration) ወደ አማርኛ ፊደል ይቀይራል።
    ቀድሞ አማርኛ ወይም English ከሆነ ይመልሰዋል።
    """
    # ቀድሞ አማርኛ ፊደል ከሆነ translate አያስፈልግም
    has_amharic = any('\u1200' <= c <= '\u137F' for c in text)
    if has_amharic:
        return text

    prompt = f"""ይህ መልእክት አማርኛን በ Latin ፊደል የጻፈ ሊሆን ይችላል (Ethiopic transliteration)።
ወደ አማርኛ ፊደል ቀይረህ ስጠኝ። ትርጉም አያስፈልግም — ፊደሉን ብቻ ቀይር።
English ቃላት ካሉ እንዳሉ ተው።
ቁጥሮች እንዳሉ ተው።

መልእክት: "{text}"

አማርኛ ፊደል ብቻ ስጥ። ምንም ማብራሪያ አታክል።"""

    result = addis_call(prompt, max_tokens=150, temperature=0.1)
    if result:
        print(f"🔄 Translated: '{text}' → '{result}'")
        return result
    return text  # fallback — original text


def parse_user_intent(amharic_text: str, sender_first_name: str) -> dict:
    """
    (ቀድሞ translated የሆነ) አማርኛ text ወስዶ intent + number + name ይመልሳል።
    """
    prompt = f"""ከዚህ Telegram መልእክት ውስጥ intent፣ number፣ name extract አድርግ።
JSON ብቻ መልስ። ምንም ሌላ ቃል አታክል።

መልእክት: "{amharic_text}"
ላኪ ስም: "{sender_first_name}"

Rules:
- intent = "book"     ← ሰው ቁጥር ሊይዝ/ሊመዘገብ ከፈለገ (ያዝ፣ እፈልጋለሁ፣ ስጠኝ፣ ቁጥር ፈልጌያለሁ፣ register፣ book፣ take)
- intent = "question" ← ጥያቄ ከሆነ (ምን፣ እንዴት፣ መቼ፣ ቁጥር አለ?)
- intent = "other"    ← ሌላ ከሆነ
- number = ያለው ቁጥር 1–100፣ ከሌለ null
- name   = በመልእክቱ የተጠቀሰ ስም፣ ከሌለ null

JSON format ብቻ:
{{"intent": "book", "number": 21, "name": null}}"""

    result = addis_call(prompt, max_tokens=80, temperature=0.1)

    try:
        match = re.search(r'\{.*?\}', result, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return {
                "intent": parsed.get("intent", "other"),
                "number": parsed.get("number", None),
                "name": parsed.get("name", None)
            }
    except Exception as e:
        print(f"❌ parse_user_intent JSON error: {e} | raw: {result}")

    # Fallback — ቁጥር ብቻ ካለ
    for word in amharic_text.split():
        cleaned = word.replace("#", "").strip()
        try:
            num = int(cleaned)
            if 1 <= num <= 100:
                return {"intent": "book", "number": num, "name": None}
        except ValueError:
            continue

    return {"intent": "other", "number": None, "name": None}


def ask_addis_ai(user_message: str, amharic_message: str, context_info: str) -> str:
    """
    ጥያቄ ወይም other intent ሲሆን AI ይመልሳል።
    user_message  = original (Latin ወይም አማርኛ)
    amharic_message = translated አማርኛ
    """
    prompt = f"""አንተ የሎተሪ bot ነህ። አማርኛ ብቻ ተናገር። አጭር እና ግልጽ መልስ ስጥ። emoji ተጠቀም።

=== ጨዋታው እንዴት ይሰራል ===
- ሎተሪ ለ 20 ሰው ብቻ ነው
- እያንዳንዱ ሰው 5 ቁጥሮች ያገኛል (ለምሳሌ 1#2#3#4#5#)
- ዋጋ: 400 ብር (ሙሉ) ወይም 200 ብር (ግማሽ)
- ሽልማት: 1ኛ=5000ብር 🥇, 2ኛ=1000ብር 🥈, 3ኛ=400ብር 🥉
- ቁጥር ለመያዝ: ቁጥሩን ብቻ ፃፍ (ለምሳሌ: 21) ወይም "ያዝ 21" ብል
- ከፈልህ/ሽ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729
- ክፍያ ከፈለህ/ሽ screenshot ላክ — admin ያረጋግጣል ✅

=== የአሁን ሁኔታ ===
{context_info}

ተጠቃሚ (አማርኛ): {amharic_message}"""

    result = addis_call(prompt, max_tokens=300, temperature=0.7)
    if result:
        return result
    return "❌ AI አገልግሎት ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።"

# ==================== BOT HANDLERS ====================

async def start_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return

    data = load_data()
    for i in range(1, 21):
        start = (i - 1) * 5 + 1
        data["slots"][str(i)] = {
            "numbers": list(range(start, start + 5)),
            "owner": None,
            "first_name": None,
            "paid": False
        }

    message_text = build_full_message(data)
    sent = await update.message.reply_text(message_text)
    data["lottery_message_id"] = sent.message_id
    data["chat_id"] = update.effective_chat.id
    save_data(data)


async def mark_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    if not context.args:
        await update.message.reply_text("አጠቃቀም: /paid <ቁጥር>\nለምሳሌ: /paid 6")
        return

    try:
        number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ቁጥር ብቻ ፃፍ")
        return

    data = load_data()
    slot_id, slot = get_slot_by_number(number, data)

    if not slot:
        await update.message.reply_text("❌ ቁጥር አልተገኘም")
        return

    if not slot["owner"]:
        await update.message.reply_text("❌ ይህ ቁጥር ገና አልተያዘም")
        return

    data["slots"][slot_id]["paid"] = True
    save_data(data)

    await update_lottery_message(context.bot, data)
    await update.message.reply_text(f"✅ {slot['first_name']} ክፍያ ተረጋግጧል!")


async def update_lottery_message(bot: Bot, data: dict):
    if data.get("lottery_message_id") and data.get("chat_id"):
        try:
            new_text = build_full_message(data)
            await bot.edit_message_text(
                chat_id=data["chat_id"],
                message_id=data["lottery_message_id"],
                text=new_text
            )
        except Exception as e:
            print(f"Message update error: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    sender_first_name = update.effective_user.first_name or "ተጠቃሚ"
    data = load_data()

    filled = sum(1 for s in data["slots"].values() if s["owner"])
    free = 20 - filled
    context_info = f"ሞልቷል: {filled}/20 slots። ነፃ slots: {free}"

    # ── Step 1: Latin → አማርኛ translate ──
    amharic_text = translate_to_amharic(user_text)

    # ── Step 2: Intent parse (translated text ይጠቀማል) ──
    parsed = parse_user_intent(amharic_text, sender_first_name)
    intent = parsed.get("intent", "other")
    number = parsed.get("number", None)
    ai_name = parsed.get("name", None)

    display_name = ai_name if ai_name else sender_first_name

    print(f"📩 original='{user_text}' | amharic='{amharic_text}' | intent={intent} | number={number} | name={display_name}")

    # ── Step 3: Handle intent ──
    if intent == "book" and number and 1 <= number <= 100:
        slot_id, slot = get_slot_by_number(number, data)

        if slot is None:
            await update.message.reply_text(f"❌ ቁጥር {number} አልተገኘም።")
            return

        if slot["owner"]:
            free_slots = [s for s in data["slots"].values() if not s["owner"]]
            free_numbers = [s["numbers"][0] for s in free_slots[:5]]
            ctx = f"ቁጥር {number} አስቀድሞ በ {slot['first_name']} ተይዟል። ነፃ ቁጥሮች: {free_numbers}"
            reply = ask_addis_ai(user_text, amharic_text, ctx)
            await update.message.reply_text(reply)
        else:
            data["slots"][slot_id]["owner"] = update.effective_user.id
            data["slots"][slot_id]["first_name"] = display_name
            save_data(data)

            nums = slot["numbers"]
            reply = (
                f"✅ {display_name} ቁጥሮቻቸው:\n"
                f"{nums[0]:02d}# {nums[1]:02d}# {nums[2]:02d}# {nums[3]:02d}# {nums[4]:02d}#\n\n"
                f"💰 400 ብር ከፍለህ/ሽ slot ህን/ሽን አረጋግጥ!\n\n"
                f"🏦 CBE: 1000641057146\n"
                f"🏦 አዋሽ: 01335630641400\n"
                f"🏦 ዳሽን: 5389857825011\n"
                f"📱 ቴሌ ብር: 0952346729"
            )
            await update.message.reply_text(reply)
            await update_lottery_message(context.bot, data)

            filled = sum(1 for s in data["slots"].values() if s["owner"])
            if filled == 20:
                await update.message.reply_text(
                    "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! ትዕግስት አድርጉ... 🎰"
                )

    elif intent == "book" and not number:
        free_slots = [s for s in data["slots"].values() if not s["owner"]]
        free_numbers = [s["numbers"][0] for s in free_slots[:5]]
        await update.message.reply_text(
            f"🎯 ቁጥር ይምረጡ! ነፃ ቁጥሮች (ምሳሌ): {free_numbers}\n"
            f"ቁጥሩን ብቻ ፃፍ። ለምሳሌ: 21"
        )

    else:
        # question ወይም other — AI ይመልሳል
        reply = ask_addis_ai(user_text, amharic_text, context_info)
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
    server = HTTPServer(("0.0.0.0", port), KeepAlive)
    server.serve_forever()

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
