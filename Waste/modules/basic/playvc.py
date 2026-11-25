"""Connected-ID music playback with queue + autoplay.

The account that sends .play is the connected /add account.  No separate
STRING_SESSION is needed for these commands.
"""
import asyncio
import html
import logging
import shutil
import os
import uuid
from collections import defaultdict

from pyrogram import Client, filters, enums
from pyrogram.enums import ChatMemberStatus
from pytgcalls.types import GroupCallConfig

try:
    from pytgcalls.types import MediaStream
except Exception:  # alag py-tgcalls version
    MediaStream = None

from Waste.modules.basic.vcfight import _bridge, _join
from Waste.modules.help import add_command_help

log = logging.getLogger(__name__)
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _ensure_ffmpeg():
    """Render par ffmpeg install nahi hota. requirements.txt me `imageio-ffmpeg`
    ho to uski binary ko PATH me `ffmpeg` naam se laga do."""
    try:
        if shutil.which("ffmpeg"):
            return
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bindir = os.path.join(os.getcwd(), ".ffbin")
        os.makedirs(bindir, exist_ok=True)
        link = os.path.join(bindir, "ffmpeg")
        if not os.path.lexists(link):
            try:
                os.symlink(exe, link)
            except OSError:
                shutil.copy2(exe, link)
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")
    except Exception as exc:
        print(f"[playvc] ffmpeg setup failed: {exc}")


_ensure_ffmpeg()
print("[playvc] module loaded | ffmpeg:", shutil.which("ffmpeg"))

_music_enabled = defaultdict(lambda: True)
_music = defaultdict(lambda: {
    "queue": [],
    "current": None,
    "task": None,
})


def _state(client, chat_id):
    return _music[(id(client), int(chat_id))]


def _dbg(*args):
    print("[playvc]", *args, flush=True)


async def _stage(coro, seconds, name):
    """Koi step atak jaye to chup-chap latakne ki jagah error dikhao."""
    try:
        return await asyncio.wait_for(coro, seconds)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{name} {int(seconds)}s me nahi hua (timeout)")


async def _owner_uid(client):
    """Return the connected account owner/user id for hosted or static sessions."""
    uid = getattr(client, "_waste_owner_uid", None)
    if uid:
        return int(uid)
    try:
        me = getattr(client, "me", None) or await client.get_me()
        return int(me.id) if me else None
    except Exception:
        return None


def _safe_title(info, fallback):
    title = str(info.get("title") or fallback)
    return title.replace("<", "&lt;").replace(">", "&gt;")[:180]


