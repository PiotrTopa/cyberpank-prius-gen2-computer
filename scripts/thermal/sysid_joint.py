#!/usr/bin/env python3
"""Joint fit of die -R_int- sink -R_sink(F)- (T_amb+T_off) over run3, run4, run5, run7.
Measured supply power is the input; observable = bulk SoC (mean of cpu0-7 and GPU zones),
then repeated with the GPU zone alone. R_int differs per load type (synthetic / roadsense)."""
import csv, json, sys
import numpy as np
from scipy.linalg import expm
from scipy.optimize import least_squares
FANS=[0,10,20,40,70,100]
Z=["cpu%d-thermal"%i for i in range(8)]+["gpu-top-thermal","gpu-bottom-thermal"]
def fn(x):
    try: return float(x)
    except (TypeError,ValueError): return float('nan')
def clean(x):
    i=np.where(~np.isnan(x))[0]; return np.interp(np.arange(len(x)),i,x[i])
def load(run,obs):
    rows=[r for r in csv.DictReader(open(run+'/samples.csv')) if r['phase'] not in('init','done','rs_stopping') and r['fan_set'] not in('','nan')]
    z=Z if obs=='soc' else Z[-2:]
    y=np.array([np.mean([fn(r[k]) for k in z]) if obs=='soc' else max(fn(r[k]) for k in z) for r in rows])
    p=clean(np.array([fn(r['p_w']) for r in rows])); a=clean(np.array([fn(r['bmp2_t']) for r in rows]))
    F=np.array([100 if r['phase']=='abort' else int(float(r['fan_set'])) for r in rows])
    rs=np.array([r['rs_state'] in('running','starting','stopping','grace') for r in rows])
    return y,p,a,F,rs
def sim(th,D):
    v=np.exp(th); Cd,Ri_s,Ri_b,Cs=v[:4]; off=v[4]; R=dict(zip(FANS,v[5:]))
    out=[]
    for y,p,a,F,rs in D:
        M={}
        for f in set(F.tolist()):
            for b,Ri in ((0,Ri_s),(1,Ri_b)):
                A=np.array([[-1/(Ri*Cd),1/(Ri*Cd)],[1/(Ri*Cs),-1/(Ri*Cs)-1/(R[f]*Cs)]])
                B=np.array([[1/Cd,0],[0,1/(R[f]*Cs)]])
                W=np.zeros((4,4));W[:2,:2]=A;W[:2,2:]=B;E=expm(W);M[(f,b)]=(E[:2,:2],E[:2,2:])
        x=np.array([y[0],y[0]]);o=np.zeros(len(y))
        for k in range(len(y)):
            o[k]=x[0];Ad,Bd=M[(int(F[k]),int(rs[k]))]
            x=Ad@x+Bd@np.array([p[k],a[k]+off])
        out.append((o-y)[20::2])
    return np.concatenate(out)
for obs in ('soc','gpu'):
    D=[load(r,obs) for r in ('run3','run4','run5','run7')]
    th0=np.log([0.6,5.8,5.8,70,5.0, 4.5,4,3,1.8,1.3,1.2])
    r=least_squares(sim,th0,args=(D,),max_nfev=25,diff_step=0.02)
    v=np.exp(r.x);res=dict(zip(['C_die','R_int_syn','R_int_rs','C_sink','T_off'],np.round(v[:5],3).tolist()))
    res['R_sink']=dict(zip(FANS,np.round(v[5:],3).tolist()));res['rms_K']=round(float(np.sqrt(np.mean(r.fun**2))),3)
    n=0
    per={}
    for name,d in zip(('run3','run4','run5','run7'),D):
        m=len(d[0][20::2]);per[name]=round(float(np.sqrt(np.mean(r.fun[n:n+m]**2))),2);n+=m
    res['rms_per_run']=per
    print(obs,json.dumps(res),flush=True)
    json.dump(res,open('joint_%s.json'%obs,'w'),indent=1)
