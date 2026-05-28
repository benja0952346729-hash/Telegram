import os
import re
import json
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from google import genai
from google.genai import types

# ==================== CONFIG ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
DATA_FILE = "lottery_data.json"

# ==================== GEMINI MULTI-KEY ROTATION ====================
GEMINI_KEYS = [
    os.getenv("GEMINI_API_KEY_1"),
    os.getenv("GEMINI_API_KEY_2"),
    os.getenv("GEMINI_API_KEY_3"),
    os.getenv("GEMINI_API_KEY_4"),
    os.getenv("GEMINI_API_KEY_5"),
    os.getenv("GEMINI_API_KEY_6"),
    os.getenv("GEMINI_API_KEY_7"),
    os.getenv("GEMINI_API_KEY_8"),
    os.getenv("GEMINI_API_KEY_9"),
    os.getenv("GEMINI_API_KEY_10"),
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
    # half
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
        t = slot["type"]
        if t is None:
            lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ነፃ")
        elif t == "full":
            paid = "ከፍሏል ✅" if slot["p1_paid"] else "ገና ✅ አልከፈለም ⏳"
            lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ሙሉ | {slot['p1_name']} (ID:{slot['p1_id']}) | {paid}")
        elif t == "half":
            p1_paid = "✅" if slot["p1_paid"] else "⏳"
            if slot["p2_id"] is None:
                lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ግማሽ | p1={slot['p1_name']} (ID:{slot['p1_id']}) {p1_paid} | p2=ክፍት")
            else:
                p2_paid = "✅" if slot["p2_paid"] else "⏳"
                lines.append(f"Slot {slot_id} ({nums[0]}-{nums[-1]}): ግማሽ ሙሉ | p1={slot['p1_name']} (ID:{slot['p1_id']}) {p1_paid} | p2={slot['p2_name']} (ID:{slot['p2_id']}) {p2_paid}")

    filled = sum(1 for s in data["slots"].values() if is_slot_full_booked(s))
    summary = f"\n--- ጠቅላላ: {filled}/20 slots ሞልቷል ---\n"
    return summary + "\n".join(lines)

# ==================== GEMINI AI CALL (አዲሱ SDK) ====================

def gemini_call(prompt: str, max_tokens: int = 500, temperature: float = 0.2) -> str:
    for attempt in range(2):
        key = get_next_gemini_key()
        key_preview = key[:8] + "..." if key else "None"
        try:
            client = genai.Client(api_key=key)
            response = client.models.generate_content(
                model="gemini-2.0-flash-lite",
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
                reason = "❌ Quota ተጠቀሰ — key limit ደረሰ"
            elif "PERMISSION_DENIED" in err:
                reason = "❌ Permission የለም — key ተሰናክሏል"
            elif "UNAVAILABLE" in err or "503" in err:
                reason = "❌ Gemini server አይሰራም — ቆይቶ ሞክር"
            elif "timeout" in err.lower():
                reason = "❌ Timeout — Gemini ዘግይቷል"
            else:
                reason = f"❌ ያልታወቀ error: {err}"
            print(f"⚠️ Gemini attempt {attempt+1} (key: {key_preview}): {reason}")
    print("🔴 Gemini ሙሉ በሙሉ አልሰራም — ሁሉም attempts ከሸፉ")
    return ""

# ==================== AI BRAIN ====================

def ai_brain(user_message: str, user_id: int, user_name: str, full_state: str) -> dict:
    prompt = f"""አንተ የሎተሪ ስርዓት ሙሉ admin brain ነህ። ሁሉንም ውሳኔ አንተ ትሰጣለህ።
Bot worker ብቻ ነው — አንተ የሰጠህውን action ያስፈጽማል።

========= የሎተሪ ህጎች =========
- ሎተሪ ለ 20 ሰው ብቻ (slots 1-20)
- እያንዳንዱ slot 5 ቁጥሮች አሉት (1-5, 6-10, ... 96-100)
- ሙሉ = 400 ብር (አንድ ሰው slot ሙሉ ይይዛል)
- ግማሽ = 200 ብር (ሁለት ሰዎች አንድ slot ይካፈላሉ)
- ሽልማት: 1ኛ=5000ብር, 2ኛ=1000ብር, 3ኛ=400ብር
- ክፍያ: CBE 1000641057146, አዋሽ 01335630641400, ዳሽን 5389857825011, ቴሌ 0952346729

========= slot ለይዝ ምልክቶች =========
ሙሉ: "76" → slot ያዘ (ቁጥር 76 ያለበት slot)
ግማሽ: "76+" ወይም "76 ግማሽ" ወይም "76 200" → ግማሽ slot ፍለጋ

========= የአሁን ሎተሪ ሁኔታ (ሙሉ) =========
{full_state}

========= ተጠቃሚ መረጃ =========
User ID: {user_id}
User Name: {user_name}
መልእክት: "{user_message}"

========= ውሳኔ አሰጣጥ ህጎች =========
1. ሰው ቁጥር ከፃፈ → slot ፈልግ (ቁጥሩ የትኛው slot ውስጥ ነው?)
2. Slot ነፃ ከሆነ → book
3. Slot ሙሉ/ተይዞ ከሆነ → ሌላ ነፃ slot ምረጥ ወይም ለ user ንገረው
4. ሰው ቀድሞ ያዘ → "ቀድሞ ይዘሃል" ንገረው
5. ክፍያ ያልተረጋገጠ slot ክፍት አይደለም
6. Admin ብቻ mark_paid ሊጠቀም ይችላል
7. ግማሽ + "half/ግማሽ/+/200" ምልክት ካለ → book_half_p1 ወይም book_half_p2
8. reply በ አማርኛ ብቻ፣ አጭር፣ emoji ጋር

========= OUTPUT FORMAT =========
JSON ብቻ ስጥ። ምንም ሌላ ቃል አታስቀምጥ። markdown backticks አታስቀምጥ።

ምሳሌዎች:
{{"action":"book_full","number":76,"name":"አበበ","reply":"✅ 76# ተይዟል! 400 ብር ክፈል 🙏"}}
{{"action":"book_half_p1","number":76,"name":"አበበ","reply":"✅ 76# ግማሽ ተይዟል (200ብር) 🤝"}}
{{"action":"book_half_p2","number":76,"name":"አበበ","reply":"✅ ቀላቀለ! Slot ሙሉ ሆኗል 🎉"}}
{{"action":"cancel","number":76,"reply":"✅ 76# ተሰርዟል።"}}
{{"action":"mark_paid","number":76,"which":1,"reply":"✅ ክፍያ ተረጋግጧል!"}}
{{"action":"reply","reply":"❓ ምን ልርዳህ?"}}

አሁን JSON ብቻ ስጥ:"""

    raw = gemini_call(prompt, max_tokens=300, temperature=0.1)
    print(f"🧠 AI Brain raw: {raw}")

    try:
        clean = re.sub(r'```(?:json)?', '', raw).strip()
        match = re.search(r'\{.*?\}', clean, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print(f"❌ AI Brain parse error: {e}")

    return {"action": "reply", "reply": "❌ ጊዜያዊ ችግር አለ። ቆይተህ ሞክር።"}


# ==================== BOT EXECUTOR (Worker) ====================

def execute_action(action_data: dict, user_id: int, data: dict) -> dict:
    action = action_data.get("action", "reply")
    reply  = action_data.get("reply", "")
    number = action_data.get("number")
    name   = action_data.get("name", "ተጠቃሚ")
    which  = action_data.get("which", 1)
    changed = False

    if action in ("book_full", "book_half_p1", "book_half_p2") and number:
        slot_id, slot = get_slot_by_number(number, data)
        if slot_id:
            if action == "book_full" and slot["type"] is None:
                data["slots"][slot_id].update({
                    "type": "full",
                    "p1_id": user_id, "p1_name": name, "p1_paid": False,
                    "p2_id": None, "p2_name": None, "p2_paid": False,
                })
                changed = True

            elif action == "book_half_p1" and slot["type"] is None:
                data["slots"][slot_id].update({
                    "type": "half",
                    "p1_id": user_id, "p1_name": name, "p1_paid": False,
                    "p2_id": None, "p2_name": None, "p2_paid": False,
                })
                changed = True

            elif action == "book_half_p2" and slot["type"] == "half" and slot["p2_id"] is None:
                data["slots"][slot_id].update({
                    "p2_id": user_id, "p2_name": name, "p2_paid": False,
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
    data["chat_id"] = update.effective_chat.id
    save_data(data)
    await update.message.reply_text("✅ ሎተሪ ጀምሯል!")


async def mark_paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /paid 76    → p1
    /paid 76 2  → p2
    Admin ብቻ
    """
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

    data = load_data()
    action_data = {"action": "mark_paid", "number": number, "which": which, "reply": ""}
    result = execute_action(action_data, update.effective_user.id, data)

    if result["changed"]:
        save_data(result["data"])
        await update_lottery_message(context.bot, result["data"])
        slot_id, slot = get_slot_by_number(number, result["data"])
        name = slot["p2_name"] if which == 2 else slot["p1_name"]
        await update.message.reply_text(f"✅ {name} ክፍያ ተረጋግጧል!")
    else:
        await update.message.reply_text("❌ Slot አልተገኘም ወይም ተሳስቷል")


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

    raw_text   = update.message.text.strip()
    user_id    = update.effective_user.id
    user_name  = update.effective_user.first_name or "ተጠቃሚ"
    data       = load_data()

    full_state = build_full_state_for_ai(data)

    print(f"📩 {user_name} ({user_id}): '{raw_text}'")

    action_data = ai_brain(raw_text, user_id, user_name, full_state)
    print(f"🧠 Action: {action_data}")

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
        print("❌ ምንም Gemini API key አልተገኘም! .env ፋይሉን ፈትሽ።")
        return

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

    async def error_handler(update, context):
        print(f"🔴 ERROR: {context.error}")
        import traceback
        traceback.print_exc()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start_lottery", start_lottery))
    app.add_handler(CommandHandler("paid", mark_paid_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print(f"✅ {len(GEMINI_KEYS)} Gemini API keys loaded")
    print("✅ Bot እየሰራ ነው... (Gemini AI Brain Mode)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
