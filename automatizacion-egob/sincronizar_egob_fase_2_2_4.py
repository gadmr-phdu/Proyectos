"""TALLER URBANO 2.2.4: ultimo movimiento visible, omitiendo archivo y subrogaciones.
Por defecto simula. Guardado explicitamente autorizado; no infiere responsable ni estado.
"""
import argparse,csv,json,re,sys,time,os
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import motor_egob_fase_2_1_13 as motor
from sincronizar_egob_fase_2_2_2 import proyectos_paginados,tramite_simple

CAMPOS=['proyecto_id','proyecto','tramite_origen','resultado','ultima_accion','fecha_ultimo_movimiento_ecuador',
        'fecha_ultimo_movimiento_utc','de','para','dependencia','comentario','tramite_documento_identificado',
        'movimientos_totales','movimientos_excluidos','url_egob','observaciones']

# Las subrogaciones pueden aparecer como accion propia o como comentario de reasignacion.
EXCLUIR_ACCION=('archivad','archivacion','archivo','subrog','subrogacion')
EXCLUIR_TEXTO=('subrog','encargo transferido','transferencia de encargo','reemplazo temporal')

def excluir(evento):
    accion=motor.norm(evento['accion']);comentario=motor.norm(evento['comentario'])
    return any(x in accion for x in EXCLUIR_ACCION) or any(x in comentario for x in EXCLUIR_TEXTO)

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
                if {'fecha y hora','accion','de','para'}.issubset(set(keys)):
                    cab=keys;continue
                if cab is None or len(vals)!=len(cab):continue
                d=dict(zip(cab,vals));fecha=motor.date_iso(d.get('fecha y hora'))
                if not fecha:continue
                eventos.append({'fecha':fecha,'accion':motor.clean(d.get('accion')),
                    'de':motor.clean(d.get('de')),'para':motor.clean(d.get('para')),
                    'dependencia':motor.clean(d.get('area')),
                    'comentario':motor.clean(d.get('comentario')),
                    'issue':motor.linked_issue_from_row(tr),'indice':idx})
    return sorted(eventos,key=lambda e:(e['fecha'],e['indice']),reverse=True)

def evaluar(response,proyecto,issue):
    eventos=movimientos(response);visibles=[e for e in eventos if not excluir(e)]
    base=f'{motor.EGOB_BASE}/issues/{issue}'
    row={k:'' for k in CAMPOS}
    row.update(proyecto_id=proyecto['id'],proyecto=proyecto.get('proyecto') or '',tramite_origen=issue,
        url_egob=base,movimientos_totales=len(eventos),movimientos_excluidos=len(eventos)-len(visibles))
    if not visibles:
        row.update(resultado='sin_movimiento_visible',observaciones='No hay movimientos fechados distintos de archivo/subrogacion, o no se pudo interpretar el historial.')
        return row,None
    e=visibles[0];utc=motor.fecha_egob_a_utc(e['fecha'])
    row.update(resultado='simulado',ultima_accion=e['accion'],fecha_ultimo_movimiento_ecuador=e['fecha'],
        fecha_ultimo_movimiento_utc=utc,de=e['de'],para=e['para'],dependencia=e['dependencia'],
        comentario=e['comentario'],tramite_documento_identificado=e['issue'] or '',
        observaciones='Movimiento informativo; destinatario no equivale a responsable actual.')
    descripcion=e['accion']
    if e['de']:descripcion+=' | De: '+e['de']
    if e['para']:descripcion+=' | Para: '+e['para']
    if e['dependencia']:descripcion+=' | Area: '+e['dependencia']
    if e['comentario']:descripcion+=' | Comentario: '+e['comentario']
    # Solo columnas existentes de egob_estado_actual. No sobreescribir responsable,
    # cargo_area, tramite_activo, fecha_recibido ni estado con inferencias.
    payload={'proyecto_id':proyecto['id'],'tramite_origen':issue,
        'ultimo_movimiento':descripcion,'fecha_ultimo_movimiento':utc,
        'url_egob':base,'sincronizado_at':datetime.now(timezone.utc).isoformat(),
        'error_sincronizacion':None}
    return row,payload

def guardar(sb,key,payload):
    r=requests.post(f'{sb}/rest/v1/egob_estado_actual',
        params={'on_conflict':'proyecto_id'},
        headers=motor.headers(key,'resolution=merge-duplicates,return=representation'),
        json=payload,timeout=45)
    motor.check(r)

