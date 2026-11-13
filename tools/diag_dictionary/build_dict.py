"""Merge the harvests into the machine-readable NHW20 DTC dictionary."""
import json, sys, re, collections, datetime
pages_json, areas_json, hv_json, out = sys.argv[1:5]
BASE = "https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/"
SYSTEMS = {
    'SFI SYSTEM': ('engine', 'Engine (SFI) — ECM'),
    'HYBRID CONTROL SYSTEM': ('hv', 'Hybrid control — HV control ECU'),
    'HYBRID BATTERY SYSTEM': ('battery', 'Hybrid battery — battery ECU'),
    'ELECTRONICALLY CONTOROLLED BRAKE SYSTEM': ('brake', 'Electronically controlled brake — skid control ECU'),
    'ELECTRIC POWER STEERING SYSTEM': ('eps', 'Electric power steering — power steering ECU'),
    'SHIFT CONTROL SYSTEM (PARKING LOCK CONTROL)': ('transmission', 'Shift control (parking lock) — transmission control ECU'),
    'SUPPLEMENTAL RESTRAINT SYSTEM': ('airbag', 'Supplemental restraint — airbag ECU'),
    'AIR CONDITIONING SYSTEM': ('ac', 'Air conditioning — A/C amplifier'),
    'PUSH BUTTON START SYSTEM': ('power_source', 'Push button start — power source control ECU'),
    'MULTIPLEX COMMUNICATION SYSTEM': ('gateway', 'Multiplex communication (BEAN) — gateway ECU'),
    'ENGINE IMMOBILIZER SYSTEM (W/ SMART ENTRY SYSTEM)': ('immobiliser', 'Engine immobiliser (w/ smart entry) — transponder key ECU'),
    'ENGINE IMMOBILIZER SYSTEM (W/O SMART ENTRY SYSTEM)': ('immobiliser', 'Engine immobiliser (w/o smart entry) — transponder key ECU'),
    'POWER DOOR LOCK CONTROL SYSTEM': ('body', 'Power door lock — body ECU'),
    'WIRELESS DOOR LOCK CONTROL SYSTEM (W/ SMART ENTRY SYSTEM)': ('body', 'Wireless door lock (w/ smart entry) — body ECU'),
    'WIRELESS DOOR LOCK CONTROL SYSTEM (W/O SMART ENTRY SYSTEM)': ('body', 'Wireless door lock (w/o smart entry) — body ECU'),
}
ACR = {'ecu','ecm','pcm','hv','can','bean','abs','vsc','ig','acc','lin','srs','mg1','mg2','avc','lan','act','batt','stsw',
       'gnd','ram','rom','egr','maf','vvt','evap','rh','lh','ecb','eps','iat','ect','iac','obd','vin','dc','ig1','ig2','igct',
       'hc','nox','mil','sfi','thw','tha','vg','ne','g2','ocv','ho2s','af','tc','cg','ts','bz','sla','slr','smr','iact','ptc'}
def nice(title):
    t = title.replace('”', '"').replace('−', '-').strip()
    if t.isupper() or not re.search('[a-z]', t):
        t = t.capitalize()
        t = re.sub(r'[A-Za-z0-9]+', lambda m: m.group(0).upper() if m.group(0).lower() in ACR else m.group(0), t)
        t = re.sub(r'\b([a-z])/([a-z])\b', lambda m: m.group(0).upper(), t)      # a/c, a/f, w/c
        t = re.sub(r'"([a-z])"', lambda m: m.group(0).upper(), t)
        t = re.sub(r'\b([uvwpb]) (phase|position|signal)', lambda m: m.group(1).upper() + ' ' + m.group(2), t)
        t = t.replace('+b ', '+B ').replace('(+b', '(+B')
    return t
pages = json.load(open(pages_json)); areas = json.load(open(areas_json)); hv = json.load(open(hv_json))
codes = collections.OrderedDict()
def entry(code, ecu):
    lst = codes.setdefault(code, [])
    for e in lst:
        if e['ecu'] == ecu: return e
    e = {'ecu': ecu}; lst.append(e); return e
unknown_sys = collections.Counter()
for p in pages:
    if p['system'] not in SYSTEMS: unknown_sys[p['system']] += 1; continue
    ecu, sysname = SYSTEMS[p['system']]
    e = entry(p['code'], ecu)
    e.setdefault('system', sysname)
    e.setdefault('description', nice(p['title']))
    e.setdefault('page', '05-%d' % p['page'])
    e.setdefault('url', BASE + p['file'].replace('.txt', '.pdf'))
    a = areas.get(p['file'], {})
    key = p['code'] + ('/' + p['sub'] if p['sub'] else '')
    ta = a.get(key) or a.get(p['code'])
    if p['sub']:
        if ecu == 'hv':
            inf = e.setdefault('inf', {}).setdefault(p['sub'], {})
            inf.setdefault('page', '05-%d' % p['page'])
            if a.get(key): inf.setdefault('trouble_area', a[key])
        else:
            e.setdefault('sub', p['sub'])      # Toyota 2-digit blink code (C1241/41)
    if ta and 'trouble_area' not in e: e['trouble_area'] = ta
