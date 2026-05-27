import os
import json
import requests
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADDIS_AI_API_KEY = os.getenv("ADDIS_AI_API_KEY")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DATA_FILE = "lottery_data.json"

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
    """
    FIX: ቁጥር ሲያዝ ስም የመጀመሪያ ቁጥር ላይ ብቻ ይፃፋል
    ለምሳሌ:
    06# ብኒያም ✅
    07#
    08#
    09#
    10#
    """
    groups = []
    for slot_id, slot in data["slots"].items():
        lines = []
        for idx, num in enumerate(slot["numbers"]):
            if idx == 0 and slot["owner"]:
                # ስም እና paid mark የመጀመሪያ ቁጥር ላይ ብቻ
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
def ask_addis_ai(prompt: str, context_info: str) -> str:
    """Addis AI — አማርኛ native support"""
    system_context = f"""አንተ የሎተሪ bot ነህ። አማርኛ ብቻ ተናገር። አጭር እና ግልጽ መልስ ስጥ። emoji ተጠቀም።

የአሁን ሁኔታ: {context_info}

ህጎች:
- ክፍያ ጥያቄ → CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729
- ሌላ ጥያቄ → ጨዋ እና አጭር መልስ"""

    full_prompt = f"{system_context}\n\nተጠቃሚ: {prompt}"

    try:
        response = requests.post(
            "https://api.addisassistant.com/api/v1/chat_generate",
            headers={
                "x-api-key": ADDIS_AI_API_KEY,
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
            timeout=15
        )
        data = response.json()
        return data.get("response_text", "❌ መልስ ማምጣት አልተቻለም።")
    except Exception as e:
        print(f"Addis AI error: {e}")
        return "❌ AI አገልግሎት ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።"

def is_booking_intent(user_message: str, number: int) -> bool:
    """Addis AI — ሰው ቁጥር ሊይዝ ፈልጓል? YES ወይም NO"""
    try:
        response = requests.post(
            "https://api.addisassistant.com/api/v1/chat_generate",
            headers={
                "x-api-key": ADDIS_AI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "Addis-፩-አሌፍ",
                "prompt": (
                    f"ይህ ሰው ቁጥር {number}ን በሎተሪ ሊይዝ/ሊመዘገብ ፈልጓል?\n"
                    f"መልእክት: '{user_message}'\n"
                    f"YES ወይም NO ብቻ መልስ።"
                ),
                "target_language": "am",
                "generation_config": {
                    "temperature": 0.1,
                    "maxOutputTokens": 5
                }
            },
            timeout=10
        )
        data = response.json()
        answer = data.get("response_text", "NO").strip().upper()
        return "YES" in answer
    except Exception as e:
        print(f"Intent check error: {e}")
        return False

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
    first_name = update.effective_user.first_name or "ተጠቃሚ"
    data = load_data()

    # ቁጥር ተፅፏል?
    number = None
    for word in user_text.split():
        cleaned = word.replace("#", "").strip()
        try:
            number = int(cleaned)
            break
        except ValueError:
            continue

    if number and 1 <= number <= 100:
        slot_id, slot = get_slot_by_number(number, data)

        if slot["owner"]:
            # Taken
            free_slots = [s for s in data["slots"].values() if not s["owner"]]
            free_numbers = [s["numbers"][0] for s in free_slots[:5]]
            context_info = f"ቁጥር {number} አስቀድሞ በ {slot['first_name']} ተይዟል። ነፃ ቁጥሮች: {free_numbers}"
            reply = ask_addis_ai(user_text, context_info)
            await update.message.reply_text(reply)
        else:
            # Intent check
            if not is_booking_intent(user_text, number):
                filled = sum(1 for s in data["slots"].values() if s["owner"])
                free = 20 - filled
                context_info = f"ሞልቷል: {filled}/20 slots። ነፃ slots: {free}"
                reply = ask_addis_ai(user_text, context_info)
                await update.message.reply_text(reply)
                return

            # ነፃ ነው — ይያዛል
            data["slots"][slot_id]["owner"] = update.effective_user.id
            data["slots"][slot_id]["first_name"] = first_name
            save_data(data)

            nums = slot["numbers"]
            reply = (
                f"✅ {first_name} ቁጥሮችህ/ሽ:\n"
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
    else:
        # ቁጥር አይደለም
        filled = sum(1 for s in data["slots"].values() if s["owner"])
        free = 20 - filled
        context_info = f"ሞልቷል: {filled}/20 slots። ነፃ slots: {free}"
        reply = ask_addis_ai(user_text, context_info)
        await update.message.reply_text(reply)

# ==================== MAIN ====================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start_lottery", start_lottery))
    app.add_handler(CommandHandler("paid", mark_paid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot እየሰራ ነው...")
    app.run_polling()

if __name__ == "__main__":
    main()
