#!/usr/bin/env python3
"""
Blog MYL Explorer - Servidor Flask
Navegacion por Año > Mes > todas las imágenes del mes
"""

import os, io, re, ssl, threading, gzip, zipfile, urllib.request, urllib.parse, urllib.error, json, time, html as html_module, calendar
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, request, send_file, Response, stream_with_context
from bs4 import BeautifulSoup
from blog_catalog import BlogCatalog

ssl._create_default_https_context = ssl._create_unverified_context

app = Flask(__name__)
BASE_URL = "https://blog.myl.cl"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'es-CL,es;q=0.9',
}

cache_months = None
cache_posts  = {}
cache_images = {}

CASA_URL = "https://casamyl.cl"
cache_casa_collections = None
cache_casa_products    = {}

MESES_ES = {
    '01':'Enero','02':'Febrero','03':'Marzo','04':'Abril',
    '05':'Mayo','06':'Junio','07':'Julio','08':'Agosto',
    '09':'Septiembre','10':'Octubre','11':'Noviembre','12':'Diciembre'
}

WP_API = BASE_URL + "/wp-json/wp/v2"

def fetch(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  ERROR {url}: {e}")
        return None

def fetch_json(url):
    """Fetch JSON from the WP REST API; returns (data, total_pages)."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            pages = int(r.headers.get('X-WP-TotalPages', 1))
            return json.loads(r.read()), pages
    except Exception as e:
        print(f"  ERROR API {url}: {e}")
        return None, 0

# Modo público (deploy en Render): sin Facebook, sin datos locales del CDN, sin
# escaneos pesados contra el blog y sin escribir a disco. Lo activa MYL_PUBLICO=1.
PUBLICO = os.environ.get('MYL_PUBLICO') == '1'
_BLOQUEADAS_PUBLICO = ('/api/fb', '/api/cdnmap', '/api/mazos/save', '/api/blog/enumerate')

# Hosts de los que la app baja imágenes (proxy y ZIPs): sin esto, abierta al
# público serviría de proxy para cualquier URL.
_HOSTS_IMG = {'cdn.shopify.com', 'dojiw2m9tvv09.cloudfront.net', 'cdnx.jumpseller.com',
              'cdn.mazos.cl', 'codicetcg.b-cdn.net', 'static.dotgg.gg', 'cmsassets.rgpub.io'}

def _host_ok(url):
    u = urlparse(url or '')
    host = (u.hostname or '').lower()
    if u.scheme not in ('http', 'https') or not host:
        return False
    if host == 'myl.cl' or host.endswith('.myl.cl') or host in _HOSTS_IMG:
        return True
    return not PUBLICO and host.endswith('.fbcdn.net')

# Catálogo local del blog (solo URLs): las vistas del blog leen de aquí y se
# sincroniza solo en segundo plano. Mientras no exista, se consulta en vivo.
catalog = BlogCatalog(fetch_json, WP_API, Path(__file__).with_name('blog_catalog.json'))
if os.environ.get('MYL_SYNC', '1') == '1':
    catalog.start()      # sync al arrancar y luego cada 10 min (también bajo gunicorn)

def fetch_shopify(url):
    """Fetch JSON from Shopify's public storefront API."""
    shopify_headers = {**HEADERS, 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
    try:
        req = urllib.request.Request(url, headers=shopify_headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ERROR SHOPIFY {url}: {e}")
        return None

def shopify_thumb(src, width=400):
    """Generate a resized Shopify CDN URL (e.g. image_400x.jpg)."""
    m = re.match(r'^(https://[^?]+?)(\.[^.?/]+)(\?.*)?$', src)
    if m:
        return f"{m.group(1)}_{width}x{m.group(2)}{m.group(3) or ''}"
    return src

# ── CASAMYL: COLECCIONES ──────────────────────────────────────────────────────

def get_casa_collections():
    global cache_casa_collections
    if cache_casa_collections is not None:
        return cache_casa_collections
    print("Cargando colecciones CasaMYL...")
    data = fetch_shopify(f"{CASA_URL}/collections.json?limit=250")
    if not data:
        return []
    colls = []
    for c in data.get('collections', []):
        colls.append({
            'id':     c['id'],
            'handle': c['handle'],
            'title':  c['title'],
        })
    cache_casa_collections = colls
    print(f"  {len(colls)} colecciones")
    return colls

# ── CASAMYL: PRODUCTOS + IMÁGENES DE UNA COLECCIÓN ───────────────────────────

def get_casa_products(handle):
    if handle in cache_casa_products:
        return cache_casa_products[handle]
    print(f"  Cargando productos de colección '{handle}'...")
    all_products = []
    page = 1
    while True:
        data = fetch_shopify(
            f"{CASA_URL}/collections/{handle}/products.json?limit=250&page={page}"
        )
        if not data:
            break
        products = data.get('products', [])
        if not products:
            break
        for p in products:
            images = []
            for img in p.get('images', []):
                src = img.get('src', '')
                if not src:
                    continue
                images.append({
                    'url':      src,
                    'thumb':    shopify_thumb(src, 400),
                    'alt':      p['title'][:60],
                    'filename': os.path.basename(urlparse(src.split('?')[0]).path),
                    'post_url': f"{CASA_URL}/products/{p['handle']}",
                })
            all_products.append({
                'title':        p['title'],
                'handle':       p['handle'],
                'url':          f"{CASA_URL}/products/{p['handle']}",
                'product_type': p.get('product_type', ''),
                'images':       images,
            })
        page += 1
        if page > 10:
            break
    cache_casa_products[handle] = all_products
    print(f"  Colección '{handle}': {len(all_products)} productos")
    return all_products

# ── GORILATCG (tienda Bsale) ──────────────────────────────────────────────────
# Cada pagina /collection/<slug> trae sus productos como JSON embebido
# (window.INIT.collections.push). El campo 'notice' es la sub-coleccion real
# (ej. 'Shogun 5 "Onna-Musha"'); sin notice se deduce del sufijo del titulo.
# Las imagenes del CDN vienen guardadas con Content-Encoding gzip.

GORILA_URL    = "https://www.gorilatcg.cl"
GORILA_CDN    = "dojiw2m9tvv09.cloudfront.net"
# Solo MYL: fuera otros juegos y accesorios
GORILA_SKIP   = re.compile(r'pokemon|one-piece|magic|elite-trainer|protector|playmat'
                           r'|deckbox|dados|carpeta', re.I)
GORILA_NOCLAS = 'Sin clasificar'
cache_gorila_collections = None
cache_gorila = {}

def _gorila_fetch(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  ERROR GORILA {url}: {e}")
        return None

def _gorila_page(slug, page):
    """(items, n_paginas) de una pagina de coleccion (Bsale sirve max 100/pag)."""
    html = _gorila_fetch(f"{GORILA_URL}/collection/{slug}?limit=100&page={page}"
                         f"&with_stock=0&order=id&way=ASC")
    if not html:
        return [], 0
    m = re.search(r'window\.INIT\.collections\.push\(\s*(\{.*?\})\s*\);?\s*</script>', html, re.S)
    items = json.loads(m.group(1)).get('items', []) if m else []
    pages = max([int(p) for p in re.findall(r'[?&;]page=(\d+)', html)] + [1])
    return items, pages

def _gorila_group(title, notice):
    notice = ' '.join((notice or '').split())
    if notice:
        return notice
    # 'Alcis (LPE4 - Real)' -> 'LPE4 · Real' ; 'Fenix - Arte Shogun' -> 'Arte Shogun'
    m = re.search(r'\(([^()]+?)\s+-\s+([^()]+)\)\s*$', title)
    if m:
        return f"{m.group(1).strip()} · {m.group(2).strip()}"
    m = re.search(r'\s-\s+(.+)$', title)
    if m:
        suf = re.sub(r'\(\s*carta\s*x\s*\d+\s*copias?\s*\)', '', m.group(1), flags=re.I)
        suf = ' '.join(suf.split())
        if suf:
            return suf
    return GORILA_NOCLAS

def _gorila_img(src):
    src = (src or '').split('?')[0]
    base, name = src.rsplit('/', 1)
    return src, f"{base}/M_{name}"      # M_ = miniatura 400 px del propio CDN

def get_gorila_collections():
    global cache_gorila_collections
    if cache_gorila_collections is not None:
        return cache_gorila_collections
    html = _gorila_fetch(GORILA_URL + '/')
    if not html:
        return []
    soup, seen, colls = BeautifulSoup(html, 'html.parser'), set(), []
    for a in soup.select('a[href*="/collection/"]'):
        m = re.search(r'/collection/([a-z0-9-]+)$', a.get('href', ''))
        if not m or m.group(1) in seen or GORILA_SKIP.search(m.group(1)):
            continue
        seen.add(m.group(1))
        title = ' '.join(a.get_text(' ').split())
        if not title or title.lower() in ('ver todo', m.group(1)):
            title = m.group(1).replace('-', ' ').capitalize()
        colls.append({'slug': m.group(1), 'title': title})
    # Singles primero: es donde suele estar lo que se busca (y acelera /locate)
    colls.sort(key=lambda c: 'single' not in c['slug'])
    cache_gorila_collections = colls
    return colls

def get_gorila_collection(slug):
    """Productos de una coleccion agrupados por sub-coleccion, ordenados."""
    if slug in cache_gorila:
        return cache_gorila[slug]
    items, pages = _gorila_page(slug, 1)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for more, _ in ex.map(lambda p: _gorila_page(slug, p), range(2, pages + 1)):
            items += more
    groups = {}
    for it in items:
        if not it.get('defaultImage'):
            continue
        title = ' '.join((it.get('title') or '').split())
        name  = _gorila_group(title, it.get('notice'))
        g = groups.setdefault(name.casefold(), {'name': name, 'images': [], 'last_id': 0})
        full, thumb = _gorila_img(it['defaultImage'])
        ext = os.path.splitext(full)[1].lower() or '.jpg'
        g['images'].append({
            'url':      full,
            'thumb':    thumb,
            'alt':      title[:60],
            'filename': title,
            'dlname':   re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title) + ext,
            'post_url': GORILA_URL + it.get('link', ''),
        })
        g['last_id'] = max(g['last_id'], it.get('id') or 0)
    out = sorted(groups.values(),
                 key=lambda g: (g['name'] == GORILA_NOCLAS, g['name'].casefold()))
    for g in out:
        g['images'].sort(key=lambda i: i['filename'].casefold())
    cache_gorila[slug] = out
    print(f"  GorilaTCG '{slug}': {len(items)} productos en {len(out)} grupos")
    return out

# ── MYLSERENA (tienda Jumpseller) ─────────────────────────────────────────────
# Bloque (PB/PE/IMP/FX) > edicion (categoria hija) > grupo (sub-categoria).
# Cada pagina de categoria lista sus hijas directas (a.theme-filters__link,
# title="Ir a <nombre>") y sus productos, 30 por pagina, incluidos los de las
# sub-categorias. El original de una imagen es cdnx.../image/<id>/<cualquier
# nombre>: el CDN ignora el nombre y entrega los bytes y el tipo reales.

SERENA_URL    = "https://mylserena.cl"
SERENA_CDN    = "cdnx.jumpseller.com"
SERENA_IMG    = f"https://{SERENA_CDN}/mylserena/image"
SERENA_BLOQUES = [
    ('pb',  'Primer Bloque (PB)',   'singles-pb-1'),
    ('pe',  'Primera Era (PE)',     'primera-era/singles-pe'),
    ('imp', 'Imperio (IMP)',        'singles-imperio'),
    ('fx',  'Furia Extendido (FX)', 'furia'),
]
# Filtros por precio ('seleccion-a-1000-o-menos'): repiten cartas de otras ediciones
SERENA_SKIP   = re.compile(r'(mil|1000)-[oy]-menos')
SERENA_GENERIC = re.compile(r'ver (m[aá]s|todo)', re.I)
SERENA_NOCLAS = 'Sin clasificar'
cache_serena_bloques = None
cache_serena = {}

def _serena_fetch(path):
    try:
        req = urllib.request.Request(f"{SERENA_URL}/{path}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.read().decode('utf-8', errors='ignore')
    except Exception as e:
        print(f"  ERROR SERENA {path}: {e}")
        return None

def _serena_children(soup, path):
    """Categorias hijas directas de 'path', en el orden del sitio: [(path, nombre)].
    Los filtros a veces muestran solo parte de las hijas y el menu omite otras:
    se juntan todos los enlaces de la pagina."""
    out = {}
    for a in soup.select('a[href]'):
        m = re.fullmatch('/' + re.escape(path) + r'/([a-z0-9-]+)', a['href'])
        if not m or SERENA_SKIP.search(m.group(1)):
            continue
        key  = f"{path}/{m.group(1)}"
        name = (re.sub(r'^Ir a\s+', '', a.get('title') or '')
                or ' '.join(a.get_text(' ').split()))
        if SERENA_GENERIC.fullmatch(name):  # 'Ver Más' de las plantillas por secciones
            name = ''
        if not out.get(key):                # el 1er enlace con nombre manda
            out[key] = name
    return [(p, n or p.rsplit('/', 1)[1].replace('-', ' ').capitalize()) for p, n in out.items()]

def _serena_title(soup):
    """Nombre de la categoria segun su propia pagina (h1)."""
    h1 = soup.select_one('h1.theme-section__title') if soup else None
    return ' '.join(h1.get_text(' ').split()) if h1 else ''

def _serena_page(path, page):
    """(productos, n_paginas, soup) de una pagina de categoria."""
    html = _serena_fetch(f"{path}?page={page}")
    if not html:
        return [], 0, None
    soup, items = BeautifulSoup(html, 'html.parser'), []
    for b in soup.select('.product-block-product-feed'):
        a   = b.select_one('a.product-block__name')
        img = b.select_one('img.product-block__image')
        m   = re.search(r'/image/(\d+)/', img.get('src', '')) if img else None
        if not a or not m:
            continue
        items.append({'id': b.get('data-product-id'), 'img': m.group(1),
                      'title': ' '.join(a.get_text(' ').split()), 'link': a.get('href', '')})
    pages = max([int(p) for p in re.findall('/' + re.escape(path) + r'\?page=(\d+)', html)] + [1])
    return items, pages, soup

def _serena_all(path):
    """Todos los productos de una categoria (todas sus paginas) + soup de la 1a."""
    items, pages, soup = _serena_page(path, 1)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for more, _, _ in ex.map(lambda p: _serena_page(path, p), range(2, pages + 1)):
            items += more
    return items, soup

def _serena_group(title):
    """Grupo por el ultimo parentesis del titulo: 'X (LPB 4.0 - Legendaria)' -> 'LPB 4.0 · Legendaria'."""
    m = re.search(r'\(([^()]+)\)\s*$', title)
    if not m:
        return SERENA_NOCLAS
    a, _, b = m.group(1).partition(' - ')
    return f"{a.strip()} · {b.strip()}" if b else a.strip()

def get_serena_bloques():
    """[{key, title, root, editions:[{path, title}]}]; sin hijas, la edicion es el propio bloque."""
    global cache_serena_bloques
    if cache_serena_bloques is not None:
        return cache_serena_bloques
    with ThreadPoolExecutor(max_workers=4) as ex:
        pages = list(ex.map(lambda b: _serena_page(b[2], 1), SERENA_BLOQUES))
    out = []
    for (key, title, root), (_, _, soup) in zip(SERENA_BLOQUES, pages):
        if soup is None:
            return []                       # sin cache: se reintenta al reabrir
        eds = [{'path': p, 'title': n} for p, n in _serena_children(soup, root)]
        out.append({'key': key, 'title': title, 'root': root,
                    'editions': eds or [{'path': root, 'title': title}]})
    cache_serena_bloques = out
    return out

def get_serena_edition(path):
    """Productos de una edicion agrupados por sub-categoria (la mas profunda manda)."""
    if path in cache_serena:
        return cache_serena[path]
    items, soup = _serena_all(path)
    if soup is None:
        return []
    subs = _serena_children(soup, path)
    with ThreadPoolExecutor(max_workers=8) as ex:
        lvl1 = list(ex.map(lambda s: _serena_all(s[0]), subs))
        subs = [(p, _serena_title(s1) or n) for (p, n), (_, s1) in zip(subs, lvl1)]
        subs2 = []                          # nietas, ej. kvm-titanes/kit-de-batalla/furia-estelar
        for (p1, n1), (_, s1) in zip(subs, lvl1):
            if s1:
                subs2 += [(p2, n2) for p2, n2 in _serena_children(s1, p1)]
        lvl2 = list(ex.map(lambda s: _serena_all(s[0]), subs2))
        n1_of = dict(subs)
        subs2 = [(p, f"{n1_of[p.rsplit('/', 1)[0]]} › {_serena_title(s2) or n}")
                 for (p, n), (_, s2) in zip(subs2, lvl2)]
    order, group_of = {}, {}
    for (_, name), (its, _) in list(zip(subs, lvl1)) + list(zip(subs2, lvl2)):
        order.setdefault(name, len(order))
        for it in its:
            group_of[it['id']] = name
    groups, seen = {}, set()
    for it in items + [it for its, _ in lvl1 + lvl2 for it in its]:
        if it['id'] in seen:
            continue
        seen.add(it['id'])
        name = group_of.get(it['id']) or _serena_group(it['title'])
        g = groups.setdefault(name, {'name': name, 'images': []})
        slug = it['link'].strip('/')
        g['images'].append({
            'url':      f"{SERENA_IMG}/{it['img']}/{slug}",
            'thumb':    f"{SERENA_IMG}/{it['img']}/resize/306/407",
            'alt':      it['title'][:60],
            'filename': it['title'],
            # sin extension: el ZIP la pone segun los bytes (png/webp/jpg)
            'dlname':   re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', it['title']),
            'post_url': SERENA_URL + it['link'],
        })
    out = sorted(groups.values(), key=lambda g: (g['name'] not in order, g['name'] == SERENA_NOCLAS,
                                                 order.get(g['name'], 0), g['name'].casefold()))
    for g in out:
        g['images'].sort(key=lambda i: i['filename'].casefold())
    cache_serena[path] = out
    print(f"  MylSerena '{path}': {len(seen)} productos en {len(out)} grupos")
    return out

def _read_body(r):
    """Cuerpo de una respuesta urllib, descomprimido si viene con gzip (CDN Bsale)."""
    data = r.read()
    if r.headers.get('Content-Encoding', '').lower() == 'gzip' or data[:2] == b'\x1f\x8b':
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return data

# ── ARCHIVO: años y meses via WP REST API ─────────────────────────────────────

def get_archive_months():
    global cache_months
    if catalog.ready:
        return [{'year': k[:4], 'month': k[5:7],
                 'label': f"{MESES_ES.get(k[5:7], k[5:7])} {k[:4]}",
                 'url': f"{BASE_URL}/{k[:4]}/{k[5:7]}/", 'key': f"{k[:4]}/{k[5:7]}"}
                for k in catalog.months()]
    if cache_months is not None:
        return cache_months
    print("Cargando archivo via API...")
    seen_keys = set()
    months = []
    page = 1
    while True:
        url = f"{WP_API}/posts?per_page=100&page={page}&_fields=date&orderby=date&order=desc"
        data, total_pages = fetch_json(url)
        if not data:
            break
        for post in data:
            d = post.get('date', '')
            m = re.match(r'(\d{4})-(\d{2})', d)
            if not m:
                continue
            year, month = m.group(1), m.group(2)
            key = f"{year}/{month}"
            if key not in seen_keys:
                seen_keys.add(key)
                months.append({
                    'year': year, 'month': month,
                    'label': f"{MESES_ES.get(month, month)} {year}",
                    'url': f"{BASE_URL}/{year}/{month}/",
                    'key': key,
                })
        if page >= total_pages:
            break
        page += 1

    cache_months = months
    print(f"  {len(months)} meses")
    return months

def get_years():
    months = get_archive_months()
    years = {}
    for m in months:
        years.setdefault(m['year'], []).append(m)
    return [{'year': y, 'months': years[y]} for y in sorted(years, reverse=True)]

# ── POSTS DE UN MES via WP REST API ───────────────────────────────────────────

def get_posts_for_month(year, month):
    key = f"{year}/{month}"
    if key in cache_posts:
        return cache_posts[key]

    # Calcular rango del mes
    last_day = calendar.monthrange(int(year), int(month))[1]
    after  = f"{year}-{month}-01T00:00:00"
    before = f"{year}-{month}-{last_day:02d}T23:59:59"

    all_posts = []
    page = 1
    while True:
        url = (f"{WP_API}/posts?per_page=100&page={page}"
               f"&after={after}&before={before}"
               f"&_fields=id,date,link,title&orderby=date&order=desc")
        data, total_pages = fetch_json(url)
        if not data:
            break
        for post in data:
            link  = post.get('link', '')
            title = post.get('title', {}).get('rendered', '') or link
            title = re.sub(r'<[^>]+>', '', title)  # strip HTML entities tags
            title = html_module.unescape(title)
            date_text = post.get('date', '')[:10]
            all_posts.append({
                'title': title[:80], 'url': link,
                'date': date_text,
                'id': post.get('id', abs(hash(link)) % 1000000),
            })
        if page >= total_pages:
            break
        page += 1

    cache_posts[key] = all_posts
    print(f"  Mes {key}: {len(all_posts)} posts")
    return all_posts

# ── IMÁGENES DE UN POST ───────────────────────────────────────────────────────

def get_images_from_post(post_url):
    if post_url in cache_images:
        return cache_images[post_url]

    html = fetch(post_url)
    if not html:
        cache_images[post_url] = []
        return []

    soup    = BeautifulSoup(html, 'html.parser')
    content = (soup.find('div', class_='entry-content') or
               soup.find('div', class_='post-content') or
               soup.find('article') or soup)

    images, seen = [], set()
    for img in content.find_all('img'):
        src = img.get('src', '') or img.get('data-src', '')
        if not src or 'wp-content/uploads' not in src or src in seen:
            continue
        seen.add(src)
        full_src = re.sub(r'-\d+x\d+(\.\w+)$', r'\1', src)
        alt      = img.get('alt', '').strip() or os.path.basename(src)
        images.append({
            'url': full_src, 'thumb': src,
            'alt': alt[:60],
            'filename': os.path.basename(urlparse(full_src).path),
            'post_url': post_url,
        })

    cache_images[post_url] = images
    return images

# ── RUTAS ─────────────────────────────────────────────────────────────────────

@app.before_request
def _modo_publico():
    if PUBLICO and request.path.startswith(_BLOQUEADAS_PUBLICO):
        return jsonify({'success': False, 'error': 'No disponible en la versión pública'}), 404

@app.route('/api/config')
def api_config():
    return jsonify({'success': True, 'publico': PUBLICO})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/years')
def api_years():
    return jsonify({'success': True, 'data': get_years()})

@app.route('/api/posts')
def api_posts():
    year  = request.args.get('year', '')
    month = request.args.get('month', '')
    if not year or not month:
        return jsonify({'success': False, 'error': 'Falta year/month'}), 400
    posts = get_posts_for_month(year, month)
    return jsonify({'success': True, 'data': posts, 'count': len(posts)})

@app.route('/api/month/images/stream')
def api_month_images_stream():
    """
    SSE: va enviando imágenes a medida que las descubre.
    El frontend recibe eventos progresivos sin esperar al final.
    """
    year  = request.args.get('year', '')
    month = request.args.get('month', '')
    if not year or not month:
        return jsonify({'error': 'Falta year/month'}), 400

    if catalog.ready:
        return _sse(_month_from_catalog(year, f"{int(month):02d}"))

    def generate():
        posts = get_posts_for_month(year, month)
        total = len(posts)

        yield f"data: {json.dumps({'type':'total','total':total})}\n\n"

        # Usamos threads para ir más rápido
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(get_images_from_post, p['url']): p for p in posts}
            done = 0
            for future in as_completed(futures):
                post  = futures[future]
                done += 1
                try:
                    images = future.result()
                except Exception:
                    images = []

                payload = {
                    'type':       'post_done',
                    'post_title': post['title'],
                    'post_url':   post['url'],
                    'post_date':  post['date'],
                    'done':       done,
                    'total':      total,
                    'images':     images,
                }
                yield f"data: {json.dumps(payload)}\n\n"

        yield f"data: {json.dumps({'type':'finished'})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

def _sse(gen):
    return Response(stream_with_context(gen), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

def _ev(obj):
    return f"data: {json.dumps(obj)}\n\n"

def _month_from_catalog(year, month):
    """Mismo protocolo SSE que la versión en vivo, pero local: cada post trae
    además su radar (subidas adjuntas a él que no se usan en ningún post)."""
    posts = catalog.posts_for_month(year, month)
    yield _ev({'type': 'total', 'total': len(posts)})
    for done, (pid, p) in enumerate(posts, 1):
        yield _ev({'type': 'post_done', 'post_title': p['t'][:80] or p['l'],
                   'post_url': p['l'], 'post_date': p['d'][:10],
                   'done': done, 'total': len(posts),
                   'images': catalog.post_images(pid, p),
                   'radar': catalog.post_radar(pid)})
    yield _ev({'type': 'finished'})

@app.route('/api/post/images')
def api_post_images():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'success': False, 'error': 'URL requerida'}), 400
    images = get_images_from_post(url)
    return jsonify({'success': True, 'data': images, 'count': len(images)})

IMG_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')

def _img_ext(body):
    """Extension segun los bytes magicos de la imagen."""
    if body[1:4] == b'PNG':
        return '.png'
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return '.webp'
    if body[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    return '.jpg'

@app.route('/api/download-zip', methods=['POST'])
def api_download_zip():
    data  = request.get_json()
    urls  = data.get('urls', [])
    names = data.get('names') or {}      # url -> nombre de archivo (opcional)
    title = data.get('title', 'myl')
    urls  = [u for u in urls if _host_ok(u)][:500]
    if not urls:
        return jsonify({'success': False, 'error': 'Sin URLs'}), 400

    def _get(img_url):
        try:
            req = urllib.request.Request(img_url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return _read_body(r)
        except Exception as e:
            print(f"  x {img_url}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_get, urls))

    buf, used = io.BytesIO(), set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, (img_url, body) in enumerate(zip(urls, results)):
            if body is None:
                continue
            name = names.get(img_url)
            if name:
                if os.path.splitext(name)[1].lower() not in IMG_EXTS:
                    name += _img_ext(body)           # MylSerena: el nombre llega sin extension
                stem, ext = os.path.splitext(name)
                n = 2
                while name.lower() in used:          # dos productos con el mismo titulo
                    name, n = f"{stem} ({n}){ext}", n + 1
            else:
                name = f"{i:02d}_{os.path.basename(urlparse(img_url).path)}"
            used.add(name.lower())
            zf.writestr(name, body)

    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"{_zip_safe(title)}.zip")

def _zip_safe(name):
    """Nombre de archivo legible (con espacios/acentos) valido en Windows."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name or '').strip(' .')
    return name[:120] or 'myl'

@app.route('/api/casa/collections')
def api_casa_collections():
    return jsonify({'success': True, 'data': get_casa_collections()})

@app.route('/api/casa/collection/images/stream')
def api_casa_collection_images_stream():
    handle = request.args.get('collection', '')
    if not handle:
        return jsonify({'error': 'Falta collection'}), 400

    def generate():
        products = get_casa_products(handle)
        total = len(products)
        yield f"data: {json.dumps({'type':'total','total':total})}\n\n"

        for i, product in enumerate(products, 1):
            payload = {
                'type':       'post_done',
                'post_title': product['title'],
                'post_url':   product['url'],
                'post_date':  product.get('product_type', ''),
                'done':       i,
                'total':      total,
                'images':     product['images'],
            }
            yield f"data: {json.dumps(payload)}\n\n"

        yield f"data: {json.dumps({'type':'finished'})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/api/proxy-image')
def proxy_image():
    img_url = request.args.get('url', '')
    if not _host_ok(img_url):
        return '', 400
    try:
        req = urllib.request.Request(img_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            return send_file(io.BytesIO(_read_body(r)), mimetype=r.headers.get('Content-Type', 'image/jpeg'))
    except:
        return '', 500

@app.route('/api/gorila/collections')
def api_gorila_collections():
    return jsonify({'success': True, 'data': get_gorila_collections()})

@app.route('/api/gorila/collection')
def api_gorila_collection():
    slug = request.args.get('slug', '')
    if not re.fullmatch(r'[a-z0-9-]+', slug):
        return jsonify({'success': False, 'error': 'slug invalido'}), 400
    groups = get_gorila_collection(slug)
    return jsonify({'success': True, 'data': groups})

@app.route('/api/gorila/locate')
def api_gorila_locate():
    """URL de producto -> (coleccion, grupo). Recorre las colecciones hasta hallarlo."""
    m = re.search(r'gorilatcg\.cl(/product/[^?#\s]+)', request.args.get('url', ''))
    if not m:
        return jsonify({'success': False, 'error': 'No es una URL de producto de gorilatcg.cl'}), 400
    target = GORILA_URL + m.group(1).rstrip('/')
    for c in get_gorila_collections():
        for g in get_gorila_collection(c['slug']):
            if any(i['post_url'] == target for i in g['images']):
                return jsonify({'success': True, 'slug': c['slug'], 'group': g['name']})
    return jsonify({'success': False, 'error': 'Producto no encontrado en ninguna colección'}), 404

@app.route('/api/serena/bloques')
def api_serena_bloques():
    return jsonify({'success': True, 'data': get_serena_bloques()})

def _serena_valid_path(path):
    return (re.fullmatch(r'[a-z0-9-]+(/[a-z0-9-]+)*', path or '')
            and any(path == r or path.startswith(r + '/') for _, _, r in SERENA_BLOQUES))

@app.route('/api/serena/edition')
def api_serena_edition():
    path = request.args.get('path', '')
    if not _serena_valid_path(path):
        return jsonify({'success': False, 'error': 'path invalido'}), 400
    return jsonify({'success': True, 'data': get_serena_edition(path)})

@app.route('/api/serena/locate')
def api_serena_locate():
    """URL de producto -> (bloque, edicion, grupo), por el breadcrumb de la ficha."""
    m = re.search(r'mylserena\.cl/([a-z0-9-]+)/?(?:[?#]|$)', request.args.get('url', '').strip())
    if not m:
        return jsonify({'success': False, 'error': 'No es una URL de producto de mylserena.cl'}), 400
    html = _serena_fetch(m.group(1))
    if not html:
        return jsonify({'success': False, 'error': 'No se pudo leer el producto'}), 502
    crumbs = [a.get('href', '').strip('/') for a in
              BeautifulSoup(html, 'html.parser').select('a.theme-breadcrumbs__link')]
    target = f"{SERENA_URL}/{m.group(1)}"
    for b in get_serena_bloques():
        for ed in b['editions']:
            if not any(c == ed['path'] or c.startswith(ed['path'] + '/') for c in crumbs):
                continue
            for g in get_serena_edition(ed['path']):
                if any(i['post_url'] == target for i in g['images']):
                    return jsonify({'success': True, 'bloque': b['key'],
                                    'path': ed['path'], 'group': g['name']})
    return jsonify({'success': False, 'error': 'Producto fuera de PB/PE/IMP/FX o no encontrado'}), 404

# ── CASAMYL: MAPA COMPLETO DEL CDN ────────────────────────────────────────────
# El mapa lo genera ../casamyl_cdn_mapper.py (sitemap + Wayback) y descarga los
# archivos a ../casamyl_cdn/. Aqui solo se navega: resolucion, categorias,
# miniaturas locales y una sonda para nombres recien subidos que el mapper no ve.

CDNMAP_DIR    = Path(__file__).resolve().parent.parent / 'casamyl_cdn'
CDNMAP_BASE   = "https://cdn.shopify.com/s/files/1/0103/3601/0303/"
CDNMAP_EXTRAS = CDNMAP_DIR / 'extras_cdn.json'   # hallazgos de la sonda
CDNMAP_DIMS   = CDNMAP_DIR / '.dims.json'        # cache de resoluciones
CDNMAP_THUMBS = CDNMAP_DIR / '.thumbs'

CDNMAP_CATS = [
    ('premier',    'Premier / Torneos', r'premier|torneo|campeon|nacional|clasificat|liga|crpb|regional'),
    ('protector',  'Protectores',       r'protector|sleeve|funda'),
    ('playmat',    'Playmats',          r'playmat|tapete'),
    ('producto',   'Mazos / Kits / Sobres', r'mazo|kit|sobre|display|caja|booster|carpeta|binder|deck'),
    ('promo',      'Banners / Promos',  r'banner|carrusel|promo|preview|prevent|whatsapp|grafica|afiche'),
    ('mockup',     'Mockups / Render',  r'mockup|render'),
]
CDNMAP_PROBE_EXTS = ['.png', '.jpg', '.jpeg', '.webp', '.PNG', '.JPG']


def _cdnmap_local(path):
    """Ruta local de un path del mapa, sin permitir salir de CDNMAP_DIR."""
    p = (CDNMAP_DIR / path).resolve()
    if CDNMAP_DIR.resolve() not in p.parents:
        return None
    return p


def _cdnmap_read_json(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _cdnmap_dims(paths):
    """Resolucion (w, h) de cada archivo local, cacheada en disco por path+tamano."""
    from PIL import Image
    cache = _cdnmap_read_json(CDNMAP_DIMS, {})

    def measure(path):
        local = _cdnmap_local(path)
        if not local or not local.exists():
            return path, None, None
        key = f"{path}|{local.stat().st_size}"
        if key in cache:
            return path, key, cache[key]
        try:
            with Image.open(local) as im:
                return path, key, [im.width, im.height]
        except Exception:
            return path, None, None

    out, changed = {}, False
    with ThreadPoolExecutor(16) as ex:
        for path, key, wh in ex.map(measure, paths):
            if not wh:
                continue
            if key not in cache:
                cache[key] = wh
                changed = True
            out[path] = tuple(wh)
    if changed:
        CDNMAP_DIMS.write_text(json.dumps(cache), encoding='utf-8')
    return out


@app.route('/api/cdnmap')
def api_cdnmap():
    mapa = _cdnmap_read_json(CDNMAP_DIR / 'mapa_cdn.json', None)
    if mapa is None:
        return jsonify({'success': False,
                        'error': 'No hay mapa: ejecuta python casamyl_cdn_mapper.py'}), 404
    items = {v['path']: dict(v) for v in mapa.get('vivos', [])}
    for e in _cdnmap_read_json(CDNMAP_EXTRAS, []):
        items.setdefault(e['path'], dict(e))

    dims = _cdnmap_dims(list(items))
    cat_res = [(k, re.compile(rx, re.I)) for k, _, rx in CDNMAP_CATS]
    data = []
    for path, it in items.items():
        name = path.split('/', 1)[-1]
        w, h = dims.get(path, (0, 0))
        cats = [k for k, rx in cat_res if rx.search(name)]
        if w and h and w < h and 0.66 <= w / h <= 0.76:
            cats.insert(0, 'carta')
        if 'sonda' in it.get('fuente', []):
            cats.insert(0, 'sonda')
        data.append({
            'path': path, 'name': name, 'folder': path.split('/', 1)[0],
            'url': CDNMAP_BASE + path, 'bytes': it.get('bytes') or 0,
            'w': w, 'h': h, 'cats': cats or ['otros'], 'fuente': it.get('fuente', []),
            'local': path in dims,
        })
    cats = [{'key': 'carta', 'label': 'Formato carta'}, {'key': 'sonda', 'label': 'Sonda (nuevos)'}]
    cats += [{'key': k, 'label': lbl} for k, lbl, _ in CDNMAP_CATS]
    cats.append({'key': 'otros', 'label': 'Otros'})
    return jsonify({'success': True, 'data': data, 'cats': cats})


@app.route('/api/cdnmap/thumb')
def api_cdnmap_thumb():
    path = request.args.get('p', '')
    local = _cdnmap_local(path)
    if not local:
        return '', 400
    if not local.exists():
        # Sin copia local: miniatura redimensionada por el propio CDN de Shopify
        return Response(status=302, headers={'Location': f"{CDNMAP_BASE}{urllib.parse.quote(path)}?width=320"})
    import hashlib
    from PIL import Image
    thumb = CDNMAP_THUMBS / (hashlib.sha1(f"{path}|{local.stat().st_size}".encode()).hexdigest() + '.webp')
    if not thumb.exists():
        CDNMAP_THUMBS.mkdir(exist_ok=True)
        with Image.open(local) as im:
            im.thumbnail((320, 320))
            if im.mode not in ('RGB', 'RGBA'):
                im = im.convert('RGBA')
            im.save(thumb, 'WEBP', quality=80)
    return send_file(thumb, mimetype='image/webp', max_age=86400)


@app.route('/api/cdnmap/file')
def api_cdnmap_file():
    path = request.args.get('p', '')
    local = _cdnmap_local(path)
    if not local:
        return '', 400
    if local.exists():
        return send_file(local)
    return Response(status=302, headers={'Location': CDNMAP_BASE + urllib.parse.quote(path)})


@app.route('/api/cdnmap/zip', methods=['POST'])
def api_cdnmap_zip():
    paths = (request.get_json() or {}).get('paths', [])
    if not paths:
        return jsonify({'success': False, 'error': 'Sin archivos'}), 400
    buf = io.BytesIO()
    # Las imagenes ya vienen comprimidas: ZIP_STORED evita gastar CPU en nada
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:
        for path in paths:
            local = _cdnmap_local(path)
            if not local:
                continue
            name = path.split('/', 1)[-1]
            try:
                if local.exists():
                    zf.write(local, name)
                else:
                    req = urllib.request.Request(CDNMAP_BASE + urllib.parse.quote(path), headers=HEADERS)
                    with urllib.request.urlopen(req, timeout=30) as r:
                        zf.writestr(name, r.read())
            except Exception as e:
                print(f"  x {path}: {e}")
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"casamyl_cdn_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")


@app.route('/api/cdnmap/probe', methods=['POST'])
def api_cdnmap_probe():
    """Prueba variantes de un nombre (NOMBRE, NOMBRE1..20, _1, -1 x extensiones)
    en files/ del CDN; lo que exista se descarga y queda en extras_cdn.json."""
    base = ((request.get_json() or {}).get('name') or '').strip()
    base = re.sub(r'\.(png|jpe?g|webp)$', '', base, flags=re.I)
    if not re.fullmatch(r'[A-Za-z0-9_\-]{2,80}', base):
        return jsonify({'success': False, 'error': 'Nombre inválido (solo letras, números, _ y -)'}), 400

    suffixes = [''] + [f"{s}{n}" for n in range(1, 21) for s in ('', '_', '-')]
    candidates = [f"files/{base}{s}{e}" for s in suffixes for e in CDNMAP_PROBE_EXTS]

    def head(path):
        try:
            req = urllib.request.Request(CDNMAP_BASE + urllib.parse.quote(path), headers=HEADERS, method='HEAD')
            with urllib.request.urlopen(req, timeout=10) as r:
                return path, int(r.headers.get('Content-Length') or 0)
        except Exception:
            return path, None

    with ThreadPoolExecutor(32) as ex:
        hits = [(p, s) for p, s in ex.map(head, candidates) if s is not None]

    extras = {e['path']: e for e in _cdnmap_read_json(CDNMAP_EXTRAS, [])}
    for path, size in hits:
        local = _cdnmap_local(path)
        if local and not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(CDNMAP_BASE + urllib.parse.quote(path), headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                local.write_bytes(r.read())
        extras[path] = {'path': path, 'bytes': size, 'url': CDNMAP_BASE + path, 'fuente': ['sonda']}
    CDNMAP_DIR.mkdir(exist_ok=True)
    CDNMAP_EXTRAS.write_text(json.dumps(list(extras.values()), indent=2, ensure_ascii=False), encoding='utf-8')
    return jsonify({'success': True, 'tested': len(candidates), 'hits': [p for p, _ in hits]})

# ── RADAR: medios de borradores ───────────────────────────────────────────────

def _get_all_published_ids():
    """
    Devuelve un set con todos los post IDs publicados.
    También incluye IDs de páginas para evitar falsos positivos.
    """
    ids = set()
    for ctype in ('posts', 'pages'):
        page = 1
        while True:
            data, total_pages = fetch_json(
                f"{WP_API}/{ctype}?per_page=100&page={page}&_fields=id"
            )
            if not data:
                break
            for p in data:
                ids.add(p['id'])
            if page >= total_pages:
                break
            page += 1
    return ids

@app.route('/api/blog/radar/stream')
def api_blog_radar_stream():
    """
    SSE — escanea TODA la biblioteca de medios y cruza con posts publicados.
    Cualquier imagen cuyo post padre NO esté publicado = borrador / pre-lanzamiento.
    """
    if catalog.ready:
        return _sse(_radar_from_catalog())

    def generate():
        # ── Fase 1: todos los IDs publicados ─────────────────────────────
        yield f"data: {json.dumps({'type':'radar_info','phase':'Cargando posts publicados...'})}\n\n"
        published_ids = _get_all_published_ids()
        yield f"data: {json.dumps({'type':'radar_info','phase':f'{len(published_ids)} posts/páginas conocidos. Escaneando biblioteca de medios...'})}\n\n"

        # ── Fase 2: paginar TODA la biblioteca de medios ──────────────────
        # Primer pase: obtener total de páginas
        first_batch, total_pages = fetch_json(
            f"{WP_API}/media?per_page=100&page=1&media_type=image"
            f"&_fields=id,date,source_url,title,post,media_details"
        )
        total_pages = total_pages or 1

        orphans      = []   # post == 0
        draft_media  = {}   # post_id → [media, ...]   (post no publicado)

        def _process_batch(batch):
            for m in (batch or []):
                src      = m.get('source_url', '')
                post_id  = m.get('post', 0)
                img = {
                    'url':      src,
                    'thumb':    src,
                    'alt':      (m.get('title') or {}).get('rendered', '') or src.split('/')[-1],
                    'filename': src.split('/')[-1],
                    'post_url': f"https://blog.myl.cl/?p={post_id}" if post_id else src,
                    'media_id': m['id'],
                    'date':     m.get('date', '')[:10],
                }
                if post_id == 0:
                    orphans.append(img)
                elif post_id not in published_ids:
                    draft_media.setdefault(post_id, []).append(img)

        _process_batch(first_batch)

        # Páginas restantes en paralelo
        def _fetch_page(p):
            data, _ = fetch_json(
                f"{WP_API}/media?per_page=100&page={p}&media_type=image"
                f"&_fields=id,date,source_url,title,post,media_details"
            )
            return data

        pages_left = list(range(2, total_pages + 1))
        done_pages = 1

        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(_fetch_page, p): p for p in pages_left}
            for future in as_completed(futures):
                _process_batch(future.result())
                done_pages += 1
                pct = int(done_pages / total_pages * 100)
                yield f"data: {json.dumps({'type':'radar_progress','done':done_pages,'total':total_pages,'pct':pct})}\n\n"

        # ── Fase 3: emitir resultados ─────────────────────────────────────
        total_batches = (1 if orphans else 0) + len(draft_media)
        batch_n = 0

        if orphans:
            batch_n += 1
            yield f"data: {json.dumps({'type':'post_done','post_title':f'Huérfanas — sin post ({len(orphans)})','post_url':'#','post_date':'','done':batch_n,'total':total_batches,'images':orphans})}\n\n"

        for post_id, imgs in sorted(draft_media.items(), reverse=True):
            batch_n += 1
            date_label = imgs[0]['date'] if imgs else ''
            yield f"data: {json.dumps({'type':'post_done','post_title':f'Borrador post #{post_id} ({len(imgs)} imgs)','post_url':f'https://blog.myl.cl/?p={post_id}','post_date':date_label,'done':batch_n,'total':total_batches,'images':imgs})}\n\n"

        yield f"data: {json.dumps({'type':'finished','radar_total':len(orphans)+sum(len(v) for v in draft_media.values()),'draft_posts':len(draft_media)})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _radar_from_catalog():
    orphans, drafts = catalog.radar()
    total_batches = (1 if orphans else 0) + len(drafts)
    n = 0
    if orphans:
        n += 1
        yield _ev({'type': 'post_done', 'post_title': f'Huérfanas — sin post ({len(orphans)})',
                   'post_url': '#', 'post_date': '', 'done': n, 'total': total_batches,
                   'images': sorted(orphans, key=lambda i: i['date'], reverse=True)})
    for post_id, imgs in sorted(drafts.items(), reverse=True):
        n += 1
        yield _ev({'type': 'post_done', 'post_title': f'Borrador post #{post_id} ({len(imgs)} imgs)',
                   'post_url': f'https://blog.myl.cl/?p={post_id}', 'post_date': imgs[0]['date'],
                   'done': n, 'total': total_batches, 'images': imgs})
    yield _ev({'type': 'finished', 'radar_total': len(orphans) + sum(map(len, drafts.values())),
               'draft_posts': len(drafts)})

def _media_month_from_catalog(year, month):
    """Biblioteca del mes por día; lo subido que no se usa en ningún post va al radar del día."""
    por_dia = {}
    for mid, m in sorted(catalog.media_for_month(year, month), key=lambda x: x[1]['d']):
        usadas, radar = por_dia.setdefault(m['d'][:10], ([], []))
        (usadas if catalog.is_used(m) else radar).append(catalog.img(mid, m))
    total = sum(len(u) + len(r) for u, r in por_dia.values())
    yield _ev({'type': 'total', 'total': total, 'dias': len(por_dia)})
    for dia in sorted(por_dia):
        usadas, radar = por_dia[dia]
        dt = datetime.strptime(dia, '%Y-%m-%d')
        yield _ev({'type': 'post_done',
                   'post_title': f"{dt.day:02d} {MESES_ES.get(f'{dt.month:02d}', '')} {dt.year}",
                   'post_url': '#', 'post_date': f"{len(usadas) + len(radar)} img",
                   'images': usadas, 'radar': radar})
    yield _ev({'type': 'finished', 'total': total, 'dias': len(por_dia)})

@app.route('/api/blog/catalog/status')
def api_blog_catalog_status():
    return jsonify({'success': True, **catalog.status()})

@app.route('/api/blog/catalog/sync', methods=['POST'])
def api_blog_catalog_sync():
    """Sync en segundo plano; full=1 rehace todo (detecta lo borrado en el blog)."""
    full = request.args.get('full') == '1'
    if full and PUBLICO:
        return jsonify({'success': False, 'error': 'Resync completa deshabilitada en la versión pública'}), 403
    threading.Thread(target=catalog.sync, kwargs={'full': full}, daemon=True).start()
    return jsonify({'success': True, 'full': full})

# ── BIBLIOTECA DE MEDIOS (todas las imágenes subidas en un mes) ───────────────

@app.route('/api/blog/media-month/stream')
def api_blog_media_month_stream():
    """
    SSE — TODAS las imágenes de la biblioteca de medios de WordPress subidas en un
    mes, agrupadas por día. Incluye imágenes que NO están enlazadas a ningún post
    (p. ej. cartas sueltas tipo Azi-Demoniaco.png). Se basa en /wp-json/wp/v2/media,
    el índice autoritativo de todo lo subido (no en el HTML de los posts).
    """
    year  = request.args.get('year', '')
    month = request.args.get('month', '')
    if not re.match(r'^\d{4}$', year) or not re.match(r'^\d{1,2}$', month):
        return jsonify({'error': 'Falta year/month válidos'}), 400
    month = f"{int(month):02d}"
    last_day = calendar.monthrange(int(year), int(month))[1]
    after  = f"{year}-{month}-01T00:00:00"
    before = f"{year}-{month}-{last_day:02d}T23:59:59"

    if catalog.ready:
        return _sse(_media_month_from_catalog(year, month))

    def generate():
        # Paginar toda la biblioteca de medios del mes
        all_media, page, total_pages = [], 1, 1
        while True:
            url = (f"{WP_API}/media?per_page=100&page={page}&media_type=image"
                   f"&after={after}&before={before}&orderby=date&order=asc"
                   f"&_fields=id,date,source_url,title,media_details")
            data, total_pages = fetch_json(url)
            if not data:
                break
            all_media.extend(data)
            yield f"data: {json.dumps({'type':'loading','page':page,'pages':total_pages})}\n\n"
            if page >= (total_pages or 1):
                break
            page += 1

        # Agrupar por día
        por_dia = {}
        for m in all_media:
            src = m.get('source_url', '')
            if not src:
                continue
            dia   = m.get('date', '')[:10]   # YYYY-MM-DD
            sizes = (m.get('media_details') or {}).get('sizes') or {}
            thumb = ((sizes.get('medium') or sizes.get('thumbnail') or {}).get('source_url')) or src
            por_dia.setdefault(dia, []).append({
                'url':      src,
                'thumb':    thumb,
                'alt':      (m.get('title') or {}).get('rendered', '') or src.split('/')[-1],
                'filename': src.split('/')[-1],
                'post_url': src,
                'media_id': m.get('id'),
                'date':     dia,
            })

        total = sum(len(v) for v in por_dia.values())
        yield f"data: {json.dumps({'type':'total','total':total,'dias':len(por_dia)})}\n\n"

        for dia in sorted(por_dia.keys()):
            imgs = por_dia[dia]
            try:
                dt = datetime.strptime(dia, '%Y-%m-%d')
                label = f"{dt.day:02d} {MESES_ES.get(f'{dt.month:02d}', '')} {dt.year}"
            except Exception:
                label = dia
            yield ("data: " + json.dumps({
                'type':       'post_done',
                'post_title': label,
                'post_url':   '#',
                'post_date':  f"{len(imgs)} img",
                'images':     imgs,
            }) + "\n\n")

        yield f"data: {json.dumps({'type':'finished','total':total,'dias':len(por_dia)})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── MAZOS.CL ──────────────────────────────────────────────────────────────────

MAZOS_API  = "https://api.mazos.cl/graphql"
MAZOS_HDRS = {
    "Content-Type": "application/json",
    "Origin": "https://mazos.cl",
    "User-Agent": HEADERS["User-Agent"],
}
MAZOS_OUT   = "mazos_cards"
MYL_ORDER   = ["primer-bloque", "primera-era", "furia-extendido", "imperio", "riftbound"]

# Orden de salida curado (la API no expone fecha de lanzamiento real).
# Editable a mano: blog_myl_explorer/mazos_release_order.json
RELEASE_ORDER_FILE = Path(__file__).with_name("mazos_release_order.json")
_cache_release_order = None

_cache_mazos_cards = None


def _get_release_order(game_slug: str) -> dict:
    """Devuelve {product_slug: {order, date}} para un bloque, segun la tabla curada.

    El JSON ya viene ordenado cronologicamente por 'date' (fecha estimada de
    salida derivada de blog.myl.cl / casamyl.cl), asi que el indice = orden.
    """
    global _cache_release_order
    if _cache_release_order is None:
        try:
            _cache_release_order = json.loads(RELEASE_ORDER_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  mazos release-order: no se pudo cargar ({exc})")
            _cache_release_order = {}
    block = sorted(_cache_release_order.get(game_slug, []),
                   key=lambda it: (it.get("date", "9999"), it["slug"]))
    return {item["slug"]: {"order": i, "date": item.get("date")}
            for i, item in enumerate(block, 1)}


class MazosUnavailable(Exception):
    """La API de mazos.cl no respondio (caida, timeout o error HTTP)."""


class MazosSchemaError(Exception):
    """La API respondio, pero rechazo la query (p. ej. mazos.cl cambio el esquema)."""


def _mazos_gql(query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(MAZOS_API, data=payload, headers=MAZOS_HDRS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        # Un 400 de GraphQL NO es una caida: la API esta viva y rechaza la query.
        try:
            body = json.loads(exc.read())
        except Exception:
            raise MazosUnavailable(f"mazos.cl respondio HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as exc:
        raise MazosUnavailable(
            "La API de mazos.cl no esta disponible ahora mismo "
            "(el servidor no responde). Intenta de nuevo mas tarde."
        ) from exc
    if body.get("errors") and not body.get("data"):
        msg = body["errors"][0].get("message", "error desconocido")
        raise MazosSchemaError(f"mazos.cl rechazo la query (¿cambio su API?): {msg}")
    return body["data"]


def _mazos_encode_url(url: str) -> str:
    """Percent-encode caracteres no-ASCII en el path de una URL CDN."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        p._replace(path=urllib.parse.quote(p.path, safe="/:@!$&'()*+,;="))
    )


_CATALOG_PAGE = 100  # CardCatalog limita a 100 cartas por pagina (pidas lo que pidas)
_CATALOG_QUERY = """query($i: CardCatalogInput!) { CardCatalog(input: $i) {
  total pages cards { id slug name collectorCode imageUrl type cost race frequency variant
    game{ id name slug } edition{ id name slug } } } }"""
_GAMES_QUERY = "{ games{ id name slug editions{ id name slug pathFiles cardCount } } }"

# Snapshot en disco del ultimo catalogo COMPLETO: si mazos.cl cae o cambia su API,
# la app sigue pudiendo explorar (imagenes del CDN aparte).
CATALOG_SNAPSHOT_FILE = Path(__file__).with_name("mazos_catalog_snapshot.json")
_CATALOG_RETRY_SECS   = 300  # si se sirvio el snapshot, reintentar en vivo cada 5 min
_catalog_lock         = __import__("threading").Lock()
_catalog_last_try     = 0.0


class CatalogIncomplete(Exception):
    """La descarga en vivo no trajo el catalogo entero: no se guarda."""


def _fetch_catalog_page(game_slug: str, page: int) -> dict:
    return _mazos_gql(_CATALOG_QUERY, {"i": {"gameId": game_slug, "page": page,
                                             "limit": _CATALOG_PAGE}})["CardCatalog"]


def _download_full_catalog() -> dict:
    """Descarga juegos + todas las cartas MYL. Lanza excepcion si algo falta.
    Desde 2026-10 mazos.cl elimino la query `cards`; se pagina `CardCatalog` por juego."""
    games = [g for g in _mazos_gql(_GAMES_QUERY)["games"] if g["slug"] in MYL_ORDER]
    firsts = {g: _fetch_catalog_page(g, 1) for g in MYL_ORDER}
    jobs = [(g, p) for g, f in firsts.items() for p in range(2, f["pages"] + 1)]
    cards = [c for f in firsts.values() for c in f["cards"]]
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_fetch_catalog_page, g, p) for g, p in jobs]
        for fut in as_completed(futs):
            cards.extend(fut.result()["cards"])  # una pagina caida aborta todo
    unique = list({c["id"]: c for c in cards}.values())
    expected = sum(f["total"] for f in firsts.values())
    if len(unique) != expected:
        raise CatalogIncomplete(f"llegaron {len(unique)} cartas unicas, la API reporta {expected}")
    return {"downloaded_at": datetime.now().isoformat(timespec="seconds"),
            "total": expected, "games": games, "cards": unique}


def _save_catalog_snapshot(cat: dict) -> None:
    tmp = CATALOG_SNAPSHOT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cat, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CATALOG_SNAPSHOT_FILE)  # atomico: nunca queda un snapshot a medias


