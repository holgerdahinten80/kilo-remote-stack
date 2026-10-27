#!/usr/bin/env python3
"""Lichter-PWA fuer ioBroker (simple-api v3)."""
import io
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SIMPLE = 'http://127.0.0.1:8087'
CONTROL = 'http://127.0.0.1:8894'
ADMIN_STATE = '0_userdata.0.iobroker.admin'
SYSTEM_ITEMS = [
    {'id': '__admin__', 'name': 'ioBroker Admin', 'vendor': 'System'},
    {'id': '__services__', 'name': 'Dienste', 'vendor': 'System'},
    {'id': '__rdc__', 'name': 'RDC-Agent', 'vendor': 'System'},
    {'id': '__jellyfin__', 'name': 'Jellyfin', 'vendor': 'System'},
    {'id': '__dispatcharr__', 'name': 'Dispatcharr', 'vendor': 'System'},
]
TOKEN_FILE = '/usr/local/share/dispatcharr/zap.token'

_obj_cache = {'t': 0, 'data': None}
_names_cache = {'t': 0, 'data': None}
OBJECTS_JSONL = '/media/ssd/iobroker/iobroker-data/objects.jsonl'
VIRTUAL_VENDORS = {'iobroker', 'node-red', 'virtual smart home', 'virtual smart', 'virtual'}


def norm_name(s):
    s = (s or '').lower()
    for a, b in (('Ã¤', 'ae'), ('Ã¶', 'oe'), ('Ã¼', 'ue'), ('ÃŸ', 'ss')):
        s = s.replace(a, b)
    return s.strip()


HIDDEN_FILE = '/usr/local/share/dispatcharr/hidden.json'
_hidden_cache = {'t': 0, 'data': None}


def hidden_rules():
    """Liste ausgeblendeter Eintraege: [name] oder [name, anbieter]."""
    if _hidden_cache['data'] is not None and time.time() - _hidden_cache['t'] < 60:
        return _hidden_cache['data']
    rules = []
    try:
        with open(HIDDEN_FILE, encoding='utf-8') as f:
            data = json.load(f)
        for r in data:
            if isinstance(r, list) and r:
                rules.append((norm_name(r[0]), norm_name(r[1]) if len(r) > 1 and r[1] else None))
            elif isinstance(r, str):
                rules.append((norm_name(r), None))
    except Exception:
        pass
    _hidden_cache.update(t=time.time(), data=rules)
    return rules


def is_hidden(name, vendor):
    n, v = norm_name(name), norm_name(vendor or '')
    for hn, hv in hidden_rules():
        if hn == n and (hv is None or hv == v):
            return True
    return False


def alexa_names():
    """UUID -> {name, vendor} (aus objects.jsonl, gecacht)."""
    if _names_cache['data'] and time.time() - _names_cache['t'] < 3600:
        return _names_cache['data']
    m = {}
    try:
        with open(OBJECTS_JSONL, encoding='utf-8') as f:
            for line in f:
                if 'Smart-Home-Devices' not in line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                k = o.get('k') or ''
                if k.startswith('alexa2.0.Smart-Home-Devices.') and k.count('.') == 3:
                    v = o.get('v') or {}
                    c = v.get('common') or {}
                    n = v.get('native') or {}
                    if isinstance(c.get('name'), str):
                        m[k] = {'name': c.get('name'), 'vendor': n.get('manufacturerName')}
    except Exception:
        pass
    _names_cache.update(t=time.time(), data=m)
    return m


def api(path, timeout=20):
    with urllib.request.urlopen(SIMPLE + path, timeout=timeout) as r:
        return r.read()


def api_json(path, timeout=20):
    return json.loads(api(path, timeout))


def read_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return ''


def get_objects(pattern):
    try:
        return api_json('/objects?pattern=' + pattern, timeout=25)
    except Exception:
        return {}


