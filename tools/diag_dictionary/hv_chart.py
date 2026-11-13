"""HV control DTC chart (RM1076E 05-268..): DTC / INF code / detection
condition / trouble area / CHK ENG / master warning lamp, by word coordinates.
Row extents are solved from the row centres (every single-line cell is centred
on its row): H[i+1] = 2*(y[i+1]-y[i]) - H[i], H[0] from the first text line."""
import re, sys, json, subprocess, collections
pdf, out = sys.argv[1], sys.argv[2]
DEBUG = len(sys.argv) > 3
tsv = subprocess.run(['pdftotext', '-tsv', pdf, '-'], capture_output=True, text=True).stdout
words = collections.defaultdict(list)
for ln in tsv.split('\n')[1:]:
    f = ln.split('\t')
    if len(f) < 12 or f[0] != '5': continue
    words[int(f[1])].append((float(f[6]), float(f[7]), float(f[8]), f[11]))
CODE = re.compile(r'^[PCBU][0-9A-F]{4}$')
rows = []; bad = 0
def text_of(ws):
    lines = collections.defaultdict(list)
    for x, y, w, t in ws: lines[round(y / 3.0)].append((x, t))
    txt = ' '.join(' '.join(t for x, t in sorted(lines[k])) for k in sorted(lines))
    return re.sub(r'(\w)- (\w)', r'\1\2', txt)
def fmt_area(wl):
    groups = []
    for x, y, w, t in sorted(wl, key=lambda v: v[1]):
        if groups and y - groups[-1][0] < 5: groups[-1][1].append((x, t))
        else: groups.append((y, [(x, t)]))
    out_l = []
    for y, items in groups:
        toks = [t for x, t in sorted(items)]
        bullet = toks and toks[0] == 'S'
        txt = ' '.join(toks[1:] if bullet else toks).strip()
        if not txt: continue
        if bullet or not out_l: out_l.append(txt)
        else: out_l[-1] = re.sub(r'(\w)- (\w)', r'\1\2', out_l[-1] + ' ' + txt)
    return out_l
for pg in sorted(words):
    all_ws = words[pg]
    hdr = [w for w in all_ws if w[3] == 'Trouble' and w[1] < 125]
    cond = [w for w in all_ws if w[3] == 'Condition' and w[1] < 125]
    chk = [w for w in all_ws if w[3] == 'CHK' and w[1] < 125]
    infh = [w for w in all_ws if w[3] == 'INF' and w[1] < 125]
    if not (hdr and cond and chk and infh):
        print('page', pg, 'no header'); continue
    x_inf = infh[0][0]; x_chk = chk[0][0]
    top_hdr = max(w[1] for w in all_ws if w[1] < 125 and w[3] in ('Code', 'Lamp', 'Page)')) + 8
    ws = [w for w in all_ws if top_hdr < w[1] < 800 and 'PRIUS' not in w[3] and not (w[1] > 770)]
    foot = [w[1] for w in ws if re.match(r'^\*\d:$', w[3])]
    if foot: ws = [w for w in ws if w[1] < min(foot) - 2]
    infs = sorted((y, x, t) for x, y, w, t in ws if x_inf - 8 <= x <= x_inf + 22 and re.fullmatch(r'\d{3}', t))
    if not infs: continue
    x_det = infs[0][1] + 18          # detection column starts right of the INF cell
    marks_x = sorted(x for x, y, w, t in ws if t in ('X', 'f') and abs(y - infs[0][0]) < 3 and x > x_chk - 15)
    x_area_end = marks_x[0] - 6
    # trouble-area column start: left edge of the bullet "S" marks
    bullets = [x for x, y, w, t in ws if t == 'S' and x_det + 40 < x < x_area_end]
    x_area = min(bullets) - 3 if bullets else (x_det + x_area_end) / 2
    codes = [(y, t) for x, y, w, t in ws if x < 95 and CODE.match(t)]
    pages = [(y, t) for x, y, w, t in ws if x < 95 and re.match(r'^\(05−\d+\)$', t)]
    ys = [y for y, x, t in infs]
    ymin = min(w[1] for w in ws)
    H = [2 * (ys[0] - ymin) + 11.9]
    for i in range(1, len(ys)):
        H.append(2 * (ys[i] - ys[i - 1]) - H[-1])
    if min(H) < 9: bad += 1; print('page', pg, 'row-height solve failed', [round(h, 1) for h in H])
    for i, (y, x, inf) in enumerate(infs):
        lo, hi = y - H[i] / 2 - 1, y + H[i] / 2 - 1
        if min(H) < 9: lo, hi = y - 6, y + 6
        cell = lambda a, b: text_of([w for w in ws if a <= w[0] < b and lo <= w[1] - 4.9 + 4.9 < hi])
        code = min(codes, key=lambda c: abs(c[0] - y))[1]
        page = min(pages, key=lambda c: abs(c[0] - y))[1] if pages else None
        marks = sorted((xx, t) for xx, yy, w, t in ws if abs(yy - y) < 3 and xx >= x_area_end and t in ('X', 'f'))
        det = cell(x_det, x_area)
        area = fmt_area([w for w in ws if x_area <= w[0] < x_area_end and lo <= w[1] < hi])
        rows.append(dict(code=code, inf=inf, page=int(page[4:-1]) if page else None, detection=det,
                         trouble_area=area, chk_eng=marks[0][1] == 'f' if marks else None,
                         master_warning=marks[1][1] == 'f' if len(marks) > 1 else None, chart_page=267 + pg,
                         solved=min(H) >= 9))
json.dump(rows, open(out, 'w'), indent=1, ensure_ascii=False)
print(len(rows), 'rows;', sum(1 for r in rows if r['master_warning']), 'light master warning;', bad, 'pages unsolved', sum(1 for r in rows if not r['detection']), 'empty det')
if DEBUG:
    for r in rows: print(r['code'], r['inf'], r['page'], '|', r['detection'], '|', r['trouble_area'], r['chk_eng'], r['master_warning'])
