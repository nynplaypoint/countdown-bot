import os
import re
import json
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TIMEZONE  = timezone(timedelta(hours=6))
DATA_FILE = "countdowns.json"

AWAIT_NAME     = 0
AWAIT_DATETIME = 1

# ── Persistence ───────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

countdowns: dict = load_data()

def get_items(chat_id: int) -> list:
    return countdowns.get(str(chat_id), [])

def save_items(chat_id: int, items: list):
    countdowns[str(chat_id)] = items
    save_data(countdowns)

def get_expired(chat_id: int) -> list:
    return countdowns.get(f"{chat_id}_expired", [])

def save_expired(chat_id: int, items: list):
    countdowns[f"{chat_id}_expired"] = items
    save_data(countdowns)

def move_expired(chat_id: int):
    items   = get_items(chat_id)
    expired = get_expired(chat_id)
    active  = []
    changed = False
    for item in items:
        dt = datetime.fromisoformat(item["target"])
        if (dt - now_local()).total_seconds() <= 0:
            expired.append(item)
            changed = True
        else:
            active.append(item)
    if changed:
        save_items(chat_id, active)
        save_expired(chat_id, expired)

def next_id(items: list) -> int:
    return max((i["id"] for i in items), default=0) + 1

# ── Time helpers ──────────────────────────────────────────────────────────────
def now_local() -> datetime:
    return datetime.now(TIMEZONE)

def format_delta(seconds: float) -> str:
    if seconds <= 0:
        return "Time's up!"
    total   = int(seconds)
    days    = total // 86400
    hours   = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    secs    = total % 60
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

def fmt_hms(seconds: int) -> str:
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if d:
        return f"{d}d {h:02d}:{m:02d}:{s:02d}"
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# ── Parse helpers ─────────────────────────────────────────────────────────────
def parse_cnt_args(args: list) -> datetime | None:
    text   = " ".join(args)
    time_m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if not time_m:
        return None
    hour, minute = int(time_m.group(1)), int(time_m.group(2))
    if hour > 23 or minute > 59:
        return None
    rest = text[:time_m.start()] + text[time_m.end():]
    nums = re.findall(r"\d+", rest)
    now  = now_local()
    if not nums:
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if len(nums) >= 3:
        nums_int = [int(n) for n in nums[:3]]
        year     = next((n for n in nums_int if n > 1000), None)
        rem      = [n for n in nums_int if n != year]
        if year is None or len(rem) < 2:
            return None
        rem.sort()
        month, day = rem[0], rem[1]
        try:
            return datetime(year, month, day, hour, minute, tzinfo=TIMEZONE)
        except ValueError:
            return None
    return None

def parse_time_only(text: str) -> datetime | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if h > 23 or mn > 59:
        return None
    return now_local().replace(hour=h, minute=mn, second=0, microsecond=0)

def parse_duration(args: list) -> int | None:
    total   = 0
    matched = False
    pattern = re.compile(r"(\d+)(y|mm|d|h|m|s)", re.IGNORECASE)
    for m in pattern.finditer(" ".join(args)):
        val, unit = int(m.group(1)), m.group(2).lower()
        if unit == "y":    total += val * 365 * 86400
        elif unit == "mm": total += val * 30  * 86400
        elif unit == "d":  total += val * 86400
        elif unit == "h":  total += val * 3600
        elif unit == "m":  total += val * 60
        elif unit == "s":  total += val
        matched = True
    return total if matched and total > 0 else None

# ── Scheduled checker — fires every 10s to notify finished countdowns ─────────
notified_ids: set = set()  # track already-notified item ids

