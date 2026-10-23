import glob, json, os, re, sqlite3, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

HOME = os.path.expanduser("~")
EXTENSIONS_ROOT = os.path.join(HOME, ".vscode", "extensions")

def extension_version(path):
    match = re.search(r"kilo-code-(\d+(?:\.\d+)*)", os.path.basename(path))
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)

def find_cli():
    override = os.environ.get("KILO_CLI")
    if override and os.path.isfile(override):
        return override
    pattern = os.path.join(EXTENSIONS_ROOT, "kilocode.kilo-code-*", "bin", "kilo.exe")
    candidates = [path for path in glob.glob(pattern) if os.path.isfile(path)]
    if candidates:
        return max(candidates, key=lambda path: extension_version(os.path.dirname(os.path.dirname(path))))
    return os.path.join(EXTENSIONS_ROOT, "kilocode.kilo-code", "bin", "kilo.exe")
DEFAULT_PROJECT = os.path.join(HOME, "source", "repos", "PythonTest")
INBOX = os.path.join(HOME, "Desktop", "KiloCodeChat.inbox.txt")
SETTINGS = os.path.join(HOME, "Desktop", "KiloCodeWeb.settings.json")
def load_settings():
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            data = json.load(f)
        recent = [p for p in data.get("recent_projects", []) if isinstance(p, str) and os.path.isdir(p)]
        last = data.get("last_project", "")
        if last and os.path.isdir(last) and last not in recent:
            recent.insert(0, last)
        sessions = data.get("project_sessions", {})
        if not isinstance(sessions, dict): sessions = {}
        return {"last_project": last if last and os.path.isdir(last) else DEFAULT_PROJECT,
                "recent_projects": recent[:12], "project_sessions": sessions}
    except (OSError, ValueError, TypeError):
        return {"last_project": DEFAULT_PROJECT, "recent_projects": [], "project_sessions": {}}

def write_settings(data):
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_project():
    return load_settings()["last_project"]

def save_project(project):
    data = load_settings()
    recent = [p for p in data["recent_projects"] if os.path.normcase(p) != os.path.normcase(project)]
    recent.insert(0, project)
    data["last_project"] = project
    data["recent_projects"] = recent[:12]
    write_settings(data)

def save_session(project, session_id):
    data = load_settings()
    sessions = data.get("project_sessions", {})
    sessions[os.path.normcase(os.path.abspath(project))] = session_id
    data["project_sessions"] = sessions
    write_settings(data)

def clear_session(project):
    data = load_settings()
    sessions = data.get("project_sessions", {})
    sessions.pop(os.path.normcase(os.path.abspath(project)), None)
    data["project_sessions"] = sessions
    write_settings(data)

