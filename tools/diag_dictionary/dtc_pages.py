"""Step 1: harvest per-DTC circuit pages ("DTC  <code>  <TITLE>") from the
pdftotext -layout dumps of RM1076E section 05."""
import re, sys, json, glob, os
src = sys.argv[1]; out = sys.argv[2]
HDR = re.compile(r'^\s*DTC\s+([PCBU][0-9A-F]{4})(?:/(\S+))?\s+(\S.*?)\s*$')
SYS = re.compile(r'DIAGNOSTICS\s+−\s+(.*?)\s*(?:05−\d+)?\s*$')
PAGE = re.compile(r'(?:^|\s)05−(\d+)(?:\s|$)')
entries = []
for path in sorted(glob.glob(os.path.join(src, 'm_05_*.txt'))):
    lines = open(path, encoding='utf-8', errors='replace').read().split('\n')
    system = None; page = None
    i = 0
    filepage = int(re.search(r'm_05_(\d+)', path).group(1))
    while i < len(lines):
        ln = lines[i]
        if 'DIAGNOSTICS' in ln and '−' in ln:
            m = SYS.search(ln)
            if m:
                s = re.sub(r'\s+', ' ', m.group(1)).strip()
                s = re.sub(r'\s*05[A-Z0-9]{3}−\d+$', '', s)
                # wrapped system title: next line continues, e.g. "CONTROL)"
                nxt = lines[i+1].strip() if i+1 < len(lines) else ''
                if s.count('(') > s.count(')') and nxt:
                    s = s + ' ' + re.sub(r'\s*05[A-Z0-9]{3}−\d+$', '', nxt).strip()
                system = s
            for j in (i-1, i, i+1):
                if 0 <= j < len(lines):
                    pm = PAGE.search(lines[j])
                    if pm: page = int(pm.group(1)); break
        m = HDR.match(ln)
        if m and not ln.strip().lower().startswith('dtc no'):
            code, sub, title = m.group(1), m.group(2), m.group(3)
            if re.search(r'[a-z]{3}', title):   # running text, not a header
                i += 1; continue
            # continuation lines (upper-case wrapped title)
            j = i + 1
            while j < len(lines) and j < i + 4:
                t = lines[j].strip()
                if not t:
                    j += 1; break
                if HDR.match(lines[j]) or re.search(r'[a-z]{3}', t) or 'DESCRIPTION' in t:
                    break
                title += ' ' + t; j += 1
            title = re.sub(r'\s+', ' ', title).strip()
            entries.append(dict(code=code, sub=sub if sub and sub != '−' else None, title=title,
                                system=system, page=page or filepage, file=os.path.basename(path)))
        i += 1
json.dump(entries, open(out, 'w'), indent=1, ensure_ascii=False)
codes = {e['code'] for e in entries}
print(len(entries), 'entries', len(codes), 'codes')
from collections import Counter
for s, n in Counter(e['system'] for e in entries).most_common(): print(n, s)