def _get_mazos_catalog(force_live: bool = False) -> dict:
    """Primero intenta mazos.cl en vivo; si la descarga es completa actualiza el
    snapshot en disco. Si falla, sirve el ultimo snapshot. Cachea en memoria.
    Devuelve {games, cards, downloaded_at, total, source: 'live'|'snapshot'}."""
    global _cache_mazos_cards, _catalog_last_try
    with _catalog_lock:
        cached = _cache_mazos_cards
        stale = cached is not None and cached["source"] == "snapshot" \
            and time.time() - _catalog_last_try > _CATALOG_RETRY_SECS
        if cached is not None and not force_live and not stale:
            return cached
        _catalog_last_try = time.time()
        print("mazos.cl: descargando catálogo completo (CardCatalog paginado)...")
        try:
            cat = _download_full_catalog()
            _save_catalog_snapshot(cat)
            _cache_mazos_cards = {**cat, "source": "live"}
            print(f"  mazos: {cat['total']} cartas descargadas, snapshot actualizado")
        except Exception as exc:
            print(f"  mazos: descarga en vivo falló ({exc}); uso snapshot en disco")
            if cached is not None:
                return cached
            if not CATALOG_SNAPSHOT_FILE.exists():
                raise
            cat = json.loads(CATALOG_SNAPSHOT_FILE.read_text(encoding="utf-8"))
            _cache_mazos_cards = {**cat, "source": "snapshot", "live_error": str(exc)}
            print(f"  mazos: snapshot del {cat['downloaded_at']} ({cat['total']} cartas)")
        return _cache_mazos_cards


