"""TALLER URBANO Fase 2.2.2. Simulacion por defecto; escritura solo con --guardar.
Seleccion conservadora de asignaciones; no confunde el ultimo movimiento con el documento activo.
"""
import argparse, contextlib, csv, io, json, re, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import motor_egob_fase_2_1_13 as motor

CAMPOS=['proyecto_id','tramite_origen','resultado','criterio_seleccion','confianza_seleccion',
        'tramite_activo','responsable','dependencia','numero_asignaciones','responsables_activos',
        'ultima_accion','fecha_ultimo_movimiento_ecuador','fecha_ultimo_movimiento_utc',
        'ultimo_destinatario','observaciones']

def proyectos_paginados(sb,key,tamano=500):
    inicio=0
    while True:
        r=motor.check(requests.get(f'{sb}/rest/v1/proyectos',headers=motor.headers(key),
            params={'select':'id,item,proyecto,tramite','order':'id.asc','limit':tamano,'offset':inicio},timeout=45))
        lote=r.json()
        if not isinstance(lote,list):raise RuntimeError('Respuesta inesperada de proyectos')
        if not lote:break
        yield from lote
        inicio+=len(lote)
        if len(lote)<tamano:break

def tramite_simple(valor):
    valor=str(valor or '').strip()
    if re.fullmatch(r'\d{6,9}',valor):return valor
    m=re.fullmatch(r'https?://[^\s]+/issues/(\d{6,9})/?',valor)
    return m.group(1) if m else None

def movimientos(response):
    eventos=[]
    for part in motor.html_parts(motor.response_obj(response)):
        soup=BeautifulSoup(part,'html.parser')
        for tabla in soup.select('table'):
            cab=None
            for idx,tr in enumerate(tabla.find_all('tr')):
                cells=tr.find_all(['th','td'],recursive=False)
                vals=[motor.clean(c.get_text(' ',strip=True)) for c in cells]
                keys=[motor.norm(v) for v in vals]
                if {'fecha y hora','accion','de','para'}.issubset(keys):cab=keys;continue
                if cab is None or len(vals)!=len(cab):continue
                d=dict(zip(cab,vals));fecha=motor.date_iso(d.get('fecha y hora'))
                if not fecha:continue
                accion=motor.norm(d.get('accion'))
                comentario=motor.clean(d.get('comentario'))
                # El historial contiene eventos de encargos y subrogaciones: no se interpretan como reasignacion.
                if accion in ('reasignado','reasignacion') and any(p in motor.norm(comentario) for p in ('encargo transferido','subrogacion','transferencia de encargo','reemplazo temporal')):
                    continue
                eventos.append({'indice':idx,'fecha':fecha,'accion':motor.clean(d.get('accion')),
                    'de':motor.clean(d.get('de')),'para':motor.clean(d.get('para')),
                    'comentario':comentario,'issue_explicito':motor.linked_issue_from_row(tr)})
    return sorted(eventos,key=lambda e:(e['fecha'],e['indice']),reverse=True)

def arbol_documentos(response):
    resultado={}
    for part in motor.html_parts(motor.response_obj(response)):
        soup=BeautifulSoup(part,'html.parser')
        for a in soup.select('a[href]'):
            m=re.search(r'/issues/(\d{6,9})(?:[/?#]|$)',a.get('href',''))
            if m:resultado[m.group(1)]=motor.clean(a.get_text(' ',strip=True))
    return resultado

def personas_unicas(rows):
    return sorted({motor.norm(r['name']) for r in rows if r.get('name')})