def build_devices():
    """Alle Schalter + Lichter als EINE Liste (Objekte gecacht)."""
    if _obj_cache['data'] and time.time() - _obj_cache['t'] < 300:
        return _obj_cache['data']

    items = [dict(x) for x in SYSTEM_ITEMS]

    objs = get_objects('javascript.0.Schalter.*')
    seen = set()
    for oid, o in objs.items():
        c = o.get('common') or {}
        if o.get('type') == 'state' and c.get('type') == 'boolean' and c.get('write') is not False:
            seg = oid.split('.')[-1]
            name = c.get('name') or seg
            if name.startswith('Schalter.'):
                name = name.split('.', 1)[1]
            if norm_name(name) in seen:
                name = seg
            seen.add(norm_name(name))
            if is_hidden(name, 'iobroker'):
                continue
            items.append({'id': oid, 'name': name, 'vendor': 'ioBroker', 'bright': None})

    aobjs = get_objects('alexa2.0.Smart-Home-Devices.*')
    names = alexa_names()
    schalter_names = {norm_name(x['name']) for x in items}

    devs = {}
    for oid, o in aobjs.items():
        if o.get('type') != 'state':
            continue
        if oid.endswith('.powerState'):
            devs.setdefault(oid[:-len('.powerState')], {})['power'] = oid
        elif oid.endswith('.brightness'):
            devs.setdefault(oid[:-len('.brightness')], {})['bright'] = oid

    for dev, st in devs.items():
        if 'power' not in st:
            continue
        info = names.get(dev) or {}
        nm = info.get('name') or dev.split('.')[-1]
        ven = (info.get('vendor') or '').strip()
        # Virtuelle Duplikate (ioBroker/Node-RED/virtual smart home) ausblenden,
        # wenn es den Namen schon als ioBroker-Schalter gibt
        if ven.lower() in VIRTUAL_VENDORS and norm_name(nm) in schalter_names:
            continue
        if is_hidden(nm, ven):
            continue
        items.append({'id': st['power'], 'bright': st.get('bright'),
                      'name': nm, 'vendor': ven or None})

    items.sort(key=lambda x: (x['name'] or '').lower())
    data = {'items': items}
    _obj_cache.update(t=time.time(), data=data)
    return data


def values(ids):
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 60):
        chunk = ids[i:i + 60]
        try:
            res = api_json('/getBulk/' + ','.join(chunk) + '/', timeout=25)
            for r in res:
                out[r.get('id')] = r.get('val')
        except Exception:
            pass
    return out


def set_value(sid, val):
    enc = urllib.parse.quote(str(val))
    return api('/set/' + sid + '?value=' + enc, timeout=15)


def toggle(sid):
    return api('/toggle/' + sid, timeout=15)


def control(path, timeout=180):
    with urllib.request.urlopen(CONTROL + path, timeout=timeout) as r:
        return json.loads(r.read())


def system_values():
    out = {}
    try:
        out['__admin__'] = bool(api_json('/get/' + ADMIN_STATE).get('val'))
    except Exception:
        out['__admin__'] = None
    try:
        status = control('/status')
    except Exception:
        status = {}
    group = ('sonarr', 'radarr', 'nzbget')
    out['__services__'] = bool(status) and all(status.get(n) == 'running' for n in group)
    out['__jellyfin__'] = status.get('jellyfin') == 'running' if status else None
    out['__dispatcharr__'] = status.get('dispatcharr') == 'running' if status else None
    try:
        st = control('/service/status')
        out['__rdc__'] = st.get('rdc-agent') == 'active'
    except Exception:
        out['__rdc__'] = None
    return out


SAY_DEFAULT = 'Bitte runterkommen zum tragen helfen.'
_echo_cache = {'t': 0, 'data': None}


def echo_speak_ids():
    """Alle Echo-Geraete mit Commands.speak."""
    if _echo_cache['data'] and time.time() - _echo_cache['t'] < 3600:
        return _echo_cache['data']
    ids = []
    try:
        objs = get_objects('alexa2.0.Echo-Devices.*')
        ids = sorted(oid for oid in objs if oid.endswith('.Commands.speak'))
    except Exception:
        pass
    _echo_cache.update(t=time.time(), data=ids)
    return ids


def announce_worker(text, times=3, gap=6):
    """Durchsage mehrfach (Standard: 3x) mit Pause dazwischen."""
    for i in range(max(1, times)):
        try:
            announce_all(text)
        except Exception:
            pass
        if i < times - 1:
            time.sleep(gap)