def _get_mazos_cards():
    return _get_mazos_catalog()["cards"]


def _mazos_img_ext(url: str) -> str:
    """Extension real de la imagen (el CDN sirve .webp, a veces 'x.webp-<timestamp>')."""
    m = re.search(r'\.(webp|png|jpe?g)(?:-\d+)?$', urllib.parse.urlsplit(url or "").path, re.I)
    return "." + m.group(1).lower() if m else ".png"


_PAGE_SIZE = 20  # mazos.cl devuelve 20 productos por página

def _fetch_products_page(slug: str, page: int) -> list:
    """Trae una página de productos para un bloque dado."""
    data = _mazos_gql(
        f'{{ products(game: "{slug}", page: {page}) '
        f'{{ total products {{ id name slug imageUrl }} }} }}'
    )
    return data["products"]


def _all_products_for_game(slug: str) -> list:
    """Pagina automáticamente y retorna TODOS los productos de un bloque."""
    import math
    first = _fetch_products_page(slug, 1)
    total = first["total"]
    all_prods = list(first["products"])

    if total > _PAGE_SIZE:
        pages = math.ceil(total / _PAGE_SIZE)
        with ThreadPoolExecutor(max_workers=min(pages - 1, 8)) as ex:
            futs = {ex.submit(_fetch_products_page, slug, p): p
                    for p in range(2, pages + 1)}
            for fut in as_completed(futs):
                try:
                    all_prods.extend(fut.result()["products"])
                except Exception as exc:
                    print(f"  mazos page error ({slug} p{futs[fut]}): {exc}")

    # Deduplica por id y ordena por id desc (más nuevo primero)
    seen, unique = set(), []
    for p in all_prods:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)
    unique.sort(key=lambda p: p["id"], reverse=True)
    return unique