def seleccionar(rows,docs,eventos):
    """Solo alta confianza cuando hay evidencia de documento y destinatario.
    Si el ultimo evento es archivo o hay multiples destinatarios, se bloquea.
    """
    valid=[];seen=set()
    for r in rows:
        key=(r.get('issue'),motor.norm(r.get('name')),motor.norm(r.get('area')))
        if key in seen:continue
        seen.add(key)
        if r.get('issue') in docs and r.get('name') and r.get('area'):valid.append(r)
    ultimo=eventos[0] if eventos else None
    resultado={'asignacion':None,'criterio':'SIN_EVIDENCIA','confianza':'REVISION',
               'observacion':'No hay movimientos o asignaciones verificables.'}
    if not ultimo:return resultado
    accion=motor.norm(ultimo['accion']);dest=ultimo.get('para','')
    if accion in ('reasignado','reasignacion') and dest:
        candidatos=[r for r in valid if motor.same_person(r['name'],dest)]
        # Coincidencia temporal exacta, en hora Ecuador, es evidencia adicional fuerte.
        fecha=[r for r in candidatos if r.get('received')==ultimo['fecha']]
        if len({r['issue'] for r in fecha})==1:
            return {'asignacion':fecha[0],'criterio':'REASIGNACION_DESTINATARIO_FECHA',
                    'confianza':'ALTA','observacion':'Destinatario y hora de recepcion coinciden; documento en arbol.'}
        if ultimo.get('issue_explicito'):
            exactos=[r for r in candidatos if r['issue']==ultimo['issue_explicito']]
            if len(exactos)==1:return {'asignacion':exactos[0],'criterio':'REASIGNACION_ID_EXPLICITO',
                    'confianza':'ALTA','observacion':'ID explicito en fila HTML y destinatario coinciden.'}
        if len({r['issue'] for r in candidatos})==1:
            return {'asignacion':candidatos[0],'criterio':'REASIGNACION_UNICO_DOCUMENTO',
                    'confianza':'ALTA','observacion':'Unico documento activo del destinatario dentro del arbol.'}
        resultado.update(criterio='REASIGNACION_AMBIGUA',observacion=f'{len(candidatos)} asignaciones del destinatario; sin coincidencia temporal o ID unico.')
        return resultado
    if accion=='registro' and dest:
        # El numero administrativo registrado debe coincidir exactamente con el titulo del arbol.
        m=re.search(r'\b(GADMR-[A-Z0-9-]+-20\d{2}-\d+-[A-Z])\b',ultimo['comentario'],re.I)
        if m:
            codigo=m.group(1).upper()
            ids=[id_ for id_,titulo in docs.items() if codigo in titulo.upper()]
            candidatos=[r for r in valid if r['issue'] in ids and motor.same_person(r['name'],dest)]
            if len({r['issue'] for r in candidatos})==1:
                return {'asignacion':candidatos[0],'criterio':'REGISTRO_CODIGO_DOCUMENTO_DESTINATARIO',
                        'confianza':'ALTA','observacion':'Codigo de memorando en comentario, arbol y asignacion coincidentes; fecha de registro distinta de recepcion.'}
        resultado.update(criterio='REGISTRO_NO_VINCULADO',observacion='No se pudo vincular de manera unica el registro al destinatario y documento activo.')
        return resultado
    resultado.update(criterio='ULTIMA_ACCION_NO_ASIGNACION',
        observacion=f'Ultima accion {ultimo["accion"]}; {len(personas_unicas(valid))} responsables distintos con asignaciones activas. No equivale a cierre global.')
    return resultado

def consultar(session,issue,proyecto):
    base=f'{motor.EGOB_BASE}/issues/{issue}'
    active=motor.check(session.get(base+'/get_in_process',timeout=45))
    route=motor.check(session.get(base+'/render_issue_route',timeout=45))
    tree=motor.check(session.get(base+'/get_issue_tree',timeout=45))
    docs=arbol_documentos(tree)
    rows=motor.active_rows(active)
    eventos=movimientos(route)
    seleccion=seleccionar(rows,docs,eventos)
    ultimo=eventos[0] if eventos else None
    elegido=seleccion['asignacion']
    datos={'proyecto_id':proyecto['id'],'tramite_origen':issue,
        'resultado':'revision' if not elegido else 'simulado',
        'criterio_seleccion':seleccion['criterio'],'confianza_seleccion':seleccion['confianza'],
        'tramite_activo':elegido['issue'] if elegido else '',
        'responsable':elegido['name'] if elegido else '',
        'dependencia':elegido['area'] if elegido else '',
        'numero_asignaciones':len(rows),
        'responsables_activos':' | '.join(sorted({r['name'] for r in rows})),
        'ultima_accion':ultimo['accion'] if ultimo else '',
        'fecha_ultimo_movimiento_ecuador':ultimo['fecha'] if ultimo else '',
        'fecha_ultimo_movimiento_utc':motor.fecha_egob_a_utc(ultimo['fecha']) if ultimo else '',
        'ultimo_destinatario':ultimo['para'] if ultimo else '',
        'observaciones':seleccion['observacion']}
    payload=None
    if elegido and ultimo:
        # No se afirma que el documento elegido sea el del movimiento, salvo cuando el criterio lo prueba.
        descripcion=ultimo['accion']+' de '+(ultimo['de'] or 'origen no identificado')
        if ultimo['para']:descripcion+=' a '+ultimo['para']
        payload={'proyecto_id':proyecto['id'],'tramite_origen':issue,
            'tramite_activo':elegido['issue'],'tipo_documento':elegido.get('tipo'),
            'responsable':elegido['name'],'cargo_area':elegido['area'],
            'estado':'EN PROCESO','ultimo_movimiento':descripcion,
            'fecha_ultimo_movimiento':motor.fecha_egob_a_utc(ultimo['fecha']),
            'url_egob':base,'sincronizado_at':datetime.now(timezone.utc).isoformat(),
            'error_sincronizacion':None}
        # No enviamos asunto ni fecha_recibido: preservamos datos anteriores.
    return datos,payload