def echo_targets():
    """Je Echo-Geraet die beste Ansage-Adresse: announce/announcement, sonst speak."""
    devs = {}
    try:
        objs = get_objects('alexa2.0.Echo-Devices.*')
        for oid in objs:
            parts = oid.split('.')
            if len(parts) < 6:
                continue
            serial = parts[3]
            d = devs.setdefault(serial, {})
            if oid.endswith('.Commands.announce'):
                d.setdefault('ann', oid)
            if oid.endswith('.Commands.announcement'):
                d['ann'] = oid          # bevorzugt
            if oid.endswith('.Commands.speak'):
                d['speak'] = oid
    except Exception:
        pass
    return devs


def announce_all(text, delay=0.15):
    """Durchsage an alle Echos (announce/announcement, sonst speak)."""
    devs = echo_targets()
    ok = 0
    total = 0
    for serial, d in devs.items():
        target = d.get('ann') or d.get('speak')
        if not target:
            continue
        total += 1
        try:
            set_value(target, text)
            ok += 1
            time.sleep(delay)
        except Exception:
            pass
    return ok, total


PAGE = r"""<!DOCTYPE html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#111418">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/lights/manifest.webmanifest">
<link rel="apple-touch-icon" href="/lights/icon-192.png">
<link rel="icon" href="/lights/icon-192.png">
<title>Schalter</title>
<style>
 :root{color-scheme:dark;}
 *{box-sizing:border-box;}
 body{margin:0;background:#111418;color:#e8eaed;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       overflow-x:hidden;}
 header{position:sticky;top:0;z-index:5;background:#111418;border-bottom:1px solid #262b31;
        padding:12px 14px;display:flex;align-items:center;justify-content:space-between;}
 h1{font-size:18px;margin:0;}
 #reload{background:#1e242b;color:#cfd3d7;border:1px solid #2b3239;border-radius:999px;
         padding:8px 13px;font-size:13px;font-weight:600;cursor:pointer;}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#7f868d;
    margin:14px 14px 4px;}
 ul{list-style:none;margin:0;padding:12px;padding-bottom:30px;display:grid;
    grid-template-columns:1fr 1fr;gap:10px;}
 li{display:flex;flex-direction:column;gap:6px;padding:12px;border:1px solid #2b3239;
    border-radius:12px;background:#181d23;}
 .top{display:flex;align-items:center;justify-content:space-between;gap:8px;}
 .name{font-size:14px;font-weight:600;overflow-wrap:anywhere;min-width:0;}
 .bottom{display:flex;align-items:center;justify-content:flex-end;margin-top:auto;}
 .dim{color:#7f868d;font-weight:400;font-size:12px;}
 .sw{width:56px;height:30px;border-radius:999px;background:#2b3239;position:relative;
     cursor:pointer;flex:0 0 auto;transition:background .15s;}
 .sw::after{content:'';position:absolute;top:3px;left:3px;width:24px;height:24px;border-radius:50%;
     background:#e8eaed;transition:left .15s;}
 .sw.on{background:#f59e0b;}
 .sw.on::after{left:29px;}
 .br{display:flex;align-items:center;gap:6px;width:100%;min-width:0;}
 input[type=range]{width:100%;accent-color:#f59e0b;}
 .br input[type=range]{flex:1;min-width:0;width:auto;}
 #toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#3b82f6;color:#fff;
        padding:9px 16px;border-radius:999px;font-size:13px;opacity:0;transition:opacity .25s;
        pointer-events:none;z-index:20;}
 #toast.show{opacity:1;} #toast.err{background:#ef4444;}
</style></head><body>
<header><h1>Schalter <span style="font-size:11px;color:#7f868d;font-weight:400">v7</span></h1><button id="reload">Aktualisieren</button></header>
<div style="padding:10px 12px 0;"><button id="say" style="width:100%;padding:14px;border-radius:12px;border:1px solid #b45309;background:#f59e0b;color:#111418;font-size:15px;font-weight:700;cursor:pointer;">&#128227; Ansage an alle Echos</button></div>
<div id="content">lade ...</div>
<div id="toast"></div>
<script>
const withTok = p => p + (p.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent('__ZAP__');
const toastEl=document.getElementById('toast'); let tt=null;
function toast(m,e){toastEl.textContent=m;toastEl.className='show'+(e?' err':'');clearTimeout(tt);tt=setTimeout(()=>toastEl.className='',1500);}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function setVal(id,val,label){
  try{
    const r = await fetch(withTok('/lights/set?id='+encodeURIComponent(id)+'&value='+encodeURIComponent(val)),{method:'POST'});
    const j = await r.json();
    if(!j.ok) throw new Error('err');
    toast(label+' '+(val==1||val===true?'an':'aus'));
  }catch(e){ toast('Fehler', true); }
}
function row(x){
  const li=document.createElement('li');
  const on = x.val===true||x.val===1||x.val==='true';
  const vend = x.vendor ? ' <span class="dim">'+esc(x.vendor)+'</span>' : '';
  const slider = x.bright
    ? '<div class="br"><input type="range" min="1" max="100" value="'+Math.max(1,Math.round(x.brightVal||1))+'">'
      + '<span class="dim">'+Math.round(x.brightVal||0)+'%</span></div>'
    : '';
  li.innerHTML='<div class="top"><div class="name">'+esc(x.name)+'</div>'
    + '<div class="sw'+(on?' on':'')+'"></div></div>'
    + (x.vendor ? '<div class="dim">'+esc(x.vendor)+'</div>' : '')
    + slider;
  const sw=li.querySelector('.sw');
  sw.onclick=()=>{ const nv=!sw.classList.contains('on'); sw.classList.toggle('on',nv); setVal(x.id,nv,x.name); };
  const r=li.querySelector('input[type=range]');
  if(r){
    let t=null;
    r.oninput=()=>{ const s=li.querySelector('.br .dim'); if(s) s.textContent=r.value+'%'; clearTimeout(t);
      t=setTimeout(()=>setVal(x.bright,parseInt(r.value,10),x.name),400); };
  }
  return li;
}
let loading=false;
async function load(){
  if(loading) return;
  loading=true;
  try{
    document.getElementById('content').innerHTML='lade ...';
    let d;
    try{ d = await (await fetch(withTok('/lights/list') + '&_=' + Date.now())).json(); }
    catch(e){ document.getElementById('content').textContent='ioBroker nicht erreichbar'; return; }
    const box=document.getElementById('content');
    const ul=document.createElement('ul');
    (d.items||[]).forEach(x=>ul.appendChild(row(x)));
    box.replaceChildren(ul);
  } finally { loading=false; }
}
document.getElementById('reload').onclick=async()=>{
  toast('Frage Status ab ...');
  try{ await fetch(withTok('/lights/query'),{method:'POST'}); }catch(e){}
  setTimeout(load, 8000);
};
document.getElementById('say').onclick=async()=>{
  toast('Ansage lÃ¤uft (3x) ...');
  try{
    const j = await (await fetch(withTok('/lights/say'),{method:'POST'})).json();
    toast(j.ok ? ('Ansage '+(j.times||3)+'x auf '+(j.devices||'?')+' Echos') : 'Ansage fehlgeschlagen', !j.ok);
  }catch(e){ toast('Ansage fehlgeschlagen', true); }
};
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/lights/sw.js',{scope:'/lights/'}).catch(()=>{}); }
load(); setInterval(load, 60000);
</script>
</body></html>"""

