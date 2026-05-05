"""
run_scraper.py  (v3 — sin MongoDB)

Cambios respecto a v2:
    - Eliminados DatabaseManager, GoogleResultRepository y _setup_database().
    - ScraperOrchestrator ya no gestiona conexión a base de datos.
    - GoogleCSEAutomator se instancia sin db_manager ni results_repo.
    - run_with_page() se llama sin persist_to_db (parámetro eliminado en v4
      del automator).
    - El envío de URLs al store queda encapsulado en el automator.

Ejecución::

    python run_scraper.py
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from config.settings import settings
from google_cse_automator import BrowserConfig, GoogleCSEAutomator

logger = logging.getLogger(__name__)


class ScraperOrchestrator:
    """
    Orquestador de ciclos de scraping con reutilización de browser.

    Flujo por engine:
        1. Lanza un browser (Firefox).
        2. Crea un contexto con parámetros rotados (anti-fingerprint).
        3. Aplica stealth scripts.
        4. Ejecuta warmup de sesión (una vez por engine).
        5. Itera sobre keywords reutilizando la misma page.
        6. Cierra browser al terminar todas las keywords del engine.
    """

    def __init__(self) -> None:
        self._running = False

    async def _fetch_search_engines_config(self) -> list[dict]:
        """
        Placeholder: devuelve la configuración de motores y keywords.

        Returns:
            Lista de dicts con ``engine_id``, ``label`` y ``keywords``.
        """
        logger.info("[PLACEHOLDER] Obteniendo configuración de motores...")
        await asyncio.sleep(0.1)

        return [
            # {"label": "KW MONITOR", "engine_id": "b3d8ab5d4c4a84c70", "keywords": [
            #         "#CubaVive",
            #         "#DeZurdaTeam",
            #         "#YoSigoAMiPresidente",
            #         "#CubaPorLaSalud",
            #         "#NoAlTerrorismo",
            #         "#TumbaElBloqueo",
            #         "#NoMasBloqueo",
            #         "#CubaNoEstaSola",
            #         "#FidelPorSiempre",
            #         "#CubaCoopera",
            #         "#CubaPorLaVida",
            #         "#CubaSegura",
            #         "#HéroesDeAzul",
            #         "#ContraLasDrogasSeGana",
            #         "#CubaEstaFirme",
            #         "#100AñosConFidel",
            #         "#LaPatriaSeDefiende",
            #         "#CubaEstadoTerrorista",
            #         "#SOSCuba",
            #         "#PatriaYVida",
            #         "#CubaPaLaCalle",
            #         "#CubaEstadoFallido",
            #         "#LibertadParaLosPresosPoliticos",
            #         "#CubaEsUnaDictadura",
            #         "#DiazCanelSingao",
            #         "#11J",
            #         "#11JCuba",
            #         "#El11JVive",
            #         "#PresosPorque",
            #         "#FreeEl4tico",
            #         "#Free4tico",
            #         "#AbajoLaDictadura",
            #         "#NoMásMuela",
            #         "#CubaSoberana",
            #         "#95DeRaul",
            #         "#MisManosPorCuba",
            #         "#cubaestafirme",
            #         "#soscuba",
            #         "#mifirmaporlapatria",
            #         "#cubalibre",
            #         "#cubasoberana",
            #         "#cubaestadoterrorista",
            #         "#cubanosporelmundo",
            #         "#lapatriasedefiende",
            #         "#cubaviveensuhistoria",
            #         "#latijeranews",
            #         "#libertadparacuba",
            #         "#patriayvida",
            #         "#chevive",
            #         "#destacamentoderefuerzo",
            #         "#yosigoamipresidente",
            #         "#cubavencera",
            #         "#lahabana",
            #         "#crisisencuba",
            #         "#centinelasdelaverdad",
            #         "#tumbaelbloqueo",
            #         "#denunciaciudadana",
            #         "#unidosxcuba",
            #         "#latidoizquierdo",
            #         "#matancerosenvictoria",
            #         "#heroesdeazul",
            #         "#dictaduracubana",
            #         "#fidelporsiempre",
            #         "#cubaporlapaz",
            #         "#isladelajuventud",
            #         "#lms",
            #         "#camaguey",
            #         "#cubanos",
            #         "#conlaverdadsomosmasfuertes",
            #         "#cubaestadofallido",
            #         "#cubanet",
            #         "#granma",
            #         "#cubanosenflorida",
            #         "#abajoladictadura",
            #         "#libertad",
            #         "#cubanoserinde",
            #         "#fidel",
            #         "#cdrcuba",
            #         "#conelpieenelestribo",
            #         "#noticiasdecuba",
            #         "#bloqueogenocida",
            #         "#diariodecuba",
            #         "#mujeresenrevolucion",
            #         "#Cuba",
            #         "cubanos",
            #         "habana",
            #         "havana",
            #         "cubana",
            #         "díaz-canel"
            # ]},
            {"label": "general", "engine_id": "294a079ba2d4267d5", "keywords": [
                    # "https://www.facebook.com/PresidenciaDeCuba/posts/*",
                    # "https://www.facebook.com/gerardo.hernandez.nordelo/posts/*",
                    # "https://www.facebook.com/Gerardodelos5heroes/posts/*",
                    # "https://www.facebook.com/PartidoComunistadeCubacontinuadordeMartiyFidel/posts/*",
                    # "https://www.facebook.com/razonesdecuba.cu/posts/*",
                    # "https://www.facebook.com/GladysArtemisa/posts/*",
                    # "https://www.facebook.com/jorgeluis.brochelorenzo/posts/*",
                    # "https://www.facebook.com/RadioBayamo/posts/*",
                    # "'starlink revolico'",
                    # "https://www.facebook.com/groups/434004943672696/posts/*",
                    # "https://www.facebook.com/groups/1817905055123266/posts/*",
                    # "https://www.facebook.com/groups/anuncioscaibarien/posts/*",
                    # "https://www.facebook.com/groups/67706680225/posts/*",
                    # "https://www.facebook.com/profile.php?id=61575946707396/posts/*",
                    # "https://www.facebook.com/profile.php?id=61559784217848/posts/*",
                    # "https://www.facebook.com/groups/3170984329754811/posts/*",
                    # "https://www.facebook.com/groups/3063203460404398/posts/*",
                    # "https://www.facebook.com/groups/2778177215613910/posts/*",
                    # "#LaPatriaSeDefiende",
                    # "https://www.facebook.com/groups/1315855495741236/posts/*",
                    # "https://www.facebook.com/america.libre.754277/posts/*",
                    # "https://www.facebook.com/ranchueleros.por.la.libertad/posts/*",
                    # "https://www.facebook.com/cladestino.cubano/posts/*",
                    # "https://www.facebook.com/juana.arencibia.3/posts/*",
                    # "https://facebook.com/groups/463363023678634/posts/*",
                    # "https://www.facebook.com/groups/521067779912295/posts/*",
                    # "https://www.facebook.com/groups/2812476895679150/posts/*",
                    # "https://www.facebook.com/groups/731501367621528/posts/*",
                    # "https://www.facebook.com/groups/1785695291572252/posts/*",
                    # "https://www.facebook.com/groups/858435034760219/posts/*",
                    # "https://www.facebook.com/groups/4004568206333626/posts/*",
                    # "https://www.facebook.com/groups/311310173430/posts/*",
                    # "https://www.facebook.com/groups/3061231334163874/posts/*",
                    # "https://www.facebook.com/groups/cubaquierelibertad/posts/*",
                    # "https://www.facebook.com/groups/746396290081815/posts/*",
                    # "https://www.facebook.com/groups/947108259119762/posts/*",
                    # "https://www.facebook.com/groups/3250858908484723/posts/*",
                    # "https://www.facebook.com/groups/2483575065250547/posts/*",
                    # "https://www.facebook.com/groups/673606370207296/posts/*",
                    # "https://www.facebook.com/groups/241656084405758/posts/*",
                    # "https://www.facebook.com/groups/3061231334163874/posts/*",
                    # "https://www.facebook.com/groups/521067779912295/posts/*",
                    # "https://www.facebook.com/groups/4004568206333626/posts/*",
                    # "https://www.facebook.com/groups/1361964128659573/posts/*",
                    # "https://www.facebook.com/groups/426378568468028/posts/*",
                    # "https://www.facebook.com/groups/155622670007705/posts/*",
                    # "https://www.facebook.com/groups/220549474641150/posts/*",
                    # "https://www.facebook.com/groups/3634905446747150/posts/*",
                    # "https://www.facebook.com/groups/tunasrevolico2025/posts/*",
                    # "https://www.facebook.com/groups/noticiasdeactualidadencuba14ymedio/*",
                    # "https://www.facebook.com/groups/760282752906060/posts/*",
                    # "https://www.facebook.com/groups/832515118291213/posts/*",
                    # "https://www.facebook.com/groups/2620925407934757/posts/*",
                    # "https://www.facebook.com/groups/229274248975688/posts/*",
                    # "https://www.facebook.com/groups/1183956328790433/posts/*",
                    # "https://www.facebook.com/groups/clandestinosc40/posts/*",
                    # "https://www.facebook.com/groups/581317789435286/posts/*",
                    # "https://www.facebook.com/groups/1401902360104900/posts/*",
                    # "https://www.facebook.com/groups/3977140342408962/posts/*",
                    # "https://www.facebook.com/groups/216791228964592/posts/*",
                    # "https://www.facebook.com/groups/balseroscubanosunidos/*",
                    # "https://www.facebook.com/groups/2309647012645237/posts/*",
                    # "https://www.facebook.com/groups/291152555288193/posts/*",
                    # "https://www.facebook.com/groups/315511890320885/posts/*",
                    # "https://www.facebook.com/people/Conferencia-de-Obispos-Cat*",
                    # "https://www.facebook.com/people/Asere-Noticias-de-Cuba/61574148608743/",
                    # "https://www.facebook.com/people/LMS-reporta/61575946707396/",
                    # "https://www.facebook.com/people/Amelia-Calzadilla/61559641311874/",
                    # "https://www.facebook.com/people/Nioreportandouncrimen/61555478031474/",
                    # "https://www.facebook.com/people/Juan-Manuel-Cao-Live/61577042995685/",
                    # "https://www.facebook.com/people/Cubaestadofallido/61570577485947/",
                    # "https://www.facebook.com/people/Ojo-Cuba/61568721145804/",
                    # "https://www.facebook.com/people/Nio-reportando-un-crimen/61559784217848/",
                    # "#CubaVive", "#DeZurdaTeam", "#YoSigoAMiPresidente", "#CubaPorLaSalud",
                    # "#NoAlTerrorismo", "#TumbaElBloqueo", "#NoMasBloqueo", "#CubaNoEstaSola",
                    # "#FidelPorSiempre", "#CubaCoopera", "#CubaPorLaVida", "#CubaSegura",
                    # "#HéroesDeAzul", "#ContraLasDrogasSeGana", "#CubaEstaFirme",
                    # "#100AñosConFidel", "#CubaSoberana", "#95DeRaul", "#NoAlTerrorismo",
                    # "#MisManosPorCuba", "cubanos", "habana", "havana", "cubana",
                    # "díaz-canel", "castro", "#cubaestafirme", "#soscuba",
                    # "#mifirmaporlapatria", "#cubalibre",
                    # "#cubasoberana", "#cubaestadoterrorista", "#cubanosporelmundo",
                    # "#lapatriasedefiende", "#cubaviveensuhistoria", "#latijeranews",
                    # "#libertadparacuba", "#patriayvida", "#chevive", "#destacamentoderefuerzo",
                    # "#yosigoamipresidente", "#cubavencera", "#lahabana", "#crisisencuba",
                    # "#centinelasdelaverdad", "#tumbaelbloqueo", "#denunciaciudadana",
                    # "#unidosxcuba", "#latidoizquierdo", "#matancerosenvictoria",
                    # "#heroesdeazul", "#dictaduracubana",
                    # "#fidelporsiempre", "#cubaporlapaz", "#isladelajuventud",
                    # "#lms", "#camaguey", "#cubanos", "#conlaverdadsomosmasfuertes",
                    # "#cubaestadofallido", "#cubanet", "#granma", "#cubanosenflorida",
                    # "#abajoladictadura", "#libertad", "#cubanoserinde",
                    # "#fidel", "#cdrcuba", "#conelpieenelestribo", "#noticiasdecuba",
                    # "#bloqueogenocida", "#diariodecuba", "#mujeresenrevolucion",
                    # "https://www.facebook.com/sannicolasprensalibre2021/*",
                    # "https://www.facebook.com/abejasmemes/*",
                    # "https://www.facebook.com/byyanelimorales/*",
                    # "https://www.facebook.com/somosmasn/*",
                    # "https://www.facebook.com/restauracionconstitucionall/*",
                    # "https://www.facebook.com/gentemenocal/*",
                    # "https://www.facebook.com/saul.manuel.500961/*",
                    # "https://www.facebook.com/lara.crofs/*",
                    # "https://www.facebook.com/yosmany.mayeta.labrada/*",
                    # "https://www.facebook.com/juanjuanalmedio/*",
                    # "https://www.facebook.com/mundodedarwin/*",
                    # "https://www.facebook.com/juliocesar.martinezferrer/*",
                    # "https://www.facebook.com/Rnapolesnoticias/*",
                    # "https://www.facebook.com/mv.porelcambio/*",
                    # "https://www.facebook.com/raveloofficial/*",
                    "https://www.facebook.com/armandocubaprimero/*",
                    "https://www.facebook.com/eltoquecom/*",
                    "https://www.facebook.com/martinoticias/*",
                    "https://www.facebook.com/periodicodecuba/*",
                    "https://www.facebook.com/cubaherald/*",
                    "https://www.facebook.com/directorionoticias/*",
                    "https://www.facebook.com/OnCuba/*",
                    "https://www.facebook.com/CiberCuba/*",
                    "https://www.facebook.com/palsaco.cuba/*",
                    "https://www.facebook.com/NoticiasTelemundo/*",
                    "https://www.facebook.com/NTN24/*",
                    "https://www.facebook.com/DIARIODECUBA/*",
                    "https://www.facebook.com/14ymedio/*",
                    "https://www.facebook.com/libertad.religiosa.52/*",
                    "https://www.facebook.com/elestornudo/*",
                    "https://www.facebook.com/cubacutenoticias/*",
                    "https://www.facebook.com/cubanetnoticias/*",
                    "https://www.facebook.com/CubaenMiami/*",
                    "https://www.facebook.com/SwingCompletoLLC/*",
                    "https://www.facebook.com/ADNCuba/*",
                    "https://www.facebook.com/antenacubana/*",
                    "https://www.facebook.com/periodismodebarrio/*",
                    "https://www.facebook.com/TAmoCuba/*",
                    "https://www.facebook.com/yucabyte/*",
                    "https://www.facebook.com/groups/todacuba/*",
                    "https://www.facebook.com/DimeCuba/*",
                    "https://www.facebook.com/CubanosporelMundo/*",
                    "https://www.facebook.com/islalocal/*",
                    "https://www.facebook.com/PeriodicoCubano/*",
                    "https://www.facebook.com/CubitaNOW/*",
                    "https://www.facebook.com/CiberCubaNoticias/*",
                    "https://www.facebook.com/Opositor1965",
                    "https://www.facebook.com/jose.raul.gallego.2025",
                    "https://www.facebook.com/mahla.sai.3",
                    "https://www.facebook.com/elayne.castro.524",
                    "https://www.facebook.com/jovenesdelcima",
                    "https://www.facebook.com/WenceslaoCruzBlanco",
                    "https://www.facebook.com/omaritoinforma",
                    "https://www.facebook.com/garlobo.lvv",
                    "https://www.facebook.com/DavidSiloetano",
                    "Pinar del Río", "Artemisa", "La Habana", "Mayabeque",
                    "Matanzas", "Cienfuegos", "Villa Clara", "Sancti Spíritus",
                    "Ciego de Ávila", "Camagüey", "Las Tunas", "Granma",
                    "Holguín", "Santiago de Cuba", "Guantánamo",
                    "Isla de la Juventud", "Minas de Matahambre",
                    "Ciénaga de Zapata", "Plaza de la Revolución",
                    "Diez de Octubre", "Arroyo Naranjo", "San Miguel del Padrón",
                    "La Habana Vieja", "Centro Habana", "La Habana del Este",
                    "Cerro", "Cotorro", "Boyeros", "Regla", "Guanabacoa",
                    "Marianao", "La Lisa", "Cabaiguán", "Jatibonico",
                    "Taguasco", "Yaguajay", "Caimanera", "Maisí",
            ]},
            {"label": "cluster cr", "engine_id": "338c61049d6ab4ba5", "keywords": [
                "crisis", "preso", "salud", "hospital", "militar", "policia", "petroleo",
                "cuba", "economia", "Canel", "terrorista", "fallido", "corrupto", "mujeres", "gobierno",
                "isla", "cubanos", "país", "régimen", "unidos", "habana", "havana", "cubana",
                "libertad", "sistema", "redes", "mundo", "tiempo", "patria", "presidente", "revolución",
                "nacional", "política", "seguridad", "falta", "oficial", "bloqueo", "díaz-canel",
                "denuncia", "realidad", "castro", "casa", "atención", "cambio", "familia", "TRUMP",
            ]},
        ]

    async def _run_engine(
        self,
        engine_id: str,
        label: str,
        keywords: list[str],
        total_pages: int = 5,
        headless: bool = False,
    ) -> None:
        """
        Procesa todas las keywords de un engine con UN solo browser.

        El browser se crea al inicio y se cierra al terminar todas las keywords,
        evitando el overhead de crear/destruir Playwright por cada búsqueda.

        Args:
            engine_id:   ID del Custom Search Engine.
            label:       Etiqueta descriptiva para logging.
            keywords:    Lista de términos a buscar.
            total_pages: Páginas de resultados a extraer por keyword.
            headless:    Modo headless del browser.
        """
        automator = GoogleCSEAutomator(cse_id=engine_id)
        cfg = automator.cfg

        async with async_playwright() as p:
            browser: Browser = await p.firefox.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                ],
            )
            logger.info(f"[{label}] Browser iniciado para engine '{engine_id}'.")

            context: BrowserContext | None = None
            page: Page | None = None

            try:
                context = await automator.create_stealth_context(browser)
                page = await context.new_page()
                await GoogleCSEAutomator._apply_stealth(page)
                await automator._warmup_session(page)

                for kw in keywords:
                    if not self._running:
                        logger.info("Parada solicitada, abortando engine loop.")
                        break

                    kw_clean = kw.strip()
                    if not kw_clean:
                        continue

                    try:
                        logger.info(
                            f"[{label}/{engine_id}] Scraping keyword: '{kw_clean}'"
                        )
                        await automator.run_with_page(
                            page=page,
                            keyword=kw_clean,
                            total_pages=total_pages,
                        )

                    except Exception as exc:
                        logger.error(
                            f"[{label}] Fallo en keyword '{kw_clean}': {exc}",
                            exc_info=True,
                        )
                        # Intento de recuperación: nueva página en el mismo contexto
                        try:
                            await page.close()
                            page = await context.new_page()
                            await GoogleCSEAutomator._apply_stealth(page)
                            logger.info(f"[{label}] Página recreada tras fallo.")
                        except Exception as recovery_exc:
                            logger.error(
                                f"[{label}] Recuperación fallida, abortando engine: "
                                f"{recovery_exc}"
                            )
                            break

                    pause = cfg.jitter_wait(*cfg.between_keywords_range)
                    logger.debug(f"[{label}] Pausa entre keywords: {pause:.1f}s")
                    await asyncio.sleep(pause)

            finally:
                if page and not page.is_closed():
                    await page.close()
                if context:
                    await context.close()
                await browser.close()
                logger.info(f"[{label}] Browser cerrado.")

    async def _execute_cycle(self) -> None:
        """
        Un ciclo completo: itera sobre todos los engines configurados.

        Cada engine ejecuta su propio browser (secuencialmente) con todas
        sus keywords en una misma sesión de navegación.
        """
        engines_config = await self._fetch_search_engines_config()

        if not engines_config:
            logger.warning("Sin motores configurados. Saltando ciclo.")
            return

        for engine_cfg in engines_config:
            if not self._running:
                break

            engine_id: str | None = engine_cfg.get("engine_id")
            keywords: list[str] = engine_cfg.get("keywords", [])
            label: str = engine_cfg.get("label", engine_id or "?")

            if not engine_id or not keywords:
                logger.warning(f"Configuración de motor inválida: {engine_cfg}")
                continue

            valid_keywords = [kw for kw in keywords if isinstance(kw, str) and kw.strip()]
            if not valid_keywords:
                logger.debug(f"[{label}] Sin keywords válidas, omitiendo engine.")
                continue

            try:
                await self._run_engine(
                    engine_id=engine_id,
                    label=label,
                    keywords=valid_keywords,
                    total_pages=3,
                    headless=False,
                )
            except Exception as exc:
                logger.error(
                    f"Fallo crítico en engine '{label}/{engine_id}': {exc}",
                    exc_info=True,
                )

    async def start(self) -> None:
        """Inicia el bucle infinito de scraping con manejo graceful de señales."""
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        logger.info("Orquestador iniciado. Ctrl+C para detener.")

        try:
            while self._running:
                await self._execute_cycle()
                logger.info(
                    f"Ciclo completado. Pausando {settings.CYCLE_DELAY_SECONDS}s..."
                )
                await asyncio.sleep(settings.CYCLE_DELAY_SECONDS)
        except asyncio.CancelledError:
            logger.info("Bucle cancelado.")

    def stop(self) -> None:
        """Señaliza parada en el siguiente checkpoint seguro."""
        logger.info("Señal de parada recibida. Finalizando ciclo actual...")
        self._running = False


async def main() -> None:
    orchestrator = ScraperOrchestrator()
    await orchestrator.start()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Aplicación interrumpida manualmente.")
        sys.exit(0)