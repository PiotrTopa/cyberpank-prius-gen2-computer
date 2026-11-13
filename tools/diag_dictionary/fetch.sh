#!/bin/bash
# fetch RM1076E section 05 PDFs and convert to text
D=$1
fetch_one() {
  n=$(printf "%04d" $1); D=$2
  f=$D/pdf/m_05_$n.pdf
  if [ ! -s "$f" ]; then
    code=$(curl -s --retry 2 --max-time 60 -o "$f.tmp" -w "%{http_code}" "https://toyotamanuals.gitlab.io/PZ471-Z01S0-CA/htmlweb/rm/rm1076e/m_05_$n.pdf")
    if [ "$code" = "200" ]; then mv "$f.tmp" "$f"; else rm -f "$f.tmp"; echo "$n $code" >> $D/fetch_fail.log; return; fi
  fi
  [ -s "$D/txt/m_05_$n.txt" ] || pdftotext -layout "$f" "$D/txt/m_05_$n.txt" 2>/dev/null
}
export -f fetch_one
seq 1 2330 | xargs -P 6 -I{} bash -c 'fetch_one {} '"$D"
echo done > $D/fetch_done