APP_MANIFEST = {
    "name": "Schalter", "short_name": "Schalter",
    "start_url": "/lights/", "scope": "/lights/",
    "display": "standalone", "background_color": "#111418", "theme_color": "#111418",
    "icons": [
        {"src": "/lights/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "/lights/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}
SW_JS = """self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {});
"""
_ICONS = {}


def make_icon(size):
    if size in _ICONS:
        return _ICONS[size]
    from PIL import Image, ImageDraw
    img = Image.new('RGB', (size, size), (17, 20, 24))
    d = ImageDraw.Draw(img)
    cx, cy = size // 2, int(size * 0.44)
    r = int(size * 0.22)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(245, 158, 11))
    d.rectangle([cx - int(r * 0.45), cy + int(r * 0.8), cx + int(r * 0.45), cy + int(r * 1.25)],
                fill=(207, 211, 215))
    d.rectangle([cx - int(r * 0.45), cy + int(r * 1.4), cx + int(r * 0.45), cy + int(r * 1.6)],
                fill=(207, 211, 215))
    for a in range(8):
        import math
        ang = a * math.pi / 4
        x1 = cx + int(math.cos(ang) * r * 1.35)
        y1 = cy + int(math.sin(ang) * r * 1.35)
        x2 = cx + int(math.cos(ang) * r * 1.6)
        y2 = cy + int(math.sin(ang) * r * 1.6)
        d.line([x1, y1, x2, y2], fill=(245, 158, 11), width=max(2, size // 64))
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    _ICONS[size] = buf.getvalue()
    return _ICONS[size]


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        u = urlparse(self.path)
        path = u.path.rstrip('/') or '/lights'
        q = parse_qs(u.query)

        if path in ('/lights', '/lights/index.html'):
            body = PAGE.replace('__ZAP__', read_token()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, max-age=0')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == '/lights/manifest.webmanifest':
            return self._send(200, APP_MANIFEST)
        if path == '/lights/sw.js':
            body = SW_JS.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ('/lights/icon-192.png', '/lights/icon-512.png'):
            img = make_icon(192 if '192' in path else 512)
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(img)))
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(img)
            return

        token = read_token()
        if token and q.get('token', [''])[0] != token:
            return self._send(403, {'ok': False, 'error': 'bad token'})

        try:
            if path == '/lights/list':
                d = build_devices()
                ids = [x['id'] for x in d['items'] if not x['id'].startswith('__')]
                ids += [x['bright'] for x in d['items'] if x.get('bright')]
                vals = values(ids)
                sysvals = system_values()
                for x in d['items']:
                    if x['id'] in sysvals:
                        x['val'] = sysvals[x['id']]
                    else:
                        x['val'] = vals.get(x['id'])
                    if x.get('bright'):
                        x['brightVal'] = vals.get(x['bright'])
                return self._send(200, d)
            if path == '/lights/set':
                sid = q.get('id', [''])[0]
                val = q.get('value', [''])[0]
                if not sid:
                    return self._send(400, {'ok': False, 'error': 'missing id'})
                if val.lower() in ('true', 'false'):
                    val = 'true' if val.lower() == 'true' else 'false'
                an = str(val).lower() in ('true', '1', 'on', 'yes')
                if sid == '__admin__':
                    set_value(ADMIN_STATE, 'true' if an else 'false')
                elif sid == '__services__':
                    control('/start-all' if an else '/stop-all')
                elif sid == '__rdc__':
                    control('/service/start/rdc-agent' if an else '/service/stop/rdc-agent')
                elif sid in ('__jellyfin__', '__dispatcharr__'):
                    name = 'jellyfin' if sid == '__jellyfin__' else 'dispatcharr'
                    control('/start/' + name if an else '/stop/' + name)
                else:
                    set_value(sid, val)
                return self._send(200, {'ok': True, 'id': sid, 'value': val})
            if path == '/lights/say':
                text = q.get('text', [''])[0].strip() or SAY_DEFAULT
                try:
                    times = int(q.get('times', ['3'])[0])
                except Exception:
                    times = 3
                times = max(1, min(10, times))
                threading.Thread(target=announce_worker, args=(text, times),
                                 daemon=True).start()
                return self._send(200, {'ok': True, 'text': text, 'times': times,
                                        'devices': len(echo_targets())})
            if path == '/lights/query':
                try:
                    set_value('alexa2.0.Smart-Home-Devices.queryAll', 'true')
                except Exception:
                    pass
                return self._send(200, {'ok': True})
            if path == '/lights/toggle':
                sid = q.get('id', [''])[0]
                toggle(sid)
                return self._send(200, {'ok': True, 'id': sid})
            return self._send(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            return self._send(500, {'ok': False, 'error': str(e)})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8890), Handler).serve_forever()
