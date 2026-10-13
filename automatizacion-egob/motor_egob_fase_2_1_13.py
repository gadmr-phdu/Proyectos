import os, re, sys, json, getpass, unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

EGOB_BASE='https://egobedoc.gadmriobamba.gob.ec:8081'
DEFAULT_SB='https://vpihwoyucfkqocytmfbn.supabase.co'
ECUADOR_TZ=ZoneInfo('America/Guayaquil')

def ask(label,env=None,secret=False,default=None):
    value=os.getenv(env,'').strip() if env else ''
    if value: return value
    value=getpass.getpass(label+': ') if secret else input(label+(f' [{default}]' if default else '')+': ')
    return value.strip() or default or ''

def clean(x): return re.sub(r'\s+',' ',str(x or '')).strip()
def norm(x):
    x=unicodedata.normalize('NFKD',clean(x)).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+',' ',x).strip()

def headers(key,prefer='return=representation'):
    return {'apikey':key,'Authorization':f'Bearer {key}','Content-Type':'application/json','Prefer':prefer}

def check(r):
    if not r.ok: raise RuntimeError(f'HTTP {r.status_code} en {r.url}\nRespuesta: {r.text[:700]}')
    return r

def login(session,issue,username,password):
    protected=f'{EGOB_BASE}/issues/{issue}/get_in_process'
    r=check(session.get(protected,timeout=40))
    if 'cas/login' not in r.url and not BeautifulSoup(r.text,'html.parser').select_one('input[name=execution]'): return
    soup=BeautifulSoup(r.text,'html.parser'); form=soup.find('form')
    if not form: raise RuntimeError('No se encontro formulario CAS.')
    payload={x.get('name'):x.get('value','') for x in form.select('input[name]')}
    payload.update(username=username,password=password);payload.setdefault('_eventId','submit')
    post=session.post(urljoin(r.url,form.get('action') or r.url),data=payload,timeout=40)
    if 'cas/login' in post.url: raise RuntimeError('CAS no acepto el inicio de sesion.')
    verified=check(session.get(protected,timeout=40))
    if 'cas/login' in verified.url: raise RuntimeError('Sesion eGOB no autenticada.')

def response_obj(response):
    try: return response.json()
    except ValueError: return response.text

def html_parts(obj):
    if isinstance(obj,str):
        if '<' in obj and '>' in obj: yield obj
    elif isinstance(obj,dict):
        for v in obj.values(): yield from html_parts(v)
    elif isinstance(obj,list):
        for v in obj: yield from html_parts(v)

def tables(obj):
    for part in html_parts(obj):
        soup=BeautifulSoup(part,'html.parser')
        for table in soup.find_all('table'):
            rows=table.find_all('tr')
            if not rows: continue
            for i,row in enumerate(rows):
                hs=[norm(x.get_text(' ',strip=True)) for x in row.find_all(['th','td'],recursive=False)]
                if not hs: continue
                if any(h in ('activo','usuario','recibido','tipo','fecha y hora','accion','de','para') for h in hs):
                    for data in rows[i+1:]:
                        cells=[clean(x.get_text(' ',strip=True)) for x in data.find_all(['th','td'],recursive=False)]
                        if len(cells)>=len(hs): yield dict(zip(hs,cells))
                    break

def recursive_dicts(obj):
    if isinstance(obj,dict):
        yield {norm(k):v for k,v in obj.items()}
        for v in obj.values(): yield from recursive_dicts(v)
    elif isinstance(obj,list):
        for v in obj: yield from recursive_dicts(v)

def value(d,*keys):
    for k in keys:
        v=d.get(norm(k))
        if isinstance(v,(str,int,float)) and clean(v): return clean(v)
    return None

def date_iso(s):
    if not s:return None
    m=re.search(r'20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?',s)
    if not m:return None
    try:return datetime.fromisoformat(m.group().replace(' ','T')).isoformat()
    except ValueError:return None

def fecha_egob_a_utc(fecha):
    """Interpreta las fechas sin zona de eGOB como hora Ecuador continental.

    No modifica fechas que ya contienen zona horaria.
    """
    if not fecha:
        return None
    dt=datetime.fromisoformat(fecha.replace(' ', 'T'))
    if dt.tzinfo is None:
        dt=dt.replace(tzinfo=ECUADOR_TZ)
    return dt.astimezone(timezone.utc).isoformat()

