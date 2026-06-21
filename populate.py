"""
Script para poblar la tabla keywords en Supabase.
Cada término se buscará en AMBAS plataformas (Facebook e Instagram).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from supabase import create_client

# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────
SUPABASE_URL = "https://wpsxnyzeyrrxostzqifh.supabase.co/"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indwc3hueXpleXJyeG9zdHpxaWZoIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODAxMzE1OTAsImV4cCI6MjA5NTcwNzU5MH0.z3tKNqATfXyxKcroVxWOmTFp84rqP3VDvAX6KitG5DQ"

BATCH_SIZE = 100

# ── LISTA DE TÉRMINOS ─────────────────────────────────────────────────────
TERMINOS_CUBA = [
    '#cuba', '#cuban', '#cuba🇨🇺', '#cubana', '#cubanosporelmundo',
    '#cubalibre', '#cubano', '#cubanos', '#cubanlink', '#cubanosenmiami',
    '#cubanas', '#cubacoopera', '#cubanosenespaña', '#cubamined',
    '#cubanmodel', '#cubans', '#cubanlife', '#cubaporlavida', '#soscuba',
    '#cubanita', '#habana', '#lahabana', '#habanavieja', '#habanacuba',
    '#varadero', '#varaderocuba', '#varaderobeach',
    '#varaderocuba🇨🇺☀️🌊🌴', '#trinidad', '#RaúlEsRaúl',
    '#ConElPieEnElEstribo', '#LaPatriaSeDefiende', '#CubaVive',
    '#DeZurdaTeam', '#YoSigoAMiPresidente', '#CubaPorLaSalud',
    '#NoAlTerrorismo', '#TumbaElBloqueo', '#NoMasBloqueo',
    '#CubaNoEstaSola', '#FidelPorSiempre', '#CubaSegura',
    '#HéroesDeAzul', '#ContraLasDrogasSeGana', '#CubaEstaFirme',
    '#100AñosConFidel', '#CubaSoberana', '#95DeRaul', '#MisManosPorCuba',
    '#mifirmaporlapatria', '#cubaestadoterrorista',
    '#cubaviveensuhistoria', '#latijeranews', '#libertadparacuba',
    '#patriayvida', '#chevive', '#destacamentoderefuerzo', '#cubavencera',
    '#crisisencuba', '#centinelasdelaverdad', '#denunciaciudadana',
    '#unidosxcuba', '#latidoizquierdo', '#matancerosenvictoria',
    '#heroesdeazul', '#dictaduracubana', '#cubaporlapaz',
    '#isladelajuventud', '#lms', '#camaguey', '#conlaverdadsomosmasfuertes',
    '#cubaestadofallido', '#cubanet', '#granma', '#cubanosenflorida',
    '#abajoladictadura', '#cubanoserinde', '#fidel', '#cdrcuba',
    '#noticiasdecuba', '#bloqueogenocida', '#diariodecuba',
    '#mujeresenrevolucion', '"Pinar del Río"', '"Artemisa"', '"La Habana"',
    '"Mayabeque"', '"Matanzas"', '"Cienfuegos"', '"Villa Clara"',
    '"Sancti Spíritus"', '"Ciego de Ávila"', '"Camagüey"', '"Las Tunas"',
    '"Granma"', '"Holguín"', '"Santiago de Cuba"', '"Guantánamo"',
    '"Isla de la Juventud"', '"Minas de Matahambre"', '"Ciénaga de Zapata"',
    '"Plaza de la Revolución"', '"Diez de Octubre"', '"Arroyo Naranjo"',
    '"San Miguel del Padrón"', '"La Habana Vieja"', '"Centro Habana"',
    '"La Habana del Este"', '"Cerro"', '"Cotorro"', '"Boyeros"', '"Regla"',
    '"Guanabacoa"', '"Marianao"', '"La Lisa"', '"Cabaiguán"', '"Jatibonico"',
    '"Taguasco"', '"Yaguajay"', '"Caimanera"', '"Maisí"',
    '#CubaNoEstáSola', '#CubaEstáFirme', '#95DeRaúl', '#CubaVencerá',
    '#FidelCastro', '#Raúl', '#LatiendoConFidel', '#LatirAvileño',
    '#RaulEsRaúl', '#CubaQuierePaz', '#RevoluciónCubana', '#DefendiendoCuba',
    '#SanctiSpíritusEnMarcha', '#SomosContinuidad', '#CubaSeDefiende',
    '#SinPerderUnDía', '#NoAlBloqueo', '#CiegodeAvila',
    '#ArtemisaJuntosSomosMás', '#LasTunas', '#SiempreXCuba',
    '#DerechosHumanos', '#Matanzas', '#RaúlCastro', '#Camagüey',
    '#DeporteCubano', '#SomosCuba', '#EtecsaConCuba', '#AbajoElBloqueo',
    '#HastaQueSeanLibres', '#MtssCuba', '#PorLasTunasLaVictoria',
    '#ProvinciaGranma', '#VillaClaraConTodos', '#PáginasAmarillasDeEtecsa',
    '#CubaEsRevolución', '#Guantánamo', '#RaulEsRaul', '#NoMasMuela',
    '#LaPatriaSeDediende', '#SantiagoDeCuba', '#SíSePuede', '#TenemosMemoria',
    '#UniversidadCubana', '#SantoDomingo2026', '#CaféMartiano',
    '#BeisbolCubano', '#CubaEsCultura', '#PinardelRío',
    '#RevolucionEsConstruir', '#CiegodeÁvila', '#PresosPolíticos',
    '#bloqueo', '#PatriaOMuerte', '#AlasTensas', '#represiónencuba',
    '#policíacubana', '#cubahoy', '#RégimenCubano', '#ONEI',
    '#CubaSinRepresión', '#CubaIsNext', '#11J', '#protestasencuba',
    '#RaulCastro', '#ProtestasenLaHabana', '#PobrezaEnCuba',
    '#LibertadParaJorgeYNadir', '#GAESA', '#FoodMonitorProgram',
    '#ExilioCubano', '#DíazCanel', '#Cubalex', '#Cubaenlacalle',
    '#ConFilo', '#ApagonesEnCuba', '#AdriánCuruneaux', '#lafamiliacubana',
    '#freecuba', '#ServicioMilitarObligatorio', '#SeguridadDelEstado',
    '#PolicíaPolítica', '#OpositoresCubanos', '#NoMasRepresion',
    '#MigracionCubana', '#LibertadParaLosPresosPoliticos', '#LaLisa',
    '#LaChina', '#HermanosAlRescate', '#DemocraciaParaCuba',
    '#CubaIsADictatorship',
    '"El kimiko" -reggaeton -entrevista -"video clip" -reguetonero -arte -concierto -cantante -musica -"L Kimii" -Osniel -Covarrubias -Yordy',
    '("El papelito" AND -trio -violin -violinista -concierto -musica -taiger -canción) AND Droga',
    '"medicina" AND Cuba', '"malestar" AND Cuba', '"apagon" AND Cuba',
    '"toque de caldero" AND Cuba', '"gobierno" AND Cuba',
    '"fraude" AND Cuba', '"droga" AND Cuba', '"salud" AND Cuba',
    '"escuela" AND Cuba', '"educacion" AND Cuba', '"deporte" AND Cuba',
    '"crisis" AND Cuba', '"Transporte" AND Cuba', '"agua" AND Cuba',
    '"corriente" AND Cuba', '"corrupcion" AND Cuba', '#PlenoCC',
    '#NuestraRespuestaEsLaUnidad',
    '"Pleno Extraordinario del Comité Central" OR "Partido Comunista de Cuba"',
    '"policía" AND Cuba', '"PNR" AND Cuba', '"cárcel" AND Cuba',
    '"preso" AND Cuba', '"represión" AND Cuba', '"bloqueo" AND Cuba',
    '"sanciones" AND Cuba', '"imperialismo" AND Cuba', '"revolución" AND Cuba',
    '"Fidel" AND Cuba', '"soberanía" AND Cuba', '"patria" AND Cuba',
    '"guerra mediática" AND Cuba', '"doble moral" AND Cuba',
    '"visa" AND Cuba', '"consulado" AND Cuba', '"balsero" AND Cuba',
    '"emigrar" AND Cuba', '"nostalgia" AND Cuba', '"reencuentro" AND Cuba',
    '"familia" AND Cuba', '"comida" AND Cuba', '"desabastecimiento" AND Cuba',
    '"hambre" AND Cuba', '"agricultura" AND Cuba', '"pollo" AND Cuba',
    '"gasolina" AND Cuba', '"combustible" AND Cuba', '"ETECSA" AND Cuba',
    '"internet" AND Cuba', '"basura" AND Cuba', '"MLC" AND Cuba',
    '"dolar" AND Cuba', '"bodega" AND Cuba', '"inflación" AND Cuba',
    '"salario" AND Cuba', '"remesa" AND Cuba', '"especulación" AND Cuba',
    '"mercado negro" AND Cuba', '"precio" AND Cuba', '"resolviendo" AND Cuba',
    '"lucha" AND Cuba',
]


def deduplicate_terms(terms: list[str]) -> list[str]:
    """Elimina duplicados preservando orden."""
    seen = set()
    unique = []
    for term in terms:
        normalized = term.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(term.strip())
    return unique


async def insert_keywords() -> tuple[int, int]:
    """
    Inserta los términos en Supabase. Sin columna platform.
    
    Returns:
        Tupla de (insertados, omitidos por duplicado).
    """
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    terms = deduplicate_terms(TERMINOS_CUBA)
    total = len(terms)
    
    logging.info(f"Procesando {total} términos únicos...")
    
    records = [
        {"term": term, "scraped_at": None}
        for term in terms
    ]
    
    inserted = 0
    omitted = 0
    
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        
        try:
            response = (
                client.table("keywords")
                .upsert(batch, on_conflict="term")
                .execute()
            )
            
            if response.data:
                inserted += len(response.data)
                logging.info(f"Batch {i//BATCH_SIZE + 1}: {len(response.data)} insertados")
            else:
                omitted += len(batch)
                logging.warning(f"Batch {i//BATCH_SIZE + 1}: omitidos")
                
        except Exception as exc:
            logging.error(f"Error en batch {i//BATCH_SIZE + 1}: {exc}")
            omitted += len(batch)
        
        await asyncio.sleep(0.1)
    
    return inserted, omitted


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    
    inserted, omitted = await insert_keywords()
    
    print(f"\n{'='*50}")
    print(f"RESUMEN:")
    print(f"  Total términos: {len(TERMINOS_CUBA)}")
    print(f"  Insertados:     {inserted}")
    print(f"  Omitidos:       {omitted}")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())