@app.route('/api/mazos/products-catalog')
def api_mazos_products_catalog():
    """Retorna bloques MYL con TODOS sus productos físicos (paginado)."""
    try:
        games_raw = _mazos_gql("{ games{ id name slug } }")["games"]
        myl_games = [g for g in games_raw if g["slug"] in MYL_ORDER]
        myl_games.sort(key=lambda g: MYL_ORDER.index(g["slug"]) if g["slug"] in MYL_ORDER else 99)

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_all_products_for_game, g["slug"]): g for g in myl_games}
            prod_map = {}
            for fut in as_completed(futs):
                g = futs[fut]
                try:
                    prod_map[g["slug"]] = fut.result()
                except Exception as exc:
                    print(f"  mazos products error ({g['slug']}): {exc}")
                    prod_map[g["slug"]] = []

        catalog = []
        for g in myl_games:
            order_map = _get_release_order(g["slug"])
            prods = prod_map.get(g["slug"], [])
            # Adjunta orden de salida + fecha estimada; los no listados van al final.
            for p in prods:
                info = order_map.get(p["slug"]) or {}
                p["order"] = info.get("order")
                p["date"]  = info.get("date")
            prods.sort(key=lambda p: (p["order"] is None, p["order"] or 0, p["id"]))
            catalog.append({"id": g["id"], "name": g["name"], "slug": g["slug"],
                            "products": prods})
        return jsonify({"success": True, "data": catalog})
    except (MazosUnavailable, MazosSchemaError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/mazos/product')
def api_mazos_product():
    """Retorna cartas de un producto específico (con cantidad y edición)."""
    prod_id     = request.args.get("id", "")
    type_filter = request.args.get("type", "")
    ed_filter   = request.args.get("edition", "")
    if not prod_id:
        return jsonify({"success": False, "error": "Falta id"}), 400
    try:
        data = _mazos_gql(
            f'{{ product(id: {prod_id}) {{ id name slug '
            f'productCards {{ quantity card {{ id name imageUrl type cost race '
            f'edition{{ id name slug }} }} }} }} }}'
        )
        prod = data.get("product")
        if not prod:
            return jsonify({"success": False, "error": "Producto no encontrado"}), 404

        # Cross-reference edition (carga caché si aún no está disponible)
        all_cards   = _get_mazos_cards()
        cards_by_id = {c["id"]: c for c in all_cards}

        cards = []
        for pc in (prod.get("productCards") or []):
            c = pc.get("card")
            if not c:
                continue
            # Fill edition from cache if product context left it null
            if not c.get("edition") and c["id"] in cards_by_id:
                c["edition"] = cards_by_id[c["id"]].get("edition")
            if type_filter and c.get("type") != type_filter:
                continue
            if ed_filter and (not c.get("edition") or c["edition"]["slug"] != ed_filter):
                continue
            cards.append({**c, "quantity": pc.get("quantity", 1)})

        types = sorted({c.get("type", "") for c in cards if c.get("type")})
        editions_seen, editions = set(), []
        for c in cards:
            ed = c.get("edition")
            if ed and ed["slug"] not in editions_seen:
                editions_seen.add(ed["slug"])
                editions.append(ed)
        editions.sort(key=lambda e: e["name"])

        return jsonify({
            "success": True,
            "product": {"id": prod["id"], "name": prod["name"], "slug": prod["slug"]},
            "data": cards, "count": len(cards),
            "types": types, "editions": editions,
        })
    except (MazosUnavailable, MazosSchemaError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/mazos/catalog')
def api_mazos_catalog():
    """Retorna juegos MYL con ediciones y cantidad de cartas por edición.
    ?refresh=1 fuerza reintentar la descarga en vivo."""
    try:
        cat = _get_mazos_catalog(force_live=request.args.get("refresh") == "1")

        counts = {}
        for c in cat["cards"]:
            if c["edition"]:
                s = c["edition"]["slug"]
                counts[s] = counts.get(s, 0) + 1

        myl = [{**g, "editions": [{**e, "count": counts.get(e["slug"], 0)} for e in g["editions"]]}
               for g in cat["games"] if g["slug"] in MYL_ORDER]
        myl.sort(key=lambda g: MYL_ORDER.index(g["slug"]))

        return jsonify({"success": True, "data": myl, "source": cat["source"],
                        "downloaded_at": cat["downloaded_at"],
                        "live_error": cat.get("live_error")})
    except (MazosUnavailable, MazosSchemaError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/mazos/edition')
def api_mazos_edition():
    """Retorna cartas de una edición con filtros opcionales de ID y tipo."""
    slug        = request.args.get("slug", "")
    id_from     = int(request.args.get("id_from") or 0)
    id_to       = int(request.args.get("id_to")   or 9_999_999)
    type_filter = request.args.get("type", "")
    if not slug:
        return jsonify({"success": False, "error": "Falta slug"}), 400
    try:
        result = [
            c for c in _get_mazos_cards()
            if  c["edition"] and c["edition"]["slug"] == slug
            and id_from <= c["id"] <= id_to
            and (not type_filter or c.get("type") == type_filter)
        ]
        types = sorted({c.get("type", "") for c in result if c.get("type")})
        id_min = min((c["id"] for c in result), default=0)
        id_max = max((c["id"] for c in result), default=0)
        return jsonify({
            "success": True, "data": result,
            "count": len(result), "types": types,
            "id_min": id_min, "id_max": id_max,
        })
    except (MazosUnavailable, MazosSchemaError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/mazos/download-zip', methods=['POST'])
def api_mazos_download_zip():
    """Descarga cartas seleccionadas como ZIP (paralelo, con nombre de carta)."""
    body  = request.get_json()
    cards = body.get("cards", [])
    name  = body.get("name", "mazos")
    if not cards:
        return jsonify({"success": False, "error": "Sin cartas"}), 400

    def _fetch(card):
        url = card.get("imageUrl")
        if not _host_ok(url):
            return card, None
        try:
            req = urllib.request.Request(
                _mazos_encode_url(url),
                headers={"User-Agent": HEADERS["User-Agent"]},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return card, r.read()
        except Exception:
            return card, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_fetch, cards))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for card, data in results:
            if data:
                cname = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', card.get("name", "card"))
                zf.writestr(f"{card.get('id', 0):05d}_{cname}{_mazos_img_ext(card.get('imageUrl'))}", data)

    buf.seek(0)
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name[:30])
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"mazos_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")


@app.route('/api/mazos/save/stream')
def api_mazos_save_stream():
    """SSE — guarda cartas en disco con progreso en tiempo real."""
    ed_slug     = request.args.get("slug", "")
    game_slug   = request.args.get("game", "myl")
    id_from     = int(request.args.get("id_from") or 0)
    id_to       = int(request.args.get("id_to")   or 9_999_999)
    type_filter = request.args.get("type", "")
    if not ed_slug:
        return jsonify({"error": "Falta slug"}), 400

    def generate():
        try:
            subset = [
                c for c in _get_mazos_cards()
                if  c["edition"] and c["edition"]["slug"] == ed_slug
                and id_from <= c["id"] <= id_to
                and (not type_filter or c.get("type") == type_filter)
            ]
            total = len(subset)
            yield f"data: {json.dumps({'type':'total','total':total})}\n\n"
            if not total:
                yield f"data: {json.dumps({'type':'finished','ok':0,'cached':0,'err':0,'folder':''})}\n\n"
                return

            base = Path(MAZOS_OUT) / game_slug / ed_slug
            base.mkdir(parents=True, exist_ok=True)
            ok = err = cached = 0

            def _save(card):
                url   = card.get("imageUrl")
                cname = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', card.get("name", "card"))
                dest  = base / f"{card['id']:05d}_{cname}{_mazos_img_ext(url)}"
                if dest.exists():
                    return "cached"
                if not url:
                    return "err"
                try:
                    req = urllib.request.Request(
                        _mazos_encode_url(url),
                        headers={"User-Agent": HEADERS["User-Agent"]},
                    )
                    with urllib.request.urlopen(req, timeout=20) as r:
                        dest.write_bytes(r.read())
                    return "ok"
                except Exception:
                    return "err"

            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(_save, c): c for c in subset}
                done = 0
                for fut in as_completed(futs):
                    st = fut.result()
                    done += 1
                    if   st == "ok":     ok     += 1
                    elif st == "cached": cached += 1
                    else:                err    += 1
                    if done % 25 == 0 or done == total:
                        pct = round(done / total * 100)
                        yield f"data: {json.dumps({'type':'progress','done':done,'total':total,'ok':ok,'cached':cached,'err':err,'pct':pct})}\n\n"

            folder_abs = str(base.resolve())
            yield f"data: {json.dumps({'type':'finished','ok':ok,'cached':cached,'err':err,'folder':folder_abs})}\n\n"

        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','message':str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/blog/enumerate/stream')
