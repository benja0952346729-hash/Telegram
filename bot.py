import os
import json
import requests
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
        "chat_id": None,
        "payments": {}
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

# ==================== ADDIS AI ====================
def parse_user_intent(user_message: str, sender_first_name: str) -> dict:
    """
    AI ሰው የፃፈውን ተረድቶ JSON ይመልሳል።
    Return format:
    {
      "intent": "book" | "cancel" | "question" | "other",
      "number": 21,        ← ወይም null
      "name": "አበበ"        ← ወይም null (ካልተጠቀሰ sender name ይጠቀማል)
    }
    """
    prompt = f"""ከዚህ የ Telegram መልእክት ውስጥ የሚከተሉትን extract አድርግ።
JSON ብቻ መልስ። ምንም ሌላ ቃል አታክል።

መልእክት: "{user_message}"
ላኪ ስም: "{sender_first_name}"

Rules:
- intent = "book" ← ሰው ቁጥር ሊይዝ/ሊመዘገብ ከፈለገ (ያዝ፣ ይያዛልኝ፣ እፈልጋለሁ፣ register፣ ቁጥር ብቻ ሲፅፍ)
  ⚠️ "ትላንት"፣ "ነበር"፣ "ባለፈ"፣ "በፊት"፣ "ቀድሞ" ካለ → "book" አይሁን (past ማለት ነው) — ግን ሁለቱ ካሉ ለምሳሌ "ትላንት አልያዝኩም ዛሬ 10 ያዝልኝ" → "book" ይሁን
- intent = "cancel" ← ሰው ቁጥር ሊሰርዝ/ሊያስቀር ከፈለገ (አልፈልግም፣ ሰርዝ፣ አስቀር፣ cancel፣ ይሰረዝልኝ)
- intent = "payment" ← ሰው ብር ልኳል/ከፍሏል ሲል (ብር ልኬ፣ ከፈልኩ፣ ገቢ አደረጉ፣ ገቢ ላክሁ፣ ብር ቁጥር ብቻ ሲፅፍ ለምሳሌ 400 ወይም 800)
- intent = "question" ← ጥያቄ ከሆነ
- intent = "past_complaint" ← ሰው ባለፈ ጨዋታ ቅሬታ ሲያቀርብ (ትላንት ይዤ ነበር ለምን አልያዝኩም፣ ቀድሞ ከፍዬ ነበር፣ ባለፈ ጨዋታ etc) — ዛሬ action ከሌለ
- intent = "other" ← ሌላ ከሆነ
- number = ዋና ቁጥር (1-100)፣ ከሌለ null
- numbers = ሁሉም ቁጥሮች array (ለምሳሌ [10, 25, 36])፣ አንድ ብቻ ከሆነ [21]፣ ከሌለ []
- amount = የብር መጠን ካለ (ለምሳሌ 400፣ 800፣ 200)፣ ከሌለ null
- name = በመልእክቱ ውስጥ የተጠቀሰ ስም ከሌለ null (sender ስም አይደለም)

ምሳሌዎች:
"10,25,36" → {{"intent":"book","number":10,"numbers":[10,25,36],"name":null,"amount":null}}
"21  29  31" → {{"intent":"book","number":21,"numbers":[21,29,31],"name":null,"amount":null}}
"25&26&31" → {{"intent":"book","number":25,"numbers":[25,26,31],"name":null,"amount":null}}
"06 አበበ ብሎ ያዝ" → {{"intent":"book","number":6,"numbers":[6],"name":"አበበ","amount":null}}

JSON format ብቻ:
{{"intent": "book", "number": 21, "numbers": [21], "name": "አበበ", "amount": null}}"""

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
                    "temperature": 0.1,
                    "maxOutputTokens": 80
                }
            },
            timeout=15
        )
        data = response.json()
        inner = data.get("data", data)
        raw = inner.get("response_text", "").strip()

        # JSON parse
        import re
        match = re.search(r'\{.*?\}', raw, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return {
                "intent": parsed.get("intent", "other"),
                "number": parsed.get("number", None),
                "numbers": parsed.get("numbers", []),
                "name": parsed.get("name", None),
                "amount": parsed.get("amount", None)
            }
    except Exception as e:
        print(f"❌ parse_user_intent error: {e}")

    # Fallback — ቁጥር ብቻ ካለ book አድርጎ ይቁጠር
    for word in user_message.split():
        cleaned = word.replace("#", "").strip()
        try:
            num = int(cleaned)
            if 1 <= num <= 100:
                return {"intent": "book", "number": num, "numbers": [num], "name": None, "amount": None}
        except ValueError:
            continue

    return {"intent": "other", "number": None, "numbers": [], "name": None, "amount": None}


def ask_addis_ai(prompt: str, context_info: str) -> str:
    """General AI reply — ጥያቄ ሲኖር"""
    system_context = f"""አንተ የሎተሪ bot አስተዳዳሪ ነህ። አማርኛ ብቻ ተናገር። አጭር፣ ግልጽ እና ሰብዓዊ መልስ ስጥ። emoji ተጠቀም።

የአሁን ሁኔታ:
{context_info}

ህጎቸ እና አስተሳሰቦች:
- ክፍያ ጥያቄ → CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729
- ባለፈ/ትላንት ጨዋታ ቅሬታ → "ያ ጨዋታ ተጠናቋል። ይህ አዲስ ጨዋታ ነው። እንደገና መመዝገብ ይቻላል!" በለ
- slot ለምን አልተያዘም ጥያቄ → slot ሁኔታ ተጠቅምህ አስረዳ (ሂሳብ አልተረጋገጠም፣ ቀደም ሌላ ሰው ይዞታል፣ etc)
- ሰው ተበሳጭቶ ቢነግርህ → ይቅርታ ጠይቅ፣ ያስረዳ፣ መፍትሔ ስጥ
- ሌላ ጥያቄ → ጨዋ እና አጭር መልስ"""

    full_prompt = f"{system_context}\n\nተጠቃሚ: {prompt}"

    try:
        response = requests.post(
            "https://api.addisassistant.com/api/v1/chat_generate",
            headers={
                "x-api-key": get_next_key(),
                "Content-Type": "application/json"
            },
            json={
                "model": "Addis-፩-አሌፍ",
                "prompt": full_prompt,
                "target_language": "am",
                "generation_config": {
                    "temperature": 0.7,
                    "maxOutputTokens": 300
                }
            },
            timeout=30
        )
        data = response.json()
        inner = data.get("data", data)
        result = inner.get("response_text", None)
        if not result:
            print(f"❌ Addis AI empty response: {data}")
            return "❌ መልስ ማምጣት አልተቻለም።"
        print(f"✅ Addis AI OK: {result[:50]}")
        return result
    except Exception as e:
        print(f"❌ Addis AI error: {e}")
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

    # ነፃ slot ቁጥሮች ዝርዝር
    free_numbers = [s["numbers"][0] for s in data["slots"].values() if not s["owner"]]

    # የያዙ ሰዎች ዝርዝር
    taken_list = []
    for s in data["slots"].values():
        if s["owner"]:
            paid_mark = "✅" if s["paid"] else "⏳"
            taken_list.append(f"{s['numbers'][0]}-{s['numbers'][4]} ({s['first_name']} {paid_mark})")

    context_info = (
        f"የሎተሪ ሁኔታ:\n"
        f"- ተይዘዋል: {filled}/20 slots\n"
        f"- ቀርተዋል: {free} slots\n"
        f"- ነፃ ቁጥሮች (የ slot መጀመሪያ): {free_numbers}\n"
        f"- የያዙ slots: {', '.join(taken_list) if taken_list else 'ማንም የለም'}\n"
        f"\n"
        f"የሎተሪ ዋጋ: 400 ብር (ግማሽ 200 ብር)\n"
        f"ሽልማቶች: 1ኛ 5000 ብር 🥇, 2ኛ 1000 ብር 🥈, 3ኛ 400 ብር 🥉\n"
        f"ክፍያ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729"
    )

    # ── AI intent parse ──
    parsed = parse_user_intent(user_text, sender_first_name)
    intent = parsed.get("intent", "other")
    number = parsed.get("number", None)
    ai_name = parsed.get("name", None)

    # ስም — AI ከparsed ካለ ይጠቀማል፣ ካልሆነ sender ስም
    display_name = ai_name if ai_name else sender_first_name
    ai_amount = parsed.get("amount", None)
    numbers_list = parsed.get("numbers", [])
    # AI extract ያደርጋል — fallback አያስፈልግም
    if not numbers_list and number:
        numbers_list = [number]

    # ቁጥር ካለ slot status context ውስጥ ጨምር
    if number and 1 <= number <= 100:
        slot_id, slot = get_slot_by_number(number, data)
        if slot:
            nums = slot["numbers"]
            if slot["owner"]:
                paid_status = "ክፍያ ተረጋግጧል ✅" if slot["paid"] else "ክፍያ ገና አልተረጋገጠም ⏳"
                context_info += (
                    f"። ቁጥር {number} ያለበት slot ({nums[0]}-{nums[4]}) "
                    f"በ {slot['first_name']} ተይዟል — {paid_status}"
                )
            else:
                context_info += (
                    f"። ቁጥር {number} ያለበት slot ({nums[0]}-{nums[4]}) ነፃ ነው 🔓"
                )

    print(f"📩 '{user_text}' → intent={intent}, number={number}, name={display_name}")

    if intent == "payment":
        user_id = str(update.effective_user.id)
        if "payments" not in data:
            data["payments"] = {}

        # ብር መጠን ማወቅ — AI amount ወይም ቁጥር ብቻ ሲፅፍ slot count × 400
        amount = 0
        if ai_amount:
            try:
                amount = int(ai_amount)
            except:
                amount = 0

        if amount == 0 and number and 1 <= number <= 100:
            # ቁጥር ብቻ ከሆነ → 400 ብር per slot
            amount = 400

        if amount > 0:
            prev = data["payments"].get(user_id, 0)
            total = prev + amount
            data["payments"][user_id] = total
            save_data(data)

            await update.message.reply_text(
                f"እሺ {total:,} ገቢ 🙏\nአስተዳዳሪው ያረጋግጣል — board ላይ ማየት ይችላሉ።"
            )
        else:
            await update.message.reply_text("❌ የብር መጠን ማወቅ አልተቻለም። ደግሞ ይሞክሩ።")
        return

    if intent == "past_complaint":
        reply = ask_addis_ai(user_text, context_info)
        await update.message.reply_text(reply)
        return

    if intent == "cancel" and number and 1 <= number <= 100:
        slot_id, slot = get_slot_by_number(number, data)

        if slot is None:
            await update.message.reply_text(f"❌ ቁጥር {number} አልተገኘም።")
            return

        if not slot["owner"]:
            await update.message.reply_text(f"❌ ቁጥር {number} ገና አልተያዘም።")
            return

        # የያዘው ሰው ወይም admin ብቻ ሊሰርዝ ይችላል
        if slot["owner"] != update.effective_user.id and update.effective_user.id != ADMIN_TELEGRAM_ID:
            await update.message.reply_text("❌ ይህ slot ያንተ/ያንቺ አይደለም።")
            return

        owner_name = slot["first_name"]
        data["slots"][slot_id]["owner"] = None
        data["slots"][slot_id]["first_name"] = None
        data["slots"][slot_id]["paid"] = False
        save_data(data)

        await update_lottery_message(context.bot, data)
        await update.message.reply_text(
            f"🗑️ {owner_name} — ቁጥር {number} slot ተሰርዟል።\nቁጥሩ አሁን ነፃ ነው! 🔓"
        )
        return

    if intent == "book" and numbers_list:
        booked = []
        already_taken = []
        not_found = []

        for num in numbers_list:
            slot_id, slot = get_slot_by_number(num, data)
            if slot is None:
                not_found.append(num)
            elif slot["owner"]:
                already_taken.append((num, slot["first_name"]))
            else:
                data["slots"][slot_id]["owner"] = update.effective_user.id
                data["slots"][slot_id]["first_name"] = display_name
                booked.append(slot)

        if booked or already_taken or not_found:
            save_data(data)

        reply_parts = []

        if booked:
            total_price = len(booked) * 400
            all_nums = []
            for s in booked:
                all_nums += s["numbers"]
            nums_str = "  ".join([f"{n:02d}#" for n in all_nums])
            reply_parts.append(
                f"✅ {display_name} ቁጥሮቻቸው:\n{nums_str}\n\n"
                f"💰 {total_price} ብር ከፍለህ/ሽ slot ህን/ሽን አረጋግጥ!\n\n"
                f"🏦 CBE: 1000641057146\n"
                f"🏦 አዋሽ: 01335630641400\n"
                f"🏦 ዳሽን: 5389857825011\n"
                f"📱 ቴሌ ብር: 0952346729"
            )
            await update_lottery_message(context.bot, data)

        if already_taken:
            taken_str = ", ".join([f"{n} (በ {name})" for n, name in already_taken])
            free_slots = [s for s in data["slots"].values() if not s["owner"]]
            free_nums = [s["numbers"][0] for s in free_slots[:5]]
            reply_parts.append(f"⚠️ እነዚህ ቁጥሮች ተይዘዋል: {taken_str}\nነፃ ቁጥሮች: {free_nums}")

        if not_found:
            reply_parts.append(f"❌ እነዚህ ቁጥሮች አልተገኙም: {not_found}")

        if reply_parts:
            await update.message.reply_text("\n\n".join(reply_parts))

        filled = sum(1 for s in data["slots"].values() if s["owner"])
        if filled == 20:
            await update.message.reply_text(
                "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! ትዕግስት አድርጉ... 🎰"
            )

    elif intent == "question":
        reply = ask_addis_ai(user_text, context_info)
        await update.message.reply_text(reply)

    else:
        # other — AI ይመልሳል
        reply = ask_addis_ai(user_text, context_info)
        await update.message.reply_text(reply)

# ==================== KEEP ALIVE ====================
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading


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