async def check_finished(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every 10 seconds. Sends notification when a countdown finishes."""
    for key, val in list(countdowns.items()):
        if key.endswith("_expired") or not isinstance(val, list):
            continue
        chat_id = int(key)
        items   = get_items(chat_id)
        expired = get_expired(chat_id)
        active  = []
        for item in items:
            dt   = datetime.fromisoformat(item["target"])
            diff = (dt - now_local()).total_seconds()
            if diff <= 0:
                uid = f"{chat_id}:{item['id']}"
                expired.append(item)
                if uid not in notified_ids:
                    notified_ids.add(uid)
                    try:
                        await ctx.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"🔔 *{item['name']}* is done\\!\n"
                                f"📅 {datetime.fromisoformat(item['target']).strftime('%d %b %Y %H:%M')} \\(BD\\)"
                            ),
                            parse_mode="MarkdownV2",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton(
                                    "🗑 Delete from history",
                                    callback_data=f"delexp:{item['id']}"
                                )
                            ]])
                        )
                    except Exception:
                        pass
            else:
                active.append(item)
        if len(active) != len(items):
            save_items(chat_id, active)
            save_expired(chat_id, expired)

# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Countdown Bot*\n\nUse /help to see all commands.",
        parse_mode="Markdown"
    )

# ── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Commands & Usage*\n\n"
        "*➕ /cnt — Set a countdown*\n"
        "• `/cnt` — guided \\(asks name then date\\)\n"
        "• `/cnt 2026 01 04 23:03` — date inline, asks name\n"
        "• `/cnt 23:03 2026 01 04` — any order works\n"
        "• `/cnt 23:03` — today at 23:03, asks name\n\n"
        "*📋 /list — Active countdowns*\n"
        "Only shows countdowns that haven't expired yet\\.\n\n"
        "*🕓 /previous — Expired countdowns*\n"
        "Shows countdowns that already finished\\.\n"
        "• `/previous clear` — delete all expired\n\n"
        "*🗑 /del — Delete active countdown*\n"
        "• `/del` — tap a button to pick one\n"
        "• `/del list 1` — delete \\#1 from list\n\n"
        "*⏱ /live — Live countdown timer*\n"
        "• `/live 1d 2h 3m 4s` — duration\n"
        "• `/live till 16:56` — until clock time today\n"
        "Units: `y`\\=year `mm`\\=month `d`\\=day `h`\\=hour `m`\\=min `s`\\=sec\n\n"
        "When a countdown finishes, the bot sends a notification with a delete button\\.\n\n"
        "_All times are Bangladesh time \\(UTC\\+6\\)_"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")

# ── /cnt ─────────────────────────────────────────────────────────────────────
async def cmd_cnt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if args:
        dt = parse_cnt_args(args)
        if dt:
            ctx.user_data["cnt_target"] = dt.isoformat()
            await update.message.reply_text(
                f"📅 Target: *{dt.strftime('%d %b %Y %H:%M')}* (BD)\n\n"
                "What's the name for this countdown?\nOr /cancel to abort.",
                parse_mode="Markdown"
            )
            return AWAIT_NAME
        else:
            await update.message.reply_text(
                "❌ Couldn't parse. Try: `/cnt 2026 01 04 23:03` or `/cnt 23:03`",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

    ctx.user_data.pop("cnt_target", None)
    await update.message.reply_text("➕ *New Countdown*\n\nWhat's the name?", parse_mode="Markdown")
    return AWAIT_NAME

async def recv_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name    = update.message.text.strip()
    chat_id = update.effective_chat.id

    if "cnt_target" in ctx.user_data:
        dt    = datetime.fromisoformat(ctx.user_data.pop("cnt_target"))
        items = get_items(chat_id)
        item  = {"id": next_id(items), "name": name, "target": dt.isoformat()}
        items.append(item)
        save_items(chat_id, items)
        diff = (dt - now_local()).total_seconds()
        await update.message.reply_text(
            f"✅ *{name}* saved!\n"
            f"📅 `{dt.strftime('%d %b %Y %H:%M')}` (BD)\n"
            f"⏳ *{format_delta(diff)}* remaining\n\nUse /list to view all.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    ctx.user_data["cnt_name"] = name
    await update.message.reply_text(
        f"Got it: *{name}*\n\n"
        "Now send the date & time:\n"
        "`YYYY-MM-DD HH:MM` or `DD/MM/YYYY HH:MM`\n"
        "_e.g. 2026-01-04 23:03_\n\nOr /cancel to abort.",
        parse_mode="Markdown"
    )
    return AWAIT_DATETIME

async def recv_datetime_guided(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    fmts = [
        "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M", "%Y/%m/%d %H:%M",
        "%d %b %Y %H:%M", "%d %B %Y %H:%M",
    ]
    dt = None
    for fmt in fmts:
        try:
            dt = datetime.strptime(update.message.text.strip(), fmt).replace(tzinfo=TIMEZONE)
            break
        except ValueError:
            continue
    if not dt:
        await update.message.reply_text("❌ Wrong format. Try: `2026-01-04 23:03`", parse_mode="Markdown")
        return AWAIT_DATETIME

    chat_id = update.effective_chat.id
    items   = get_items(chat_id)
    name    = ctx.user_data.get("cnt_name", "Countdown")
    item    = {"id": next_id(items), "name": name, "target": dt.isoformat()}
    items.append(item)
    save_items(chat_id, items)
    diff = (dt - now_local()).total_seconds()
    await update.message.reply_text(
        f"✅ *{name}* saved!\n"
        f"📅 `{dt.strftime('%d %b %Y %H:%M')}` (BD)\n"
        f"⏳ *{format_delta(diff)}* remaining\n\nUse /list to view all.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ── /list ─────────────────────────────────────────────────────────────────────
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items   = get_items(chat_id)
    # filter out expired inline (checker will handle notify)
    active = [i for i in items if (datetime.fromisoformat(i["target"]) - now_local()).total_seconds() > 0]

    live_timer_lines = []
    for key, job in list(quick_live_jobs.items()):
        if key.startswith(f"{chat_id}:"):
            remaining = int(job.data["end_time"] - now_local().timestamp())
            if remaining > 0:
                live_timer_lines.append(f"⏱ *Live Timer* — *{fmt_hms(remaining)}* remaining")

    if not active and not live_timer_lines:
        await update.message.reply_text(
            "📋 No active countdowns.\nUse /cnt to add one or /previous to see expired."
        )
        return

    lines    = ["📋 *Active Countdowns:*\n"]
    keyboard = []

    if active:
        lines.append("*— Scheduled —*")
        for i, item in enumerate(active, 1):
            dt   = datetime.fromisoformat(item["target"])
            diff = (dt - now_local()).total_seconds()
            lines.append(f"*{i}.* {item['name']}\n    ⏳ {format_delta(diff)}")
            keyboard.append([
                InlineKeyboardButton(f"🔴 Live · {item['name']}", callback_data=f"live:{item['id']}"),
                InlineKeyboardButton(f"🗑 #{i}", callback_data=f"del:{item['id']}"),
            ])

    if live_timer_lines:
        lines.append("\n*— Live Timers —*")
        for line in live_timer_lines:
            lines.append(line)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
    )

# ── /previous ─────────────────────────────────────────────────────────────────
async def cmd_previous(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args    = ctx.args

    if args and args[0].lower() == "clear":
        save_expired(chat_id, [])
        await update.message.reply_text("🗑 Cleared all expired countdowns.")
        return

    expired = get_expired(chat_id)
    if not expired:
        await update.message.reply_text("🕓 No expired countdowns yet.")
        return

    lines    = ["🕓 *Expired Countdowns:*\n"]
    keyboard = []
    for i, item in enumerate(expired, 1):
        dt = datetime.fromisoformat(item["target"])
        lines.append(f"*{i}.* {item['name']}\n    📅 {dt.strftime('%d %b %Y %H:%M')}")
        keyboard.append([
            InlineKeyboardButton(f"🗑 Delete #{i} · {item['name']}", callback_data=f"delexp:{item['id']}")
        ])

    keyboard.append([InlineKeyboardButton("🗑 Clear All", callback_data="delexp:all")])
    lines.append("\n_Tap a button to delete individually or clear all._")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ── /del ──────────────────────────────────────────────────────────────────────
async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    items   = [i for i in get_items(chat_id)
               if (datetime.fromisoformat(i["target"]) - now_local()).total_seconds() > 0]
    args    = ctx.args

    if not items:
        await update.message.reply_text("📋 No active countdowns to delete.")
        return

    if len(args) == 2 and args[0].lower() == "list":
        try:
            pos = int(args[1])
        except ValueError:
            await update.message.reply_text("❌ Use: `/del list 1`", parse_mode="Markdown")
            return
        if pos < 1 or pos > len(items):
            await update.message.reply_text(f"❌ No item #{pos}. You have {len(items)} countdown(s).")
            return
        removed = items.pop(pos - 1)
        save_items(chat_id, items)
        _stop_live_job(chat_id, removed["id"])
        await update.message.reply_text(f"🗑 *{removed['name']}* deleted.", parse_mode="Markdown")
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 {i}. {item['name']}", callback_data=f"del:{item['id']}")]
        for i, item in enumerate(items, 1)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="del:cancel")])
    await update.message.reply_text(
        "🗑 *Which one to delete?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

def _stop_live_job(chat_id: int, item_id: int):
    for key, job in list(live_jobs.items()):
        if key.startswith(f"{chat_id}:") and job.data.get("item_id") == item_id:
            job.schedule_removal()
            live_jobs.pop(key, None)
            break

# ── /live ─────────────────────────────────────────────────────────────────────
quick_live_jobs: dict = {}

async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "⏱ Usage:\n`/live 1d 2h 3m 4s` — duration\n`/live till 16:56` — until a clock time",
            parse_mode="Markdown"
        )
        return

    chat_id  = update.effective_chat.id
    end_time = None

    if args[0].lower() == "till" and len(args) >= 2:
        dt = parse_time_only(args[1])
        if not dt:
            await update.message.reply_text("❌ Use: `/live till 16:56`", parse_mode="Markdown")
            return
        end_time  = dt.timestamp()
        remaining = int(end_time - now_local().timestamp())
        if remaining <= 0:
            await update.message.reply_text(f"❌ *{args[1]}* has already passed today.", parse_mode="Markdown")
            return
    else:
        total_secs = parse_duration(args)
        if not total_secs:
            await update.message.reply_text(
                "❌ Can't parse. Try `/live 1h 30m` or `/live till 16:56`",
                parse_mode="Markdown"
            )
            return
        end_time = now_local().timestamp() + total_secs

    remaining = int(end_time - now_local().timestamp())
    msg = await update.message.reply_text(f"⏱ *{fmt_hms(remaining)}*", parse_mode="Markdown")
    job_key  = f"{chat_id}:{msg.message_id}"
    stop_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏹ Stop", callback_data=f"qlstop:{msg.message_id}")
    ]])
    await ctx.bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg.message_id, reply_markup=stop_btn)
    job = ctx.job_queue.run_repeating(
        quick_live_tick, interval=1, first=1,
        chat_id=chat_id, name=job_key,
        data={"msg_id": msg.message_id, "end_time": end_time, "job_key": job_key}
    )
    quick_live_jobs[job_key] = job

async def quick_live_tick(ctx: ContextTypes.DEFAULT_TYPE):
    d         = ctx.job.data
    chat_id   = ctx.job.chat_id
    remaining = int(d["end_time"] - now_local().timestamp())
    stop_btn  = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏹ Stop", callback_data=f"qlstop:{d['msg_id']}")
    ]])
    if remaining <= 0:
        ctx.job.schedule_removal()
        quick_live_jobs.pop(d["job_key"], None)
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=d["msg_id"],
                text="⏱ *00:00*\n\n🎉 Time's up!",
                parse_mode="Markdown", reply_markup=None
            )
        except Exception:
            pass
        # Send separate notification with sound
        await ctx.bot.send_message(
            chat_id=chat_id,
            text="🔔 *Live timer finished\\!* 🎉",
            parse_mode="MarkdownV2"
        )
        return
    try:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=d["msg_id"],
            text=f"⏱ *{fmt_hms(remaining)}*",
            parse_mode="Markdown", reply_markup=stop_btn
        )
    except Exception:
        ctx.job.schedule_removal()
        quick_live_jobs.pop(d["job_key"], None)

# ── Callback handler ──────────────────────────────────────────────────────────
live_jobs: dict = {}

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    data    = query.data
    await query.answer()

    # Delete active
    if data.startswith("del:"):
        val = data.split(":", 1)[1]
        if val == "cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            return
        item_id = int(val)
        items   = get_items(chat_id)
        target  = next((i for i in items if i["id"] == item_id), None)
        if not target:
            await query.edit_message_text("❌ Not found.")
            return
        items = [i for i in items if i["id"] != item_id]
        save_items(chat_id, items)
        _stop_live_job(chat_id, item_id)
        await query.edit_message_text(f"🗑 *{target['name']}* deleted.", parse_mode="Markdown")
        return

    # Delete expired (single or all)
    if data.startswith("delexp:"):
        val = data.split(":", 1)[1]
        if val == "all":
            save_expired(chat_id, [])
            await query.edit_message_text("🗑 All expired countdowns cleared.", reply_markup=None)
            return
        item_id = int(val)
        expired = get_expired(chat_id)
        target  = next((i for i in expired if i["id"] == item_id), None)
        expired = [i for i in expired if i["id"] != item_id]
        save_expired(chat_id, expired)
        name = target["name"] if target else "Item"
        await query.edit_message_text(f"🗑 *{name}* removed from history.", parse_mode="Markdown", reply_markup=None)
        return

    # Start live ticker from /list
    if data.startswith("live:"):
        item_id = int(data.split(":", 1)[1])
        items   = get_items(chat_id)
        target  = next((i for i in items if i["id"] == item_id), None)
        if not target:
            await query.edit_message_text("❌ Not found.")
            return
        dt       = datetime.fromisoformat(target["target"])
        diff     = (dt - now_local()).total_seconds()
        stop_btn = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏹ Stop Live", callback_data=f"stop:{item_id}")
        ]])
        msg = await query.message.reply_text(
            f"🔴 *LIVE* — {target['name']}\n"
            f"📅 {dt.strftime('%d %b %Y %H:%M')} (BD)\n"
            f"⏳ *{format_delta(diff)}*",
            parse_mode="Markdown", reply_markup=stop_btn
        )
        job_key = f"{chat_id}:{msg.message_id}"
        job = ctx.job_queue.run_repeating(
            live_tick, interval=1, first=1,
            chat_id=chat_id, name=job_key,
            data={"item_id": item_id, "msg_id": msg.message_id,
                  "name": target["name"], "target": target["target"], "job_key": job_key}
        )
        live_jobs[job_key] = job
        return

    if data.startswith("stop:"):
        msg_id  = query.message.message_id
        job_key = f"{chat_id}:{msg_id}"
        job = live_jobs.pop(job_key, None)
        if job:
            job.schedule_removal()
        await query.edit_message_text("⏹ Live stopped.", reply_markup=None)
        return

    if data.startswith("qlstop:"):
        msg_id  = int(data.split(":", 1)[1])
        job_key = f"{chat_id}:{msg_id}"
        job = quick_live_jobs.pop(job_key, None)
        if job:
            job.schedule_removal()
        await query.edit_message_text("⏹ Timer stopped.", reply_markup=None)
        return

async def live_tick(ctx: ContextTypes.DEFAULT_TYPE):
    d        = ctx.job.data
    chat_id  = ctx.job.chat_id
    dt       = datetime.fromisoformat(d["target"])
    diff     = (dt - now_local()).total_seconds()
    stop_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏹ Stop Live", callback_data=f"stop:{d['item_id']}")
    ]])
    try:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=d["msg_id"],
            text=(f"🔴 *LIVE* — {d['name']}\n"
                  f"📅 {dt.strftime('%d %b %Y %H:%M')} (BD)\n"
                  f"⏳ *{format_delta(diff)}*"),
            parse_mode="Markdown", reply_markup=stop_btn
        )
        if diff <= 0:
            ctx.job.schedule_removal()
            live_jobs.pop(d["job_key"], None)
            # Send notification with delete button
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"🔔 *{d['name']}* has arrived\\! 🎉\n📅 {dt.strftime('%d %b %Y %H:%M')} \\(BD\\)",
                parse_mode="MarkdownV2",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 Delete from history", callback_data=f"delexp:{d['item_id']}")
                ]])
            )
    except Exception:
        ctx.job.schedule_removal()
        live_jobs.pop(d["job_key"], None)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("cnt", cmd_cnt)],
        states={
            AWAIT_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_name)],
            AWAIT_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_datetime_guided)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("previous", cmd_previous))
    app.add_handler(CommandHandler("del",      cmd_del))
    app.add_handler(CommandHandler("live",     cmd_live))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Background checker every 10 seconds
    app.job_queue.run_repeating(check_finished, interval=10, first=5)

    print("✅ Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
