"""One-off send script for Task E: Shadow XM6 lots."""

import asyncio
import json
import sqlite3
import time
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

env_path = "/home/egor/scraper/avito-hunter/.env"
env = {}
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")

token = env.get("TELEGRAM_BOT_TOKEN")
chat_id = env.get("TELEGRAM_CHAT_ID")
proxy = env.get("HTTP_PROXY") or "http://127.0.0.1:10809"

lots = [
    {
        "id": "8251818479",
        "title": "Наушники sony wh 1000xm6 в Севастополе",
        "price": 15000,
        "url": "https://www.avito.ru/sevastopol/audio_i_video/naushniki_sony_wh_1000xm6_8251818479",
        "city": "Севастополь",
    },
    {
        "id": "8300098203",
        "title": "Наушники беспроводные Sony WH-1000XM6 в Екатеринбурге",
        "price": 15000,
        "url": "https://www.avito.ru/ekaterinburg/audio_i_video/naushniki_besprovodnye_sony_wh-1000xm6_8300098203",
        "city": "Екатеринбург",
    },
]


def make_keyboard(avito_id: str, url: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔗 Открыть на Авито", url=url)],
        [
            InlineKeyboardButton(text="👍 норм", callback_data=f"fb:{avito_id}:ok"),
            InlineKeyboardButton(text="🚫 фейк", callback_data=f"fb:{avito_id}:fake"),
        ],
        [
            InlineKeyboardButton(text="📈 дорого", callback_data=f"fb:{avito_id}:expensive"),
            InlineKeyboardButton(text="❌ не та модель", callback_data=f"fb:{avito_id}:wrong_model"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def main():
    session = AiohttpSession(proxy=proxy)
    bot = Bot(token=token, session=session)

    db_path = "/home/egor/scraper/avito-hunter/data/hunter.db"
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    for idx, lot in enumerate(lots):
        text = (
            "найдено до включения алертов\n"
            "headphones / sony_xm6\n"
            f"{lot['title']}\n"
            f"цена {lot['price']} ₽  (потолок 16100 ₽)\n"
            "модель: sony_wh1000xm6\n"
            "⚠ цена подозрительно низкая, проверь оригинал и серийный номер"
        )
        markup = make_keyboard(lot["id"], lot["url"])
        try:
            msg = await bot.send_message(chat_id, text, reply_markup=markup)
            print(f"SENT lot {lot['id']}: message_id={msg.message_id}")

            now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            payload = json.dumps(lot)
            key = f"{lot['id']}:watch:{lot['price']}:sony_xm6"
            cur.execute(
                """
                INSERT INTO notifications(avito_id, channel, idempotency_key, verdict, sent_at, status, attempts, payload, created_at)
                VALUES (?, 'telegram', ?, 'watch', ?, 'sent', 1, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = 'sent',
                    sent_at = excluded.sent_at
            """,
                (lot["id"], key, now_iso, payload, now_iso),
            )
            con.commit()
            print(f"MARKED SENT in DB for {lot['id']}")
        except Exception as e:
            print(f"FAILED sending lot {lot['id']}: {e}")

        if idx < len(lots) - 1:
            await asyncio.sleep(1.5)

    con.close()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
