import asyncio
import json
import time
import traceback
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ChatMemberStatus
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from . import db
from .config import OWNER_ID, FORCE_JOIN_CHANNEL, PUBLIC_URL
from .providers import REGISTRY
from .utils import clean_text, chunk_text, escape_html
from .keycheck import inspect_key, try_model


# In-memory ephemeral context: per (chat_id, root_message_id) -> history list
_HISTORY: dict = defaultdict(list)
_PENDING_KEY: dict = {}  # user_id -> api_key (for /tryke flow)


# ---------- Helpers ----------
def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


async def force_join_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Enforce channel membership before any non-owner can use the bot."""
    if not FORCE_JOIN_CHANNEL:
        return True
    user = update.effective_user
    if not user or is_owner(user.id):
        return True
    try:
        member = await context.bot.get_chat_member(f"@{FORCE_JOIN_CHANNEL}", user.id)
        if member.status in (
            ChatMemberStatus.MEMBER, ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
        ):
            return True
    except Exception:
        pass
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL}")],
        [InlineKeyboardButton("I have joined", callback_data="verify_join")],
    ])
    await update.effective_message.reply_text(
        "Access restricted.\n\nYou must join our official channel to use this bot.\n"
        "Tap Join, then press I have joined to verify.",
        reply_markup=kb,
    )
    return False


async def safe_reply(update: Update, text: str, **kw):
    text = clean_text(text)
    first = None
    for chunk in chunk_text(text):
        msg = await update.effective_message.reply_text(chunk, **kw)
        first = first or msg
    return first


async def safe_edit(message, text: str):
    text = clean_text(text)
    chunks = list(chunk_text(text))
    try:
        await message.edit_text(chunks[0])
    except Exception:
        return
    for extra in chunks[1:]:
        await message.reply_text(extra)


# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.upsert_user(update.effective_user)
    if not await force_join_ok(update, context):
        return
    name = escape_html(update.effective_user.first_name or "there")
    txt = (
        f"Welcome, {name}.\n\n"
        "This is an advanced multi-AI assistant.\n"
        "Available providers:\n"
    )
    for k, (n, _) in REGISTRY.items():
        txt += f"  .{k}  —  {n}\n"
    txt += (
        "\nUsage:\n"
        "  .g your question      (Gemini)\n"
        "  .pr your question     (Perplexity)\n"
        "  .co your question     (Copilot)\n"
        "  .key <API_KEY>        (inspect any provider key)\n\n"
        "Reply to any bot answer to continue the same conversation.\n"
        "Use /help to see all user commands."
    )
    await safe_reply(update, txt)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_ok(update, context):
        return
    lines = [
        "User Commands",
        "",
        "/start   — Welcome and provider list",
        "/help    — This message",
        "/menu    — Provider menu",
        "/ping    — Latency check",
        "/key <API_KEY>   — Inspect API key (models, limits, expiry)",
        "/tryke <model> <prompt>  — Try a model using the last inspected key",
        "",
        "AI Shortcuts (both . and / work):",
    ]
    for k, (n, _) in REGISTRY.items():
        lines.append(f"  .{k} or /{k}  —  {n}")
    lines.append("\nReply to any bot answer to continue that chat.")
    await safe_reply(update, "\n".join(lines))


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_ok(update, context):
        return
    buttons = []
    row = []
    for k, (name, _) in REGISTRY.items():
        row.append(InlineKeyboardButton(name, callback_data=f"pick:{k}"))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("API Key Inspector", callback_data="info:keycheck")])
    await update.effective_message.reply_text(
        "Select an AI provider:", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = time.time()
    m = await update.effective_message.reply_text("Pinging...")
    dt = (time.time() - t) * 1000
    await m.edit_text(f"Pong  •  {dt:.0f} ms")


# ---------- AI call ----------
async def _call_provider(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         provider_key: str, prompt: str):
    if not await force_join_ok(update, context):
        return
    if not prompt.strip():
        await safe_reply(update, "Please provide a question after the command.")
        return

    name, fn = REGISTRY[provider_key]
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    # Determine session root: if user replied to a previous bot message, reuse its history.
    root_id = None
    rep = update.effective_message.reply_to_message
    if rep and rep.from_user and rep.from_user.id == context.bot.id:
        sess = await db.get_session(update.effective_chat.id, rep.message_id)
        if sess:
            provider_key = sess[0]  # keep original provider when replying
            name, fn = REGISTRY.get(provider_key, (name, fn))
            try:
                _HISTORY[(update.effective_chat.id, rep.message_id)] = json.loads(sess[1])
            except Exception:
                pass
            root_id = rep.message_id

    history_key = (update.effective_chat.id, root_id) if root_id else None
    history = _HISTORY.get(history_key, []) if history_key else []

    placeholder = await update.effective_message.reply_text(f"{name} is thinking...")
    try:
        answer = await asyncio.wait_for(fn(prompt, history), timeout=120)
        answer = clean_text(answer) or "No content returned."
        await safe_edit(placeholder, f"{name}\n\n{answer}")

        # Persist session keyed on placeholder.message_id so replies continue.
        new_root = root_id or placeholder.message_id
        hist = _HISTORY[(update.effective_chat.id, new_root)]
        hist.append({"q": prompt, "a": answer[:4000]})
        _HISTORY[(update.effective_chat.id, new_root)] = hist[-10:]
        await db.save_session(update.effective_chat.id, new_root, provider_key,
                              json.dumps(_HISTORY[(update.effective_chat.id, new_root)]))
        await db.log("INFO", update.effective_user.id, provider_key, prompt[:200])
    except asyncio.TimeoutError:
        await safe_edit(placeholder, f"{name} timed out. Please try again.")
        await db.log("ERROR", update.effective_user.id, provider_key, "timeout")
    except Exception as e:
        tb = traceback.format_exc(limit=2)
        await safe_edit(placeholder, f"{name} error.\n\n{e}")
        await db.log("ERROR", update.effective_user.id, provider_key, f"{e}\n{tb}")


def make_provider_handler(key: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await db.upsert_user(update.effective_user)
        text = update.effective_message.text or ""
        # strip leading /cmd or .cmd
        parts = text.split(None, 1)
        prompt = parts[1] if len(parts) > 1 else ""
        await _call_provider(update, context, key, prompt)
    return handler


# ---------- API key inspector ----------
async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_ok(update, context):
        return
    args = context.args
    if not args:
        await safe_reply(update, "Usage: /key <API_KEY>\nExample: /key sk-...")
        return
    key = args[0]
    placeholder = await update.effective_message.reply_text("Inspecting key...")
    try:
        info = await inspect_key(key)
        if not info.get("valid"):
            await safe_edit(placeholder,
                f"{info.get('provider', 'Unknown')}  •  INVALID\n"
                f"Status: {info.get('status')}\n"
                f"Detail: {json.dumps(info.get('error'))[:600]}")
            return
        _PENDING_KEY[update.effective_user.id] = key
        models = info.get("models", [])
        limits = info.get("limits", {})
        lines = [
            f"{info['provider']}  •  ACTIVE",
            f"Models available ({len(models)}):",
        ]
        for m in models[:30]:
            lines.append(f"  - {m}")
        if len(models) > 30:
            lines.append(f"  ... +{len(models)-30} more")
        if limits:
            lines.append("\nLimits / Quota:")
            for k, v in limits.items():
                lines.append(f"  {k}: {v}")
        lines.append("\nTry a model:")
        lines.append("  /tryke <model> <your prompt>")
        await safe_edit(placeholder, "\n".join(lines))
    except Exception as e:
        await safe_edit(placeholder, f"Inspection failed: {e}")


async def cmd_tryke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await force_join_ok(update, context):
        return
    key = _PENDING_KEY.get(update.effective_user.id)
    if not key:
        await safe_reply(update, "First inspect a key with /key <API_KEY>.")
        return
    if len(context.args) < 2:
        await safe_reply(update, "Usage: /tryke <model> <prompt>")
        return
    model = context.args[0]
    prompt = " ".join(context.args[1:])
    placeholder = await update.effective_message.reply_text(f"Calling {model}...")
    try:
        out = await asyncio.wait_for(try_model(key, model, prompt), timeout=90)
        await safe_edit(placeholder, f"{model}\n\n{out}")
    except Exception as e:
        await safe_edit(placeholder, f"Call failed: {e}")


# ---------- Owner commands (hidden from users) ----------
async def _owner_only(update: Update) -> bool:
    if not is_owner(update.effective_user.id):
        # silent: pretend command doesn't exist
        return False
    return True


async def cmd_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    lines = [
        "Owner Commands",
        "",
        "/stats        — bot statistics",
        "/logs [n]     — last n log entries (default 20)",
        "/users        — total user count",
        "/setchannel <username>  — set force-join channel",
        "/ban <user_id>",
        "/unban <user_id>",
        "/announce <text>        — broadcast to all users",
        "/announce_reply         — reply to a message with this to broadcast it",
        "/owner        — this menu",
    ]
    await safe_reply(update, "\n".join(lines))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    s = await db.stats()
    ch = await db.get_setting("force_join", FORCE_JOIN_CHANNEL or "(none)")
    await safe_reply(update,
        f"Bot Status\n"
        f"  Users:    {s['users']}\n"
        f"  Banned:   {s['banned']}\n"
        f"  Messages: {s['messages']}\n"
        f"  Errors:   {s['errors']}\n"
        f"  Channel:  {ch}\n"
        f"  Providers: {', '.join(REGISTRY.keys())}")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    n = 20
    if context.args:
        try: n = max(1, min(100, int(context.args[0])))
        except: pass
    rows = await db.get_logs(n)
    if not rows:
        await safe_reply(update, "No logs yet.")
        return
    lines = ["Recent Logs (newest first)"]
    for ts, lvl, uid, prov, msg in rows:
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
        lines.append(f"[{when}] {lvl} u={uid} {prov}: {msg[:140]}")
    await safe_reply(update, "\n".join(lines))


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    ids = await db.all_user_ids()
    await safe_reply(update, f"Total active users: {len(ids)}")


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    global FORCE_JOIN_CHANNEL
    if not context.args:
        await safe_reply(update, "Usage: /setchannel <username_without_@>  (use 'off' to disable)")
        return
    val = context.args[0].lstrip("@")
    if val.lower() == "off":
        val = ""
    await db.set_setting("force_join", val)
    # update runtime
    import bot.config as cfg
    cfg.FORCE_JOIN_CHANNEL = val
    from . import handlers as h
    h.FORCE_JOIN_CHANNEL = val  # local rebind not needed but explicit
    await safe_reply(update, f"Force-join channel set to: {val or '(disabled)'}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    if not context.args:
        await safe_reply(update, "Usage: /ban <user_id>"); return
    try:
        uid = int(context.args[0])
        await db.set_banned(uid, 1)
        await safe_reply(update, f"User {uid} banned.")
    except Exception as e:
        await safe_reply(update, f"Failed: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    if not context.args:
        await safe_reply(update, "Usage: /unban <user_id>"); return
    try:
        uid = int(context.args[0])
        await db.set_banned(uid, 0)
        await safe_reply(update, f"User {uid} unbanned.")
    except Exception as e:
        await safe_reply(update, f"Failed: {e}")


async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _owner_only(update): return
    text = " ".join(context.args).strip()
    rep = update.effective_message.reply_to_message
    if not text and rep:
        text = rep.text or rep.caption or ""
    if not text:
        await safe_reply(update, "Usage: /announce <text>  or reply to a message with /announce")
        return
    ids = await db.all_user_ids()
    ok = fail = 0
    status = await update.effective_message.reply_text(f"Broadcasting to {len(ids)} users...")
    for i, uid in enumerate(ids, 1):
        try:
            await context.bot.send_message(uid, clean_text(text))
            ok += 1
        except Exception:
            fail += 1
        if i % 25 == 0:
            await asyncio.sleep(1)  # throttle
            try:
                await status.edit_text(f"Progress: {i}/{len(ids)}  ok={ok} fail={fail}")
            except Exception:
                pass
    await status.edit_text(f"Announcement complete.\n  Delivered: {ok}\n  Failed:    {fail}")


# ---------- Callback queries ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data == "verify_join":
        if await force_join_ok(update, context):
            try:
                await q.edit_message_text("Verified. You can now use the bot. Send /start.")
            except Exception:
                pass
        return
    if data.startswith("pick:"):
        k = data.split(":", 1)[1]
        name = REGISTRY.get(k, (k,))[0]
        await q.edit_message_text(
            f"Selected: {name}\nSend: .{k} your question\n"
            f"Or:    /{k} your question"
        )
        return
    if data == "info:keycheck":
        await q.edit_message_text(
            "API Key Inspector\n\nSend: /key <API_KEY>\n\n"
            "Supports: OpenAI, Anthropic, Google Gemini, Groq, OpenRouter, "
            "Cohere, DeepSeek, xAI, Together AI, and any OpenAI-compatible key."
        )


# ---------- Dot-prefix dispatcher ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages starting with '.' as commands, and reply-to-bot continuations."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return
    await db.upsert_user(update.effective_user)
    if await db.is_banned(update.effective_user.id):
        return

    # 1) Reply-to-bot continuation (no command prefix) -> use that session's provider
    if msg.reply_to_message and msg.reply_to_message.from_user \
            and msg.reply_to_message.from_user.id == context.bot.id \
            and not text.startswith(("/", ".")):
        sess = await db.get_session(msg.chat_id, msg.reply_to_message.message_id)
        if sess:
            provider_key = sess[0]
            await _call_provider(update, context, provider_key, text)
            return

    # 2) Dot-prefix commands: .g .pr .co .key .help .menu .ping .start ...
    if text.startswith("."):
        first, _, rest = text[1:].partition(" ")
        cmd = first.lower()
        if cmd in REGISTRY:
            await _call_provider(update, context, cmd, rest)
            return
        # alias dot-commands to slash equivalents
        alias = {
            "start": cmd_start, "help": cmd_help, "menu": cmd_menu,
            "ping": cmd_ping, "key": cmd_key, "tryke": cmd_tryke,
        }
        if cmd in alias:
            # rebuild context.args for compatibility
            context.args = rest.split() if rest else []
            await alias[cmd](update, context)
            return
        # owner dot-commands
        if is_owner(update.effective_user.id):
            owner_alias = {
                "owner": cmd_owner, "stats": cmd_stats, "logs": cmd_logs,
                "users": cmd_users, "setchannel": cmd_setchannel,
                "ban": cmd_ban, "unban": cmd_unban, "announce": cmd_announce,
            }
            if cmd in owner_alias:
                context.args = rest.split() if rest else []
                await owner_alias[cmd](update, context)
                return


# ---------- Error handler ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))[:1800]
    try:
        await db.log("ERROR", 0, "system", tb)
    except Exception:
        pass


def register_handlers(app: Application):
    # User commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("key", cmd_key))
    app.add_handler(CommandHandler("tryke", cmd_tryke))

    # Provider slash commands (dynamic)
    for k in list(REGISTRY.keys()):
        app.add_handler(CommandHandler(k, make_provider_handler(k)))

    # Owner commands
    app.add_handler(CommandHandler("owner", cmd_owner))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("setchannel", cmd_setchannel))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("announce", cmd_announce))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