def extract_session_id(value):
    if isinstance(value, dict):
        for key in ("sessionID", "sessionId", "session_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        session = value.get("session")
        if isinstance(session, dict) and isinstance(session.get("id"), str):
            return session["id"]
        event_type = str(value.get("type", "")).lower()
        if "session" in event_type:
            def nested_id(obj):
                if isinstance(obj, dict):
                    if isinstance(obj.get("id"), str) and obj["id"]: return obj["id"]
                    for nested in obj.values():
                        found = nested_id(nested)
                        if found: return found
                elif isinstance(obj, list):
                    for nested in obj:
                        found = nested_id(nested)
                        if found: return found
                return None
            found = nested_id(value)
            if found: return found
        for child in value.values():
            found = extract_session_id(child)
            if found: return found
    elif isinstance(value, list):
        for child in value:
            found = extract_session_id(child)
            if found: return found
    return None

SETTINGS_DATA = load_settings()
STATE = {"events": [], "busy": False, "status": "Bereit",
         "project": SETTINGS_DATA["last_project"], "recent_projects": SETTINGS_DATA["recent_projects"],
         "session_id": SETTINGS_DATA["project_sessions"].get(os.path.normcase(os.path.abspath(SETTINGS_DATA["last_project"])), "")}
LOCK = threading.Lock()

PAGE = r'''<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT ↔ Kilo Code</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"></script>
<style>
:root{color-scheme:light;--line:#d9dee7;--bg:#f4f6f9;--blue:#155eef}*{box-sizing:border-box}
body{margin:0;background:var(--bg);font:15px "Segoe UI",Arial,sans-serif;color:#202632}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 20px;display:flex;align-items:center;gap:12px}
header h1{font-size:19px;margin:0;font-weight:650}.pill{font-size:12px;padding:4px 9px;border-radius:12px;background:#e7f7ec;color:#176b35}
main{width:100%;max-width:none;margin:0;padding:12px;display:grid;grid-template-rows:auto minmax(240px,1fr) auto;gap:12px;height:calc(100vh - 58px)}
.toolbar,.card,.composer{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}
.toolbar{display:flex;gap:8px;align-items:center}.toolbar label{white-space:nowrap}.project-combo{display:flex;flex:1;min-width:0;position:relative}.project-combo input{width:100%;min-width:0;border-radius:6px 0 0 6px}.project-combo > button{border-radius:0 6px 6px 0;width:42px;font-size:16px}.project-options{display:none;position:absolute;z-index:20;top:calc(100% + 3px);left:0;right:0;max-height:260px;overflow-y:auto;background:#fff;border:1px solid #c8cfda;border-radius:6px;box-shadow:0 6px 18px #0002}.project-options.open{display:block}.project-option{display:block;width:100%;max-width:100%;text-align:left;white-space:normal;word-break:normal;overflow-wrap:anywhere;border:0;border-radius:0;padding:9px 10px}.project-option:hover,.project-option:focus{background:#eef2f7}
input,textarea,button{font:inherit;border:1px solid #c8cfda;border-radius:6px;padding:9px}
button{cursor:pointer;background:#fff}button.primary{background:var(--blue);color:#fff;border-color:var(--blue)}button:disabled{opacity:.5;cursor:wait}
.workspace{display:grid;grid-template-columns:minmax(0,var(--answer-width,1.7fr)) 8px minmax(240px,1fr);gap:0;min-height:0}.splitter{cursor:col-resize;touch-action:none;position:relative;align-self:stretch}.splitter:after{content:"";position:absolute;top:0;bottom:0;left:3px;width:2px;background:#c8cfda;border-radius:2px}.splitter:hover:after,.splitter.dragging:after{background:var(--blue);width:4px;left:2px}
.card{min-height:0;overflow:auto}.card h2{font-size:14px;color:#586174;margin:0 0 10px;padding-bottom:8px;border-bottom:1px solid #edf0f4}
#answer{line-height:1.55;overflow-wrap:anywhere}#answer a{color:#075bc8;text-decoration:underline;cursor:pointer}#answer table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:12px 0}#answer th,#answer td{border:1px solid #cbd2dc;padding:7px 10px;text-align:left}#answer th{background:#eef2f7}#answer pre{background:#f1f3f6;padding:12px;border-radius:7px;overflow:auto}#answer code{font-family:Consolas,monospace;background:#f1f3f6;padding:2px 4px;border-radius:4px}#answer pre code{padding:0;background:transparent}#answer blockquote{border-left:3px solid #cbd2dc;margin-left:0;padding-left:12px;color:#5e6675}
.workspace>section.card:last-child{display:flex;flex-direction:column;margin-left:12px}#log{font:12px Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;color:#4b5565;flex:1;min-height:0;overflow-y:auto}.log-entry{padding:6px 8px;margin:0 0 5px;border-left:3px solid #c8cfda;background:#f8fafc;border-radius:3px}.log-entry.log-error{border-left-color:#d92d20;background:#fff3f2;color:#912018}.log-entry.log-action{border-left-color:#1570ef}.log-entry.log-question{border-left-color:#b54708;background:#fffaeb;color:#7a2e0e}.log-entry.log-question .log-detail{color:#7a2e0e}.log-entry.log-success{border-left-color:#16a34a}.log-title{font:600 12px 'Segoe UI',Arial,sans-serif;margin-bottom:3px}.log-detail{font:11px Consolas,monospace;color:#667085;white-space:pre-wrap;overflow-wrap:anywhere}.log-entry details{margin-top:4px}.log-entry summary{font:11px 'Segoe UI',Arial,sans-serif;cursor:pointer;color:#475467}.composer textarea{width:100%;resize:vertical;min-height:76px}.actions{display:flex;gap:8px;align-items:center;margin-top:8px}.hint{font-size:12px;color:#667085;margin-left:auto}
@media(max-width:800px){main{height:auto;min-height:calc(100vh - 65px)}.workspace{grid-template-columns:1fr!important;grid-template-rows:minmax(280px,1fr) 8px 220px}.splitter{cursor:row-resize}.splitter:after{left:0;right:0;top:3px;bottom:auto;width:auto;height:2px}.workspace>section.card:last-child{margin-left:0}.toolbar{flex-wrap:wrap}}
</style></head><body>
<header><h1>ChatGPT ↔ Kilo Code</h1><span id="status" class="pill">Bereit</span></header>
<main><div class="toolbar"><label for="project">Projektordner</label><div class="project-combo"><input id="project" type="text" autocomplete="off" placeholder="Projektpfad eingeben …"><button id="toggleProjects" type="button" aria-label="Letzte Projekte anzeigen" title="Letzte Projekte anzeigen">▾</button><div id="projectOptions" class="project-options" role="listbox" aria-label="Letzte Projekte"></div></div></div>
<div class="workspace"><section class="card"><h2>Antwort von Kilo Code · Markdown</h2><div id="answer"><p>Noch keine Antwort. Stelle unten eine Aufgabe.</p></div></section>
<div id="splitter" class="splitter" role="separator" aria-label="Breite von Markdown und Kommunikationsprotokoll ändern" aria-orientation="vertical" tabindex="0"></div><section class="card"><h2>Kommunikationsprotokoll</h2><div id="log"></div></section></div>
<section class="composer"><textarea id="prompt" placeholder="Aufgabe an Kilo Code … (Enter = senden, Shift+Enter = Zeilenumbruch)"></textarea>
<div class="actions"><button id="send" class="primary">An Kilo Code senden</button><button id="newSession">Neue Session</button><button id="copy">Antwort kopieren</button><button id="save">Protokoll speichern</button><label title="Erlaubt Kilo Code, Berechtigungsanfragen ohne Einzelbestätigung zu genehmigen."><input id="autoApprove" type="checkbox" checked style="width:auto;padding:0"> Auto-Approve</label><span class="hint">Läuft nur lokal auf diesem PC · Links öffnen sich im Browser</span></div></section></main>
<script>
const answer=document.getElementById('answer'), answerCard=document.querySelector('.workspace>section.card:first-child'), log=document.getElementById('log'), statusEl=document.getElementById('status'), promptEl=document.getElementById('prompt'), sendBtn=document.getElementById('send'), splitter=document.getElementById('splitter'), workspace=document.querySelector('.workspace');
let savedSplit=localStorage.getItem('kilo-split-percent'); function setSplit(p){p=Math.max(25,Math.min(80,p));workspace.style.setProperty('--answer-width',p+'fr');workspace.style.gridTemplateColumns='minmax(0, '+p+'fr) 8px minmax(240px, '+(100-p)+'fr)';localStorage.setItem('kilo-split-percent',String(p));} if(savedSplit)setSplit(Number(savedSplit)); let dragging=false; splitter.addEventListener('pointerdown',e=>{dragging=true;splitter.classList.add('dragging');splitter.setPointerCapture(e.pointerId);}); splitter.addEventListener('pointermove',e=>{if(!dragging)return;const r=workspace.getBoundingClientRect();setSplit((e.clientX-r.left)/r.width*100);}); function stopDrag(){dragging=false;splitter.classList.remove('dragging');} splitter.addEventListener('pointerup',stopDrag); splitter.addEventListener('pointercancel',stopDrag); splitter.addEventListener('keydown',e=>{if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;e.preventDefault();setSplit(Number(localStorage.getItem('kilo-split-percent')||63)+(e.key==='ArrowRight'?3:-3));});
let lastEvent=0, logText='';
const projectInput=document.getElementById('project'), projectOptions=document.getElementById('projectOptions'), toggleProjects=document.getElementById('toggleProjects');
let projectEdited=false;
projectInput.addEventListener('input',()=>{projectEdited=true;});
function renderRecentProjects(projects,current){projectOptions.replaceChildren();for(const path of projects||[]){const opt=document.createElement('button');opt.type='button';opt.className='project-option';opt.setAttribute('role','option');opt.textContent=path;opt.title=path;opt.addEventListener('click',()=>{projectInput.value=path;projectEdited=false;projectOptions.classList.remove('open');projectInput.focus();});projectOptions.appendChild(opt);}if(!(projects||[]).length){const empty=document.createElement('div');empty.className='project-option';empty.textContent='Noch keine gespeicherten Projekte';projectOptions.appendChild(empty);}}
toggleProjects.addEventListener('click',()=>{const opening=!projectOptions.classList.contains('open');projectOptions.classList.toggle('open',opening);if(opening)renderRecentProjects(window.recentProjectPaths||[],projectInput.value);});
projectInput.addEventListener('focus',()=>{if(projectOptions.classList.contains('open'))renderRecentProjects(window.recentProjectPaths||[],projectInput.value);});
document.addEventListener('click',e=>{if(!e.target.closest('.project-combo'))projectOptions.classList.remove('open');const link=e.target.closest('#answer a');if(link){e.preventDefault();window.open(link.href,'_blank','noopener,noreferrer');}});
async function init(){const r=await fetch('/api/state');const s=await r.json();projectInput.value=s.project;projectEdited=false;renderRecentProjects(s.recent_projects,s.project);update(s);}
function update(s){statusEl.textContent=s.status;sendBtn.disabled=s.busy;if(s.project&&!projectEdited&&document.activeElement!==projectInput)projectInput.value=s.project;if(s.recent_projects){window.recentProjectPaths=s.recent_projects;renderRecentProjects(s.recent_projects,s.project);}for(const e of s.events||[]){if(e.id<=lastEvent)continue;lastEvent=e.id;if(e.kind==='markdown'){const wasAtBottom=answerCard.scrollHeight-answerCard.scrollTop-answerCard.clientHeight<80;const current=answer.dataset.raw||'';const raw=current+(current?'\n\n':'')+e.text;answer.dataset.raw=raw;let html=window.marked?marked.parse(raw,{gfm:true,breaks:false}):'<pre>'+escapeHtml(raw)+'</pre>';answer.innerHTML=window.DOMPurify?DOMPurify.sanitize(html,{USE_PROFILES:{html:true}}):html;if(wasAtBottom)answerCard.scrollTop=answerCard.scrollHeight;}else{appendLogEntry(e.text);}}log.scrollTop=log.scrollHeight;}
function appendLogEntry(raw){logText+=raw+'\n';const row=document.createElement('div');row.className='log-entry';let ev=null;try{ev=JSON.parse(raw);}catch{}let title=raw,detail='',kind='';if(ev&&typeof ev==='object'){const type=String(ev.type||ev.event||ev.kind||'JSON-Ereignis');const part=ev.part&&typeof ev.part==='object'?ev.part:{};const state=part.state&&typeof part.state==='object'?part.state:{};const input=state.input||part.input||ev.input||ev.arguments||ev.args||part.arguments||part.args;const names={'tool_call':'Werkzeug wird aufgerufen','tool_result':'Werkzeug-Ergebnis','tool_use':'Werkzeug wird verwendet','tool':'Werkzeug-Aktion','error':'Fehler','completion':'Aufgabe abgeschlossen','finish':'Aufgabe beendet','text':'Textausgabe','command':'Befehl'};title=names[type]||type.replace(/[_-]+/g,' ');const tool=ev.tool_name||ev.name||part.tool_name||part.name||part.tool||state.tool;const isQuestion=/question|ask_user|follow.?up/i.test([type,tool||'',ev.name||''].join(' '));const summary=ev.message||ev.error||part.message||part.text||ev.command||part.command||ev.status||part.status;if(isQuestion){title='Rückfrage von Kilo Code';detail=input!==undefined?JSON.stringify(input,null,2):(summary&&typeof summary==='string'?summary:JSON.stringify(ev,null,2));kind=' log-question';}else{if(tool)title+=' · '+String(tool);if(summary&&typeof summary==='string')detail=summary;else if(summary!==undefined)detail=JSON.stringify(summary);kind=(/error|fail/i.test(type)||ev.error)?' log-error':(/tool|command|action/i.test(type)?' log-action':(/complete|finish|success/i.test(type)?' log-success':''));}}else{detail=raw;}row.className+=kind;const heading=document.createElement('div');heading.className='log-title';heading.textContent=title;row.appendChild(heading);if(detail){const body=document.createElement('div');body.className='log-detail';body.textContent=detail;row.appendChild(body);}if(ev){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='JSON-Details anzeigen';const pre=document.createElement('pre');pre.className='log-detail';pre.textContent=JSON.stringify(ev,null,2);details.append(summary,pre);row.appendChild(details);}log.appendChild(row);}
function escapeHtml(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function poll(){try{const r=await fetch('/api/state');update(await r.json());}catch(e){}setTimeout(poll,700);}
async function send(){const text=promptEl.value.trim();if(!text)return;const project=document.getElementById('project').value.trim();const autoApprove=document.getElementById('autoApprove').checked;const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:text,project,auto_approve:autoApprove})});const s=await r.json();if(!r.ok){alert(s.error||'Anfrage fehlgeschlagen');return;}promptEl.value='';projectEdited=false;update(s);}
sendBtn.onclick=send;document.getElementById('newSession').onclick=async()=>{const project=projectInput.value.trim();if(!project){alert('Bitte zuerst einen Projektordner angeben.');return;}if(sendBtn.disabled){alert('Bitte warte, bis die aktuelle Aufgabe beendet ist.');return;}const r=await fetch('/api/new-session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project})});const s=await r.json();if(!r.ok){alert(s.error||'Session konnte nicht zurückgesetzt werden.');return;}update(s);const url=window.location.href;const tab=window.open(url,'_blank');if(!tab){statusEl.textContent='Pop-up blockiert – neuen Tab bitte erlauben';alert('Der Browser hat das Öffnen eines neuen Tabs blockiert. Bitte Pop-ups für diese lokale Web-App erlauben und erneut auf „Neue Session“ klicken.');return;}statusEl.textContent='Neuer Tab für die nächste Session geöffnet';};promptEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
document.getElementById('copy').onclick=async()=>{await navigator.clipboard.writeText(answer.dataset.raw||answer.innerText);statusEl.textContent='Antwort kopiert';};
document.getElementById('save').onclick=()=>{const blob=new Blob([logText+'\n\n--- KILO-CODE-ANTWORT ---\n'+(answer.dataset.raw||answer.innerText)],{type:'text/plain;charset=utf-8'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='kilo-kommunikation.txt';a.click();URL.revokeObjectURL(a.href);};
init();poll();
</script></body></html>'''

def add(kind, text):
    with LOCK:
        STATE["events"].append({"id": len(STATE["events"])+1, "kind": kind, "text": str(text)})
        if len(STATE["events"]) > 3000:
            STATE["events"] = STATE["events"][-2000:]

DEFAULT_MODEL = "schleissheimer-native/schleissheimer/charlie"
MODEL_PROVIDER_MAP = {"schleissheimer": "schleissheimer-native"}


def map_model(model):
    provider, _, rest = model.partition("/")
    if provider in MODEL_PROVIDER_MAP and rest:
        return MODEL_PROVIDER_MAP[provider] + "/" + rest
    return model


def _model_from_state(data):
    if isinstance(data, dict):
        for name in ("recentModels", "favoriteModels"):
            items = data.get(name)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        provider = item.get("providerID")
                        model = item.get("modelID")
                        if isinstance(provider, str) and isinstance(model, str) and provider and model:
                            return provider + "/" + model
        for value in data.values():
            found = _model_from_state(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _model_from_state(value)
            if found:
                return found
    return None


def find_model():
    override = (os.environ.get("KILO_MODEL") or "").strip()
    if override:
        return map_model(override)
    try:
        db = os.path.join(os.environ.get("APPDATA", ""), "Code", "User", "globalStorage", "state.vscdb")
        if os.path.isfile(db):
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            try:
                for _key, value in con.execute("SELECT key, value FROM ItemTable WHERE key LIKE '%kilo%'"):
                    text = value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)
                    try:
                        data = json.loads(text)
                    except (ValueError, TypeError):
                        continue
                    model = _model_from_state(data)
                    if model:
                        return map_model(model)
            finally:
                con.close()
    except Exception:
        pass
    return DEFAULT_MODEL


def run_task(prompt, project, origin="WEB-CHAT", auto_approve=False):
    with LOCK:
        if STATE["busy"]:
            add("log", "Anfrage abgelehnt: Kilo Code verarbeitet bereits eine Aufgabe.")
            return
        STATE["busy"] = True
        STATE["status"] = "Kilo Code arbeitet …"
        STATE["project"] = project
    settings = load_settings()
    project_key = os.path.normcase(os.path.abspath(project))
    session_id = settings.get("project_sessions", {}).get(project_key, "")
    with LOCK: STATE["session_id"] = session_id
    add("log", "\n[%s] ANFRAGE — QUELLE: %s" % (time.strftime("%H:%M:%S"), origin))
    add("log", "Projektordner: " + project)
    add("log", "Session: " + (session_id if session_id else "Neue Session"))
    add("log", "Prompt:\n" + prompt)
    add("markdown", "## Anfrage\n\n" + prompt + "\n\n---\n\n## Antwort von Kilo Code\n")
    try:
        cli = find_cli()
        if not os.path.isfile(cli):
            raise FileNotFoundError("Kilo CLI nicht gefunden: " + cli + " (Erweiterungsordner: " + EXTENSIONS_ROOT + ")")
        if not os.path.isdir(project):
            raise FileNotFoundError("Projektordner existiert nicht: " + project)
        model = find_model()
        command = [cli, "run", "--dir", project, "--format", "json", "--print-logs", "--model", model]
        add("log", "Modell: " + model)
        if auto_approve:
            command.append("--auto")
            add("log", "Auto-Approve ist für diesen Auftrag aktiviert.")
        else:
            add("log", "Auto-Approve ist deaktiviert. Die CLI kann Berechtigungsfragen im nicht-interaktiven Modus nicht an die Web-App weiterreichen.")
        if session_id:
            command.extend(["--session", session_id])
        command.append(prompt)
        add("log", "CLI-Befehl:\n" + subprocess.list2cmdline(command))
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout:
            line = line.rstrip()
            if not line: continue
            try:
                event = json.loads(line)
                found_session = extract_session_id(event)
                if found_session and found_session != session_id:
                    session_id = found_session
                    save_session(project, session_id)
                    with LOCK: STATE["session_id"] = session_id
                    add("log", "Session-ID gespeichert: " + session_id)
                kind = event.get("type", "")
                part = event.get("part") or {}
                if kind == "text":
                    add("markdown", part.get("text", ""))
                elif kind == "error":
                    add("log", "FEHLER: " + json.dumps(event, ensure_ascii=False))
                else:
                    add("log", json.dumps(event, ensure_ascii=False))
            except json.JSONDecodeError:
                add("log", line)
        code = proc.wait()
        add("log", "Prozess beendet, Exit-Code: " + str(code))
        with LOCK: STATE["status"] = "Fertig" if code == 0 else "Fehlercode " + str(code)
    except Exception as exc:
        add("log", "FEHLER: " + str(exc))
        add("markdown", "\n\n## Fehler\n\n" + str(exc))
        with LOCK: STATE["status"] = "Fehler"
    finally:
        with LOCK: STATE["busy"] = False

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def reply(self, status, obj, content_type="application/json; charset=utf-8"):
        data = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/state":
            with LOCK: snapshot = dict(STATE, events=list(STATE["events"]))
            self.reply(200, snapshot)
        else: self.reply(404, {"error":"Not found"})
    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/new-session"): return self.reply(404, {"error":"Not found"})
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
        except Exception: return self.reply(400, {"error":"Ungültige Anfrage"})
        prompt = str(body.get("prompt", "")).strip()
        project = str(body.get("project", DEFAULT_PROJECT)).strip()
        auto_approve = bool(body.get("auto_approve", False))
        with LOCK:
            if STATE["busy"]: return self.reply(409, {"error":"Kilo Code verarbeitet bereits eine Aufgabe."})
        if path == "/api/new-session":
            if not os.path.isdir(project):
                return self.reply(400, {"error":"Der angegebene Projektordner existiert nicht."})
            try:
                clear_session(project)
                with LOCK:
                    STATE["project"] = project
                    STATE["session_id"] = ""
                    STATE["status"] = "Neue Session bereit"
                add("log", "Neue Session für Projekt vorbereitet: " + project)
                with LOCK: snapshot = dict(STATE, events=list(STATE["events"]))
                return self.reply(200, snapshot)
            except OSError as exc:
                return self.reply(500, {"error":"Session konnte nicht zurückgesetzt werden: " + str(exc)})
        if not prompt: return self.reply(400, {"error":"Bitte eine Aufgabe eingeben."})
        if not os.path.isdir(project):
            return self.reply(400, {"error":"Der angegebene Projektordner existiert nicht."})
        try:
            save_project(project)
            with LOCK:
                STATE["project"] = project
                STATE["recent_projects"] = load_settings()["recent_projects"]
        except OSError as exc:
            return self.reply(500, {"error":"Projektordner konnte nicht gespeichert werden: " + str(exc)})
        threading.Thread(target=run_task, args=(prompt, project, "WEB-CHAT", auto_approve), daemon=True).start()
        time.sleep(.05)
        with LOCK: snapshot = dict(STATE, events=list(STATE["events"]))
        self.reply(202, snapshot)

def inbox_watch():
    while True:
        try:
            if os.path.isfile(INBOX) and not STATE["busy"]:
                with open(INBOX, "r", encoding="utf-8") as f: prompt = f.read().strip()
                if prompt:
                    os.remove(INBOX)
                    run_task(prompt, STATE["project"], "CHATGPT-INBOX")
        except Exception as exc: add("log", "Inbox-Fehler: " + str(exc))
        time.sleep(.5)

if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = False
    threading.Thread(target=inbox_watch, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Kilo Code Web-App: http://127.0.0.1:8765", flush=True)
    server.serve_forever()


