"""
run_scraper.py
Orquestador: UN browser + UN contexto por engine. 
Fallback a nuevo contexto + Tor SOLO ante CAPTCHA.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from config.settings import settings
from google_cse_automator import BrowserConfig, GoogleCSEAutomator, TorManager
from utils.captcha_guard import CaptchaError

logger = logging.getLogger(__name__)

class ScraperOrchestrator:
    def __init__(self) -> None:
        self._running = False
        self._tor_manager = TorManager(
            proxy="socks5://127.0.0.1:9050",
            control_port=9051,
            password=None,
        )
        self._cfg = BrowserConfig()

    # ------------------------------------------------------------------ #
    # Config                                                              #
    # ------------------------------------------------------------------ #

    async def _fetch_engines_config(self) -> list[dict]:
        """
        Placeholder: devuelve la configuración de motores y keywords.

        Returns:
            Lista de dicts con ``engine_id``, ``label`` y ``keywords``.
        """
        logger.info("[PLACEHOLDER] Obteniendo configuración de motores...")
        await asyncio.sleep(0.1)

        return [
            # {"label": "IG-KW-Engine", "engine_id": "c4b97eed1414fcb14", "keywords": 
            #     [
            #         "#LaPatriaSeDefiende", 'Cuba',
            #         "#CubaVive", "#DeZurdaTeam", "#YoSigoAMiPresidente", "#CubaPorLaSalud",
            #         "#NoAlTerrorismo", "#TumbaElBloqueo", "#NoMasBloqueo", "#CubaNoEstaSola",
            #         "#FidelPorSiempre", "#CubaCoopera", "#CubaPorLaVida", "#CubaSegura",
            #         "#HéroesDeAzul", "#ContraLasDrogasSeGana", "#CubaEstaFirme",
            #         "#100AñosConFidel", "#CubaSoberana", "#95DeRaul", "#NoAlTerrorismo",
            #         "#MisManosPorCuba",  "cubanos", "habana", "havana", "cubana",
            #         "díaz-canel", "#cubaestafirme", "#soscuba",
            #         "#mifirmaporlapatria", "#cubalibre", "revolico",
            #         "#cubasoberana", "#cubaestadoterrorista", "#cubanosporelmundo",
            #         "#lapatriasedefiende", "#cubaviveensuhistoria", "#latijeranews",
            #         "#libertadparacuba", "#patriayvida", "#chevive", "#destacamentoderefuerzo",
            #         "#yosigoamipresidente", "#cubavencera", "#lahabana", "#crisisencuba",
            #         "#centinelasdelaverdad", "#tumbaelbloqueo", "#denunciaciudadana",
            #         "#unidosxcuba", "#latidoizquierdo", "#matancerosenvictoria",
            #         "#heroesdeazul", "#dictaduracubana",
            #         "#fidelporsiempre", "#cubaporlapaz", "#isladelajuventud",
            #         "#lms", "#camaguey", "#cubanos", "#conlaverdadsomosmasfuertes",
            #         "#cubaestadofallido", "#cubanet", "#granma", "#cubanosenflorida",
            #         "#abajoladictadura", "#libertad", "#cubanoserinde",
            #         "#fidel", "#cdrcuba", "#conelpieenelestribo", "#noticiasdecuba",
            #         "#bloqueogenocida", "#diariodecuba", "#mujeresenrevolucion"
            #     ]
            # },
            # {"label": "KW MONITOR", "engine_id": "b3d8ab5d4c4a84c70", "keywords":
            #     [
            #         "#LaPatriaSeDefiende",
            #         "#CubaVive", "#DeZurdaTeam", "#YoSigoAMiPresidente", "#CubaPorLaSalud",
            #         "#NoAlTerrorismo", "#TumbaElBloqueo", "#NoMasBloqueo", "#CubaNoEstaSola",
            #         "#FidelPorSiempre", "#CubaCoopera", "#CubaPorLaVida", "#CubaSegura",
            #         "#HéroesDeAzul", "#ContraLasDrogasSeGana", "#CubaEstaFirme",
            #         "#100AñosConFidel", "#CubaSoberana", "#95DeRaul", "#NoAlTerrorismo",
            #         "#MisManosPorCuba",  "cubanos", "habana", "havana", "cubana",
            #         "díaz-canel", "castro", "#cubaestafirme", "#soscuba",
            #         "#mifirmaporlapatria", "#cubalibre",
            #         "#cubasoberana", "#cubaestadoterrorista", "#cubanosporelmundo",
            #         "#lapatriasedefiende", "#cubaviveensuhistoria", "#latijeranews",
            #         "#libertadparacuba", "#patriayvida", "#chevive", "#destacamentoderefuerzo",
            #         "#yosigoamipresidente", "#cubavencera", "#lahabana", "#crisisencuba",
            #         "#centinelasdelaverdad", "#tumbaelbloqueo", "#denunciaciudadana",
            #         "#unidosxcuba", "#latidoizquierdo", "#matancerosenvictoria",
            #         "#heroesdeazul", "#dictaduracubana",
            #         "#fidelporsiempre", "#cubaporlapaz", "#isladelajuventud",
            #         "#lms", "#camaguey", "#cubanos", "#conlaverdadsomosmasfuertes",
            #         "#cubaestadofallido", "#cubanet", "#granma", "#cubanosenflorida",
            #         "#abajoladictadura", "#libertad", "#cubanoserinde",
            #         "#fidel", "#cdrcuba", "#conelpieenelestribo", "#noticiasdecuba",
            #         "#bloqueogenocida", "#diariodecuba", "#mujeresenrevolucion"
            #     ]
            # },
            # {"label": "general", "engine_id": "294a079ba2d4267d5", "keywords": [
            #         "https://www.facebook.com/PresidenciaDeCuba/posts/*",
            #         "https://www.facebook.com/gerardo.hernandez.nordelo/posts/*",
            #         "https://www.facebook.com/Gerardodelos5heroes/posts/*",
            #         "https://www.facebook.com/PartidoComunistadeCubacontinuadordeMartiyFidel/posts/*",
            #         "https://www.facebook.com/razonesdecuba.cu/posts/*",
            #         "https://www.facebook.com/GladysArtemisa/posts/*",
            #         "https://www.facebook.com/jorgeluis.brochelorenzo/posts/*",
            #         "https://www.facebook.com/RadioBayamo/posts/*",
            #         "https://www.facebook.com/groups/434004943672696/posts/*",
            #         "https://www.facebook.com/groups/1817905055123266/posts/*",
            #         "https://www.facebook.com/groups/anuncioscaibarien/posts/*",
            #         "https://www.facebook.com/groups/67706680225/posts/*",
            #         "https://www.facebook.com/profile.php?id=61575946707396/posts/*",
            #         "https://www.facebook.com/profile.php?id=61559784217848/posts/*",
            #         "https://www.facebook.com/groups/3170984329754811/posts/*",
            #         "https://www.facebook.com/groups/3063203460404398/posts/*",
            #         "https://www.facebook.com/groups/2778177215613910/posts/*",
            #         "https://www.facebook.com/groups/1315855495741236/posts/*",
            #         "https://www.facebook.com/america.libre.754277/posts/*",
            #         "https://www.facebook.com/ranchueleros.por.la.libertad/posts/*",
            #         "https://www.facebook.com/cladestino.cubano/posts/*",
            #         "https://www.facebook.com/juana.arencibia.3/posts/*",
            #         "https://facebook.com/groups/463363023678634/posts/*",
            #         "https://www.facebook.com/groups/521067779912295/posts/*",
            #         "https://www.facebook.com/groups/2812476895679150/posts/*",
            #         "https://www.facebook.com/groups/731501367621528/posts/*",
            #         "https://www.facebook.com/groups/1785695291572252/posts/*",
            #         "https://www.facebook.com/groups/858435034760219/posts/*",
            #         "https://www.facebook.com/groups/4004568206333626/posts/*",
            #         "https://www.facebook.com/groups/311310173430/posts/*",
            #         "https://www.facebook.com/groups/3061231334163874/posts/*",
            #         "https://www.facebook.com/groups/cubaquierelibertad/posts/*",
            #         "https://www.facebook.com/groups/746396290081815/posts/*",
            #         "https://www.facebook.com/groups/947108259119762/posts/*",
            #         "https://www.facebook.com/groups/3250858908484723/posts/*",
            #         "https://www.facebook.com/groups/2483575065250547/posts/*",
            #         "https://www.facebook.com/groups/673606370207296/posts/*",
            #         "https://www.facebook.com/groups/241656084405758/posts/*",
            #         "https://www.facebook.com/groups/521067779912295/posts/*",
            #         "https://www.facebook.com/groups/4004568206333626/posts/*",
            #         "https://www.facebook.com/groups/1361964128659573/posts/*",
            #         "https://www.facebook.com/groups/426378568468028/posts/*",
            #         "https://www.facebook.com/groups/155622670007705/posts/*",
            #         "https://www.facebook.com/groups/220549474641150/posts/*",
            #         "https://www.facebook.com/groups/3634905446747150/posts/*",
            #         "https://www.facebook.com/groups/tunasrevolico2025/posts/*",
            #         "https://www.facebook.com/groups/noticiasdeactualidadencuba14ymedio/*",
            #         "https://www.facebook.com/groups/760282752906060/posts/*",
            #         "https://www.facebook.com/groups/832515118291213/posts/*",
            #         "https://www.facebook.com/groups/2620925407934757/posts/*",
            #         "https://www.facebook.com/groups/229274248975688/posts/*",
            #         "https://www.facebook.com/groups/1183956328790433/posts/*",
            #         "https://www.facebook.com/groups/clandestinosc40/posts/*",
            #         "https://www.facebook.com/groups/581317789435286/posts/*",
            #         "https://www.facebook.com/groups/1401902360104900/posts/*",
            #         "https://www.facebook.com/groups/3977140342408962/posts/*",
            #         "https://www.facebook.com/groups/216791228964592/posts/*",
            #         "https://www.facebook.com/groups/balseroscubanosunidos/*",
            #         "https://www.facebook.com/groups/2309647012645237/posts/*",
            #         "https://www.facebook.com/groups/291152555288193/posts/*",
            #         "https://www.facebook.com/groups/315511890320885/posts/*",
            #         "https://www.facebook.com/people/Conferencia-de-Obispos-Cat*",
            #         "https://www.facebook.com/people/Asere-Noticias-de-Cuba/61574148608743/",
            #         "https://www.facebook.com/people/LMS-reporta/61575946707396/",
            #         "https://www.facebook.com/people/Amelia-Calzadilla/61559641311874/",
            #         "https://www.facebook.com/people/Nioreportandouncrimen/61555478031474/",
            #         "https://www.facebook.com/people/Juan-Manuel-Cao-Live/61577042995685/",
            #         "https://www.facebook.com/people/Cubaestadofallido/61570577485947/",
            #         "https://www.facebook.com/people/Ojo-Cuba/61568721145804/",
            #         "https://www.facebook.com/people/Nio-reportando-un-crimen/61559784217848/",
            #         "https://www.facebook.com/sannicolasprensalibre2021/*",
            #         "https://www.facebook.com/abejasmemes/*",
            #         "https://www.facebook.com/byyanelimorales/*",
            #         "https://www.facebook.com/somosmasn/*",
            #         "https://www.facebook.com/restauracionconstitucionall/*",
            #         "https://www.facebook.com/gentemenocal/*",
            #         "https://www.facebook.com/saul.manuel.500961/*",
            #         "https://www.facebook.com/lara.crofs/*",
            #         "https://www.facebook.com/yosmany.mayeta.labrada/*",
            #         "https://www.facebook.com/juanjuanalmedio/*",
            #         "https://www.facebook.com/mundodedarwin/*",
            #         "https://www.facebook.com/juliocesar.martinezferrer/*",
            #         "https://www.facebook.com/Rnapolesnoticias/*",
            #         "https://www.facebook.com/mv.porelcambio/*",
            #         "https://www.facebook.com/raveloofficial/*",
            #         "https://www.facebook.com/armandocubaprimero/*",
            #         "https://www.facebook.com/eltoquecom/*",
            #         "https://www.facebook.com/martinoticias/*",
            #         "https://www.facebook.com/periodicodecuba/*",
            #         "https://www.facebook.com/cubaherald/*",
            #         "https://www.facebook.com/directorionoticias/*",
            #         "https://www.facebook.com/OnCuba/*",
            #         "https://www.facebook.com/CiberCuba/*",
            #         "https://www.facebook.com/palsaco.cuba/*",
            #         "https://www.facebook.com/NoticiasTelemundo/*",
            #         "https://www.facebook.com/NTN24/*",
            #         "https://www.facebook.com/DIARIODECUBA/*",
            #         "https://www.facebook.com/14ymedio/*",
            #         "https://www.facebook.com/libertad.religiosa.52/*",
            #         "https://www.facebook.com/elestornudo/*",
            #         "https://www.facebook.com/cubacutenoticias/*",
            #         "https://www.facebook.com/cubanetnoticias/*",
            #         "https://www.facebook.com/CubaenMiami/*",
            #         "https://www.facebook.com/SwingCompletoLLC/*",
            #         "https://www.facebook.com/ADNCuba/*",
            #         "https://www.facebook.com/antenacubana/*",
            #         "https://www.facebook.com/periodismodebarrio/*",
            #         "https://www.facebook.com/TAmoCuba/*",
            #         "https://www.facebook.com/yucabyte/*",
            #         "https://www.facebook.com/groups/todacuba/*",
            #         "https://www.facebook.com/DimeCuba/*",
            #         "https://www.facebook.com/CubanosporelMundo/*",
            #         "https://www.facebook.com/islalocal/*",
            #         "https://www.facebook.com/PeriodicoCubano/*",
            #         "https://www.facebook.com/CubitaNOW/*",
            #         "https://www.facebook.com/CiberCubaNoticias/*",
            #         "https://www.facebook.com/Opositor1965",
            #         "https://www.facebook.com/jose.raul.gallego.2025",
            #         "https://www.facebook.com/mahla.sai.3",
            #         "https://www.facebook.com/elayne.castro.524",
            #         "https://www.facebook.com/jovenesdelcima",
            #         "https://www.facebook.com/WenceslaoCruzBlanco",
            #         "https://www.facebook.com/omaritoinforma",
            #         "https://www.facebook.com/garlobo.lvv",
            #         "https://www.facebook.com/DavidSiloetano",
            #         "Pinar del Río",
            #         "Artemisa",
            #         "La Habana",
            #         "Mayabeque",
            #         "Matanzas",
            #         "Cienfuegos",
            #         "Villa Clara",
            #         "Sancti Spíritus",
            #         "Ciego de Ávila",
            #         "Camagüey",
            #         "Las Tunas",
            #         "Granma",
            #         "Holguín",
            #         "Santiago de Cuba",
            #         "Guantánamo",
            #         "Isla de la Juventud",
            #         "Minas de Matahambre",
            #         "Ciénaga de Zapata",
            #         "Plaza de la Revolución",
            #         "Diez de Octubre",
            #         "Arroyo Naranjo",
            #         "San Miguel del Padrón",
            #         "La Habana Vieja",
            #         "Centro Habana",
            #         "La Habana del Este",
            #         "Cerro",
            #         "Cotorro",
            #         "Boyeros",
            #         "Regla",
            #         "Guanabacoa",
            #         "Marianao",
            #         "La Lisa",
            #         "Cabaiguán",
            #         "Jatibonico",
            #         "Taguasco",
            #         "Yaguajay",
            #         "Caimanera",
            #         "Maisí"
            # ]},
            # {"label": "cluster cr", "engine_id": "338c61049d6ab4ba5", "keywords": [
            #     "crisis", "preso", "salud", "hospital", "militar", "policia", "petroleo", 
            #     "cuba", "economia", "Canel", "terrorista", "fallido", "corrupto", "mujeres", "gobierno",
            #     "isla", "cubanos", "país", "régimen", "unidos", "habana", "havana", "cubana",
            #     "libertad", "sistema", "redes", "mundo", "tiempo", "patria", "presidente", "revolución",
            #     "nacional", "política", "seguridad", "falta", "oficial", "bloqueo", "díaz-canel",
            #     "denuncia", "realidad", "castro", "casa", "atención", "cambio", "familia", "TRUMP"
            # ]},
            {"label": "general", "engine_id": "294a079ba2d4267d5", "keywords": [
                "https://www.facebook.com/cubasatelite/posts/*"
            ]}
        ]

    # ------------------------------------------------------------------ #
    # Helpers de contexto                                                 #
    # ------------------------------------------------------------------ #

    async def _create_context_and_page(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        proxy: Optional[Dict[str, str]] = None,
    ) -> Tuple[BrowserContext, Page]:
        """
        Crea un contexto fresco + página con stealth y warmup aplicados.

        Args:
            browser:   Browser activo.
            automator: Instancia de GoogleCSEAutomator (para stealth/warmup).
            proxy:     Dict de proxy opcional (Tor). None = conexión directa.

        Returns:
            Tupla (context, page) lista para scraping.
        """
        context = await browser.new_context(
            **self._cfg.build_context_options(proxy=proxy)
        )
        page = await context.new_page()
        await GoogleCSEAutomator._apply_stealth(page)
        await automator._warmup_session(page)
        return context, page

    async def _rotate_to_tor(
        self,
        browser: Browser,
        automator: GoogleCSEAutomator,
        old_context: BrowserContext,
        label: str,
    ) -> Tuple[BrowserContext, Page]:
        """
        Cierra el contexto bloqueado, rota el circuito Tor y abre uno nuevo.

        Flujo:
            1. Cierra old_context (libera la ventana bloqueada por CAPTCHA).
            2. Solicita NEWNYM al control port de Tor.
            3. Crea nuevo contexto con proxy SOCKS5 + stealth + warmup.

        Args:
            browser:     Browser activo (se reutiliza, NO se cierra).
            automator:   Instancia del automator.
            old_context: Contexto a cerrar.
            label:       Etiqueta de engine para logging.

        Returns:
            Tupla (nuevo_context, nueva_page) lista para reintentar.
        """
        # 1. Cerrar solo el contexto bloqueado, NO el browser
        try:
            await old_context.close()
        except Exception as close_exc:
            logger.debug(f"[{label}] Error cerrando contexto anterior: {close_exc}")

        # 2. Renovar circuito Tor
        await self._tor_manager.renew_circuit()
        logger.info(f"[{label}] Circuito Tor renovado. Abriendo contexto con proxy...")

        # 3. Nuevo contexto con proxy Tor
        proxy_opts = self._tor_manager.get_proxy_settings()
        return await self._create_context_and_page(browser, automator, proxy=proxy_opts)

    # ------------------------------------------------------------------ #
    # Engine loop                                                         #
    # ------------------------------------------------------------------ #

    async def _run_engine_keywords(
        self,
        engine_id: str,
        label: str,
        keywords: List[str],
        total_pages: int = 3,
    ) -> None:
        """
        Procesa todas las keywords de un engine con UN solo browser y contexto.

        Ciclo de vida:
            - Browser: creado una vez por engine, cerrado en el finally.
            - Contexto + página: creados UNA vez antes del loop de keywords.
              Solo se recrean cuando se detecta CAPTCHA (fallback a Tor).
            - Por keyword: se reutiliza la misma página existente.

        CAPTCHA handling:
            1. Se emite alerta sonora.
            2. Se cierra el contexto bloqueado (no el browser).
            3. Se rota el circuito Tor.
            4. Se abre un nuevo contexto con proxy SOCKS5.
            5. Se reintenta la keyword con la nueva IP.
        """
        automator = GoogleCSEAutomator(cse_id=engine_id, config=self._cfg)

        async with async_playwright() as p:
            browser: Browser = await p.firefox.launch(headless=False)
            logger.info(f"[{label}] Navegador iniciado para engine '{engine_id}'.")

            # ── Contexto inicial (sin Tor) — una sola ventana para todo el engine
            context, page = await self._create_context_and_page(browser, automator)

            try:
                for idx, kw in enumerate(keywords, 1):
                    if not self._running:
                        break

                    kw_clean = kw.strip()
                    if not kw_clean:
                        continue

                    logger.info(
                        f"[{label}] [{idx}/{len(keywords)}] Procesando: '{kw_clean}'"
                    )

                    try:
                        await automator.run_keyword(page, kw_clean, total_pages)

                    except CaptchaError:
                        # ── CAPTCHA detectado: rotar a Tor y reintentar ───────
                        logger.warning(
                            f"[{label}] CAPTCHA en '{kw_clean}'. "
                            "Rotando contexto + Tor..."
                        )
                        GoogleCSEAutomator._play_alert_sound()

                        try:
                            context, page = await self._rotate_to_tor(
                                browser=browser,
                                automator=automator,
                                old_context=context,
                                label=label,
                            )
                            logger.info(
                                f"[{label}] Reintentando '{kw_clean}' vía Tor..."
                            )
                            await automator.run_keyword(page, kw_clean, total_pages)

                        except Exception as tor_exc:
                            logger.error(
                                f"[{label}] Fallo en reintento Tor para "
                                f"'{kw_clean}': {tor_exc}",
                                exc_info=True,
                            )

                    except Exception as exc:
                        logger.error(
                            f"[{label}] Error inesperado en '{kw_clean}': {exc}",
                            exc_info=True,
                        )
                        # Recuperación suave: nueva página en el mismo contexto
                        try:
                            await page.close()
                            page = await context.new_page()
                            await GoogleCSEAutomator._apply_stealth(page)
                            logger.info(f"[{label}] Página recreada tras error.")
                        except Exception as recovery_exc:
                            logger.error(
                                f"[{label}] Recuperación fallida, abortando engine: "
                                f"{recovery_exc}"
                            )
                            break

                    pause = self._cfg.jitter_wait(*self._cfg.between_keywords_range)
                    logger.debug(f"[{label}] Pausa entre keywords: {pause:.1f}s")
                    await asyncio.sleep(pause)

            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                await browser.close()
                logger.info(f"[{label}] Navegador cerrado.")

    # ------------------------------------------------------------------ #
    # Ciclo principal                                                     #
    # ------------------------------------------------------------------ #

    async def _execute_cycle(self) -> None:
        engines = await self._fetch_engines_config()
        if not engines:
            logger.warning("Sin motores configurados. Saltando ciclo.")
            return

        for engine in engines:
            if not self._running:
                break

            engine_id: Optional[str] = engine.get("engine_id")
            keywords: List[str] = engine.get("keywords", [])
            label: str = engine.get("label", engine_id or "?")
            valid_kws = [k for k in keywords if isinstance(k, str) and k.strip()]

            if not engine_id or not valid_kws:
                logger.warning(f"Configuración inválida para engine '{label}', omitiendo.")
                continue

            try:
                await self._run_engine_keywords(
                    engine_id=engine_id,
                    label=label,
                    keywords=valid_kws,
                    total_pages=3,
                )
            except Exception as exc:
                logger.error(
                    f"Fallo crítico en engine '{label}': {exc}", exc_info=True
                )

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

        logger.info("Orquestador iniciado. Ctrl+C para detener.")
        try:
            while self._running:
                await self._execute_cycle()
                delay = getattr(settings, "CYCLE_DELAY_SECONDS", 300)
                logger.info(f"Ciclo completado. Pausando {delay}s...")
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("Bucle cancelado.")

    def stop(self) -> None:
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