def _download_song(query: str):
    """Resolve audio with the connected account's normal yt-dlp flow plus
    YouTube-client retries and SoundCloud fallback when YouTube blocks Render."""
    import yt_dlp

    job_id = uuid.uuid4().hex
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")
    cookie_file = os.getenv("COOKIES_FILE", "cookies.txt")
    base = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "source_address": "0.0.0.0",
        "geo_bypass": True,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 2,
        "socket_timeout": 20,
        "nocheckcertificate": True,
        "concurrent_fragment_downloads": 3,
    }
    if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0:
        base["cookiefile"] = cookie_file

    def _extract(q, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(q, download=True)

    def _find_file(info, ydl=None):
        requested = ydl.prepare_filename(info) if ydl else ""
        if requested and os.path.exists(requested):
            return requested
        stem = os.path.splitext(requested)[0] if requested else ""
        if stem:
            matches = [
                os.path.join(DOWNLOAD_DIR, name)
                for name in os.listdir(DOWNLOAD_DIR)
                if name.startswith(os.path.basename(stem) + ".")
            ]
            if matches:
                return matches[0]
        # Some postprocessors change the extension; locate the newest job file.
        candidates = [os.path.join(DOWNLOAD_DIR, n) for n in os.listdir(DOWNLOAD_DIR) if n.startswith(job_id + ".")]
        return max(candidates, key=os.path.getmtime) if candidates else None

    # A plain song name is resolved to exactly one YouTube search result.
    # Never use a related/random result for the user's explicit .play request.
    search = query if query.startswith(("http://", "https://", "ytsearch", "yt://")) else f"ytsearch1:{query}"
    errors = []
    youtube_clients = (
        ("android", "web"),
        ("android_vr", "ios", "web"),
        ("tv", "ios", "android_vr"),
        ("web_safari", "android"),
        None,
    )
    for clients in youtube_clients:
        opts = dict(base)
        if clients:
            opts["extractor_args"] = {"youtube": {"player_client": list(clients)}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search, download=True)
                if info and info.get("entries"):
                    info = next((x for x in info["entries"] if x), None)
                if not info:
                    raise RuntimeError("No audio result found")
                path = _find_file(info, ydl)
                if not path or not os.path.exists(path):
                    raise FileNotFoundError("Downloaded audio file not found")
                return path, info
        except Exception as exc:
            errors.append(exc)
            msg = str(exc).lower()
            # If YouTube is clearly blocked, don't waste time on every possible client.
            if any(x in msg for x in ("sign in to confirm", "not a bot", "http error 403")) and clients in (("tv", "ios", "android_vr"), None):
                break

    # Render/YouTube often blocks yt-dlp. SoundCloud is a practical audio fallback.
    try:
        sc_query = query
        if query.startswith("http://") or query.startswith("https://"):
            sc_query = query
        else:
            sc_query = f"scsearch1:{query}"
        opts = dict(base)
        opts.pop("extractor_args", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(sc_query, download=True)
            if info and info.get("entries"):
                info = next((x for x in info["entries"] if x), None)
            if info:
                path = _find_file(info, ydl)
                if path and os.path.exists(path):
                    return path, info
    except Exception as exc:
        errors.append(exc)

    raise errors[-1] if errors else RuntimeError("Music download failed")


async def _start_stream(call, chat_id, path):
    stream = path
    if MediaStream is not None:
        try:
            stream = MediaStream(
                path,
                audio_flags=MediaStream.Flags.REQUIRED,
                video_flags=MediaStream.Flags.IGNORE,
            )
        except Exception:
            stream = path
    try:
        await call.play(chat_id, stream, config=GroupCallConfig(auto_start=False))
    except Exception as exc:
        if type(exc).__name__ != "NoActiveGroupCall":
            raise
        # Group me VC on nahi tha: ek baar khud start karke try karo
        await call.play(chat_id, stream, config=GroupCallConfig(auto_start=True))


async def _stop_stream(client, chat_id):
    """VC se leave karo taaki audio sach me band ho."""
    try:
        _, call = await _bridge(client)
        leave = getattr(call, "leave_call", None) or getattr(call, "leave_group_call", None)
        if leave:
            await leave(int(chat_id))
    except Exception as exc:
        log.warning("Leave VC failed: %s: %s", type(exc).__name__, exc)


async def _play_track(client, chat_id, path, info):
    state = _state(client, chat_id)
    try:
        await _stage(_join(client, int(chat_id)), 45, "VC join")
        _dbg("join ok", chat_id)
        _, call = await _stage(_bridge(client), 45, "pytgcalls start")
        await _stage(_start_stream(call, int(chat_id), path), 60, "Stream start")
        _dbg("stream started", chat_id)
    except Exception as exc:
        _dbg("play failed:", type(exc).__name__, exc)
        # play fail hua: current set mat karo, warna agle .play hamesha "Queued" me jayenge
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        raise
    state["current"] = {"path": path, "info": info}

    old = state.get("task")
    if old and not old.done() and old is not asyncio.current_task():
        old.cancel()

    duration = float(info.get("duration") or 0)
    state["task"] = asyncio.create_task(_track_watcher(client, int(chat_id), path, info, duration))


async def _track_watcher(client, chat_id, path, info, duration):
    try:
        await asyncio.sleep(max(2.0, duration + 1.5) if duration else 3600)
        state = _state(client, chat_id)
        current = state.get("current")
        if not current or current.get("path") != path:
            return

        state["current"] = None
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

        while state["queue"]:
            item = state["queue"].pop(0)
            try:
                await _play_track(client, chat_id, item["path"], item["info"])
                return
            except Exception as exc:
                _dbg("queued track failed, next try:", type(exc).__name__, exc)

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Track watcher failed: %s: %s", type(exc).__name__, exc)
    finally:
        state = _state(client, chat_id)
        if state.get("current") is None and state.get("task") is asyncio.current_task():
            state["task"] = None


async def play_song_in_chat(client, chat_id: int, query: str, enqueue=False, progress=None):
    """Download and play in this group's VC using the connected account."""
    _dbg("download start:", query)
    try:
        path, info = await asyncio.wait_for(asyncio.to_thread(_download_song, query), timeout=180)
    except asyncio.TimeoutError:
        raise RuntimeError("Download 180s me nahi hua (YouTube block ya slow). cookies.txt check karo.")
    except Exception as exc:
        _dbg("download failed:", type(exc).__name__, str(exc)[:300])
        raise
    _dbg("download ok:", path)
    state = _state(client, chat_id)
    if enqueue and state.get("current"):
        state["queue"].append({"path": path, "info": info})
        return path, info, True
    if progress:
        try:
            await progress("✅ <b>ᴅᴏᴡɴʟᴏᴀᴅ ʜᴏ ɢᴀʏᴀ</b>\n\n🔊 ᴠᴄ ᴍᴇ ᴊᴏɪɴ ʜᴏ ʀᴀʜᴀ ʜᴀɪ...")
        except Exception:
            pass
    await _play_track(client, int(chat_id), path, info)
    return path, info, False


@Client.on_message(filters.group & (filters.incoming | filters.me) & filters.command(["play", "song", "vplay"], [".", "/"]))
async def play_handler(client, message):
    # The connected /add account performs playback. Incoming group messages
    # from ANY member are accepted; the sender does not need to be the
    # connected account.
    _dbg(".play received chat=", getattr(message.chat, "id", None), "from=", getattr(message.from_user, "id", None))
    if not message.from_user:
        return
    if not message.chat or getattr(message.chat.type, "value", message.chat.type) not in {"group", "supergroup"}:
        return await message.reply(
            "<blockquote>❌ ᴜsᴇ <code>.play sᴏɴɢ ɴᴀᴍᴇ</code> ɪɴsɪᴅᴇ ᴀ ɢʀᴏᴜᴘ.</blockquote>",
            parse_mode=enums.ParseMode.HTML,
        )

    if not _music_enabled[id(client)]:
        return await message.reply("🔴 <b>ᴍᴜsɪᴄ ɪs ᴏғғ ʙʏ ᴄᴏɴɴᴇᴄᴛᴇᴅ ɪᴅ ᴏᴡɴᴇʀ.</b> Use <code>.music on</code> / <code>/music on</code> to enable.", parse_mode=enums.ParseMode.HTML)

    parts = message.text.split(None, 1) if message.text else []
    query = parts[1].strip() if len(parts) > 1 else ""
    if not query:
        return await message.reply(
            "<blockquote>🎵 ᴜsᴀɢᴇ: <code>.play sᴏɴɢ ɴᴀᴍᴇ</code> ᴏʀ <code>/play sᴏɴɢ ɴᴀᴍᴇ</code></blockquote>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await message.reply(
        "<blockquote>🔎 <b>sᴇᴀʀᴄʜɪɴɢ...</b>\n\n🎵 <code>" + query.replace("<", "&lt;").replace(">", "&gt;") + "</code></blockquote>",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        current = _state(client, message.chat.id).get("current")
        async def _progress(text):
            await status.edit("<blockquote>" + text + "</blockquote>", parse_mode=enums.ParseMode.HTML)

        path, info, queued = await play_song_in_chat(client, message.chat.id, query, enqueue=bool(current), progress=_progress)
        title = _safe_title(info, query)
        duration = info.get("duration_string") or "Unknown"
        if queued:
            await status.edit(
                f"<blockquote>📥 <b>ǫᴜᴇᴜᴇᴅ</b>\n\n🎵 <b>{title}</b>\n⏱️ {duration}</blockquote>",
                parse_mode=enums.ParseMode.HTML,
            )
            # Queued files are owned by the queue and are cleaned by the watcher.
            return

        await status.edit(
            f"<blockquote>▶️ <b>ɴᴏᴡ ᴘʟᴀʏɪɴɢ</b>\n\n🎵 <b>{title}</b>\n⏱️ {duration}\n🔊 <b>ᴘʟᴀʏɪɴɢ ɪɴ ᴛʜɪs ᴠᴄ</b>\n👤 <b>ᴄᴏɴɴᴇᴄᴛᴇᴅ ɪᴅ</b></blockquote>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as exc:
        log.exception("Music play failed")
        reason = f"{type(exc).__name__}: {str(exc)[:180]}"
        if type(exc).__name__ == "NoActiveGroupCall":
            reason = "VC on nahi hai. Group me pehle voice chat start karo (ya assistant ko manage video chats admin right do)."
        try:
            await status.edit(
                f"<blockquote>❌ <b>ᴍᴜsɪᴄ ғᴀɪʟᴇᴅ</b>\n\n<code>{html.escape(reason, quote=False)}</code></blockquote>",
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass


async def _owner_command(client, message):
    owner_id = await _owner_uid(client)
    sender = getattr(message, "from_user", None)
    return bool(owner_id and (getattr(message, "outgoing", False) or (sender and sender.id == owner_id)))


async def _stop_all_music(client):
    client_key = id(client)
    for (key, chat_id), state in list(_music.items()):
        if key != client_key:
            continue
        task = state.get("task")
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
        items = ([state.get("current")] if state.get("current") else []) + list(state.get("queue", []))
        for item in items:
            path = item.get("path") if item else None
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        state["current"] = None
        state["queue"].clear()
        state["task"] = None
        await _stop_stream(client, chat_id)


@Client.on_message((filters.group | filters.private) & (filters.incoming | filters.me) & filters.command("music", [".", "/"]))
async def music_toggle_command(client, message):
    if not await _owner_command(client, message):
        return
    args = message.command[1:] if len(message.command) > 1 else []
    action = args[0].lower() if args else "status"
    enabled = _music_enabled[id(client)]
    if action == "off":
        _music_enabled[id(client)] = False
        await _stop_all_music(client)
        return await message.reply("🔴 <b>ᴍᴜsɪᴄ ɢʟᴏʙᴀʟʟʏ ᴏғғ.</b> All active VC playback stopped and queues cleared.", parse_mode=enums.ParseMode.HTML)
    if action == "on":
        _music_enabled[id(client)] = True
        return await message.reply("🟢 <b>ᴍᴜsɪᴄ ᴏɴ.</b> New <code>.play</code> requests can play again.", parse_mode=enums.ParseMode.HTML)
    status = "🟢 ON" if enabled else "🔴 OFF"
    return await message.reply(f"🎵 <b>ᴍᴜsɪᴄ ɢʟᴏʙᴀʟ sᴛᴀᴛᴜs:</b> {status}", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.group & (filters.incoming | filters.me) & filters.command("autoplay", [".", "/"]))
async def autoplay_command(client, message):
    if not message.from_user:
        return
    if not message.chat or getattr(message.chat.type, "value", message.chat.type) not in {"group", "supergroup"}:
        return await message.reply("❌ ᴜsᴇ <code>.autoplay</code> ɪɴsɪᴅᴇ ᴀ ɢʀᴏᴜᴘ.", parse_mode=enums.ParseMode.HTML)
    return await message.reply("🔁 Related-song autoplay is disabled. Playback advances only through queued songs and stops when the queue is empty.")


@Client.on_message(filters.group & (filters.incoming | filters.me) & filters.command("queue", [".", "/"]))
async def queue_command(client, message):
    if not message.from_user:
        return
    if not message.chat or getattr(message.chat.type, "value", message.chat.type) not in {"group", "supergroup"}:
        return await message.reply("❌ ᴜsᴇ <code>.queue</code> ɪɴsɪᴅᴇ ᴀ ɢʀᴏᴜᴘ.", parse_mode=enums.ParseMode.HTML)
    state = _state(client, message.chat.id)
    lines = []
    if state.get("current"):
        lines.append("▶️ " + html.escape(str(state["current"]["info"].get("title") or "Current")[:80], quote=False))
    for i, item in enumerate(state["queue"], 1):
        lines.append(f"{i}. " + html.escape(str(item["info"].get("title") or "Queued")[:80], quote=False))
    if not lines:
        return await message.reply("📭 ǫᴜᴇᴜᴇ ᴇᴍᴘᴛʏ")
    await message.reply("<blockquote>🎵 <b>ǫᴜᴇᴜᴇ</b>\n\n" + "\n".join(lines) + "</blockquote>", parse_mode=enums.ParseMode.HTML)


async def _is_group_admin(client, message):
    """Return True only when the command sender is a group admin/owner."""
    if not message.chat or getattr(message.chat.type, "value", message.chat.type) not in {"group", "supergroup"}:
        return False
    user = getattr(message, "from_user", None)
    if not user:
        return False
    if getattr(message, "outgoing", False) or user.id == await _owner_uid(client):
        return True
    try:
        member = await client.get_chat_member(message.chat.id, user.id)
        status = getattr(member.status, "value", member.status)
        return status in {ChatMemberStatus.ADMINISTRATOR.value, ChatMemberStatus.OWNER.value, "administrator", "owner", "creator"}
    except Exception:
        return False


@Client.on_message(filters.group & (filters.incoming | filters.me) & filters.command("skip", [".", "/"]))
async def skip_command(client, message):
    if not await _owner_uid(client):
        return
    if not await _is_group_admin(client, message):
        return await message.reply("❌ <b>ᴀᴅᴍɪɴ ᴏɴʟʏ</b>\n\nᴏɴʟʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs ᴄᴀɴ ᴜsᴇ <code>.skip</code> / <code>/skip</code>.", parse_mode=enums.ParseMode.HTML)
    state = _state(client, message.chat.id)
    if not state.get("current"):
        return await message.reply("❌ ɴᴏ ᴛʀᴀᴄᴋ ɪs ᴘʟᴀʏɪɴɢ")
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    current_item = state["current"]
    current_path = current_item["path"]
    current_title = str(current_item.get("info", {}).get("title") or "song")
    try:
        if os.path.exists(current_path):
            os.remove(current_path)
    except OSError:
        pass
    state["current"] = None
    while state["queue"]:
        item = state["queue"].pop(0)
        try:
            await _play_track(client, message.chat.id, item["path"], item["info"])
        except Exception as exc:
            _dbg("skip: queued track failed:", type(exc).__name__, exc)
            continue
        return await message.reply("⏭️ ɴᴇxᴛ sᴏɴɢ ᴘʟᴀʏɪɴɢ")
    await _stop_stream(client, message.chat.id)
    await message.reply("⏭️ ᴛʀᴀᴄᴋ sᴋɪᴘᴘᴇᴅ")


@Client.on_message(filters.group & (filters.incoming | filters.me) & filters.command("stop", [".", "/"]))
async def stop_command(client, message):
    if not await _owner_uid(client):
        return
    if not await _is_group_admin(client, message):
        return await message.reply("❌ <b>ᴀᴅᴍɪɴ ᴏɴʟʏ</b>\n\nᴏɴʟʏ ɢʀᴏᴜᴘ ᴀᴅᴍɪɴs ᴄᴀɴ ᴜsᴇ <code>.stop</code> / <code>/stop</code>.", parse_mode=enums.ParseMode.HTML)
    state = _state(client, message.chat.id)
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    for item in ([state.get("current")] if state.get("current") else []) + state["queue"]:
        if item and os.path.exists(item.get("path", "")):
            try:
                os.remove(item["path"])
            except OSError:
                pass
    state["current"] = None
    state["queue"].clear()
    await _stop_stream(client, message.chat.id)
    return await message.reply("⏹️ ᴍᴜsɪᴄ sᴛᴏᴘᴘᴇᴅ")


add_command_help(
    "Music",
    [
        ["play", "Any group member can use `.play song name` or `/play song name`. The connected ID joins the current group VC and plays the requested song."],
        ["autoplay on|off", "Legacy setting; playback only advances through queued songs and stops when the queue is empty."],
        ["queue", "Any group member can view the current song and queued songs."],
        ["skip", "Group admins only: skip the current song and play the next queued song; stops if queue is empty."],
        ["stop", "Group admins only: stop music and clear the queue in this group."],
        ["music on|off", "Connected-ID owner only. Globally enable or disable music in every group VC for this connected account."],
        ["vplay", "Any group member can use `.vplay song name`; it follows the same playback flow as `.play`."],
    ],
)