def parse_state(response):
    obj=response_obj(response)
    candidates=list(tables(obj))+list(recursive_dicts(obj))
    result={'tramite_activo':None,'tipo_documento':None,'asunto':None,'responsable':None,'cargo_area':None,'fecha_recibido':None,'estado':'POR VERIFICAR'}
    for d in candidates:
        active=value(d,'activo','tramite activo','numero de tramite')
        user=value(d,'usuario','responsable','asignado a','funcionario')
        received=value(d,'recibido','fecha recibido','fecha de recepcion')
        if not (active and user and re.search(r'\b\d{6,9}\b',active)):continue
        match=re.search(r'\b\d{6,9}\b',active)
        person=re.match(r'^(.*?)\s*\(\s*(.*?)\s*\)\s*$',user)
        name=clean(person.group(1) if person else user)
        area=clean(person.group(2) if person else (value(d,'dependencia','unidad administrativa','area','cargo','unidad') or ''))
        if not name or re.search(r'\d{6,}|#|memorando|tramite',name,re.I):continue
        result.update(tramite_activo=match.group(),tipo_documento=value(d,'tipo','tipo documento','tipo de documento'),responsable=name,cargo_area=area or None,fecha_recibido=date_iso(received),estado='EN PROCESO')
        return result
    # Do not infer current assignee from the last historical movement.
    return result

# La ruta de eGOB incluye movimientos operativos y cambios administrativos de encargo.
# El destinatario efectivo de una reasignacion se extrae de la transicion, NO del autor.
ADMIN_PATTERNS = ('encargo transferido', 'subrogacion', 'subrogación',
                  'reemplazo temporal', 'transferencia de encargo')
ASSIGN_RE = re.compile(r'asignado\s+ha\s+cambiado\s+de\s+(.+?)\s+a\s+(.+?)(?=(?:\s*[.;]|\s+reasignaci[oó]n\s+por\s+encargo|$))', re.I)
HEADER_RE = re.compile(r'(?P<issue>\d{6,9})\s*[›»)]?\s*#(?P<order>\d+)\s+(?P<actor>.+?)\s+(?P<date>20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2})',re.I)

def linked_issue_from_row(tr):
    """Only explicit issue links or row attributes, never document mentions in comments."""
    ids=set()
    for node in [tr]+tr.find_all(True):
        for key in ('data-issue-id','data-issue','data-tramite-id'):
            val=node.get(key)
            if val and re.fullmatch(r'\d{6,9}',str(val)): ids.add(str(val))
        if node.name=='a':
            href=node.get('href','')
            m=re.search(r'/issues/(\d{6,9})(?:/|[?#]|$)',href)
            if m: ids.add(m.group(1))
    return next(iter(ids)) if len(ids)==1 else None


def active_rows(response):
    """Read each active assignment, preserving its user and received date."""
    result=[]
    for d in list(tables(response_obj(response)))+list(recursive_dicts(response_obj(response))):
        active=value(d,'activo','tramite activo','numero de tramite')
        user=value(d,'usuario','responsable','asignado a','funcionario')
        if not active or not user: continue
        match=re.search(r'\b\d{6,9}\b',active)
        if not match:continue
        person=re.match(r'^(.*?)\s*\(\s*(.*?)\s*\)\s*$',user)
        name=clean(person.group(1) if person else user)
        area=clean(person.group(2) if person else (value(d,'dependencia','unidad administrativa','area','cargo','unidad') or ''))
        if not name or re.search(r'\d{6,}|#|memorando|tramite',name,re.I):continue
        result.append({'issue':match.group(),'name':name,'area':area or None,
                       'received':date_iso(value(d,'recibido','fecha recibido','fecha de recepcion')),
                       'tipo':value(d,'tipo','tipo documento','tipo de documento')})
    return result


def same_person(a,b):
    """eGOB may display surname-first in tables and given-name-first in active status."""
    aa=norm(a).split(); bb=norm(b).split()
    return len(aa)>=3 and sorted(aa)==sorted(bb)


