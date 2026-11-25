"""WordSeek continuous auto-player for connected /add accounts.
Uses WordSeek's bundled common word pools and its wh:<state>:<letter> feedback buttons.
"""
import asyncio
import json
import logging
import os
import random
import re
from collections import Counter

from pyrogram import Client, filters
from pyrogram.types import Message

from Waste.modules.help import add_command_help

log = logging.getLogger(__name__)
DATA_DIR = os.path.join(os.path.dirname(__file__), "wordseek_data")
WORD_POOLS = {}
for _length in (4, 5, 6):
    with open(os.path.join(DATA_DIR, f"common-{_length}.json"), encoding="utf-8") as _f:
        WORD_POOLS[_length] = [w.lower() for w in json.load(_f) if isinstance(w, str)]

# One active worker per connected account + chat.
_workers = {}


def _evaluate(guess, solution):
    """Return WordSeek/Wordle states, respecting duplicate letters."""
    remaining = Counter(solution)
    result = ["absent"] * len(guess)
    for i, ch in enumerate(guess):
        if ch == solution[i]:
            result[i] = "correct"
            remaining[ch] -= 1
    for i, ch in enumerate(guess):
        if result[i] == "absent" and remaining[ch] > 0:
            result[i] = "present"
            remaining[ch] -= 1
    return result


def _feedback_from_message(message, expected_length):
    """Read the per-letter callback_data emitted by WordSeek rich feedback."""
    markup = getattr(message, "reply_markup", None)
    keyboard = getattr(markup, "inline_keyboard", None) if markup else None
    if not keyboard:
        return None
    for row in reversed(keyboard):
        parsed = []
        for button in row:
            data = getattr(button, "callback_data", None)
            if not data:
                continue
            match = re.fullmatch(r"wh:(correct|present|absent):([a-zA-Z])", data)
            if match:
                parsed.append((match.group(1), match.group(2).lower()))
        if len(parsed) == expected_length:
            return [state for state, _letter in parsed]
    return None


async def _wait_incoming(client, chat_id, after_id, timeout=22, predicate=None):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            async for msg in client.get_chat_history(chat_id, limit=20):
                if msg.id <= after_id or msg.outgoing:
                    continue
                if predicate is None or predicate(msg):
                    return msg
        except Exception as exc:
            log.debug("WordSeek history poll failed: %s", exc)
        await asyncio.sleep(0.6)
    return None


def _is_game_started(msg):
    text = (msg.text or msg.caption or "").lower()
    return "game started" in text and "letter word" in text


def _is_win_or_end(msg):
    text = (msg.text or msg.caption or "").lower()
    return ("congrats! you guessed it correctly" in text or
            "game over! the word was" in text or
            "there is already a game in progress" in text or
            "only these lengths are allowed" in text)


async def _solve_loop(client, chat_id, length, status_message):
    key = (id(client), chat_id)
    try:
        while not asyncio.current_task().cancelled():
            start = await client.send_message(chat_id, f"/new{length}")
            started = await _wait_incoming(client, chat_id, start.id, predicate=_is_game_started)
            if not started:
                await status_message.edit_text("❌ WordSeek game start reply nahi mila. Check karo ki WordSeek bot is chat mein available hai.")
                return

            candidates = list(WORD_POOLS[length])
            guessed = set()
            game_won = False
            for attempt in range(30):
                if asyncio.current_task().cancelled():
                    return
                remaining = [w for w in candidates if w not in guessed]
                if not remaining:
                    break
                # First guess is a common WordSeek solution; thereafter use remaining candidates.
                guess = remaining[0]
                guessed.add(guess)
                sent = await client.send_message(chat_id, guess)
                response = await _wait_incoming(
                    client, chat_id, sent.id,
                    predicate=lambda m: bool(getattr(getattr(m, "reply_markup", None), "inline_keyboard", None)) or _is_win_or_end(m),
                )
                if not response:
                    await status_message.edit_text("⚠️ Guess bhej diya, lekin WordSeek ka feedback nahi mila. Auto mode stop kar diya.")
                    return
                response_text = (response.text or response.caption or "").lower()
                if "congrats! you guessed it correctly" in response_text:
                    game_won = True
                    await status_message.edit_text(f"✅ {length}-letter game solved in {attempt + 1} guesses. Next game starting...")
                    break
                if "game over! the word was" in response_text:
                    break
                if "already guessed" in response_text:
                    continue
                feedback = _feedback_from_message(response, length)
                if feedback is None:
                    await status_message.edit_text("⚠️ WordSeek feedback buttons read nahi ho paaye. Auto mode stop kar diya.")
                    return
                candidates = [w for w in candidates if _evaluate(guess, w) == feedback]
                if not candidates:
                    await status_message.edit_text("⚠️ Feedback se matching word nahi mila. Auto mode stop kar diya.")
                    return
                await asyncio.sleep(0.45)

            if asyncio.current_task().cancelled():
                return
            if game_won:
                await asyncio.sleep(1.2)
                continue
            # WordSeek ends after 30 attempts. Stop rather than accidentally restarting a failed run.
            await status_message.edit_text("ℹ️ Current game ended without a solve. Auto mode stopped. Use .auto" + str(length) + " to start again.")
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.exception("WordSeek auto solver error")
        try:
            await status_message.edit_text(f"❌ Auto solver error: {type(exc).__name__}: {exc}")
        except Exception:
            pass
    finally:
        _workers.pop(key, None)


@Client.on_message(filters.command(["auto4", "auto5", "auto6"], prefixes=[".", "/"]) & filters.me)
async def wordseek_auto(client: Client, message: Message):
    length = int(message.command[0][-1])
    key = (id(client), message.chat.id)
    old = _workers.get(key)
    if old and not old.done():
        return await message.reply_text("⚠️ Is chat mein Auto WordSeek pehle se chal raha hai. Band karne ke liye .stop bhejo.")
    status = await message.reply_text(f"🚀 WordSeek Auto-{length} started. Game start karke automatically solve karta rahega. Stop: .stop")
    task = asyncio.create_task(_solve_loop(client, message.chat.id, length, status))
    _workers[key] = task


@Client.on_message(filters.command(["stop"], prefixes=[".", "/"]) & filters.me)
async def wordseek_stop(client: Client, message: Message):
    # One .stop from the connected account stops every group-specific worker.
    account_key = id(client)
    tasks = [task for (client_key, _chat_id), task in list(_workers.items())
             if client_key == account_key and not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await message.reply_text("WordSeek Auto Solver stopped in all chats for this connected account.")
    else:
        await message.reply_text("No active WordSeek auto sessions were found for this account.")


add_command_help("wordseek", [
    [".auto4", "Automatically start and solve 4-letter WordSeek games continuously."],
    [".auto5", "Automatically start and solve 5-letter WordSeek games continuously."],
    [".auto6", "Automatically start and solve 6-letter WordSeek games continuously."],
    [".stop", "Stop the active WordSeek auto solver in this chat."],
])
