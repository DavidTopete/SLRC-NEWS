import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
from io import BytesIO
from PIL import Image
import time
import json
import os
import re
import logging
from difflib import SequenceMatcher


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
UMBRAL_SIMILITUD_TITULO = 0.80
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
MAX_NOTICIAS_POR_CORRIDA = 10
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
# IMPORTANTE:
# El Imparcial ya publica la sección de San Luis bajo /mxl/sanluis/
# y no bajo la URL antigua /sonora/sanluisriocolorado/.
#
# Tribuna se consulta mediante páginas que contienen noticias locales.
# ============================================================

FUENTES = [
    {
        "nombre": "Tribuna de San Luis",
        "url": "https://oem.com.mx/tribunadesanluis/"
    },
    {
        "nombre": "Tribuna de San Luis - Local",
        "url": "https://oem.com.mx/tribunadesanluis/local/"
    },
    {
        "nombre": "Tribuna de San Luis - Policiaca",
        "url": "https://oem.com.mx/tribunadesanluis/policiaca/"
    },
    {
        "nombre": "Tribuna de San Luis - Deportes",
        "url": "https://oem.com.mx/tribunadesanluis/deportes/"
    },
    {
        "nombre": "Tribuna de San Luis - San Luis Río Colorado",
        "url": (
            "https://oem.com.mx/tribunadesanluis/"
            "tags/temas/san-luis-rio-colorado/"
        )
    },
    {
        "nombre": "El Imparcial San Luis",
        "url": "https://www.elimparcial.com/mxl/sanluis/"
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
        for clave in ("titulo", "link", "link_original", "fuente", "enviada")
        if item.get(clave)
    }

    if not registro.get("link") and not registro.get("titulo"):
        return None

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
        self.links = set()
        self.titulos = []

        for registro in self.registros:
            for clave in ("link", "link_original"):
                if registro.get(clave):
                    self.links.add(registro[clave])

            if registro.get("titulo"):
                self.titulos.append(registro["titulo"])

    def ya_fue_enviada(self, noticia):
        if noticia["link"] in self.links:
            return True

        if noticia.get("link_original") in self.links:
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

        registro = normalizar_registro(registro)
        self.registros.append(registro)

        self.links.add(registro["link"])
        if registro.get("link_original"):
            self.links.add(registro["link_original"])
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

def es_noticia_slrc(titulo, link):
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
    )
    otras_ciudades = (
        "hermosillo", "nogales", "guaymas", "ciudad obregon",
        "caborca", "navojoa", "cananea", "agua prieta",
        "puerto penasco", "tijuana", "ensenada"
    )
    if any(ciudad in texto_titulo for ciudad in otras_ciudades) and not menciona_region:
        return False
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
        "%d/%m/%Y"
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