def api_blog_enumerate_stream():
    """
    SSE — prueba rangos numéricos en una URL base para encontrar imágenes
    que existen en el servidor pero no están enlazadas en ningún post.
    Ej: base_url=https://blog.myl.cl/wp-content/uploads/2026/05/AYDV_
        from=170&to=200&suffixes=,-1,-2&ext=.png
    """
    base_url  = request.args.get('base_url', '').strip()
    num_from  = int(request.args.get('from', 1))
    num_to    = int(request.args.get('to', 300))
    raw_ext   = request.args.get('ext', '.png')
    exts      = [e.strip() for e in raw_ext.split(',') if e.strip()]  # ej: [".jpg", ".png"]
    raw_suf   = request.args.get('suffixes', ',-1')
    suffixes  = raw_suf.split(',')   # puede incluir cadena vacía = sin sufijo

    ALLOWED = ('myl.cl', 'shopify.com', 'cdn.shopify.com')
    if not base_url or not any(d in base_url for d in ALLOWED):
        return jsonify({'error': 'URL inválida (debe ser myl.cl o shopify.com)'}), 400
    if num_to < num_from or (num_to - num_from + 1) * len(suffixes) * len(exts) > 2000:
        return jsonify({'error': 'Rango demasiado grande (máx 2000 combinaciones)'}), 400

    def generate():
        combos = [(n, s, e) for n in range(num_from, num_to + 1)
                            for s in suffixes
                            for e in exts]
        total  = len(combos)
        yield f"data: {json.dumps({'type':'total','total':total})}\n\n"

        def _probe(num, suffix, ext):
            url = f"{base_url}{num}{suffix}{ext}"
            try:
                req = urllib.request.Request(url, headers=HEADERS, method='HEAD')
                with urllib.request.urlopen(req, timeout=6) as r:
                    return url, r.status == 200
            except urllib.error.HTTPError as e:
                return url, e.code not in (404, 403)
            except Exception:
                return url, False

        checked = 0
        found   = 0
        found_imgs = []

        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(_probe, n, s, e): (n, s, e) for n, s, e in combos}
            for future in as_completed(futures):
                url, exists = future.result()
                checked += 1
                if exists:
                    found += 1
                    img = {
                        'url':      url,
                        'thumb':    url,
                        'alt':      url.split('/')[-1],
                        'filename': url.split('/')[-1],
                        'post_url': url,
                    }
                    found_imgs.append(img)
                    yield f"data: {json.dumps({'type':'found','img':img,'checked':checked,'total':total,'found':found})}\n\n"
                elif checked % 30 == 0:
                    yield f"data: {json.dumps({'type':'progress','checked':checked,'total':total,'found':found})}\n\n"

        yield f"data: {json.dumps({'type':'finished','found':found,'checked':checked})}\n\n"

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── FACEBOOK (albumes de paginas, via navegador logueado) ────────────────────

