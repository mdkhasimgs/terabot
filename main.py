import logging
from datetime import datetime, timedelta, timezone
import asyncio
import secrets
from threading import Thread
import firebase_admin
from firebase_admin import credentials, firestore

from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                          ConversationHandler, ContextTypes,
                          CallbackQueryHandler)
import aiohttp
from flask import Flask
# ---------------- CONFIG ----------------
BOT_TOKEN = "8277207454:AAHntxgp2vKdhUyr_Wo6aUhmj0jNk2LZUL0"
ADMIN_ID = 7598595878
FORCE_JOIN_CHANNEL = "@TERACLOUD_STORAGE"
UPLOAD_CHANNEL = "@terabo_storessu"

# (Optional) ShrinkMe shortener (NOT used for post links as per your request)
SHRINKME_API_KEY = "4345d0381d3b88576a15aa22aa8372a1bc4eb05f"
ADFLY_API_KEY = "bb8edc7698c9823f83dbcea91f0d0b37e95c0045"  # leave empty to disable
SHRINK_VERIFY_EXPIRY_MIN = 60  # verification token expiry minutes
FREE_VIEWS = 10  # Added missing variable definition

# Firestore
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tg-bot")

# ---------------- RUNTIME ----------------
BOT_START_TIME = datetime.now(timezone.utc)

# ---------------- STATES ----------------
UPLOAD_FILE, UPLOAD_TITLE = range(2)
SET_MORE_VIDEOS = 3
EDIT_TITLE = 4
REPLACE_FILE = 5
MULTI_UPLOAD_FILE = 6  # admin multi-upload state
ADD_USER_ID, ADD_VIEWS = range(7, 9)  # /add command states


# ---------------- HELPERS ----------------
def generate_unique_id() -> str:
    # second-precision ID
    return str(int(datetime.now(timezone.utc).timestamp()))


