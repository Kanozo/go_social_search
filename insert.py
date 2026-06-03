from utils.read_sources import load_sources_from_file
from database import SQLiteManager
from database import KeywordCreate
from database import KeywordRepository
import asyncio

async def main() -> None:

    manager = SQLiteManager("url_scraper.db")
    await manager.connect()
    repo = KeywordRepository(manager)

    items = load_sources_from_file('search/google.txt')

    for item in items:
        key = KeywordCreate(
            keyword=item,
            label="Google",
            platform="FB - IG",
            engine_id="google",
            classification="neutro"
        )
        result = await repo.insert_one(key)
        print(result)


    await manager.disconnect()


if __name__ == "__main__":
    asyncio.run(main())