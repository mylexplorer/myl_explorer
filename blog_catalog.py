"""
Catálogo local de blog.myl.cl: SOLO URLs y metadatos, nunca imágenes.

- media: toda la biblioteca de medios de WordPress (/wp/v2/media), el índice
  autoritativo de lo subido.
- posts / pages: lo publicado, con las URLs de uploads que usa cada uno en su
  contenido (+ su imagen destacada).

Con eso todo se responde en local y al instante, incluido el "radar": medios
subidos que no aparecen en ningún post/página publicado (spoilers, borradores).

Sincronización incremental en un hilo: medios nuevos por id descendente hasta
topar con uno conocido; posts/páginas con modified_after. Lo borrado en el blog
solo se detecta con una resincronización completa (sync(full=True)).
"""

import json, re, threading, time, unicodedata, html as html_module
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from concurrent.futures import ThreadPoolExecutor

CATALOG_VERSION = 1
SYNC_EVERY_SECS = 600

_UPLOAD_RE = re.compile(r'https?://[^\s"\'<>(),]+?/wp-content/uploads/[^\s"\'<>(),]+', re.I)
_IMG_RE    = re.compile(r'<img\b[^>]*>', re.I)
_ATTR_RE   = re.compile(r'([\w-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_SIZE_RE   = re.compile(r'-(\d+x\d+|scaled)(?=\.\w+$)', re.I)


def url_key(url):
    """Clave para comparar URLs de uploads: sin host, sin variante de tamaño
    (-1024x768 / -scaled), decodificada y en NFC (los acentos llegan de las dos formas)."""
    path = unicodedata.normalize('NFC', unquote(urlparse(url).path))
    return _SIZE_RE.sub('', path).lower()


def _content_images(content):
    """(imgs para mostrar en orden, set de claves de TODAS las uploads referenciadas)."""
    used = {url_key(u) for u in _UPLOAD_RE.findall(content)}
    imgs, seen = [], set()
    for tag in _IMG_RE.findall(content):
        attrs = {m.group(1).lower(): html_module.unescape(m.group(3) or m.group(4) or '')
                 for m in _ATTR_RE.finditer(tag)}
        src = attrs.get('src') or attrs.get('data-src') or ''
        if 'wp-content/uploads' not in src or src in seen:
            continue
        seen.add(src)
        full = re.sub(r'-\d+x\d+(\.\w+)$', r'\1', src)
        name = Path(urlparse(full).path).name
        imgs.append({'url': full, 'thumb': src,
                     'alt': (attrs.get('alt') or '').strip()[:60] or name})
    return imgs, used


class BlogCatalog:
    def __init__(self, fetch_json, wp_api, path):
        self._fetch = fetch_json          # (url) -> (data, total_pages)
        self._api   = wp_api
        self._path  = Path(path)
        self._lock  = threading.Lock()    # serializa syncs
        self.data   = None                # se reemplaza entero (lectores sin lock)
        self.state  = {'syncing': False, 'last_sync': None, 'last_error': None,
                       'new_media': 0, 'new_posts': 0, 'rev': 0}
        self._load()

    # ── persistencia ──────────────────────────────────────────────────────
    def _load(self):
        try:
            d = json.loads(self._path.read_text(encoding='utf-8'))
            if d.get('version') == CATALOG_VERSION:
                self._set(d)
                self.state['last_sync'] = d.get('synced_at')
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"  Catálogo blog ilegible, se rehace: {e}")

    def _save(self, d):
        tmp = self._path.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        tmp.replace(self._path)

    def _set(self, d):
        """Publica un catálogo nuevo junto con sus índices derivados."""
        media = d['media']
        by_post = {}
        for mid, m in media.items():
            if m['p']:
                by_post.setdefault(str(m['p']), []).append(mid)
        used = set()
        for kind in ('posts', 'pages'):
            for p in d[kind].values():
                used.update(p['used'])
                feat = media.get(str(p.get('feat') or 0))
                if feat:
                    used.add(url_key(feat['u']))
        self._used = used
        self._by_post = by_post
        self.data = d
        self.state['rev'] += 1

    @property
    def ready(self):
        return self.data is not None

    # ── sincronización ────────────────────────────────────────────────────
    def start(self, every=SYNC_EVERY_SECS):
        def loop():
            while True:
                self.sync()
                time.sleep(every)
        threading.Thread(target=loop, name='blog-catalog-sync', daemon=True).start()

    def sync(self, full=False):
        if not self._lock.acquire(blocking=False):
            return False                   # ya hay uno en curso
        self.state['syncing'] = True
        try:
            old = None if full or not self.ready else self.data
            d = {'version': CATALOG_VERSION,
                 'media': dict(old['media']) if old else {},
                 'posts': dict(old['posts']) if old else {},
                 'pages': dict(old['pages']) if old else {},
                 'modified': dict(old['modified']) if old else {}}
            t0 = time.time()
            new_media = self._sync_media(d['media'], incremental=old is not None)
            new_posts = 0
            for kind in ('posts', 'pages'):
                new_posts += self._sync_content(kind, d, incremental=old is not None)
            d['synced_at'] = datetime.now().isoformat(timespec='seconds')
            self._save(d)
            self._set(d)
            self.state.update(last_sync=d['synced_at'], last_error=None)
            self.state['new_media'] += new_media if old else 0
            self.state['new_posts'] += new_posts if old else 0
            print(f"  Catálogo blog: {len(d['media'])} medios, {len(d['posts'])} posts "
                  f"(+{new_media} medios, {new_posts} posts nuevos/editados) en {time.time()-t0:.0f}s")
            return True
        except Exception as e:
            self.state['last_error'] = str(e)
            print(f"  ERROR sync catálogo blog: {e}")
            return False
        finally:
            self.state['syncing'] = False
            self._lock.release()

    def _pages(self, url, start=1):
        """Página `start` y el total de páginas; lanza si la API no responde."""
        data, total = self._fetch(f"{url}&page={start}")
        if data is None:
            raise RuntimeError(f"API sin respuesta: {url}&page={start}")
        return data, total or 1

    def _all_pages(self, url):
        first, total = self._pages(url)
        out = list(first)
        with ThreadPoolExecutor(max_workers=6) as ex:
            for batch, _ in ex.map(lambda p: self._pages(url, p), range(2, total + 1)):
                out.extend(batch)
        return out

    def _sync_media(self, media, incremental):
        url = (f"{self._api}/media?per_page=100&orderby=id&order=desc"
               f"&_fields=id,date,source_url,title,post,mime_type,media_details")
        if not incremental:
            batch = self._all_pages(url)
        else:
            # Por id descendente: lo nuevo va primero; se corta al topar con lo conocido
            batch, page = [], 1
            while True:
                data, total = self._pages(url, page)
                fresh = [m for m in data if str(m['id']) not in media]
                batch.extend(fresh)
                if len(fresh) < len(data) or page >= total:
                    break
                page += 1
        for m in batch:
            sizes = (m.get('media_details') or {}).get('sizes') or {}
            thumb = (sizes.get('medium') or sizes.get('thumbnail') or {}).get('source_url')
            media[str(m['id'])] = {
                'u': m.get('source_url', ''),
                'd': (m.get('date') or '')[:19],
                'p': m.get('post') or 0,
                't': html_module.unescape((m.get('title') or {}).get('rendered', '')),
                'mt': m.get('mime_type', ''),
                **({'th': thumb} if thumb and thumb != m.get('source_url') else {}),
            }
        return len(batch)

    def _sync_content(self, kind, d, incremental):
        since = d['modified'].get(kind) if incremental else None
        # orderby=id: con modified hay empates y la paginación repite/salta posts
        url = (f"{self._api}/{kind}?per_page=100&orderby=id&order=desc"
               f"&_fields=id,date,modified,link,title,content,featured_media")
        if since:
            url += f"&modified_after={since}"
        items = self._all_pages(url)
        store = d[kind]
        if not incremental:
            store.clear()
        for p in items:
            imgs, used = _content_images((p.get('content') or {}).get('rendered', ''))
            title = re.sub(r'<[^>]+>', '', (p.get('title') or {}).get('rendered', ''))
            store[str(p['id'])] = {
                'd': (p.get('date') or '')[:19],
                'l': p.get('link', ''),
                't': html_module.unescape(title).strip(),
                'feat': p.get('featured_media') or 0,
                'imgs': imgs,
                'used': sorted(used),
            }
            mod = p.get('modified') or ''
            if mod > d['modified'].get(kind, ''):
                d['modified'][kind] = mod
        return len(items)

    # ── consultas ─────────────────────────────────────────────────────────
    def img(self, mid, m, post_url=None):
        name = Path(urlparse(m['u']).path).name
        return {'url': m['u'], 'thumb': m.get('th') or m['u'],
                'alt': m['t'] or name, 'filename': name,
                'post_url': post_url or m['u'], 'media_id': int(mid),
                'date': m['d'][:10]}

    def is_used(self, m):
        return url_key(m['u']) in self._used

    def months(self):
        keys = {p['d'][:7] for p in self.data['posts'].values() if p['d']}
        return sorted(keys, reverse=True)

    def posts_for_month(self, year, month):
        pref = f"{year}-{month}"
        out = [(pid, p) for pid, p in self.data['posts'].items() if p['d'].startswith(pref)]
        out.sort(key=lambda x: x[1]['d'], reverse=True)
        return out

    def post_images(self, pid, p):
        imgs = []
        for i in p['imgs']:
            imgs.append({**i, 'filename': Path(urlparse(i['url']).path).name,
                         'post_url': p['l']})
        return imgs

    def post_radar(self, pid):
        """Medios adjuntos a este post que no se usan en ningún post/página publicado."""
        media = self.data['media']
        out = [self.img(mid, media[mid]) for mid in self._by_post.get(str(pid), [])
               if media[mid]['mt'].startswith('image/') and not self.is_used(media[mid])]
        out.sort(key=lambda i: i['date'])
        return out

    def media_for_month(self, year, month):
        pref = f"{year}-{month}"
        return [(mid, m) for mid, m in self.data['media'].items()
                if m['d'].startswith(pref) and m['mt'].startswith('image/')]

    def radar(self):
        """(huérfanas sin post, {post_id no publicado: [imgs]}) — solo no usadas."""
        published = set(self.data['posts']) | set(self.data['pages'])
        orphans, drafts = [], {}
        for mid, m in self.data['media'].items():
            if not m['mt'].startswith('image/') or self.is_used(m):
                continue
            if not m['p']:
                orphans.append(self.img(mid, m))
            elif str(m['p']) not in published:
                drafts.setdefault(m['p'], []).append(
                    self.img(mid, m, f"https://blog.myl.cl/?p={m['p']}"))
        return orphans, drafts

    def status(self):
        d = self.data
        return {**self.state, 'ready': self.ready,
                'media': len(d['media']) if d else 0,
                'posts': len(d['posts']) if d else 0}
