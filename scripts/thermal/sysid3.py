#!/usr/bin/env python3
"""3-node joint fit: die -R_int- sink -R_sink(F)- amb+off; sink -R_sc- chassis -R_ca- amb+off."""
import json, numpy as np
from scipy.linalg import expm
from scipy.optimize import least_squares
import importlib.util,sys
src=open('sysid_joint.py').read().split("for obs in ('soc','gpu'):")[0]
exec(src)
def sim3(th,D):
    v=np.exp(th);Cd,Ri_s,Ri_b,Cs,off,Rsc,Cc,Rca=v[:8];R=dict(zip(FANS,v[8:]));out=[]
    for y,p,a,F,rs in D:
        M={}
        for f in set(F.tolist()):
            for b,Ri in ((0,Ri_s),(1,Ri_b)):
                A=np.zeros((3,3));A[0,0]=-1/(Ri*Cd);A[0,1]=1/(Ri*Cd)
                A[1,0]=1/(Ri*Cs);A[1,1]=-1/(Ri*Cs)-1/(R[f]*Cs)-1/(Rsc*Cs);A[1,2]=1/(Rsc*Cs)
                A[2,1]=1/(Rsc*Cc);A[2,2]=-1/(Rsc*Cc)-1/(Rca*Cc)
                B=np.array([[1/Cd,0],[0,1/(R[f]*Cs)],[0,1/(Rca*Cc)]])
                W=np.zeros((5,5));W[:3,:3]=A;W[:3,3:]=B;E=expm(W);M[(f,b)]=(E[:3,:3],E[:3,3:])
        x=np.array([y[0],y[0],y[0]]);o=np.zeros(len(y))
        for k in range(len(y)):
            o[k]=x[0];Ad,Bd=M[(int(F[k]),int(rs[k]))];x=Ad@x+Bd@np.array([p[k],a[k]+off])
        out.append((o-y)[20::2])
    return np.concatenate(out)
D=[load(r,'soc') for r in ('run3','run4','run5','run7')]
th0=np.log([0.6,4.4,5,16,2.3, 3,80,8, 6,6,4.5,2.8,2.5,2.4])
r=least_squares(sim3,th0,args=(D,),max_nfev=25,diff_step=0.02)
v=np.exp(r.x);res=dict(zip(['C_die','R_int_syn','R_int_rs','C_sink','T_off','R_sc','C_chassis','R_ca'],np.round(v[:8],3).tolist()))
res['R_sink']=dict(zip(FANS,np.round(v[8:],3).tolist()));res['rms_K']=round(float(np.sqrt(np.mean(r.fun**2))),3)
n=0;res['rms_per_run']={}
for name,d in zip(('run3','run4','run5','run7'),D):
    m=len(d[0][20::2]);res['rms_per_run'][name]=round(float(np.sqrt(np.mean(r.fun[n:n+m]**2))),2);n+=m
print(json.dumps(res));json.dump(res,open('joint3.json','w'),indent=1)
