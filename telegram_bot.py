import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse, parse_qs
from io import BytesIO
from PIL import Image
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET


# ============================================================
# CONFIGURACIÓN
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

log = logging.getLogger("slrc_news_bot")

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ARCHIVO_ENVIADAS = "noticias_enviadas.json"

# San Luis Río Colorado / Sonora
TZ = ZoneInfo("America/Hermosillo")

# Similitud de títulos: se calcula sobre palabras DISTINTIVAS (sin
# stopwords ni "San Luis Río Colorado"), con índice de Jaccard.
UMBRAL_SIMILITUD_TITULO = 0.85
MIN_TOKENS_COMPARABLES = 3
# Solo se comparan títulos contra los N más recientes del historial
# (la deduplicación por link sigue usando el historial completo).
MAX_TITULOS_COMPARAR = 300
MAX_HISTORIAL = 1000

# DRY_RUN=1 -> no envía a Telegram ni modifica el historial (diagnóstico).
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
# DEBUG=1 -> registra cada link descartado y el motivo.
DEBUG = os.getenv("DEBUG", "0") == "1"

LIMITE_CAPTION = 1024
# Cupo por medio en cada corrida (total = suma de cupos).
# Las notas se toman de la más reciente a la más antigua dentro de cada medio.
CUPO_POR_MEDIO = {
    "Tribuna de San Luis": 5,
    "El Imparcial": 5
}

# False -> cada medio manda como máximo su cupo, aunque el otro tenga menos.
# True  -> si un medio no llena su cupo, el otro completa hasta el total.
RELLENAR_CUPO_VACIO = False

MAX_NOTICIAS_POR_CORRIDA = sum(CUPO_POR_MEDIO.values())
MAX_CANDIDATAS_A_REVISAR = 80
DIAS_RETROCESO = 7

# El bot corre una vez al día a las 04:00.
# Se aceptan notas de hoy y ayer.
ACEPTAR_HOY_Y_AYER = False

# Tamaño máximo que intentaremos descargar como imagen.
MAX_IMAGE_BYTES = 18 * 1024 * 1024


# ============================================================
# FUENTES
# ============================================================
#
# tipo = "rss"  -> feed RSS (preferido: trae fecha exacta, título limpio
#                  e imagen; no depende de JavaScript).
# tipo = "html" -> se leen los <a> de la página (respaldo).
#
# requiere_mencion = True -> solo se aceptan notas cuyo título mencione
#                            SLRC (para feeds generales / estatales).
#
# Verificado 23/11/2026:
#   - El Imparcial /mxl/sanluis/ (HTML) solo trae ~6 enlaces en el HTML;
#     el listado se carga por JavaScript -> casi 0 notas. Se reemplaza
#     por su feed RSS de sección, que sí trae las notas de SLRC.
# ============================================================

FUENTES = [
    {
        "nombre": "El Imparcial San Luis",
        "medio": "El Imparcial",
        "tipo": "rss",
        "url": (
            "https://www.elimparcial.com/arc/outboundfeeds/rss/"
            "category/mxl/sanluis/?outputType=xml"
        ),
        "requiere_mencion": False
    },
    {
        "nombre": "El Imparcial Sonora",
        "medio": "El Imparcial",
        "tipo": "rss",
        "url": (
            "https://www.elimparcial.com/arc/outboundfeeds/rss/"
            "category/son/sonora/?outputType=xml"
        ),
        "requiere_mencion": True
    },
    {
        "nombre": "El Imparcial",
        "medio": "El Imparcial",
        "tipo": "rss",
        "url": (
            "https://www.elimparcial.com/arc/outboundfeeds/rss/"
            "?outputType=xml"
        ),
        "requiere_mencion": True
    },
    {
        # oem.com.mx bloquea las conexiones automatizadas (403), así que
        # las notas de Tribuna se obtienen del RSS público de Bing News
        # (verificado 23/11/2026: ~12 notas con link directo, fecha e imagen).
        "nombre": "Tribuna de San Luis (vía Bing News)",
        "medio": "Tribuna de San Luis",
        "tipo": "rss",
        "url": (
            "https://www.bing.com/news/search?"
            "q=site%3Aoem.com.mx%2Ftribunadesanluis&format=rss"
        ),
        "requiere_mencion": False,
        "solo_dominio": "oem.com.mx/tribunadesanluis/"
    },
    {
        "nombre": "Tribuna de San Luis - Policiaca (vía Bing News)",
        "medio": "Tribuna de San Luis",
        "tipo": "rss",
        "url": (
            "https://www.bing.com/news/search?"
            "q=site%3Aoem.com.mx%2Ftribunadesanluis%2Fpoliciaca&format=rss"
        ),
        "requiere_mencion": False,
        "solo_dominio": "oem.com.mx/tribunadesanluis/"
    },
    {
        # Lectura directa de oem.com.mx: normalmente responde 403.
        # Se mantiene por si desde tu servidor sí funciona.
        "nombre": "Tribuna de San Luis",
        "medio": "Tribuna de San Luis",
        "tipo": "html",
        "url": "https://oem.com.mx/tribunadesanluis/"
    },
    {
        "nombre": "Tribuna de San Luis - Local",
        "medio": "Tribuna de San Luis",
        "tipo": "html",
        "url": "https://oem.com.mx/tribunadesanluis/local/"
    },
    {
        "nombre": "Tribuna de San Luis - Policiaca",
        "medio": "Tribuna de San Luis",
        "tipo": "html",
        "url": "https://oem.com.mx/tribunadesanluis/policiaca/"
    },
    {
        "nombre": "Tribuna de San Luis - Deportes",
        "medio": "Tribuna de San Luis",
        "tipo": "html",
        "url": "https://oem.com.mx/tribunadesanluis/deportes/"
    },
    {
        "nombre": "Tribuna de San Luis - San Luis Río Colorado",
        "medio": "Tribuna de San Luis",
        "tipo": "html",
        "url": (
            "https://oem.com.mx/tribunadesanluis/"
            "tags/temas/san-luis-rio-colorado/"
        )
    }
]


# ============================================================
# HEADERS Y SESIÓN
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_texto(texto):
    texto = str(texto or "").lower()

    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
        "ñ": "n"
    }

    for original, reemplazo in reemplazos.items():
        texto = texto.replace(original, reemplazo)

    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()

    return texto