def generate_token(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


async def force_join_keyboard():
    return ReplyKeyboardMarkup([["Join Channel"]],
                               resize_keyboard=True,
                               one_time_keyboard=True)


async def check_force_join(update: Update,
                           context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(
            FORCE_JOIN_CHANNEL, update.effective_user.id)
        if chat_member.status not in ["member", "administrator", "creator"]:
            await update.message.reply_text(
                "Join the required channel to use this bot! \n @TERACLOUD_STORAGE",
                reply_markup=await force_join_keyboard())
            return False
    except Exception:
        await update.message.reply_text(
            "Join the required channel to use this bot!",
            reply_markup=await force_join_keyboard())
        return False
    return True


def post_card_caption(title: str, views: int, link: str) -> str:
    return f"🎬 {title}\n👁 {views} views\n{link}"


def generate_token(nbytes: int = 16) -> str:
    """Generate a URL-safe token"""
    return secrets.token_urlsafe(nbytes)


async def shorten_shrinkme(long_url: str) -> str:
    if not SHRINKME_API_KEY:
        return long_url
    api_url = f"https://shrinkme.io/api?api={SHRINKME_API_KEY}&url={long_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=20) as resp:
                data = await resp.json(content_type=None)
                return data.get("shortenedUrl") or long_url
    except Exception as e:
        logger.warning(f"ShrinkMe error: {e}")
        return long_url


async def shorten_adfly(long_url: str) -> str:
    if not ADFLY_API_KEY:
        return long_url
    api_url = f"https://adfly.site/api?api={ADFLY_API_KEY}&url={long_url}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=20) as resp:
                data = await resp.json(content_type=None)
                return data.get("shortenedUrl") or long_url
    except Exception as e:
        logger.warning(f"AdFly error: {e}")
        return long_url


async def create_verification_links(context: ContextTypes.DEFAULT_TYPE,
                                    user_id: str) -> dict:
    """Create a single-use verification token and return both shortener links"""
    token = generate_token(12)
    expires_at = datetime.now(
        timezone.utc) + timedelta(minutes=SHRINK_VERIFY_EXPIRY_MIN)
    db.collection("verifications").document(token).set({
        "user_id":
        user_id,
        "created_at":
        datetime.now(timezone.utc).isoformat(),
        "expires_at":
        expires_at.isoformat(),
        "used":
        False
    })
    bot_username = (await context.bot.get_me()).username
    long_url = f"https://t.me/{bot_username}?start=verify_{token}"

    shrink_link = await shorten_shrinkme(long_url)
    adfly_link = await shorten_adfly(long_url)
    return {"shrinkme": shrink_link, "adfly": adfly_link}


async def ensure_user_doc(user_id: str):
    db.collection("users").document(user_id).set(
        {"created_at": datetime.now(timezone.utc).isoformat()}, merge=True)


# ---------------- ADMIN PANEL ----------------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("You are not authorized!")
        return
    keyboard = [
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("📤 Upload Post", callback_data="upload")],
        [InlineKeyboardButton("👥 Total Users", callback_data="total_users")],
        [
            InlineKeyboardButton("🎥 Set More Videos Link",
                                 callback_data="set_more_videos")
        ],
        [InlineKeyboardButton("🔗 Multi Post", callback_data="multi_post")],
        [InlineKeyboardButton("⏱ Uptime", callback_data="uptime")],
    ]
    await update.message.reply_text(
        "Admin Panel", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "broadcast":
        await query.edit_message_text(
            "Send the message to broadcast to all users:")
        context.user_data["broadcast"] = True

    elif data == "upload":
        await query.edit_message_text(
            "Send the media file (photo, video, or document):")
        return UPLOAD_FILE

    elif data == "total_users":
        count = sum(1 for _ in db.collection("users").stream())
        await query.edit_message_text(f"Total registered users: {count}")

    elif data == "set_more_videos":
        await query.edit_message_text(
            "Send the new channel link for 'More Videos':")
        context.user_data["awaiting_more_videos_link"] = True
        return SET_MORE_VIDEOS

    elif data == "multi_post":
        # ENTER multi-upload state
        context.user_data["multi_posts"] = []
        await query.edit_message_text(
            "Send up to 10 media files (photo/video/document). Type /done when finished."
        )
        return MULTI_UPLOAD_FILE

    elif data == "uptime":
        total_users = sum(1 for _ in db.collection("users").stream())
        total_videos = sum(1 for _ in db.collection("posts").stream())
        uptime = datetime.now(timezone.utc) - BOT_START_TIME
        uptime_str = str(uptime).split(".")[0]
        await query.edit_message_text(
            f"📊 Bot Stats:\n\n"
            f"👥 Total Users: {total_users}\n"
            f"🎥 Total Videos: {total_videos}\n"
            f"🚀 Started At: {BOT_START_TIME.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"⏱ Uptime: {uptime_str}")


# ---------------- ADD USER VIEWS (ADMIN /add) ----------------
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END
    await update.message.reply_text("Send the user ID to add views for:")
    return ADD_USER_ID


async def add_get_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = str(int(update.message.text.strip()))
    except Exception:
        await update.message.reply_text("❌ Invalid user ID. Try again:")
        return ADD_USER_ID
    context.user_data["add_target_user"] = user_id
    await update.message.reply_text(
        f"✅ User ID set: {user_id}\nNow send how many views to add:")
    return ADD_VIEWS


async def add_get_views(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        views_to_add = int(update.message.text.strip())
        if views_to_add <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid number. Try again:")
        return ADD_VIEWS
    user_id = context.user_data.get("add_target_user")
    if not user_id:
        await update.message.reply_text(
            "⚠️ User ID missing. Restart with /add.")
        return ConversationHandler.END
    user_doc = db.collection("users").document(user_id)
    ud = user_doc.get().to_dict() or {}
    current_limit = int(ud.get("verified_until", 10))
    user_doc.set({"verified_until": current_limit + views_to_add}, merge=True)
    await update.message.reply_text(
        f"✅ Added {views_to_add} views to user {user_id}.\n"
        f"📊 New limit: {current_limit + views_to_add}")
    context.user_data.pop("add_target_user", None)
    return ConversationHandler.END


# ---------------- POST CREATION (user & admin) ----------------
async def store_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context):
        return ConversationHandler.END
    await update.message.reply_text(
        "Send the media file (photo, video, or document) for your post:")
    return UPLOAD_FILE


async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_info = {}
    media_type = None
    media_id = None

    if update.message.photo:
        media_type = "photo"
        largest = update.message.photo[-1]
        media_id = largest.file_id
        file_info["size"] = largest.file_size

    elif update.message.video:
        media_type = "video"
        v = update.message.video
        media_id = v.file_id
        file_info["size"] = v.file_size
        file_info["duration"] = v.duration
        if v.thumbnail:
            file_info["thumb_id"] = v.thumbnail.file_id

    elif update.message.document:
        media_type = "document"
        d = update.message.document
        media_id = d.file_id
        file_info["size"] = d.file_size

    else:
        await update.message.reply_text(
            "Unsupported media type! Send photo, video, or document only.")
        return UPLOAD_FILE

    context.user_data["upload_file"] = {
        "media_type": media_type,
        "media_id": media_id,
        "file_info": file_info
    }
    await update.message.reply_text(
        "✅ File received. Now send the title for this post.")
    return UPLOAD_TITLE


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    file_data = context.user_data.get("upload_file")
    if not file_data:
        await update.message.reply_text("No file uploaded!")
        return ConversationHandler.END

    unique_id = generate_unique_id()
    db.collection("posts").document(unique_id).set({
        "title":
        title,
        "file":
        file_data,
        "posted_by":
        update.effective_user.id,
        "posted_at":
        datetime.now(timezone.utc).isoformat(),
        "views":
        0
    })

    # (Optional) post to your channel as archive/promo
    f = file_data
    try:
        if f["media_type"] == "photo":
            await update.get_bot().send_photo(chat_id=UPLOAD_CHANNEL,
                                              photo=f["media_id"],
                                              caption=title,
                                              protect_content=True)
        elif f["media_type"] == "video":
            await update.get_bot().send_video(chat_id=UPLOAD_CHANNEL,
                                              video=f["media_id"],
                                              caption=title,
                                              supports_streaming=True,
                                              protect_content=True)
        elif f["media_type"] == "document":
            await update.get_bot().send_document(chat_id=UPLOAD_CHANNEL,
                                                 document=f["media_id"],
                                                 caption=title,
                                                 protect_content=True)
    except Exception as e:
        logger.warning(f"Channel post failed (non-blocking): {e}")

    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={unique_id}"  # Telegram direct link
    await update.message.reply_text(f"Post uploaded! Shareable link:\n{link}")
    context.user_data.clear()
    return ConversationHandler.END


# ---------------- START / LINK OPEN ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Force join
    if update.message and not await check_force_join(update, context):
        return

    # ensure user doc
    user_id = str(update.effective_user.id)
    await ensure_user_doc(user_id)

    # If opened via verification link
    if context.args and context.args[0].startswith("verify_"):
        token = context.args[0][7:]
        tok_ref = db.collection("verifications").document(token)
        tok_doc = tok_ref.get()
        if not tok_doc.exists:
            await update.message.reply_text("❌ Invalid verification link.")
        else:
            tok = tok_doc.to_dict()
            if tok.get("used"):
                await update.message.reply_text(
                    "⚠️ This verification link was already used.")
            elif tok.get("user_id") != user_id:
                await update.message.reply_text(
                    "❌ This verification link does not belong to you.")
            else:
                try:
                    exp = datetime.fromisoformat(tok["expires_at"])
                except Exception:
                    exp = datetime.now(timezone.utc) - timedelta(seconds=1)
                if datetime.now(timezone.utc) > exp:
                    await update.message.reply_text(
                        "⌛ Verification link expired. Request a new one.")
                else:
                    # Mark used and extend allowance
                    tok_ref.update({
                        "used":
                        True,
                        "used_at":
                        datetime.now(timezone.utc).isoformat()
                    })
                    user_doc = db.collection("users").document(user_id)
                    ud = user_doc.get().to_dict() or {}
                    views_used = int(ud.get("views_used", 0))
                    user_doc.set({"verified_until": views_used + 10},
                                 merge=True)
                    await update.message.reply_text(
                        "✅ Verification successful! You unlocked 10 more posts."
                    )
        # continue to menu

    # If opened via shareable post link
    if context.args and not context.args[0].startswith("verify_"):
        unique_id = context.args[0]
        post_ref = db.collection("posts").document(unique_id)
        post = post_ref.get()
        if not post.exists:
            await update.message.reply_text("Post not found!")
        else:
            user_doc = db.collection("users").document(user_id)
            ud = user_doc.get().to_dict() or {}
            views_used = int(ud.get("views_used", 0))
            verified_until = int(ud.get("verified_until", 10))

            if views_used >= verified_until:
                links = await create_verification_links(context, str(user_id))

                keyboard = [[
                    InlineKeyboardButton("🔗 ShrinkMe", url=links["shrinkme"]),
                    InlineKeyboardButton("🔗 AdFly", url=links["adfly"])
                ]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await update.message.reply_text(
                    f"🔒 You reached your free limit of {FREE_VIEWS} views.\n"
                    "Verify using any one link below to get more views:\n"
                    f"🔁 Each verification unlocks +10 views (valid {SHRINK_VERIFY_EXPIRY_MIN} min).",
                    reply_markup=reply_markup)
            else:
                data = post.to_dict()
                f = data["file"]
                title = data["title"]
                new_views = int(data.get("views", 0)) + 1

                await update.message.reply_text(
                    f"🎬 {title}\n👁 {new_views} views")

                try:
                    if f["media_type"] == "photo":
                        await update.message.reply_photo(f["media_id"],
                                                         protect_content=True)
                    elif f["media_type"] == "video":
                        await update.message.reply_video(
                            f["media_id"],
                            supports_streaming=True,
                            protect_content=True)
                    elif f["media_type"] == "document":
                        await update.message.reply_document(
                            f["media_id"], protect_content=True)
                except Exception as e:
                    logger.error(f"Failed to send media: {e}")
                    await update.message.reply_text(
                        "Failed to send media. It might be too large or unavailable."
                    )

                # increment counters
                post_ref.update({"views": new_views})
                user_doc.set({"views_used": views_used + 1}, merge=True)

    # Main menu
    keyboard = [["📤 Store"], ["🗂 Storage"], ["🎥 More Videos"]]
    await update.message.reply_text("Welcome! Use menu below:",
                                    reply_markup=ReplyKeyboardMarkup(
                                        keyboard, resize_keyboard=True))


# ---------------- STORAGE (with inline buttons & video thumbnail card) ----------------
async def user_storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    posts = db.collection("posts").where("posted_by", "==", user_id).stream()
    found = False
    bot_username = (await context.bot.get_me()).username

    async def send_card_for_post(post_id: str, d: dict):
        link = f"https://t.me/{bot_username}?start={post_id}"
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏ Edit Title",
                                     callback_data=f"edit_{post_id}")
            ],
            [
                InlineKeyboardButton("🗑 Delete",
                                     callback_data=f"delete_{post_id}")
            ],
            [
                InlineKeyboardButton("♻ Replace",
                                     callback_data=f"replace_{post_id}")
            ],
        ])
        if d["file"]["media_type"] == "video" and d["file"]["file_info"].get(
                "thumb_id"):
            thumb_id = d["file"]["file_info"]["thumb_id"]
            await update.message.reply_photo(photo=thumb_id,
                                             caption=post_card_caption(
                                                 d["title"], d.get("views", 0),
                                                 link),
                                             reply_markup=kb,
                                             protect_content=True)
        else:
            await update.message.reply_text(text=post_card_caption(
                d["title"], d.get("views", 0), link),
                                            reply_markup=kb)

    for p in posts:
        found = True
        await send_card_for_post(p.id, p.to_dict())

    if not found:
        await update.message.reply_text("No stored posts!")


