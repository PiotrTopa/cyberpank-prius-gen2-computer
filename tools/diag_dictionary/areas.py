"""Trouble-area column of the per-DTC circuit pages (best effort, by word
coordinates). Output: {file: {code[/inf]: [areas]}}"""
import re, sys, json, subprocess, collections, glob, os
pdfdir, pages_json, out = sys.argv[1:4]
entries = json.load(open(pages_json))
files = sorted({e['file'] for e in entries})
CODE = re.compile(r'^[PCBU][0-9A-F]{4}(/\S+)?$')
res = {}; stats = collections.Counter()
def lines_of(ws):
    groups = []
    for x, y, w, t in sorted(ws, key=lambda v: v[1]):
        if groups and y - groups[-1][0] < 5: groups[-1][1].append((x, t, y))
        else: groups.append((y, [(x, t, y)]))
    return [(y, sorted(items)) for y, items in groups]
for fn in files:
    pdf = os.path.join(pdfdir, fn.replace('.txt', '.pdf'))
    tsv = subprocess.run(['pdftotext', '-tsv', pdf, '-'], capture_output=True, text=True).stdout
    pages = collections.defaultdict(list)
    for ln in tsv.split('\n')[1:]:
        f = ln.split('\t')
        if len(f) < 12 or f[0] != '5': continue
        pages[int(f[1])].append((float(f[6]), float(f[7]), float(f[8]), f[11]))
    for pg, ws in pages.items():
        heads = [w for w in ws if w[3] == 'Trouble' and any(abs(v[1] - w[1]) < 3 and v[3].lower().startswith('area') and 0 < v[0] - w[0] < 60 for v in ws)]
        for h in heads:
            hy, hx = h[1], h[0]
            below = [w for w in ws if w[1] > hy + 6]
            # table end: first left-margin heading / footer below the header
            ends = [w[1] for w in below if (w[3] in ('PRIUS', 'WIRING', 'INSPECTION', 'MONITOR', 'HINT:', 'NOTICE:', 'CIRCUIT', 'TYPICAL', 'COMPONENT', 'DTC') and w[0] < 120)
                    or (w[3] == 'Trouble' and w[1] > hy + 6)]
            end = min(ends) - 2 if ends else 800
            tab = [w for w in below if w[1] < end]
            if not tab: continue
            bx = sorted(w[0] for w in tab if w[3] == 'S' and hx - 150 < w[0] <= hx + 8)
            if not bx: stats['nobullet'] += 1; continue
            # rightmost cluster of bullet x positions = the trouble-area column
            x_area = bx[-1]
            x_area = min(b for b in bx if x_area - b < 4)
            area_ws = [w for w in tab if w[0] >= x_area - 1]
            anchors = sorted((w[1], w[3]) for w in tab if w[0] < x_area - 150 and w[0] < 140 and CODE.match(w[3]))
            if not anchors: stats['noanchor'] += 1; continue
            def fmt(wl):
                out_l = []
                for y, items in lines_of(wl):
                    toks = [t for x, t, yy in items]
                    bullet = toks and toks[0] == 'S'
                    txt = ' '.join(toks[1:] if bullet else toks).strip()
                    if not txt: continue
                    if bullet or not out_l: out_l.append(txt)
                    else: out_l[-1] = re.sub(r'(\w)- (\w)', r'\1\2', out_l[-1] + ' ' + txt)
                return out_l
            d = res.setdefault(fn, {})
            if len(anchors) == 1:
                d[anchors[0][1]] = fmt(area_ws); stats['single'] += 1
            else:
                ys = [a[0] for a in anchors]
                ymin = min(w[1] for w in tab)
                H = [2 * (ys[0] - ymin) + 11.9]
                for i in range(1, len(ys)): H.append(2 * (ys[i] - ys[i - 1]) - H[-1])
                if min(H) < 9:
                    stats['multi_unsolved'] += 1
                    continue
                for i, (y, code) in enumerate(anchors):
                    lo, hi = y - H[i] / 2 - 1, y + H[i] / 2 - 1
                    d[code] = fmt([w for w in area_ws if lo <= w[1] < hi])
                stats['multi'] += 1
json.dump(res, open(out, 'w'), indent=1, ensure_ascii=False)
print(dict(stats), sum(len(v) for v in res.values()))
for fn in ('m_05_0931.txt', 'm_05_2159.txt', 'm_05_0139.txt', 'm_05_0306.txt', 'm_05_0782.txt'): print(fn, res.get(fn))