def escapar_html(texto):
    texto = str(texto or "")

    return (
        texto
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


PALABRAS_IGNORAR = {
    # Stopwords
    "a", "al", "ante", "con", "de", "del", "el", "en", "es", "la", "las",
    "lo", "los", "para", "por", "que", "se", "sin", "su", "sus", "un",
    "una", "unos", "unas", "y", "o", "e", "le", "les", "mas", "tras",
    "hoy", "ya",
    # Términos regionales que aparecen en casi todos los títulos
    "san", "luis", "rio", "colorado", "slrc", "sonora", "sanluisino",
    "sanluisinos", "sanluisina", "sanluisinas"
}


def tokens_distintivos(texto):
    return {
        palabra
        for palabra in limpiar_texto(texto).split()
        if palabra not in PALABRAS_IGNORAR
    }


def titulo_parecido(t1, t2):
    """
    Antes: SequenceMatcher >= 0.80 sobre el título completo.
    Problema: "San Luis Río Colorado" ocupa ~40 % del título, por lo que
    noticias DISTINTAS daban ratio 0.85-0.98 y se descartaban como
    "ya enviadas".

    Ahora: Jaccard sobre palabras distintivas.
    """
    a = limpiar_texto(t1)
    b = limpiar_texto(t2)

    if not a or not b:
        return False

    if a == b:
        return True

    ta = tokens_distintivos(a)
    tb = tokens_distintivos(b)
    union = ta | tb

    if len(union) < MIN_TOKENS_COMPARABLES:
        # Muy pocas palabras útiles: solo casi idénticos.
        return SequenceMatcher(None, a, b).ratio() >= 0.95

    return len(ta & tb) / len(union) >= UMBRAL_SIMILITUD_TITULO


def es_url_http(url):
    return bool(
        url
        and (
            url.startswith("http://")
            or url.startswith("https://")
        )
    )


def clave_articulo(link):
    """
    Identificador estable de un artículo, independiente de:
      - http / https, www, mayúsculas
      - query string (?utm=...), #fragmento, "/" final
      - sección en la URL (Tribuna publica la misma nota bajo
        /local/... y /policiaca/... con el mismo ID numérico)

    Ejemplos:
      https://oem.com.mx/tribunadesanluis/local/titulo-12345678
          -> "oem:12345678"
      https://www.elimparcial.com/mxl/sanluis/2026/11/22/titulo/
          -> "elimparcial:titulo"
    """
    if not link:
        return None

    p = urlparse(str(link).strip())
    dominio = p.netloc.lower().removeprefix("www.")
    ruta = p.path.lower().rstrip("/")
    ultimo = ruta.rsplit("/", 1)[-1]

    if dominio.endswith("oem.com.mx"):
        m = re.search(r"-(\d{5,})$", ruta)
        if m:
            return f"oem:{m.group(1)}"

    if dominio.endswith("meganoticias.mx"):
        m = re.search(r"/noticia/[^/]+/(\d+)$", ruta)
        if m:
            return f"meganoticias:{m.group(1)}"

    if dominio.endswith("elimparcial.com") and ultimo:
        return f"elimparcial:{ultimo}"

    return f"{dominio}{ruta}" if dominio else None


# ============================================================
# HISTORIAL
# ============================================================
#
# Formato de noticias_enviadas.json: una LISTA de registros.
#
#   [
#     {
#       "titulo": "Titular de la noticia",
#       "link": "https://...",
#       "link_original": "https://...",   (solo si hubo redirección)
#       "clave": "oem:12345678",          (ID estable del artículo)
#       "fuente": "Tribuna de San Luis - Local",
#       "enviada": "2026-11-23T04:00:12-07:00"
#     }
#   ]
#
# Para borrar el historial basta con dejar el archivo así:  []
# (También funciona borrar el archivo o dejarlo vacío.)
#
# Si se encuentra el formato anterior ({"links": [...], "titulos": [...]})
# se migra automáticamente al nuevo formato en el primer guardado.
# ============================================================

def normalizar_registro(item):
    """Convierte cualquier entrada válida en un dict de registro, o None."""
    if isinstance(item, str):
        # Tolerancia: una lista simple de URLs ["https://...", ...]
        return {"link": item} if es_url_http(item) else {"titulo": item}

    if not isinstance(item, dict):
        return None

    registro = {
        clave: str(item[clave]).strip()
        for clave in ("titulo", "link", "link_original", "clave", "fuente", "enviada")
        if item.get(clave)
    }

    if not registro.get("link") and not registro.get("titulo"):
        return None

    if registro.get("link") and not registro.get("clave"):
        registro["clave"] = clave_articulo(registro["link"])

    return registro


def migrar_formato_anterior(data):
    """{"links": [...], "titulos": [...]} -> lista de registros."""
    registros = []

    for link in data.get("links") or []:
        if isinstance(link, str) and link:
            registros.append({"link": link})

    for titulo in data.get("titulos") or []:
        if isinstance(titulo, str) and titulo:
            registros.append({"titulo": titulo})

    log.warning(
        "Historial en formato anterior detectado; se migra a lista "
        f"({len(registros)} registros)."
    )
    return registros


def respaldar_archivo_danado():
    try:
        respaldo = f"{ARCHIVO_ENVIADAS}.bak_{int(time.time())}"
        os.replace(ARCHIVO_ENVIADAS, respaldo)
        log.warning(f"Historial respaldado como {respaldo}")
    except OSError:
        pass


def cargar_enviadas():
    """Devuelve siempre una lista de registros (posiblemente vacía)."""
    if not os.path.exists(ARCHIVO_ENVIADAS):
        return []

    try:
        with open(ARCHIVO_ENVIADAS, "r", encoding="utf-8") as archivo:
            contenido = archivo.read().strip()

        if not contenido:
            return []

        data = json.loads(contenido)

    except Exception as error:
        log.error(f"Error leyendo historial: {error}")
        respaldar_archivo_danado()
        return []

    if isinstance(data, dict):
        data = migrar_formato_anterior(data)

    if not isinstance(data, list):
        log.error("Historial con estructura desconocida; se ignora.")
        respaldar_archivo_danado()
        return []

    registros = []
    for item in data:
        registro = normalizar_registro(item)
        if registro:
            registros.append(registro)

    return registros


def guardar_enviadas_en_disco(registros):
    registros = registros[-MAX_HISTORIAL:]
    temporal = ARCHIVO_ENVIADAS + ".tmp"

    with open(temporal, "w", encoding="utf-8") as archivo:
        json.dump(registros, archivo, ensure_ascii=False, indent=2)
        archivo.write("\n")

    os.replace(temporal, ARCHIVO_ENVIADAS)


class Historial:

    def __init__(self):
        # Lista ordenada (más antiguo -> más reciente).
        self.registros = cargar_enviadas()
        self._reindexar()

        log.info(f"Historial cargado: {len(self.registros)} registros")

    def _reindexar(self):
        self.claves = set()
        self.titulos = []

        for registro in self.registros:
            for campo in ("link", "link_original"):
                clave = clave_articulo(registro.get(campo))
                if clave:
                    self.claves.add(clave)

            if registro.get("clave"):
                self.claves.add(registro["clave"])

            if registro.get("titulo"):
                self.titulos.append(registro["titulo"])

    def ya_fue_enviada(self, noticia):
        for campo in ("link", "link_original"):
            if clave_articulo(noticia.get(campo)) in self.claves:
                return True

        return any(
            titulo_parecido(noticia["titulo"], titulo_guardado)
            for titulo_guardado in self.titulos[-MAX_TITULOS_COMPARAR:]
        )

    def registrar(self, noticia):
        registro = {
            "titulo": noticia["titulo"],
            "link": noticia["link"],
            "fuente": noticia.get("fuente"),
            "enviada": datetime.now(TZ).isoformat(timespec="seconds")
        }

        if (
            noticia.get("link_original")
            and noticia["link_original"] != noticia["link"]
        ):
            registro["link_original"] = noticia["link_original"]

        registro["clave"] = clave_articulo(noticia["link"])

        registro = normalizar_registro(registro)
        self.registros.append(registro)

        for campo in ("link", "link_original"):
            clave = clave_articulo(registro.get(campo))
            if clave:
                self.claves.add(clave)
        self.titulos.append(registro["titulo"])

    def guardar(self, encontrados=None, enviados=None):
        """
        Las estadísticas de la corrida (encontrados/enviados) ya no se
        guardan en el JSON para que el archivo sea una lista pura;
        solo se registran en el log.
        """
        if DRY_RUN:
            return

        guardar_enviadas_en_disco(self.registros)

        extra = ""
        if encontrados is not None or enviados is not None:
            extra = f" | encontradas={encontrados}, enviadas={enviados}"

        log.info(
            f"Historial guardado: {len(self.registros[-MAX_HISTORIAL:])} registros"
            f"{extra}"
        )

# ============================================================
# FILTRO GEOGRÁFICO
# ============================================================

def es_noticia_slrc(titulo, link, requiere_mencion=False):
    """Acepta noticias de la sección local y referencias explícitas a SLRC."""
    texto_titulo = limpiar_texto(titulo)
    url = urlparse(link)
    ruta = url.path.lower()
    dominio = url.netloc.lower()

    referencias = (
        "san luis rio colorado", "slrc", "san luis rc",
        "san luis r c", "golfo de santa clara", "luis b sanchez",
        "valle de san luis"
    )
    menciona_region = any(x in texto_titulo for x in referencias)
    seccion_local = (
        ("elimparcial.com" in dominio and "/mxl/sanluis/" in ruta)
        or ("oem.com.mx" in dominio and "/tribunadesanluis/" in ruta)
        or ("meganoticias.mx" in dominio and "/s-luis-r-colorado/" in ruta)
        or ("sanluisrc.gob.mx" in dominio)
    )
    otras_ciudades = (
        "hermosillo", "nogales", "guaymas", "ciudad obregon",
        "caborca", "navojoa", "cananea", "agua prieta",
        "puerto penasco", "tijuana", "ensenada"
    )
    if any(ciudad in texto_titulo for ciudad in otras_ciudades) and not menciona_region:
        return False
    if requiere_mencion:
        return menciona_region
    return menciona_region or seccion_local


# ============================================================
# FECHAS
# ============================================================

def convertir_fecha(fecha_texto):
    if not fecha_texto:
        return None

    fecha_texto = str(
        fecha_texto
    ).strip()

    try:
        if fecha_texto.endswith("Z"):
            fecha_texto = (
                fecha_texto[:-1]
                + "+00:00"
            )

        fecha = datetime.fromisoformat(
            fecha_texto
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(
                tzinfo=TZ
            )

        return fecha.astimezone(TZ)

    except (ValueError, TypeError):
        pass

    formatos = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%Y %H:%M"
    ]

    for formato in formatos:
        try:
            fecha = datetime.strptime(
                fecha_texto,
                formato
            )

            return fecha.replace(
                tzinfo=TZ
            )

        except (ValueError, TypeError):
            continue

    return None


def extraer_fecha_desde_soup(soup):
    metas = [
        {"property": "article:published_time"},
        {"property": "article:modified_time"},
        {"name": "date"},
        {"name": "pubdate"},
        {"name": "publishdate"},
        {"name": "timestamp"},
        {"itemprop": "datePublished"},
        {"itemprop": "dateModified"}
    ]

    for meta_info in metas:
        meta = soup.find(
            "meta",
            attrs=meta_info
        )

        if (
            meta
            and meta.get("content")
        ):
            fecha = convertir_fecha(
                meta.get("content")
            )

            if fecha:
                return fecha

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    for script in scripts:
        texto = script.get_text(
            " ",
            strip=True
        )

        coincidencias = re.findall(
            r'"datePublished"\s*:\s*"([^"]+)"',
            texto
        )

        for fecha_texto in coincidencias:
            fecha = convertir_fecha(
                fecha_texto
            )

            if fecha:
                return fecha

    # <time datetime="...">
    for nodo in soup.find_all("time", attrs={"datetime": True}):
        fecha = convertir_fecha(nodo.get("datetime"))
        if fecha:
            return fecha

    # Fecha visible con etiqueta, p. ej. Meganoticias: "Fecha: 23-11-2026"
    texto_pagina = soup.get_text(" ", strip=True)
    m = re.search(r"Fecha:\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})", texto_pagina)
    if m:
        fecha = convertir_fecha(m.group(1).replace("/", "-"))
        if fecha:
            return fecha

    return None


def fecha_aceptable(fecha):
    """Acepta artículos de los últimos DIAS_RETROCESO días."""
    if fecha is None:
        return True  # Sin metadatos: se mantiene como candidato, no como fecha verificada.
    hoy = datetime.now(TZ).date()
    return hoy - timedelta(days=DIAS_RETROCESO) <= fecha.date() <= hoy


# ============================================================
# IMAGEN DEL ARTÍCULO
# ============================================================

def obtener_meta_content(
    soup,
    property_name=None,
    name=None,
    itemprop=None
):
    attrs = {}

    if property_name:
        attrs["property"] = property_name

    if name:
        attrs["name"] = name

    if itemprop:
        attrs["itemprop"] = itemprop

    meta = soup.find(
        "meta",
        attrs=attrs
    )

    if meta and meta.get("content"):
        return meta.get("content").strip()

    return None


def extraer_imagen_json_ld(
    soup,
    article_url
):
    """
    Busca URLs de imagen en JSON-LD.
    Soporta:
      "image": "https://..."
      "image": ["https://..."]
      "image": {"url": "https://..."}
      "thumbnailUrl": "https://..."
    """

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    patrones = [
        r'"image"\s*:\s*"([^"]+)"',
        r'"thumbnailUrl"\s*:\s*"([^"]+)"',
        r'"image"\s*:\s*\[\s*"([^"]+)"',
        r'"image"\s*:\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"'
    ]

    for script in scripts:
        texto = script.get_text(
            " ",
            strip=True
        )

        for patron in patrones:
            match = re.search(
                patron,
                texto,
                flags=re.I
            )

            if match:
                valor = (
                    match.group(1)
                    .replace("\\/", "/")
                )

                return urljoin(
                    article_url,
                    valor
                )

    return None


def escoger_de_srcset(
    srcset,
    article_url
):
    """
    Elige la imagen con mayor descriptor de un srcset.
    """

    if not srcset:
        return None

    candidatos = []

    for fragmento in srcset.split(","):
        fragmento = fragmento.strip()

        if not fragmento:
            continue

        partes = fragmento.split()

        url = partes[0].strip()

        peso = 0

        if len(partes) > 1:
            descriptor = partes[-1]

            numeros = re.findall(
                r"\d+",
                descriptor
            )

            if numeros:
                peso = int(
                    numeros[0]
                )

        candidatos.append(
            (
                peso,
                urljoin(
                    article_url,
                    url
                )
            )
        )

    if not candidatos:
        return None

    candidatos.sort(
        key=lambda x: x[0],
        reverse=True
    )

    return candidatos[0][1]


def parece_imagen_util(url):
    if not url:
        return False

    texto = limpiar_texto(url)

    excluir = [
        "logo",
        "icon",
        "avatar",
        "sprite",
        "favicon",
        "placeholder",
        "blank",
        "ads",
        "advert",
        "pixel",
        "tracking"
    ]

    return not any(
        palabra in texto
        for palabra in excluir
    )


def extraer_imagen_desde_soup(
    soup,
    article_url
):
    """
    Orden de preferencia:
      1) og:image
      2) og:image:secure_url
      3) twitter:image
      4) twitter:image:src
      5) itemprop=image
      6) JSON-LD image/thumbnailUrl
      7) imagen dentro de <article>
      8) imagen general de la página
    """

    candidatos_meta = [
        obtener_meta_content(
            soup,
            property_name="og:image"
        ),
        obtener_meta_content(
            soup,
            property_name="og:image:secure_url"
        ),
        obtener_meta_content(
            soup,
            name="twitter:image"
        ),
        obtener_meta_content(
            soup,
            name="twitter:image:src"
        ),
        obtener_meta_content(
            soup,
            itemprop="image"
        )
    ]

    for candidato in candidatos_meta:
        if candidato:
            url = urljoin(
                article_url,
                candidato
            )

            if parece_imagen_util(url):
                return url

    json_ld = extraer_imagen_json_ld(
        soup,
        article_url
    )

    if (
        json_ld
        and parece_imagen_util(json_ld)
    ):
        return json_ld

    # Primero buscamos dentro del artículo.
    contenedores = []

    article = soup.find("article")

    if article:
        contenedores.append(article)

    main = soup.find("main")

    if main:
        contenedores.append(main)

    # Al final se permite toda la página.
    contenedores.append(soup)

    atributos_directos = [
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-url",
        "src"
    ]

    for contenedor in contenedores:

        imagenes = contenedor.find_all(
            "img"
        )

        for img in imagenes:

            # srcset / data-srcset primero, pues suelen contener
            # la imagen de mayor resolución.
            for atributo_srcset in [
                "data-srcset",
                "srcset"
            ]:
                srcset = img.get(
                    atributo_srcset
                )

                url_srcset = escoger_de_srcset(
                    srcset,
                    article_url
                )

                if (
                    url_srcset
                    and parece_imagen_util(
                        url_srcset
                    )
                ):
                    return url_srcset

            for atributo in atributos_directos:
                valor = img.get(
                    atributo
                )

                if not valor:
                    continue

                url = urljoin(
                    article_url,
                    valor.strip()
                )

                if parece_imagen_util(url):
                    return url

    return None


# ============================================================
# DESCARGA Y NORMALIZACIÓN DE LA IMAGEN
# ============================================================

def descargar_imagen(
    image_url,
    article_url
):
    """
    Este es el cambio principal.

    NO se le pide a Telegram que descargue la imagen del periódico.
    El bot descarga la imagen con headers de navegador y Referer del artículo,
    la convierte a JPEG en memoria y luego la SUBE a Telegram.

    Esto evita problemas de:
      - hotlink protection
      - Telegram sin acceso a la imagen
      - WebP/AVIF incompatibles
      - cookies/headers requeridos por el medio
    """

    if not es_url_http(image_url):
        return None

    headers_imagen = {
        "User-Agent": HEADERS["User-Agent"],
        "Accept": (
            "image/avif,image/webp,image/apng,"
            "image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Accept-Language": HEADERS[
            "Accept-Language"
        ],
        "Referer": article_url
    }

    try:
        response = SESSION.get(
            image_url,
            headers=headers_imagen,
            timeout=25,
            allow_redirects=True,
            stream=True
        )

        response.raise_for_status()

        contenido = bytearray()

        for chunk in response.iter_content(
            chunk_size=65536
        ):
            if not chunk:
                continue

            contenido.extend(chunk)

            if len(contenido) > MAX_IMAGE_BYTES:
                log.warning(
                    "Imagen demasiado grande: "
                    f"{image_url}"
                )
                return None

        if not contenido:
            return None

        content_type = (
            response.headers
            .get("Content-Type", "")
            .lower()
        )

        if (
            content_type
            and "image" not in content_type
        ):
            log.warning(
                "La URL no devolvió una imagen: "
                f"{image_url} "
                f"Content-Type={content_type}"
            )

        # Convertimos cualquier formato que Pillow entienda
        # a JPEG RGB. Así Telegram recibe siempre una foto estándar.
        entrada = BytesIO(
            bytes(contenido)
        )

        with Image.open(entrada) as imagen:
            imagen.load()

            # Corrige orientación EXIF cuando existe.
            try:
                from PIL import ImageOps
                imagen = ImageOps.exif_transpose(
                    imagen
                )
            except Exception:
                pass

            if imagen.mode not in (
                "RGB",
                "L"
            ):
                imagen = imagen.convert(
                    "RGB"
                )

            elif imagen.mode == "L":
                imagen = imagen.convert(
                    "RGB"
                )

            salida = BytesIO()

            imagen.save(
                salida,
                format="JPEG",
                quality=90,
                optimize=True
            )

            salida.seek(0)

            log.info(
                "Imagen descargada y convertida "
                f"correctamente: {image_url}"
            )

            return salida

    except Exception as error:
        log.warning(
            "No se pudo descargar/convertir imagen "
            f"{image_url}: {error}"
        )

        return None


# ============================================================
# OBTENER METADATOS DEL ARTÍCULO
# ============================================================

def obtener_metadatos_articulo(
    noticia
):
    """
    Descarga una sola vez el HTML del artículo y extrae:
      - fecha
      - imagen principal
    """

    link = noticia["link"]

    try:
        response = SESSION.get(
            link,
            timeout=20,
            allow_redirects=True
        )

        response.raise_for_status()

        # Guardamos la URL final tras redirecciones (y la original,
        # para que ambas queden en el historial).
        noticia["link_original"] = link
        noticia["link"] = response.url

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # El texto del <a> en la portada puede traer ruido
        # (sección, hora, resumen). og:title es el título real.
        og_title = obtener_meta_content(
            soup,
            property_name="og:title"
        )

        if og_title and len(og_title) >= 15:
            noticia["titulo"] = re.sub(
                r"\s*[|\-–]\s*(Tribuna de San Luis|El Imparcial).*$",
                "",
                og_title.strip(),
                flags=re.I
            )

        noticia["fecha"] = (
            extraer_fecha_desde_soup(
                soup
            )
        )

        noticia["imagen"] = (
            extraer_imagen_desde_soup(
                soup,
                response.url
            )
        )

        if noticia["imagen"]:
            log.info(
                "Imagen detectada para "
                f"'{noticia['titulo']}': "
                f"{noticia['imagen']}"
            )
        else:
            log.warning(
                "No se detectó imagen en: "
                f"{noticia['titulo']}"
            )

        return True

    except requests.exceptions.RequestException as error:
        log.warning(
            "No se pudo abrir artículo "
            f"{link}: {error}"
        )

        noticia["fecha"] = None
        noticia["imagen"] = None

        return False


# ============================================================
# DUPLICADOS
# ============================================================

def eliminar_duplicados(lista):
    unicas = []

    for noticia in lista:
        repetida = False

        for existente in unicas:
            if (
                noticia["link"]
                == existente["link"]
            ):
                repetida = True
                break

            if titulo_parecido(
                noticia["titulo"],
                existente["titulo"]
            ):
                repetida = True
                break

        if not repetida:
            unicas.append(
                noticia
            )

    return unicas


# ============================================================
# SCRAPING DE FUENTES
# ============================================================

def construir_url_absoluta(
    base_url,
    href
):
    if not href:
        return None

    href = href.strip()

    if href.startswith(
        "javascript:"
    ):
        return None

    if href.startswith("#"):
        return None

    return urljoin(
        base_url,
        href
    )


def parece_articulo(link):
    if not link:
        return False

    parsed = urlparse(link)

    path = parsed.path.lower()

    excluir = [
        "/facebook",
        "/twitter",
        "/instagram",
        "/youtube",
        "/login",
        "/suscripcion",
        "/subscription",
        "/privacy",
        "/privacidad",
        "/terminos",
        "/contacto"
    ]

    if any(
        x in path
        for x in excluir
    ):
        return False

    # Para Tribuna, los artículos terminan normalmente en un ID numérico.
    if "oem.com.mx/tribunadesanluis" in link:
        if re.search(r"-\d{5,}$", path.rstrip("/")):
            return True
        # Algunos artículos nuevos pueden usar un formato distinto de ID.
        return len(path.rstrip("/").split("/")) >= 4 and len(path.rstrip("/").split("/")[-1]) > 25

    # Meganoticias: /s-luis-r-colorado/noticia/<slug>/<id>
    if "meganoticias.mx" in link:
        return bool(re.search(r"/s-luis-r-colorado/noticia/[^/]+/\d+$", path.rstrip("/")))

    # Ayuntamiento (WordPress): /<slug-largo>/
    if "sanluisrc.gob.mx" in link:
        segmentos = [x for x in path.split("/") if x]
        return len(segmentos) == 1 and len(segmentos[0]) > 25

    # Para El Imparcial, las notas de San Luis usan /mxl/sanluis/YYYY/MM/DD/...
    if "elimparcial.com" in link:
        if re.search(
            r"/mxl/sanluis/\d{4}/\d{2}/\d{2}/",
            path
        ):
            return True

        # También aceptamos algunas notas de Sonora que mencionan SLRC.
        if re.search(
            r"/son/sonora/\d{4}/\d{2}/\d{2}/",
            path
        ):
            return True

        return False

    return False


def texto_del_enlace(item):
    """Texto visible del <a>; si es corto, usa title/aria-label/alt."""
    texto = item.get_text(" ", strip=True)

    if len(texto) >= 20:
        return texto

    alternativas = [
        item.get("title"),
        item.get("aria-label")
    ]

    img = item.find("img")

    if img:
        alternativas.append(img.get("alt"))

    for alt in alternativas:
        if alt and len(alt.strip()) >= 20:
            return alt.strip()

    return texto


def nuevo_embudo():
    return {
        "items": 0,
        "texto_corto": 0,
        "no_articulo": 0,
        "no_slrc": 0,
        "aceptadas": 0
    }


def leer_fuente_html(fuente, embudo):
    response = SESSION.get(fuente["url"], timeout=20, allow_redirects=True)

    if response.status_code != 200:
        log.warning(f"{fuente['nombre']} respondió HTTP {response.status_code}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    enlaces = soup.find_all("a", href=True)
    embudo["items"] = len(enlaces)
    noticias = []

    for posicion, item in enumerate(enlaces):
        link = construir_url_absoluta(response.url, item.get("href", ""))

        if not link:
            continue

        # Sin query ni fragment (utm_*, #comentarios...).
        p = urlparse(link)
        link = f"{p.scheme}://{p.netloc}{p.path}"

        if not parece_articulo(link):
            embudo["no_articulo"] += 1
            continue

        titulo = texto_del_enlace(item)

        if len(titulo) < 20:
            embudo["texto_corto"] += 1
            continue

        if not es_noticia_slrc(titulo, link, fuente.get("requiere_mencion", False)):
            embudo["no_slrc"] += 1
            if DEBUG:
                log.info(f"  [no_slrc] {titulo} -> {link}")
            continue

        embudo["aceptadas"] += 1
        noticias.append({
            "titulo": titulo,
            "link": link,
            "fuente": fuente["nombre"],
            "medio": fuente.get("medio", fuente["nombre"]),
            "posicion": posicion,
            "fecha": None,
            "imagen": None
        })

    if embudo["items"] and embudo["no_articulo"] == embudo["items"]:
        log.warning(
            f"{fuente['nombre']}: ningún link pasó parece_articulo(). "
            "Posible cambio de URLs o contenido cargado por JavaScript."
        )

    return noticias


def desenvolver_link_agregador(link):
    """
    Bing News entrega links tipo:
      http://www.bing.com/news/apiclick.aspx?...&url=https%3a%2f%2foem.com.mx%2f...
    Se extrae la URL real del artículo (parámetro url=).
    """
    if not link:
        return link

    p = urlparse(link)

    if "bing.com" in p.netloc.lower() and "apiclick" in p.path.lower():
        destino = parse_qs(p.query).get("url", [None])[0]
        if destino and es_url_http(destino):
            return destino

    return link


def imagen_de_item_rss(item):
    media = "{http://search.yahoo.com/mrss/}"

    # Bing News: <News:Image>http://www.bing.com/th?id=...</News:Image>
    for nodo in item.iter():
        etiqueta = str(nodo.tag).rsplit("}", 1)[-1].split(":")[-1]
        if etiqueta.lower() == "image" and (nodo.text or "").strip().startswith("http"):
            return nodo.text.strip().replace("http://", "https://", 1)

    for etiqueta in (f"{media}content", f"{media}thumbnail"):
        for nodo in item.iter(etiqueta):
            url = nodo.get("url")
            tipo = (nodo.get("type") or nodo.get("medium") or "image").lower()
            if url and ("image" in tipo):
                return url.strip()

    enclosure = item.find("enclosure")
    if enclosure is not None and "image" in (enclosure.get("type") or ""):
        url = (enclosure.get("url") or "").strip()
        if url:
            return url

    # WordPress: primera <img> dentro de content:encoded o description.
    for etiqueta in ("{http://purl.org/rss/1.0/modules/content/}encoded", "description"):
        html = item.findtext(etiqueta) or ""
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html, flags=re.I)
        if m and parece_imagen_util(m.group(1)):
            return m.group(1).strip()

    return None


def leer_fuente_rss(fuente, embudo):
    response = SESSION.get(
        fuente["url"],
        timeout=20,
        allow_redirects=True,
        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}
    )

    if response.status_code != 200:
        log.warning(f"{fuente['nombre']} respondió HTTP {response.status_code}")
        return []

    try:
        raiz = ET.fromstring(response.content)
    except ET.ParseError as error:
        log.warning(f"{fuente['nombre']}: RSS inválido ({error})")
        return []

    items = list(raiz.iter("item"))
    embudo["items"] = len(items)
    noticias = []

    for posicion, item in enumerate(items):
        titulo = re.sub(r"\s+", " ", item.findtext("title") or "").strip()
        link = desenvolver_link_agregador((item.findtext("link") or "").strip())

        if not es_url_http(link):
            embudo["no_articulo"] += 1
            continue

        # Agregadores (Bing): aceptar solo notas del sitio indicado.
        if fuente.get("solo_dominio") and (
            fuente["solo_dominio"] not in link or not parece_articulo(link)
        ):
            embudo["no_articulo"] += 1
            continue

        if len(titulo) < 15:
            embudo["texto_corto"] += 1
            continue

        if not es_noticia_slrc(titulo, link, fuente.get("requiere_mencion", False)):
            embudo["no_slrc"] += 1
            if DEBUG:
                log.info(f"  [no_slrc] {titulo} -> {link}")
            continue

        fecha = None
        pub = item.findtext("pubDate")
        if pub:
            try:
                fecha = parsedate_to_datetime(pub.strip()).astimezone(TZ)
            except (TypeError, ValueError):
                fecha = convertir_fecha(pub)

        embudo["aceptadas"] += 1
        noticias.append({
            "titulo": titulo,
            "link": link,
            "fuente": fuente["nombre"],
            "medio": fuente.get("medio", fuente["nombre"]),
            "posicion": posicion,
            "fecha": fecha,
            "imagen": imagen_de_item_rss(item)
        })

    return noticias


def obtener_noticias(historial):
    """
    Devuelve las noticias NO enviadas, ordenadas de la más reciente
    a la más antigua. main() envía las primeras MAX_NOTICIAS_POR_CORRIDA.
    """
    lectores = {"rss": leer_fuente_rss, "html": leer_fuente_html}

    # 1) Recolectar de todas las fuentes, deduplicando por ID de artículo.
    por_clave = {}

    for indice, fuente in enumerate(FUENTES):
        # robots.txt de oem.com.mx pide Crawl-delay: 2
        if indice:
            time.sleep(2)

        embudo = nuevo_embudo()
        lector = lectores.get(fuente.get("tipo", "html"), leer_fuente_html)

        try:
            log.info(f"Leyendo: {fuente['nombre']}")
            noticias = lector(fuente, embudo)
        except requests.exceptions.RequestException as error:
            log.warning(f"Error de red en {fuente['nombre']}: {error}")
            continue
        except Exception as error:
            log.exception(f"Error inesperado en {fuente['nombre']}: {error}")
            continue

        log.info(
            f"Embudo {fuente['nombre']}: "
            + ", ".join(f"{k}={v}" for k, v in embudo.items())
        )

        for noticia in noticias:
            clave = clave_articulo(noticia["link"])
            noticia["clave"] = clave
            previa = por_clave.get(clave)

            if previa is None:
                por_clave[clave] = noticia
                continue

            # Misma nota en varias fuentes/páginas: completar datos.
            if not previa.get("fecha") and noticia.get("fecha"):
                previa["fecha"] = noticia["fecha"]
            if not previa.get("imagen") and noticia.get("imagen"):
                previa["imagen"] = noticia["imagen"]
            # Si la previa vino de HTML (sin fecha), preferir el título más largo.
            if not previa.get("fecha") and len(noticia["titulo"]) > len(previa["titulo"]):
                previa["titulo"] = noticia["titulo"]

    recolectadas = list(por_clave.values())

    # 2) Quitar las ya enviadas (por ID de artículo y por título).
    nuevas = [n for n in recolectadas if not historial.ya_fue_enviada(n)]

    log.info(
        f"Recolectadas: {len(recolectadas)} únicas | "
        f"ya enviadas: {len(recolectadas) - len(nuevas)} | "
        f"nuevas: {len(nuevas)}"
    )

    # 3) Completar fecha/imagen abriendo el artículo SOLO si falta algo
    #    (las notas de RSS normalmente ya traen ambas).
    revisadas = 0
    for noticia in nuevas:
        if noticia.get("fecha") and noticia.get("imagen"):
            continue
        if revisadas >= MAX_CANDIDATAS_A_REVISAR:
            break

        fecha_rss, imagen_rss = noticia.get("fecha"), noticia.get("imagen")
        obtener_metadatos_articulo(noticia)
        noticia["fecha"] = noticia.get("fecha") or fecha_rss
        noticia["imagen"] = noticia.get("imagen") or imagen_rss
        noticia["clave"] = clave_articulo(noticia["link"])
        revisadas += 1
        time.sleep(0.3)

    # 4) Filtrar por fecha, revalidar y quitar duplicados entre fuentes.
    finales = []
    claves_finales = set()
    rechazadas_fecha = 0
    duplicadas = 0

    # Ordenar primero por fecha para que, entre duplicados, gane la más completa.
    nuevas.sort(
        key=lambda n: n.get("fecha") or datetime.min.replace(tzinfo=TZ),
        reverse=True
    )

    for noticia in nuevas:
        if not fecha_aceptable(noticia.get("fecha")):
            rechazadas_fecha += 1
            if DEBUG:
                log.info(f"  [fecha {noticia.get('fecha')}] {noticia['titulo']}")
            continue

        if (
            noticia["clave"] in claves_finales
            or historial.ya_fue_enviada(noticia)
            or any(titulo_parecido(noticia["titulo"], n["titulo"]) for n in finales)
        ):
            duplicadas += 1
            continue

        claves_finales.add(noticia["clave"])
        finales.append(noticia)

    log.info(
        f"Resultado: {len(finales)} listas para enviar | "
        f"{rechazadas_fecha} fuera de rango de fecha | "
        f"{duplicadas} duplicadas"
    )

    for medio, cupo in CUPO_POR_MEDIO.items():
        disponibles = sum(1 for n in finales if n.get("medio") == medio)
        if disponibles < cupo:
            log.warning(
                f"{medio}: solo {disponibles} noticias nuevas disponibles "
                f"(cupo: {cupo}). Revisa su embudo arriba."
            )
        else:
            log.info(f"{medio}: {disponibles} noticias nuevas disponibles (cupo: {cupo})")

    return finales


def puede_enviar(medio, conteo_por_medio, total_enviadas, disponibles_por_medio):
    """Decide si todavía cabe una nota de este medio en la corrida."""
    if total_enviadas >= MAX_NOTICIAS_POR_CORRIDA:
        return False

    cupo = CUPO_POR_MEDIO.get(medio, 0)
    usados = conteo_por_medio.get(medio, 0)

    if usados < cupo:
        return True

    if not RELLENAR_CUPO_VACIO:
        return False

    # Rellenar: solo si algún otro medio ya no puede llenar su cupo.
    espacio_libre = sum(
        max(0, c - max(conteo_por_medio.get(m, 0), disponibles_por_medio.get(m, 0)))
        for m, c in CUPO_POR_MEDIO.items() if m != medio
    )
    extra_usado = sum(
        max(0, conteo_por_medio.get(m, 0) - c) for m, c in CUPO_POR_MEDIO.items()
    )
    return extra_usado < espacio_libre


def seleccionar_por_cupo(noticias):
    """Vista previa (DRY_RUN) de lo que se enviaría respetando los cupos."""
    conteo, seleccion = {}, []
    disponibles = {}
    for n in noticias:
        disponibles[n.get("medio")] = disponibles.get(n.get("medio"), 0) + 1
    for n in noticias:
        if puede_enviar(n.get("medio"), conteo, len(seleccion), disponibles):
            conteo[n.get("medio")] = conteo.get(n.get("medio"), 0) + 1
            seleccion.append(n)
    return seleccion


# ============================================================
# TELEGRAM
# ============================================================

def validar_respuesta_telegram(
    response
):
    log.info(
        f"Telegram status: "
        f"{response.status_code}"
    )

    try:
        payload = response.json()
    except ValueError:
        log.error(
            "Telegram devolvió respuesta no JSON: "
            f"{response.text[:500]}"
        )
        return False

    if (
        response.status_code != 200
        or not payload.get(
            "ok",
            False
        )
    ):
        log.error(
            "Telegram rechazó el mensaje: "
            f"{payload}"
        )
        return False

    return True


def post_telegram(url, data, files=None, timeout=30, intentos=3):
    """POST a Telegram respetando 429 (retry_after)."""
    for intento in range(1, intentos + 1):
        if files:
            # files = {"photo": (nombre, BytesIO, mime)}
            for valor in files.values():
                valor[1].seek(0)

        response = requests.post(
            url, data=data, files=files, timeout=timeout
        )

        if response.status_code != 429:
            return response

        try:
            espera = int(
                response.json()
                .get("parameters", {})
                .get("retry_after", 5)
            )
        except ValueError:
            espera = 5

        log.warning(
            f"Telegram 429 (rate limit). Esperando {espera}s "
            f"(intento {intento}/{intentos})"
        )
        time.sleep(espera + 1)

    return response


def enviar_mensaje(
    texto,
    mostrar_preview=False
):
    url = (
        "https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    try:
        response = post_telegram(
            url,
            data={
                "chat_id": CHAT_ID,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview":
                    not mostrar_preview
            },
            timeout=30
        )

        return validar_respuesta_telegram(
            response
        )

    except requests.exceptions.RequestException as error:
        log.error(
            f"Error enviando mensaje: {error}"
        )
        return False


def enviar_foto_subida(
    noticia,
    imagen_jpeg
):
    """
    SUBE la imagen a Telegram mediante multipart/form-data.
    Telegram ya no necesita entrar al sitio del periódico.
    """

    titulo_crudo = noticia["titulo"]

    fuente = escapar_html(
        noticia["fuente"]
    )

    link = escapar_html(
        noticia["link"]
    )

    def armar_caption(t):
        return (
            f"<b>{escapar_html(t)}</b>\n"
            f"Fuente: {fuente}\n"
            f'<a href="{link}">Abrir noticia</a>'
        )

    caption = armar_caption(titulo_crudo)

    # Telegram limita el caption a 1024 caracteres (de texto visible);
    # recortamos el título con margen para no exceder el límite.
    while len(caption) > LIMITE_CAPTION and len(titulo_crudo) > 40:
        titulo_crudo = titulo_crudo[:len(titulo_crudo) - 50].rstrip() + "…"
        caption = armar_caption(titulo_crudo)

    url = (
        "https://api.telegram.org/"
        f"bot{TOKEN}/sendPhoto"
    )

    try:
        imagen_jpeg.seek(0)

        response = post_telegram(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML"
            },
            files={
                "photo": (
                    "noticia.jpg",
                    imagen_jpeg,
                    "image/jpeg"
                )
            },
            timeout=60
        )

        if validar_respuesta_telegram(
            response
        ):
            log.info(
                "Foto SUBIDA correctamente "
                f"para: {noticia['titulo']}"
            )
            return True

        return False

    except requests.exceptions.RequestException as error:
        log.error(
            "Error subiendo foto a Telegram: "
            f"{error}"
        )
        return False


def enviar_noticia(
    noticia
):
    """
    Estrategia:
      1) Si detectamos imagen, el bot la descarga.
      2) La convierte a JPEG.
      3) La SUBE directamente a Telegram.
      4) Si algo falla, manda mensaje con preview como respaldo.
    """

    image_url = noticia.get(
        "imagen"
    )

    if image_url:
        imagen_jpeg = descargar_imagen(
            image_url,
            noticia["link"]
        )

        if imagen_jpeg:
            if enviar_foto_subida(
                noticia,
                imagen_jpeg
            ):
                return True

    # Fallback cuando no se pudo encontrar/descargar/subir imagen.
    log.warning(
        "Usando fallback de preview para: "
        f"{noticia['titulo']}"
    )

    titulo = escapar_html(
        noticia["titulo"]
    )

    fuente = escapar_html(
        noticia["fuente"]
    )

    link = escapar_html(
        noticia["link"]
    )

    mensaje = (
        f"<b>{titulo}</b>\n"
        f"Fuente: {fuente}\n"
        f"Link: {link}"
    )

    return enviar_mensaje(
        mensaje,
        mostrar_preview=True
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if DRY_RUN:
        log.info("=== DRY_RUN: no se envía nada ni se modifica el historial ===")
        noticias = obtener_noticias(Historial())
        elegidas = {id(n) for n in seleccionar_por_cupo(noticias)}
        for i, n in enumerate(noticias, 1):
            marca = "ENVIARÍA" if id(n) in elegidas else "fuera de cupo"
            log.info(
                f"{i:02d}. [{marca}] {n.get('fecha')} | {n.get('medio')} | "
                f"{n['titulo']} | img={'sí' if n.get('imagen') else 'no'}"
            )
        return

    if not TOKEN:
        log.error(
            "Falta configurar TOKEN."
        )
        return

    if not CHAT_ID:
        log.error(
            "Falta configurar CHAT_ID."
        )
        return

    log.info(
        "Buscando noticias nuevas "
        "de San Luis Río Colorado..."
    )

    historial = Historial()

    # Registrar ejecución aunque no haya noticias.
    historial.guardar(
        encontrados=0,
        enviados=0
    )

    noticias_a_enviar = (
        obtener_noticias(
            historial
        )
    )

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=0
    )

    if not noticias_a_enviar:
        log.info(
            "No hay noticias nuevas "
            "para publicar."
        )
        return

    # ========================================================
    # ENCABEZADO
    # ========================================================

    ahora = (
        datetime
        .now(TZ)
        .strftime("%d/%m/%Y")
    )

    encabezado = (
        "<b>SAN LUIS NOTICIAS</b>\n"
        f"<b>Fecha:</b> {ahora}"
    )

    if not enviar_mensaje(encabezado, mostrar_preview=False):
        log.error("No se pudo publicar el encabezado; se cancela la ejecución.")
        return

    time.sleep(2)

    # ========================================================
    # NOTICIAS
    # ========================================================

    total_enviadas = 0
    total_fallidas = 0

    claves_enviadas_corrida = set()
    conteo_por_medio = {}
    disponibles_por_medio = {}
    for n in noticias_a_enviar:
        disponibles_por_medio[n.get("medio")] = disponibles_por_medio.get(n.get("medio"), 0) + 1

    for noticia in noticias_a_enviar:
        if total_enviadas >= MAX_NOTICIAS_POR_CORRIDA:
            break

        # Cupo por medio (5 Tribuna + 5 El Imparcial). Si una nota falla,
        # la siguiente del mismo medio ocupa su lugar.
        if not puede_enviar(
            noticia.get("medio"),
            conteo_por_medio,
            total_enviadas,
            disponibles_por_medio
        ):
            continue

        # Doble seguro anti-repetición dentro de la misma corrida.
        if (
            noticia["clave"] in claves_enviadas_corrida
            or historial.ya_fue_enviada(noticia)
        ):
            continue

        enviado = enviar_noticia(
            noticia
        )

        if enviado:
            claves_enviadas_corrida.add(noticia["clave"])
            conteo_por_medio[noticia.get("medio")] = (
                conteo_por_medio.get(noticia.get("medio"), 0) + 1
            )
            historial.registrar(
                noticia
            )

            total_enviadas += 1

            # Guardar inmediatamente.
            historial.guardar(
                encontrados=len(
                    noticias_a_enviar
                ),
                enviados=total_enviadas
            )

        else:
            total_fallidas += 1

            log.warning(
                "No se pudo enviar; "
                "se reintentará en próxima corrida: "
                f"{noticia['titulo']}"
            )

        time.sleep(1)

    historial.guardar(
        encontrados=len(
            noticias_a_enviar
        ),
        enviados=total_enviadas
    )

    log.info(
        "Enviadas por medio: "
        + ", ".join(f"{m}={conteo_por_medio.get(m, 0)}/{c}" for m, c in CUPO_POR_MEDIO.items())
    )

    log.info(
        f"Total enviadas: "
        f"{total_enviadas} | "
        f"Total fallidas: "
        f"{total_fallidas}"
    )


# ============================================================
# EJECUCIÓN
# ============================================================

if __name__ == "__main__":
    main()