for r in hv:
    e = entry(r['code'], 'hv')
    e.setdefault('system', SYSTEMS['HYBRID CONTROL SYSTEM'][1])
    inf = e.setdefault('inf', {}).setdefault(r['inf'], {})
    inf['detection'] = r['detection'].replace('−', '-')
    if r['trouble_area']: inf['trouble_area'] = r['trouble_area']
    inf['master_warning'] = r['master_warning']; inf['chk_eng'] = r['chk_eng']
    if r['page']: inf.setdefault('page', '05-%d' % r['page'])
    inf['chart_page'] = '05-%d' % r['chart_page']
    if 'page' not in e and r['page']: e['page'] = '05-%d' % r['page']
    if 'trouble_area' not in e and r['trouble_area']: e['trouble_area'] = r['trouble_area']
# master warning summary per HV code
for code, lst in codes.items():
    for e in lst:
        if e.get('inf'):
            mws = [v.get('master_warning') for v in e['inf'].values() if v.get('master_warning') is not None]
            if mws: e['master_warning'] = 'all' if all(mws) else ('some' if any(mws) else 'none')
# hand additions from the charts (codes without a circuit page of their own)
HAND = [
 ('B2799', 'hv', 'Immobilizer malfunction', '05-268', 'm_05_0268.pdf'),
 ('P0500', 'hv', 'Vehicle speed sensor "A" (no vehicle speed signal during cruise control driving)', '05-268', 'm_05_0268.pdf'),
 ('P0571', 'hv', 'Brake switch "A" circuit (open or short in stop lamp switch circuit)', '05-268', 'm_05_0268.pdf'),
 ('P0607', 'hv', 'Control module performance (STP signal inconsistent with skid control ECU, cruise control)', '05-268', 'm_05_0268.pdf'),
 ('P2251', 'engine', 'Oxygen sensor reference ground circuit/open (A/F sensor bank 1 sensor 1)', '05-177', 'm_05_0039.pdf'),
 ('B1474', 'ac', 'A/C inverter (w/ converter inverter assy) malfunction', '05-1047', 'm_05_1047.pdf'),
 ('B1499', 'ac', 'Multiplex communication circuit', '05-2250', 'm_05_1047.pdf'),
 ('B1424', 'ac', 'Solar sensor circuit (driver side) — may be displayed when checked in a dark place', '05-1047', 'm_05_1047.pdf'),
 ('U0123', 'brake', 'Yaw rate sensor communication malfunction (blink code 62)', '05-750', 'm_05_0750.pdf'),
 ('U0124', 'brake', 'Deceleration sensor communication malfunction (blink code 95)', '05-750', 'm_05_0750.pdf'),
]
for code, ecu, desc, page, f in HAND:
    e = entry(code, ecu)
    e.setdefault('description', desc); e.setdefault('page', page); e.setdefault('url', BASE + f)
    e.setdefault('system', next(v[1] for v in SYSTEMS.values() if v[0] == ecu)); e['hand'] = True
for code, lst in codes.items():
    for e in lst:
        if 'description' not in e:
            infs = list(e.get('inf', {}).values())
            e['description'] = infs[0].get('detection', '') if infs else ''
doc = {
 'meta': {
   'vehicle': 'Toyota Prius NHW20 (2004-2009)',
   'source': 'Toyota repair manual RM1076E, section 05 (Diagnostics), ' + BASE + 'm_05_NNNN.pdf',
   'method': 'machine extraction (pdftotext): per-DTC circuit page titles, trouble-area columns, HV control DTC chart (INF codes, CHK ENG / master warning lamp columns). Verify against the manual page before acting on an entry.',
   'generated': datetime.date.today().isoformat(),
   'ecu_keys': {v[0]: v[1] for v in SYSTEMS.values()},
 },
 'codes': dict(sorted(codes.items())),
}
json.dump(doc, open(out, 'w'), indent=1, ensure_ascii=False, sort_keys=False)
n_inf = sum(len(e.get('inf', {})) for l in codes.values() for e in l)
print(len(codes), 'codes,', sum(len(l) for l in codes.values()), 'entries,', n_inf, 'INF codes; unknown systems:', dict(unknown_sys))
print(collections.Counter(e['ecu'] for l in codes.values() for e in l))
for c in ('C2300', 'B2278', 'P0852', 'P0420', 'C1241', 'U0100'): print(c, json.dumps(codes[c], ensure_ascii=False)[:600])
