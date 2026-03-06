"""Comprueba la fecha del último dato disponible en NGL para todas las estaciones candidatas.
Usa threads para hacer todas las peticiones en paralelo.
"""
import urllib.request, sys, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, 'scripts')
from fetch_gps import parse_tenv3

candidates = {
    'La Palma':  ['LPAL', 'MAZO', 'LP01'],
    'El Hierro': ['EH01', 'FRON', 'LRES'],
    'Tenerife':  ['STTE', 'TN02', 'LLAG', 'TENE', 'TN01', 'GRAF', 'IZAN', 'SNMG', 'TN03'],
}

today = datetime.date.today()

def check(isla, sid):
    url = f'https://geodesy.unr.edu/gps_timeseries/tenv3/IGS14/{sid}.tenv3'
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            content = r.read().decode('utf-8', errors='replace')
        rows = parse_tenv3(content)
        if rows:
            last = rows[-1]['date']
            age  = (today - last).days
            return (isla, sid, last, age, len(rows), None)
        return (isla, sid, None, None, 0, 'sin filas')
    except Exception as e:
        return (isla, sid, None, None, 0, str(e))

all_tasks = [(isla, sid) for isla, ids in candidates.items() for sid in ids]

print(f"Consultando {len(all_tasks)} estaciones en paralelo...\n")
results = {}
with ThreadPoolExecutor(max_workers=12) as ex:
    futures = {ex.submit(check, isla, sid): (isla, sid) for isla, sid in all_tasks}
    for f in as_completed(futures, timeout=35):
        isla, sid, last, age, n, err = f.result()
        results.setdefault(isla, [])
        if err:
            results[isla].append((9999, sid, None, 0, err))
        else:
            results[isla].append((age, sid, last, n, None))

print(f"{'Isla':<12} {'ID':<6} {'Ultimo dato':<14} {'Edad(d)':<10} {'Filas':<8} {'Error'}")
print("-" * 70)
for isla in ['La Palma', 'El Hierro', 'Tenerife']:
    rows = sorted(results.get(isla, []))
    for i, (age, sid, last, n, err) in enumerate(rows):
        mark = " ***" if i == 0 and not err else ""
        last_str = str(last) if last else '-'
        age_str  = str(age) if age != 9999 else '-'
        err_str  = (err[:40] if err else '')
        print(f"{isla if i==0 else '':<12} {sid:<6} {last_str:<14} {age_str:<10} {n:<8} {err_str}{mark}")
    print()
