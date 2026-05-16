import asyncio
import logging
from telegram_notifier import TelegramNotifier
from database.google_result_db import GoogleResultRepository
from database.core_db import DatabaseManager

# Configurar logging una sola vez
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main() -> None:
    # Simulación de carga de variables (usar .env en realidad)
    API_ID = 12880411
    API_HASH = "fac717534b467665c05b1d417df5f30d"
    SESSION_STRING = "1AZWarzQBu4XZ6WsfmOXfZmAoL4yoZ2srveRf__APFVRNaAnnw7mRJD4JvmPitgik9dixu3HdHELOUN2BzlbRbEFwSN9xELIPLj-CbwFm0AVZQbwgDDTWaY9j6GhatqaA_pwKvEUxnt-_pMF6N9vWPa3wxFI05GCaqX8SsPZP-FIvO6DVlavJETO0By6IQj-Y0_mS4AhXWB6M1PYY1B7f9iTDLKDmic-jb_rY_8x--Q0FuzQuYib2RE8jUTR24u_kAZ9lJvli8mfvCteKAUdSpBySaRYbbX7ZItDkltdCTNtj0ujqxJtBl47Y9kwKQHC_6XpQ-tZRCz1XDiKkAolkUTfUmn_phTc="
    GROUP_TARGET = -1003857299252

    from config.settings import settings
    db_manager = DatabaseManager(settings.MONGO_URL, settings.DB_NAME)
    await db_manager.connect()
    logger.info(f"Conexión a MongoDB establecida: {settings.DB_NAME}")
    repo = GoogleResultRepository(db_manager)
    await repo.initialize()
    
    try:
        async with TelegramNotifier(API_ID, API_HASH, SESSION_STRING, GROUP_TARGET) as bot:
            while True: 
                pending = await repo.get_unprocessed()
                
                for item in pending:
                    
                    
                    if await bot.send_message(item.url):
                        await repo.mark_as_processed(item.id)
                        logger.info(f"Notificado y actualizado: {item.url}")
                        await asyncio.sleep(2)
                    else:
                        logger.warning(f"No se pudo enviar: {item.url}")
                await asyncio.sleep(120)

    except Exception as e:
        logger.error(f"Error en el proceso: {e}")
    finally:
        # Si db_manager no tiene .close(), accede al cliente interno (motor/pymongo)
        if hasattr(db_manager, 'client'):
            db_manager.client.close()
            logger.info("Conexión a MongoDB cerrada.")

if __name__ == "__main__":
    asyncio.run(main())