async def storage_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, post_id = query.data.split("_", 1)
    post_ref = db.collection("posts").document(post_id)

    if action == "delete":
        post_ref.delete()
        await query.edit_message_text("🗑 Post deleted.")

    elif action == "edit":
        context.user_data["edit_post"] = post_id
        await query.edit_message_text("Send new title:")
        return EDIT_TITLE

    elif action == "replace":
        context.user_data["replace_post"] = post_id
        await query.edit_message_text("Send new file (photo/video/document):")
        return REPLACE_FILE


async def edit_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_title = update.message.text
    post_id = context.user_data.get("edit_post")
    if post_id:
        db.collection("posts").document(post_id).update({"title": new_title})
        await update.message.reply_text(f"✅ Title updated: {new_title}")
    context.user_data.pop("edit_post", None)
    return ConversationHandler.END


async def replace_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post_id = context.user_data.get("replace_post")
    if not post_id:
        return ConversationHandler.END

    file_info = {}
    media_type = None
    media_id = None

    if update.message.photo:
        media_type = "photo"
        largest = update.message.photo[-1]
        media_id = largest.file_id
        file_info["size"] = largest.file_size

    elif update.message.video:
        media_type = "video"
        v = update.message.video
        media_id = v.file_id
        file_info["size"] = v.file_size
        file_info["duration"] = v.duration
        if v.thumbnail:
            file_info["thumb_id"] = v.thumbnail.file_id

    elif update.message.document:
        media_type = "document"
        d = update.message.document
        media_id = d.file_id
        file_info["size"] = d.file_size

    else:
        await update.message.reply_text("Unsupported type! Try again.")
        return REPLACE_FILE

    db.collection("posts").document(post_id).update({
        "file": {
            "media_type": media_type,
            "media_id": media_id,
            "file_info": file_info
        }
    })
    await update.message.reply_text("✅ File replaced.")
    context.user_data.pop("replace_post", None)
    return ConversationHandler.END