def match_active_assignment(rows,movement,tree_ids):
    """Require unique active issue, matching user and membership in the issue tree.

    Received is NOT used to filter: it may predate the latest reassignment.
    """
    valid=[r for r in rows if same_person(r['name'],movement['destino'])
           and r['issue'] in tree_ids and r.get('area')]
    distinct={r['issue'] for r in valid}
    if len(distinct)!=1:return None
    issue=next(iter(distinct))
    areas={norm(r['area']) for r in valid if r['issue']==issue}
    if len(areas)!=1:return None
    return next(r for r in valid if r['issue']==issue)


def tree_issue_ids(response):
    ids=set()
    for html in html_parts(response_obj(response)):
        soup=BeautifulSoup(html,'html.parser')
        for a in soup.select('a[href]'):
            m=re.search(r'/issues/(\d{6,9})(?:[/?#]|$)',a.get('href',''))
            if m:ids.add(m.group(1))
        for tag in soup.select('[data-issue-id]'):
            val=str(tag.get('data-issue-id',''))
            if re.fullmatch(r'\d{6,9}',val):ids.add(val)
    return ids


def operational_movement(response):
    """Movimientos reales por fila, sin cruzar columnas ni inventar IDs."""
    obj=response_obj(response)
    events=[]
    for part in html_parts(obj):
        soup=BeautifulSoup(part,'html.parser')
        for table in soup.find_all('table'):
            header=None
            for row_index,tr in enumerate(table.find_all('tr')):
                cells=tr.find_all(['th','td'],recursive=False)
                vals=[clean(c.get_text(' ',strip=True)) for c in cells]
                keys=[norm(v) for v in vals]
                if {'fecha y hora','accion','de','para'}.issubset(set(keys)):
                    header=keys
                    continue
                if not header or len(vals)!=len(header):continue
                d=dict(zip(header,vals))
                dt=date_iso(d.get('fecha y hora'))
                action=norm(d.get('accion'))
                origin=clean(d.get('de'))
                dest=clean(d.get('para'))
                note=clean(d.get('comentario'))
                if not dt or action not in ('reasignado','reasignacion'):continue
                if not origin or not dest:continue
                if any(x in norm(note) for x in ('encargo transferido','subrogacion','transferencia de encargo','reemplazo temporal')):continue
                events.append({'fecha':dt,'accion':'Reasignación','origen':origin,'destino':dest,
                    'tramite':linked_issue_from_row(tr),'cargo_destino':None,'fuente':'tabla HTML','indice':row_index,
                    'area_movimiento':clean(d.get('area')) or None,
                    'comentario':note[:250]})
    events.sort(key=lambda e:(e['fecha'],e['indice']),reverse=True)
    print('\nULTIMAS REASIGNACIONES DETECTADAS (verifique contra eGOB):')
    for e in events[:8]:
        print('  {fecha} | {origen} -> {destino} | area: {area_movimiento}'.format(**e))
    return events[0] if events else None


