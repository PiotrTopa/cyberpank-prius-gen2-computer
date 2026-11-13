# NHW20 DTC dictionary generator

Rebuilds `cyberpunk_computer/diag/data/dtc_nhw20.json` from the Toyota repair
manual RM1076E, section 05 (Diagnostics), which is online one page group per
PDF. Needs `curl`, `pdftotext` (poppler) and Python 3.

    W=$(mktemp -d); mkdir -p $W/pdf $W/txt
    ./fetch.sh $W                                   # m_05_0001..2330.pdf -> pdf/, txt/ (777 exist)
    python3 -I dtc_pages.py  $W/txt $W/dtc_pages.json        # "DTC <code> <TITLE>" circuit pages
    python3 -I areas.py      $W/pdf $W/dtc_pages.json $W/areas.json   # trouble-area columns
    python3 -I hv_chart.py   $W/pdf/m_05_0268.pdf $W/hv_chart.json    # HV chart: INF codes, lamps
    python3 -I build_dict.py $W/dtc_pages.json $W/areas.json $W/hv_chart.json \
        ../../cyberpunk_computer/diag/data/dtc_nhw20.json

What each step extracts and how far to trust it is in
`docs/TOYOTA_DIAGNOSTICS.md`, section 5.