# ---------------- MORE VIDEOS ----------------
async def more_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = db.collection("config").document("settings").get().to_dict() or {}
    link = cfg.get("more_videos", "https://t.me/+qfuDy4wteyc2Mjdl")
    await update.message.reply_text(f"🎥 More videos here:\n{link}")


# ---------------- BROADCAST ----------------
async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("broadcast"):
        return
    text = update.message.text
    count = 0
    for u in db.collection("users").stream():
        try:
            await context.bot.send_message(int(u.id),
                                           text,
                                           protect_content=True)
            count += 1
        except Exception:
            continue
    await update.message.reply_text(f"Broadcast sent to {count} users!")
    context.user_data.pop("broadcast", None)


# ---------------- SET MORE VIDEOS LINK ----------------
async def set_more_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_more_videos_link"):
        return ConversationHandler.END
    link = update.message.text.strip()
    db.collection("config").document("settings").set({"more_videos": link},
                                                     merge=True)
    await update.message.reply_text(f"✅ 'More Videos' link updated to:\n{link}"
                                    )
    context.user_data.pop("awaiting_more_videos_link", None)
    return ConversationHandler.END


# ---------------- MULTI UPLOAD (ADMIN) ----------------
async def multi_upload_file(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """Collect files while in MULTI_UPLOAD_FILE state. Use caption as title if present."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Not authorized.")
        return ConversationHandler.END

    if "multi_posts" not in context.user_data:
        context.user_data["multi_posts"] = []

    if len(context.user_data["multi_posts"]) >= 10:
        await update.message.reply_text(
            "❌ Max 10 posts allowed. Type /done now.")
        return MULTI_UPLOAD_FILE

    file_info = {}
    media_type = None
    media_id = None
    title = update.message.caption or "Untitled"

    if update.message.photo:
        media_type = "photo"
        largest = update.message.photo[-1]
        media_id = largest.file_id
        file_info["size"] = largest.file_size

    elif update.message.video:
        media_type = "video"
        v = update.message.video
        media_id = v.file_id
        file_info["size"] = v.file_size
        file_info["duration"] = v.duration
        if v.thumbnail:
            file_info["thumb_id"] = v.thumbnail.file_id

    elif update.message.document:
        media_type = "document"
        d = update.message.document
        media_id = d.file_id
        file_info["size"] = d.file_size

    else:
        await update.message.reply_text(
            "Unsupported type! Send photo, video, or document.")
        return MULTI_UPLOAD_FILE

    context.user_data["multi_posts"].append({
        "title": title,
        "file": {
            "media_type": media_type,
            "media_id": media_id,
            "file_info": file_info
        }
    })
    await update.message.reply_text(
        f"✅ File {len(context.user_data['multi_posts'])}/10 received. Send next or /done."
    )
    return MULTI_UPLOAD_FILE


async def multi_upload_done(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """Finish multi-upload, save each post, send to channel, reply with one Telegram link per post."""
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END

    posts = context.user_data.get("multi_posts", [])
    if not posts:
        await update.message.reply_text("No files received.")
        return ConversationHandler.END

    bot_username = (await update.get_bot().get_me()).username
    links = []

    for p in posts:
        unique_id = generate_unique_id()
        db.collection("posts").document(unique_id).set({
            "title":
            p["title"],
            "file":
            p["file"],
            "posted_by":
            ADMIN_ID,
            "posted_at":
            datetime.now(timezone.utc).isoformat(),
            "views":
            0
        })

        # Post to channel archive
        try:
            f = p["file"]
            if f["media_type"] == "photo":
                await update.get_bot().send_photo(UPLOAD_CHANNEL,
                                                  f["media_id"],
                                                  caption=p["title"],
                                                  protect_content=True)
            elif f["media_type"] == "video":
                await update.get_bot().send_video(UPLOAD_CHANNEL,
                                                  f["media_id"],
                                                  caption=p["title"],
                                                  supports_streaming=True,
                                                  protect_content=True)
            elif f["media_type"] == "document":
                await update.get_bot().send_document(UPLOAD_CHANNEL,
                                                     f["media_id"],
                                                     caption=p["title"],
                                                     protect_content=True)
        except Exception as e:
            logger.warning(f"Multi-upload channel post failed: {e}")

        links.append(f"https://t.me/{bot_username}?start={unique_id}")

    context.user_data.pop("multi_posts", None)
    await update.message.reply_text("✅ Multi Post Links:\n" + "\n".join(links))
    return ConversationHandler.END


# ---------------- FLASK SERVER ----------------
app = Flask("")


@app.route("/")
def home():
    return "Bot is alive!"


def run():
    app.run(host="0.0.0.0", port=3000)


def keep_alive():
    t = Thread(target=run)
    t.start()


# ---------------- MAIN ----------------
def main():
    keep_alive()

    application = Application.builder().token(BOT_TOKEN).build()

    # Conversation: single conv handles admin actions + multi-upload state
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_panel),
            CommandHandler("add", add_command),  # /add feature
            MessageHandler(filters.Regex("^📤 Store$"), store_post)
        ],
        states={
            UPLOAD_FILE:
            [MessageHandler(filters.ALL & ~filters.COMMAND, receive_file)],
            UPLOAD_TITLE:
            [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            SET_MORE_VIDEOS:
            [MessageHandler(filters.TEXT & ~filters.COMMAND, set_more_videos)],
            EDIT_TITLE:
            [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_title)],
            REPLACE_FILE:
            [MessageHandler(filters.ALL & ~filters.COMMAND, replace_file)],

            # /done handled inside state
            MULTI_UPLOAD_FILE: [
                MessageHandler(filters.ALL & ~filters.COMMAND,
                               multi_upload_file),
                CommandHandler("done", multi_upload_done),
            ],

            # /add states
            ADD_USER_ID:
            [MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_userid)],
            ADD_VIEWS:
            [MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_views)],
        },
        fallbacks=[],  # don't put /done here; it won't trigger while in state
        name="main_conv",
        persistent=False)

    # Admin callbacks (including the button that ENTERS multi-upload state)
    application.add_handler(
        CallbackQueryHandler(
            admin_button,
            pattern=
            "^(broadcast|upload|total_users|set_more_videos|multi_post|uptime)$"
        ))

    # Storage callbacks
    application.add_handler(
        CallbackQueryHandler(storage_button,
                             pattern="^(edit_|delete_|replace_).+"))

    # Generic handlers
    application.add_handler(conv)

    # /start must be outside conv
    application.add_handler(CommandHandler("start", start))

    application.add_handler(
        MessageHandler(filters.Regex("^🗂 Storage$"), user_storage))
    application.add_handler(
        MessageHandler(filters.Regex("^🎥 More Videos$"), more_videos))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        asyncio.set_event_loop_policy(
            asyncio.WindowsSelectorEventLoopPolicy())  # harmless elsewhere
    except Exception:
        pass
    main()
