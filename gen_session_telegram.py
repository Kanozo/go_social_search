import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 12880411
API_HASH = "fac717534b467665c05b1d417df5f30d"
   

async def main():
    session = StringSession()
    async with TelegramClient(session, API_ID, API_HASH) as client:
        print("✅ StringSession generada:")
        print(session.save())

asyncio.run(main())