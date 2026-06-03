from utils.read_sources import load_sources_from_file
from database import SQLiteManager
from database import KeywordCreate
from database import KeywordRepository
import asyncio


async def main() -> None:
    manager = SQLiteManager("url_scraper.db")
    await manager.connect()
    repo = KeywordRepository(manager)

    groups = await repo.get_labels()
    for g in groups:
        print(g)
        
    await manager.disconnect()