def guardar(sb,key,payload):
    r=requests.post(f'{sb}/rest/v1/egob_estado_actual',
        params={'on_conflict':'proyecto_id'},
        headers=motor.headers(key,'resolution=merge-duplicates,return=representation'),
        json=payload,timeout=45)
    motor.check(r)

def main():
    p=argparse.ArgumentParser(description='Fase 2.2.2: seleccion conservadora de asignaciones')
    p.add_argument('--guardar',action='store_true');p.add_argument('--limite',type=int,default=0)
    p.add_argument('--pausa',type=float,default=.7)
    args=p.parse_args()
    if args.limite<0 or args.pausa<0:p.error('Limite y pausa deben ser >= 0')
    sb=motor.ask('Supabase URL','SUPABASE_URL',default=motor.DEFAULT_SB).rstrip('/')
    if sb.endswith('/rest/v1'):sb=sb[:-8]
    key=motor.ask('Supabase SERVICE_ROLE legacy key','SUPABASE_SERVICE_ROLE_KEY',secret=True)
    user=motor.ask('Usuario eGOB','EGOB_USERNAME')
    pwd=motor.ask('Contrasena eGOB','EGOB_PASSWORD',secret=True)
    if not all([key,user,pwd]):raise RuntimeError('Faltan credenciales')
    proyectos=list(proyectos_paginados(sb,key))
    elegibles=[(x,tramite_simple(x.get('tramite'))) for x in proyectos]
    omitidos=[x for x,t in elegibles if not t]
    elegibles=[(x,t) for x,t in elegibles if t]
    if args.limite:elegibles=elegibles[:args.limite]
    print(f'Proyectos: {len(proyectos)} | Elegibles: {len(elegibles)} | Omitidos: {len(omitidos)}')
    print('Modo:', 'GUARDADO (solo confianza ALTA)' if args.guardar else 'SIMULACION, SIN ESCRITURAS')
    if args.guardar and input('Escriba GUARDAR para autorizar: ').strip()!='GUARDAR':print('Cancelado');return
    ruta=Path('resultado_fase_2_2_2_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.csv')
    ses=requests.Session();ses.headers['User-Agent']='TallerUrbano-eGOB-Sync/2.2.2'
    logged=False;counts=Counter()
    with ruta.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=CAMPOS);writer.writeheader()
        for n,(proyecto,issue) in enumerate(elegibles,1):
            print(f'[{n}/{len(elegibles)}] Proyecto {proyecto["id"]} | eGOB {issue}',end=' | ',flush=True)
            row={k:'' for k in CAMPOS};row.update(proyecto_id=proyecto['id'],tramite_origen=issue)
            try:
                if not logged:motor.login(ses,issue,user,pwd);logged=True
                row,payload=consultar(ses,issue,proyecto)
                if args.guardar and payload and row['confianza_seleccion']=='ALTA':
                    guardar(sb,key,payload);row['resultado']='guardado'
                print(row['resultado'].upper(),row['criterio_seleccion'],row['tramite_activo'])
            except Exception as e:
                row['resultado']='error';row['observaciones']=str(e).replace('\n',' ')[:400]
                if 'cas/login' in str(e).lower() or 'autentic' in str(e).lower():logged=False
                print('ERROR',row['observaciones'])
            counts[row['resultado']]+=1
            writer.writerow(row);f.flush()
            if args.pausa and n<len(elegibles):time.sleep(args.pausa)
    print('RESUMEN:',json.dumps(counts,ensure_ascii=False))
    print('CSV:',ruta.resolve())
    print('Casos REVISION o ERROR: sin modificaciones en Supabase.')

if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):print('\nInterrumpido');sys.exit(130)
    except Exception as e:print('ERROR GENERAL:',e);sys.exit(1)