def main():
    p=argparse.ArgumentParser(description='Fase 2.2.4: ultimo movimiento sin archivo ni subrogaciones')
    p.add_argument('--guardar',action='store_true',help='Autorizar escritura de ultimo_movimiento en Supabase')
    p.add_argument('--sin-interaccion',action='store_true',help='Permitir ejecucion desatendida, exige EGOB_SYNC_AUTORIZADO=SI')
    p.add_argument('--limite',type=int,default=0);p.add_argument('--pausa',type=float,default=.7)
    args=p.parse_args()
    if args.limite<0 or args.pausa<0:p.error('Limite y pausa deben ser >= 0')
    sb=motor.ask('Supabase URL','SUPABASE_URL',default=motor.DEFAULT_SB).rstrip('/')
    if sb.endswith('/rest/v1'):sb=sb[:-8]
    key=motor.ask('Supabase SERVICE_ROLE legacy key','SUPABASE_SERVICE_ROLE_KEY',secret=True)
    user=motor.ask('Usuario eGOB','EGOB_USERNAME')
    pwd=motor.ask('Contrasena eGOB','EGOB_PASSWORD',secret=True)
    if not all((key,user,pwd)):raise RuntimeError('Faltan credenciales')
    proyectos=list(proyectos_paginados(sb,key))
    if args.limite:proyectos=proyectos[:args.limite]
    if args.guardar:
        print('ATENCION: se actualizara el ultimo movimiento en Supabase, NO el responsable.')
        if args.sin_interaccion:
            if os.getenv('EGOB_SYNC_AUTORIZADO')!='SI':
                raise RuntimeError('Falta EGOB_SYNC_AUTORIZADO=SI para guardado desatendido')
        elif input('Escriba GUARDAR para confirmar: ').strip()!='GUARDAR':
            print('Cancelado');return
    ruta=Path('resultado_fase_2_2_4_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.csv')
    ses=requests.Session();ses.headers['User-Agent']='TallerUrbano-eGOB-Sync/2.2.4'
    logged=False;counter=Counter()
    print('Modo:', 'GUARDADO' if args.guardar else 'SIMULACION SIN ESCRITURAS','| Registros:',len(proyectos))
    with ruta.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=CAMPOS);writer.writeheader()
        for i,proyecto in enumerate(proyectos,1):
            issue=tramite_simple(proyecto.get('tramite'))
            row={k:'' for k in CAMPOS};row.update(proyecto_id=proyecto['id'],
                proyecto=proyecto.get('proyecto') or '',tramite_origen=issue or str(proyecto.get('tramite') or ''))
            try:
                if not issue:
                    row.update(resultado='tramite_no_numerico',observaciones='Valor no compatible con un ID eGOB de 6 a 9 digitos. Verificar si es referencia documental o error de digitacion.')
                else:
                    if not logged:motor.login(ses,issue,user,pwd);logged=True
                    r=motor.check(ses.get(f'{motor.EGOB_BASE}/issues/{issue}/render_issue_route',timeout=45))
                    if 'cas/login' in r.url.lower():raise RuntimeError('Sesion CAS caducada')
                    row,payload=evaluar(r,proyecto,issue)
                    if args.guardar and payload:
                        guardar(sb,key,payload);row['resultado']='guardado'
            except Exception as exc:
                err=str(exc).replace('\n',' ')[:450]
                row.update(resultado='error',observaciones=err)
                if '404' in err:row['observaciones']='HTTP 404: comprobar numero eGOB en public.proyectos.tramite; no se modifica el registro existente.'
                if 'cas' in err.lower() or 'autentic' in err.lower():logged=False
            writer.writerow(row);f.flush();counter[row['resultado']]+=1
            print(f'[{i}/{len(proyectos)}] Proyecto {proyecto["id"]} / {issue or "sin ID"}: {row["resultado"]} {row["ultima_accion"]}')
            if args.pausa and i<len(proyectos):time.sleep(args.pausa)
    print('RESUMEN:',json.dumps(counter,ensure_ascii=False));print('CSV:',ruta.resolve())

if __name__=='__main__':
    try:main()
    except (KeyboardInterrupt,EOFError):print('Interrumpido');sys.exit(130)
    except Exception as exc:print('ERROR GENERAL:',exc);sys.exit(1)