def obtener_noticias(
    historial
):
    candidatas = []
    vistos_en_corrida = {}

    for fuente in FUENTES:

        embudo = {
            "anchors": 0,
            "texto_corto": 0,
            "no_articulo": 0,
            "no_slrc": 0,
            "repetido_en_corrida": 0,
            "ya_enviada": 0,
            "candidatas": 0
        }

        try:
            log.info(
                f"Leyendo: "
                f"{fuente['nombre']}"
            )

            response = SESSION.get(
                fuente["url"],
                timeout=20,
                allow_redirects=True
            )

            if response.status_code != 200:
                log.warning(
                    f"{fuente['nombre']} respondió "
                    f"HTTP {response.status_code}"
                )
                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser"
            )

            links = soup.find_all(
                "a",
                href=True
            )

            embudo["anchors"] = len(links)

            for posicion, item in enumerate(
                links
            ):
                titulo = texto_del_enlace(item)

                href = item.get(
                    "href",
                    ""
                ).strip()

                link = construir_url_absoluta(
                    response.url,
                    href
                )

                if not link:
                    continue

                # Normalizar: sin query ni fragment (utm_*, #comentarios...).
                p = urlparse(link)
                link = f"{p.scheme}://{p.netloc}{p.path}"

                if not parece_articulo(
                    link
                ):
                    embudo["no_articulo"] += 1
                    continue

                if (
                    not titulo
                    or len(titulo) < 20
                ):
                    embudo["texto_corto"] += 1
                    continue

                if not es_noticia_slrc(
                    titulo,
                    link
                ):
                    embudo["no_slrc"] += 1
                    if DEBUG:
                        log.info(f"  [no_slrc] {titulo} -> {link}")
                    continue

                # Mismo link visto antes en esta corrida: conservar el
                # título más largo (suele ser el titular completo).
                if link in vistos_en_corrida:
                    previa = vistos_en_corrida[link]
                    if len(titulo) > len(previa["titulo"]):
                        previa["titulo"] = titulo
                    embudo["repetido_en_corrida"] += 1
                    continue

                noticia = {
                    "titulo": titulo,
                    "link": link,
                    "fuente":
                        fuente["nombre"],
                    "posicion":
                        posicion
                }

                vistos_en_corrida[link] = noticia

                if historial.ya_fue_enviada(
                    noticia
                ):
                    embudo["ya_enviada"] += 1
                    if DEBUG:
                        log.info(f"  [ya_enviada] {titulo} -> {link}")
                    continue

                embudo["candidatas"] += 1

                candidatas.append(
                    noticia
                )

            log.info(
                f"Embudo {fuente['nombre']}: "
                + ", ".join(f"{k}={v}" for k, v in embudo.items())
            )

            if embudo["candidatas"] == 0 and embudo["no_articulo"] == embudo["anchors"]:
                log.warning(
                    f"{fuente['nombre']}: ningún link pasó parece_articulo(). "
                    "Posible cambio de estructura de URLs o contenido cargado por JavaScript."
                )

        except requests.exceptions.RequestException as error:
            log.warning(
                "Error de red en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

        except Exception as error:
            log.exception(
                "Error inesperado en "
                f"{fuente['nombre']}: "
                f"{error}"
            )

    candidatas = eliminar_duplicados(
        candidatas
    )

    log.info(
        f"Candidatas únicas antes de fecha: "
        f"{len(candidatas)}"
    )

    noticias_finales = []
    rechazadas_fecha = 0
    rechazadas_tras_og_title = 0

    for noticia in candidatas[:MAX_CANDIDATAS_A_REVISAR]:

        obtener_metadatos_articulo(
            noticia
        )
        time.sleep(0.3)

        if not fecha_aceptable(
            noticia.get("fecha")
        ):
            rechazadas_fecha += 1
            log.info(
                "Ignorada por fecha "
                f"({noticia.get('fecha')}): "
                f"{noticia['titulo']}"
            )
            continue

        # El título pudo cambiar con og:title o el link con la redirección:
        # revalidar contra historial y contra lo ya aceptado.
        if historial.ya_fue_enviada(noticia) or any(
            noticia["link"] == n["link"]
            or titulo_parecido(noticia["titulo"], n["titulo"])
            for n in noticias_finales
        ):
            rechazadas_tras_og_title += 1
            continue

        noticias_finales.append(
            noticia
        )

    log.info(
        f"Resultado: {len(noticias_finales)} aceptadas | "
        f"{rechazadas_fecha} fuera de rango de fecha | "
        f"{rechazadas_tras_og_title} duplicadas tras leer el artículo"
    )

    # Priorizar artículos fechados recientemente; los sin fecha quedan al final.
    noticias_finales.sort(
        key=lambda n: n.get("fecha") or datetime.min.replace(tzinfo=TZ),
        reverse=True
    )
    return noticias_finales


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
        for i, n in enumerate(noticias, 1):
            marca = "ENVIARÍA" if i <= MAX_NOTICIAS_POR_CORRIDA else "excede límite"
            log.info(
                f"{i:02d}. [{marca}] {n.get('fecha')} | {n['fuente']} | "
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

    for noticia in noticias_a_enviar:
        if total_enviadas >= MAX_NOTICIAS_POR_CORRIDA:
            break

        enviado = enviar_noticia(
            noticia
        )

        if enviado:
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
