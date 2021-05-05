#!/usr/local/bin/python3
# coding: utf-8

# ytdl-bot - bot.py
# 5/3/21 18:31
#

__author__ = "Benny <benny.think@gmail.com>"

import tempfile
import os
import re
import logging
import threading
import asyncio
import traceback
import functools

import fakeredis
import youtube_dl
from FastTelethon.FastTelethon import upload_file
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo
from telethon.utils import get_input_media
from hachoir.metadata.video import MkvMetadata
from tgbot_ping import get_runtime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(filename)s [%(levelname)s]: %(message)s')

token = os.getenv("TOKEN") or "17Zg"
app_id = int(os.getenv("APP_ID") or "922")
app_hash = os.getenv("APP_HASH") or "490"

bot = TelegramClient('bot', app_id, app_hash).start(bot_token=token)

r = fakeredis.FakeStrictRedis()

EXPIRE = 5


async def upload_callback(current, total, chat_id, message):
    key = f"{chat_id}-{message.id}"
    # if the key exists, we shouldn't send edit message
    if not r.exists(key):
        r.set(key, "ok", ex=EXPIRE)
        msg = f'Uploading {round(current / total * 100, 2)}%: {current}/{total}'
        await bot.edit_message(chat_id, message, msg)


async def sync_edit_message(chat_id, message, msg):
    # try to avoid flood
    key = f"{chat_id}-{message.id}"
    if not r.exists(key):
        r.set(key, "ok", ex=EXPIRE)
        await bot.edit_message(chat_id, message, msg)


def go(chat_id, message, msg):
    asyncio.run(sync_edit_message(chat_id, message, msg))


def progress_hook(d: dict, chat_id, message):
    if d['status'] == 'downloading':
        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate", "N/A")
        percent = d.get("_percent_str", "N/A")
        speed = d.get("_speed_str", "N/A")
        msg = f'Downloading {percent}: {downloaded}/{total} @ {speed}'
        threading.Thread(target=go, args=(chat_id, message, msg)).start()


def run_in_executor(f):
    @functools.wraps(f)
    def inner(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: f(*args, **kwargs))

    return inner


@run_in_executor
def ytdl_download(url, tempdir, chat_id, message) -> dict:
    response = dict(status=None, error=None, filepath=None)
    logging.info("Downloading for %s", url)
    output = os.path.join(tempdir, '%(title)s.%(ext)s')
    ydl_opts = {
        'progress_hooks': [lambda d: progress_hook(d, chat_id, message)],
        'outtmpl': output,
        'restrictfilenames': True
    }
    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        response["status"] = True
        response["filepath"] = os.path.join(tempdir, [i for i in os.listdir(tempdir)][0])
    except Exception:
        err = traceback.format_exc()
        logging.error("Download  failed for %s ", url)
        response["status"] = False
        response["error"] = err

    return response


@bot.on(events.NewMessage(pattern='/start'))
async def send_welcome(event):
    async with bot.action(event.chat_id, 'typing'):
        await bot.send_message(event.chat_id, "Wrapper for youtube-dl.")
        raise events.StopPropagation


@bot.on(events.NewMessage(pattern='/help'))
async def send_welcome(event):
    async with bot.action(event.chat_id, 'typing'):
        await bot.send_message(event.chat_id, "Bot is not working? "
                                              "Wait a few seconds, send your link again or report bugs at "
                                              "https://github.com/tgbot-collection/ytdl-bot/issues")
        raise events.StopPropagation


@bot.on(events.NewMessage(pattern='/ping'))
async def send_welcome(event):
    async with bot.action(event.chat_id, 'typing'):
        bot_info = get_runtime("botsrunner_ytdl_1", "YouTube-dl")
        await bot.send_message(event.chat_id, f"{bot_info}\n", parse_mode='md')
        raise events.StopPropagation


@bot.on(events.NewMessage(pattern='/about'))
async def send_welcome(event):
    async with bot.action(event.chat_id, 'typing'):
        await bot.send_message(event.chat_id, "YouTube-DL by @BennyThink\n"
                                              "GitHub: https://github.com/tgbot-collection/ytdl-bot")
        raise events.StopPropagation


@bot.on(events.NewMessage(incoming=True))
async def echo_all(event):
    chat_id = event.message.chat_id
    url = event.message.text
    logging.info("start %s", url)
    if not re.findall(r"^https?://", url.lower()):
        await event.reply("I think you should send me a link. Don't you agree with me?")
        return
    message = await event.reply("Processing...")
    temp_dir = tempfile.TemporaryDirectory()

    async with bot.action(chat_id, 'video'):
        logging.info("downloading start")
        result = await ytdl_download(url, temp_dir.name, chat_id, message)
        logging.info("downloading complete")

    if result["status"]:
        async with bot.action(chat_id, 'document'):
            video_path = result["filepath"]
            await bot.edit_message(chat_id, message, 'Download complete. Sending now...')
            metadata, mime_type = get_metadata(video_path)
            with open(video_path, 'rb') as f:
                input_file = await upload_file(
                    bot,
                    f,
                    progress_callback=lambda x, y: upload_callback(
                        x, y, chat_id, message))
            input_media = get_input_media(input_file)
            input_media.attributes = [
                DocumentAttributeVideo(
                    round_message=False,
                    supports_streaming=True,
                    **metadata
                ),
                DocumentAttributeFilename(
                    os.path.basename(video_path)),
                ]
            input_media.mime_type = mime_type
            await bot.send_file(chat_id, input_media)
            await bot.edit_message(chat_id, message, 'Download success!✅')
    else:
        async with bot.action(chat_id, 'typing'):
            tb = result["error"][0:4000]
            await bot.edit_message(chat_id, message, f"{url} download failed❌：\n```{tb}```",
                                   parse_mode='markdown')

    temp_dir.cleanup()


def get_metadata(video_path):
    try:
        metadata = extractMetadata(createParser(video_path))
        if isinstance(metadata, MkvMetadata):
            return dict(
                duration=metadata.get('duration').seconds,
                w=metadata['video[1]'].get('width'),
                h=metadata['video[1]'].get('height')
                ), metadata.get('mime_type')
        else:
            return dict(
                duration=metadata.get('duration').seconds,
                w=metadata.get('width'),
                h=metadata.get('height')
                ), metadata.get('mime_type')
    except Exception as e:
        logging.error(e)
        return dict(duration=0, w=0, h=0), 'application/octet-stream'


if __name__ == '__main__':
    bot.start()
    bot.run_until_disconnected()
