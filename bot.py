import os
import json
import asyncio
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from groq import Groq

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
GROQ_API_KEY = "YOUR_GROQ_API_KEY"
ADMIN_TELEGRAM_ID = 123456789  # አንተ Telegram ID
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

groq_client = Groq(api_key=GROQ_API_KEY)

# ==================== DATA MANAGEMENT ====================
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # Initialize fresh lottery data
    slots = {}
    for i in range(1, 21):  # 20 slots
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
    """ቁጥሩ የትኛው slot ውስጥ እንደሆነ ያወቃል"""
    for slot_id, slot in data["slots"].items():
        if number in slot["numbers"]:
            return slot_id, slot
    return None, None

def build_numbers_text(data: dict) -> str:
    """ሁሉም ቁጥሮች ያለ owner ይፃፋሉ"""
    lines = []
    for slot_id, slot in data["slots"].items():
        first_num = slot["numbers"][0]
        last_num = slot["numbers"][-1]
        if slot["owner"]:
            name = slot["first_name"]
            paid_mark = "✅" if slot["paid"] else "⏳"
            lines.append(f"{first_num:02d}# - {last_num:02d}# {name} {paid_mark}")
        else:
            for num in slot["numbers"]:
                lines.append(f"{num:02d}#")
    return "\n".join(lines)

def build_full_message(data: dict) -> str:
    numbers_text = build_numbers_text(data)
    return LOTTERY_TEMPLATE.format(numbers=numbers_text)

# ==================== GROQ AI ====================
def ask_groq(user_message: str, context_info: str) -> str:
    system_prompt = f"""አንተ የቴሌግራም lottery bot ነህ። አማርኛ ብቻ ተናገር።

የአሁን lottery ሁኔታ:
{context_info}

ህጎች:
- ሰው ቁጥር ሲጠይቅ (ለምሳሌ "53" ወይም "53 ያዝልኝ") → slot ያስሰላል እና ያረጋግጣል
- ቁጥሩ ከ 1-100 ውጪ ከሆነ → "ከ 1 እስከ 100 ያለ ቁጥር ብቻ ምረጥ" በል
- ቁጥሩ taken ከሆነ → ነፃ ቁጥሮችን ጠቁም
- ክፍያ ጥያቄ ሲመጣ → የባንክ ቁጥሮቹን ስጥ (CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ ብር 0952346729)
- ሌላ ጥያቄ ሲመጣ → ጨዋ እና አጭር መልስ ስጥ
- ሁሌም emoji ተጠቀም

አጭር እና ግልጽ መልስ ስጥ።"""

    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ],
        max_tokens=300
    )
    return response.choices[0].message.content

# ==================== BOT HANDLERS ====================
async def start_lottery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin ብቻ lottery ይጀምራል /start_lottery"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("❌ ይህ command ለ admin ብቻ ነው።")
        return

    data = load_data()
    # Reset data
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
    """/paid <slot_id> - Admin ክፍያ ሲያረጋግጥ"""
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return

    if not context.args:
        await update.message.reply_text("አጠቃቀም: /paid <ቁጥር>\nለምሳሌ: /paid 53")
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

    # Update message
    await update_lottery_message(context.bot, data)
    await update.message.reply_text(f"✅ {slot['first_name']} ክፍያ ተረጋግጧል!")

async def update_lottery_message(bot: Bot, data: dict):
    """Lottery message ያዘምናል"""
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
    """ሁሉም messages ይቀበላል"""
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    first_name = update.effective_user.first_name or "ተጠቃሚ"
    data = load_data()

    # ቁጥር ተፅፏል?
    number = None
    for word in user_text.split():
        try:
            number = int(word)
            break
        except ValueError:
            continue

    if number and 1 <= number <= 100:
        slot_id, slot = get_slot_by_number(number, data)

        if slot["owner"]:
            # Taken - Groq ያስተናግዳል
            free_slots = [s for s in data["slots"].values() if not s["owner"]]
            free_numbers = [s["numbers"][0] for s in free_slots[:5]]
            context_info = f"ቁጥር {number} አስቀድሞ በ {slot['first_name']} ተይዟል። ነፃ ቁጥሮች: {free_numbers}"
            reply = ask_groq(user_text, context_info)
            await update.message.reply_text(reply)
        else:
            # ነፃ ነው - ይያዛል
            data["slots"][slot_id]["owner"] = update.effective_user.id
            data["slots"][slot_id]["first_name"] = first_name
            save_data(data)

            nums = slot["numbers"]
            reply = (
                f"✅ {first_name} ቁጥሮችህ:\n"
                f"{nums[0]:02d}# {nums[1]:02d}# {nums[2]:02d}# {nums[3]:02d}# {nums[4]:02d}#\n\n"
                f"💰 400 ብር ከፍለህ slot ህን አረጋግጥ!\n"
                f"CBE: 1000641057146\n"
                f"አዋሽ: 01335630641400\n"
                f"ዳሽን: 5389857825011\n"
                f"ቴሌ ብር: 0952346729"
            )
            await update.message.reply_text(reply)

            # Message ያዘምናል
            await update_lottery_message(context.bot, data)

            # ሁሉም ተሞልቷ?
            filled = sum(1 for s in data["slots"].values() if s["owner"])
            if filled == 20:
                await update.message.reply_text(
                    "🎉 ሁሉም slots ተሞልቷል! ዕጣ ቅርብ ነው! ትዕግስት አድርጉ... 🎰"
                )
    else:
        # ቁጥር አይደለም - Groq ያስተናግዳል
        filled = sum(1 for s in data["slots"].values() if s["owner"])
        free = 20 - filled
        context_info = f"ሞልቷል: {filled}/20 slots። ነፃ slots: {free}"
        reply = ask_groq(user_text, context_info)
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
