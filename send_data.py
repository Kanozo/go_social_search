import json
import asyncio
from database import SQLiteManager, PostRepository
from reaper import scrape, ScrapingError
from reaper.utils import datetime_encoder
from supabase import create_client, Client

url = "https://wpsxnyzeyrrxostzqifh.supabase.co/"
key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indwc3hueXpleXJyeG9zdHpxaWZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAxMzE1OTAsImV4cCI6MjA5NTcwNzU5MH0.z3tKNqATfXyxKcroVxWOmTFp84rqP3VDvAX6KitG5DQ"

# Crear el cliente de Supabase
supabase: Client = create_client(url, key)

print("✅ Conexión establecida con Supabase")

def ok(msg: str) -> None:
    print(f"  ✅ {msg}")

def fail(msg: str) -> None:
    print(f"  ❌ {msg}")

def separador(titulo: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {titulo}")
    print(f"{'─' * 60}")

def send_resultado(result: dict) -> None:
    """Imprime el resultado completo formateado."""
    from supabase import create_client, Client
    separador("RESULTADO COMPLETO (JSON)")
    try:
        data = json.dumps(
            result,
            default=datetime_encoder
        )

        if not result.get("id"):
            raise Exception(result.get("error"))

        nuevo_post = {
            "id_post": result.get("id"),
            "data": data
        }

        # 3. Ejecutar el INSERT
        respuesta = supabase.table("Instagram").insert(nuevo_post).execute()
        
        print("✅ Insertado correctamente:")
        print(f"Insetado nuevo post de instagram con id {respuesta.data[0].get('id')}")  
        return True

    except Exception as e:
        print("❌ Error al insertar:", e)
        return False

async def test_scrape_facebook(url):
    try:
        result = await scrape(
            url,
            headless=True,
            debug=False,
            auto_scroll=False,
            infinity_scroll=False,
        )

        if result.get("raw_data_available"):
            ok("raw_data_available = True")
            # Campos específicos del reel
            if result.get("author"):
                ok(f"author.name = {result['author'].get('name', 'N/A')}")
            if result.get("reaction_count") is not None:
                ok(f"reaction_count = {result['reaction_count']}")
            if result.get("id"):
                ok(f"id = {result['id']}")
        else:
            fail(f"raw_data_available = False | error = {result.get('error')}")
            return result

        return result

    except ScrapingError as exc:
        fail(f"ScrapingError: {exc}")
        return False
    except Exception as exc:
        fail(f"Error inesperado: {type(exc).__name__}: {exc}")
        return False

async def main():
    while True:
        try:
            manager = SQLiteManager("url_scraper.db")
            await manager.connect()
            repo = PostRepository(manager)

            posts = await repo.get_pending_send(platform="instagram", limit=1)
            for post in posts:
                result = await test_scrape_facebook(post.url)
                respuesta = send_resultado(result)
                if respuesta:
                    await repo.mark_sent_to_db(post.url)
                    await repo.mark_sent(post.url)

        except Exception as exc:
            fail(f"Error inesperado: {type(exc).__name__}: {exc}")
        finally:
            asyncio.timeout(3)
            await manager.disconnect()

if __name__ == "__main__":
    asyncio.run(main())