def main():
    print('\nTALLER URBANO - eGOB FASE 2.1.13 CORRECCION DE ZONA HORARIA\n')
    sb=ask('Supabase URL','SUPABASE_URL',default=DEFAULT_SB).rstrip('/')
    if sb.endswith('/rest/v1'):sb=sb[:-8]
    key=ask('Supabase SERVICE_ROLE legacy key','SUPABASE_SERVICE_ROLE_KEY',secret=True)
    username=ask('Usuario eGOB','EGOB_USERNAME')
    password=ask('Contrasena eGOB','EGOB_PASSWORD',secret=True)
    issue=ask('Tramite de prueba',default='1243361')
    if not re.fullmatch(r'\d{6,9}',issue):raise RuntimeError('El tramite debe ser numerico.')
    if not all([key,username,password]):raise RuntimeError('Faltan credenciales.')
    projects=check(requests.get(f'{sb}/rest/v1/proyectos',headers=headers(key),params={'select':'id,item,proyecto,tramite','tramite':f'eq.{issue}','limit':1},timeout=30)).json()
    if not projects:raise RuntimeError(f'No se encontro tramite {issue} en proyectos.')
    project=projects[0];print(f"Proyecto: ITEM {project['item']} - {project['proyecto']}")
    session=requests.Session();session.headers['User-Agent']='TallerUrbano-eGOB-Sync/2.1.13'
    login(session,issue,username,password);print('Autenticacion eGOB: OK')
    active_response=check(session.get(f'{EGOB_BASE}/issues/{issue}/get_in_process',timeout=40))
    state=parse_state(active_response)
    movement=operational_movement(check(session.get(f'{EGOB_BASE}/issues/{issue}/render_issue_route',timeout=40)))
    
    if not movement:
        print('AVISO: No se encontro reasignacion operativa verificable. No se guardara.');return
    print('\nUltima reasignacion operativa:',json.dumps(movement,ensure_ascii=False,indent=2))
    # Match latest reassignment with current assignee and linked document tree.
    tree_ids=tree_issue_ids(check(session.get(f'{EGOB_BASE}/issues/{issue}/get_issue_tree',timeout=40)))
    rows=active_rows(active_response)
    assignment=match_active_assignment(rows,movement,tree_ids)
    if movement.get('tramite') and assignment and movement['tramite']!=assignment['issue']:
        print('BLOQUEADO: el ID explicito del movimiento difiere de la asignacion activa.')
        return
    # The route table has no issue IDs. In this case the matched active issue
    # is the currently assigned document, not a proven ID of the route row.
    state['responsable']=movement['destino']
    state['tramite_activo']=assignment['issue'] if assignment else None
    state['cargo_area']=assignment['area'] if assignment else None
    state['fecha_recibido']=None  # Reception may refer to an older assignment.
    state['tipo_documento']=assignment['tipo'] if assignment else None
    state['estado']='EN PROCESO' if assignment else 'POR VERIFICAR'
    print('\nVERIFICACION DE DOCUMENTO Y DEPENDENCIA:')
    print('  Documentos del arbol:',len(tree_ids))
    print('  Filas activas detectadas:',len(rows))
    print('  Coincidencia unica destinatario + documento + dependencia:',
          json.dumps(assignment,ensure_ascii=False) if assignment else 'No verificable')
    print('  Nota: tramite_activo identifica la asignacion actual; la tabla de ruta no identifica el documento por movimiento.')
    payload={**state,'proyecto_id':project['id'],'tramite_origen':issue,'ultimo_movimiento':movement['accion']+' de '+(movement['origen'] or 'origen no identificado')+' a '+movement['destino'],'fecha_ultimo_movimiento':fecha_egob_a_utc(movement['fecha']),'url_egob':f'{EGOB_BASE}/issues/{issue}','sincronizado_at':datetime.now(timezone.utc).isoformat(),'error_sincronizacion':None}
    print('\nZONA HORARIA: eGOB Ecuador',movement['fecha'],'=> Supabase UTC',payload['fecha_ultimo_movimiento'])
    print('\nDatos a guardar:');print(json.dumps(payload,ensure_ascii=False,indent=2))
    print('AVISO: Revise que el ultimo destinatario coincide con eGOB antes de guardar.')
    if not state['cargo_area']:print('AVISO: Dependencia no verificada; quedara NULL hasta obtenerla de eGOB.')
    if not state['responsable']:return
    if not state['tramite_activo'] or not state['cargo_area']:
        print('BLOQUEADO: falta ID del documento o dependencia comprobada. No se guardara.')
        print('Se requiere una respuesta eGOB que vincule el movimiento con el documento y la asignacion.')
        return
    confirm=input('\nConfirmas que la reasignacion coincide con eGOB y deseas guardar? [s/N]: ').strip().lower()
    if confirm not in ('s','si','sí'):print('Cancelado, sin cambios.');return
    r=requests.post(f'{sb}/rest/v1/egob_estado_actual',params={'on_conflict':'proyecto_id'},headers=headers(key,'resolution=merge-duplicates,return=representation'),json=payload,timeout=40)
    check(r);print('\nOK: estado actual guardado. No se modifico egob_trazabilidad.')

if __name__=='__main__':
    try:main()
    except Exception as exc:print('\nERROR:',exc);sys.exit(1)
