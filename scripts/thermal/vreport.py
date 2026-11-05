import csv,json,sys,numpy as np
d=sys.argv[1]
f=lambda x: float(x) if x not in('','None',None) else float('nan')
rows=list(csv.DictReader(open(d+'/samples.csv')))
A=lambda k:np.array([f(r[k]) for r in rows])
T=A('ts');g=np.array([max(f(r['gpu-top-thermal']),f(r['gpu-bottom-thermal'])) for r in rows]);c=np.array([max(f(r["cpu%d-thermal"%i]) for i in range(8)) for r in rows]);duty=A('fan_duty');rpm=A('fan_rpm');p=A('p_w');fps=A('road_fps');cdg=A('cd_gpu');cd4=A('cd_cpu4');dc=A('dcvsh4');amb=A('bmp2_t');box=A('box_power_w')
for ph in map(json.loads,open(d+'/phases.jsonl')):
    m=(T>=ph['t_start'])&(T<=ph['t_end']);t=T[m]-ph['t_start']
    print(ph['run'],ph['phase'],'dur %.0f tainted %s amb %.1f'%(ph['duration_s'],ph['tainted'],np.nanmean(amb[m])))
    print('  t  :',' '.join('%6d'%s for s in (0,30,60,90,120,180,300,480,600,900,1200,1499) if s<=t[-1]))
    for lab,y in (('duty',duty[m]),('rpm',rpm[m]),('gpu',g[m]),('cpu',c[m]),('P',p[m]),('road',fps[m])):
        print('  %-4s'%lab,' '.join('%6.1f'%y[min(np.searchsorted(t,s),m.sum()-1)] for s in (0,30,60,90,120,180,300,480,600,900,1200,1499) if s<=t[-1]))
    l=t>t[-1]-600
    print('  last10min: duty %.1f (min %.0f max %.0f) rpm %.0f gpu mean %.1f max %.1f cpu mean %.1f peak %.1f P %.2f box %.2f road %.2f gpuThr%% %.1f cd4%% %.1f dcvsh+%d'%(np.nanmean(duty[m][l]),np.nanmin(duty[m][l]),np.nanmax(duty[m][l]),np.nanmean(rpm[m][l]),g[m][l].mean(),g[m][l].max(),c[m][l].mean(),c[m][l].max(),np.nanmean(p[m][l]),np.nanmean(box[m][l]),np.nanmean(fps[m][l]),100*(cdg[m][l]>0).mean(),100*(cd4[m][l]>0).mean(),np.nanmax(dc[m])-np.nanmin(dc[m])))
    dd=duty[m];print('  duty changes>=1 count',int((np.abs(np.diff(dd))>=1).sum()),'on/off edges',int(((dd[1:]>0)!=(dd[:-1]>0)).sum()),'first on t',(t[dd>0][0] if (dd>0).any() else None),'last on t',(t[dd>0][-1] if (dd>0).any() else None))