_fb = None
cache_fb = {}

def fb():
    """Import perezoso: Playwright solo arranca si se usa la seccion Facebook."""
    global _fb
    if _fb is None:
        import fb_service
        _fb = fb_service
    return _fb

def _fb_slug(value):
    return re.sub(r'[^A-Za-z0-9._-]', '', value or '') or 'CartotecaMyL'

@app.route('/api/fb/albums')
def api_fb_albums():
    page = _fb_slug(request.args.get('page'))
    key = ('albums', page)
    try:
        if key not in cache_fb or request.args.get('refresh'):
            cache_fb[key] = fb().run(fb().list_albums(page))
        return jsonify({'success': True, 'data': cache_fb[key]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/fb/photos')
def api_fb_photos():
    page   = _fb_slug(request.args.get('page'))
    href   = request.args.get('href', '')
    folder = re.sub(r'[^A-Za-z0-9_]', '', request.args.get('folder', ''))
    if not href.startswith('https://www.facebook.com/') or not folder:
        return jsonify({'success': False, 'error': 'Parametros invalidos'}), 400
    key = ('photos', page, folder)
    try:
        if key not in cache_fb or request.args.get('refresh'):
            cache_fb[key] = fb().run(fb().list_photos(page, href, folder))
        photos = cache_fb[key]
        fb().mark_saved(page, folder, photos)
        return jsonify({'success': True, 'data': photos})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/fb/save', methods=['POST'])
def api_fb_save():
    body   = request.get_json() or {}
    page   = _fb_slug(body.get('page'))
    folder = re.sub(r'[^A-Za-z0-9_]', '', body.get('folder', ''))
    photos = [{'fbid': str(p['fbid']), 'set': p.get('set'), 'n': int(p.get('n') or 0)}
              for p in body.get('photos', []) if str(p.get('fbid', '')).isdigit()]
    if not folder or not photos:
        return jsonify({'success': False, 'error': 'Sin fotos'}), 400
    return jsonify({'success': True, 'job': fb().start_job(page, folder, photos)})

@app.route('/api/fb/job/<job_id>')
def api_fb_job(job_id):
    job = fb().jobs.get(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'Job desconocido'}), 404
    return jsonify({'success': True, **{k: v for k, v in job.items() if k != 'files'}})

@app.route('/api/fb/job/<job_id>/cancel', methods=['POST'])
def api_fb_job_cancel(job_id):
    job = fb().jobs.get(job_id)
    if job:
        job['cancel'] = True
    return jsonify({'success': bool(job)})

@app.route('/api/fb/job/<job_id>/zip')
def api_fb_job_zip(job_id):
    job = fb().jobs.get(job_id)
    if not job or not job['files']:
        return jsonify({'success': False, 'error': 'Nada que comprimir'}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as zf:   # JPG ya viene comprimido
        for f in job['files']:
            zf.write(f, os.path.basename(f))
    buf.seek(0)
    title = request.args.get('name') or f"fb_{job['name']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f"{_zip_safe(title)}.zip")

@app.route('/api/fb/login', methods=['GET', 'POST'])
def api_fb_login():
    if request.method == 'POST':
        cache_fb.clear()
        fb().start_login()
    return jsonify({'success': True, **fb().login_state})


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  Blog MYL Explorer → http://localhost:5000")
    print("="*50 + "\n")
    app.run(debug=True, port=5000, use_reloader=False)
