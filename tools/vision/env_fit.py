#!/usr/bin/env python3
"""Fit + evaluate the dashcam environment model (experiment venv: numpy, pandas, sklearn).

Inputs: feature CSVs from ``env_features.py`` in ``/home/piotr/datasets/dashcam/features/``:
``bdd_val.csv`` (BDD100K val, 10 000 images with timeofday / weather / scene labels),
``dawn.csv`` (DAWN fog / rain / snow, 704 images) and ``drive_home_2026-10-19.csv`` (our
70mai drive, 174 frames). Hand labels for "enclosed" on BDD (tunnel interiors and covered
car parks; BDD's ``scene=tunnel`` also tags tunnel *entrances* seen from outside):
``tools/vision/bdd_val_enclosed_labels.csv``.

    env_fit.py fit [--emit trees.py]  # time-of-day tree + enclosed / weather rule searches
    env_fit.py lights                 # the old LightThresholds rule per frame vs BDD
    env_fit.py runtime [--dump x.csv] # the committed pure-Python EnvironmentModel, all data
    env_fit.py fixture -o tests/fixtures/env_bdd_sample.csv   # stratified sample for tests

Seeds are fixed (random_state=0); the held-out split is a stratified 30 % of BDD val.
Model selection used the 70mai drive as an out-of-domain check (feature sets whose trees
called the sunny drive "dusk" were rejected), so the drive is not an untouched test set.
"""

from __future__ import annotations

import argparse
import itertools
import os
import pprint
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import StratifiedKFold, train_test_split
from sklearn.tree import DecisionTreeClassifier, _tree, export_text

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

FEAT_DIR = "/home/piotr/datasets/dashcam/features/"
TOD = {"daytime": "day", "dawn/dusk": "dusk_dawn", "night": "night"}
TOD_LABELS = ["day", "dusk_dawn", "night"]

#: time-of-day features: sky band + whole-frame percentiles/shares only (the road band and
#: the bottom of the frame hold the hood / dashboard, which differ per car and camera)
TOD_FEATS = ["sky_y", "sky_high", "sky_low", "sky_dark", "sky_blue", "y_high", "full_dark",
             "edge_full"]


def load():
    from cyberpunk_computer.vision import environment as env
    b = pd.read_csv(FEAT_DIR + "bdd_val.csv")
    d = pd.read_csv(FEAT_DIR + "drive_home_2026-10-19.csv")
    w = pd.read_csv(FEAT_DIR + "dawn.csv") if os.path.exists(FEAT_DIR + "dawn.csv") else None
    for x in [b, d] + ([w] if w is not None else []):
        der = [env.derived(r) for r in x.to_dict("records")]
        for k in der[0]:
            x[k] = [r[k] for r in der]
    lab = pd.read_csv(os.path.join(HERE, "bdd_val_enclosed_labels.csv"))
    b["enc"] = b.file.map(dict(zip(lab.file, lab.enclosed))).fillna(0).astype(int)  # -1 = ambiguous
    d["enc"] = (d.scene == "garage").astype(int)
    return b, d, w


def tree_literal(m, feats):
    """sklearn tree -> nested tuples: (feature, threshold, left, right) | (class, (p...))."""
    t, classes = m.tree_, list(m.classes_)

    def rec(i):
        if t.feature[i] == _tree.TREE_UNDEFINED:
            v = t.value[i][0]
            p = v / v.sum()
            return (classes[int(np.argmax(p))], tuple(round(float(x), 3) for x in p))
        return (feats[t.feature[i]], round(float(t.threshold[i]), 3),
                rec(t.children_left[i]), rec(t.children_right[i]))
    return rec(0)


def report(name, y, p, labels):
    y, p = np.asarray(y), np.asarray(p)
    cm = confusion_matrix(y, p, labels=labels)
    print(f"\n### {name}: accuracy {float((y == p).mean()):.3f} (n={len(y)})\n")
    print("| true \\ predicted | " + " | ".join(map(str, labels)) + " | recall |")
    print("|---" * (len(labels) + 2) + "|")
    for lab, row in zip(labels, cm):
        rec = row[labels.index(lab)] / row.sum() if row.sum() else float("nan")
        print(f"| {lab} | " + " | ".join(str(v) for v in row) + f" | {rec:.2f} |")
    prec = [cm[j, j] / cm[:, j].sum() if cm[:, j].sum() else float("nan") for j in range(len(labels))]
    print("| precision | " + " | ".join(f"{v:.2f}" for v in prec) + " | |")
    return cm


def tod_split(b):
    bt = b[b.timeofday.isin(TOD)].copy()
    bt["y"] = bt.timeofday.map(TOD)
    return bt, train_test_split(bt, test_size=0.3, random_state=0, stratify=bt.y)


def fit_tod(tr):
    return DecisionTreeClassifier(max_depth=3, min_samples_leaf=40, class_weight="balanced",
                                  random_state=0).fit(tr[TOD_FEATS], tr.y)


def enc_rule(x, ylo=60, td=10, sh=200, bl=2):
    return (x.sky_y > ylo) & (x.top_drop <= td) & (x.sky_high <= sh) & (x.sky_blue <= bl)


def clear_rule(x, bl=8, sat=15):
    return (x.sky_blue > bl) & (x.sky_sat > sat)


def overcast_rule(x, bl=1, sh=200, sat=8):
    return (x.sky_blue <= bl) & (x.sky_high >= sh) & (x.sky_sat <= sat)


def cmd_fit(a):
    b, d, w = load()
    bt, (tr, te) = tod_split(b)
    print("## Time of day: decision tree, depth 3, min 40 per leaf, balanced class weights")
    m = fit_tod(tr)
    report("time of day, BDD val held-out 30 %", te.y, m.predict(te[TOD_FEATS]), TOD_LABELS)
    accs = []
    for k_tr, k_te in StratifiedKFold(5, shuffle=True, random_state=0).split(bt, bt.y):
        mk = fit_tod(bt.iloc[k_tr])
        accs.append(float((mk.predict(bt.iloc[k_te][TOD_FEATS]) == bt.iloc[k_te].y).mean()))
    print(f"\n5-fold CV accuracy: {np.round(accs, 3).tolist()}")
    print("\n70mai drive:", pd.crosstab(d.scene, m.predict(d[TOD_FEATS])).to_dict())
    print("\n" + export_text(m, feature_names=TOD_FEATS, show_weights=True))
    final = fit_tod(bt)         # refit on all labelled BDD val for the runtime
    trees = {"TOD_TREE": tree_literal(final, TOD_FEATS)}

    print("\n## Enclosed: conjunction search (BDD enclosed hand labels + our garage)")
    bb = b[b.enc >= 0]
    groups = {"BDD enclosed": bb[bb.enc == 1], "70mai garage (inside)": d[d.scene == "garage"].iloc[:11],
              "70mai outdoor": d[d.scene == "outdoor"],
              "BDD day": bb[(bb.enc == 0) & (bb.timeofday == "daytime")],
              "BDD dusk": bb[(bb.enc == 0) & (bb.timeofday == "dawn/dusk")],
              "BDD night": bb[(bb.enc == 0) & (bb.timeofday == "night")]}
    rows = []
    for ylo, td, sh, bl in itertools.product([40, 50, 60, 70], [0, 5, 10, 15], [170, 190, 200, 210, 230],
                                             [0, 1, 2, 4]):
        r = {g: float(enc_rule(x, ylo, td, sh, bl).mean()) for g, x in groups.items()}
        rows.append(dict(sky_y_gt=ylo, top_drop_le=td, sky_high_le=sh, sky_blue_le=bl, **r))
    R = pd.DataFrame(rows)
    R = R[(R["70mai outdoor"] == 0) & (R["BDD day"] <= 0.03)]
    R["score"] = R["BDD enclosed"] + R["70mai garage (inside)"] - 5 * R["BDD day"]
    print(R.sort_values("score", ascending=False).head(8).round(3).to_string(index=False))
    print("\nchosen (sky_y > 60, top_drop <= 10, sky_high <= 200, sky_blue <= 2):",
          {g: round(float(enc_rule(x).mean()), 3) for g, x in groups.items()})

    print("\n## Weather (daytime BDD): clear-sky and grey-sky rules")
    bw = b[(b.timeofday == "daytime") & (b.weather != "undefined")].copy()
    clear = bw.weather.isin(["clear", "partly cloudy"])
    grey = bw.weather.isin(["overcast", "rainy", "snowy", "foggy"])
    for name, rule, truth in (("clear", clear_rule, clear), ("overcast", overcast_rule, grey)):
        p = rule(bw)
        print(f"{name}: precision {truth[p].mean():.2f}, recall {p[truth].mean():.2f}, "
              f"cover {p.mean():.2f}; share by BDD label {p.groupby(bw.weather).mean().round(2).to_dict()}; "
              f"70mai outdoor {rule(d[d.scene == 'outdoor']).mean():.2f}"
              + (f"; DAWN {rule(w).groupby(w.weather).mean().round(2).to_dict()}" if w is not None else ""))
    print("\nA depth-4 tree on 12 weather features (clear / overcast / rainy / snowy), for the record:")
    bw["y"] = bw.weather.replace({"partly cloudy": "clear"})
    bw = bw[bw.y != "foggy"]
    WX = ["sky_chroma", "sky_blue", "sky_sat", "sky_high", "sky_spread", "road_sat", "sat",
          "edge_full", "edge_sky", "top_mid", "road_y", "y_high"]
    trw, tew = train_test_split(bw, test_size=0.3, random_state=0, stratify=bw.y)
    mw = DecisionTreeClassifier(max_depth=4, min_samples_leaf=40, class_weight="balanced",
                                random_state=0).fit(trw[WX], trw.y)
    report("weather tree, BDD daytime held-out 30 %", tew.y, mw.predict(tew[WX]),
           ["clear", "overcast", "rainy", "snowy"])
    if a.emit:
        with open(a.emit, "w") as f:
            f.write("# emitted by tools/vision/env_fit.py fit\n")
            for k, v in trees.items():
                f.write(f"{k} = " + pprint.pformat(v, width=100) + "\n")
        print("\nwrote", a.emit)


def cmd_lights(a):
    """The old LightThresholds rule, one fresh model per frame (its bootstrap = the rule)."""
    from cyberpunk_computer.vision.auto_lights import AutoLightModel, LightThresholds
    from cyberpunk_computer.vision.light_features import LightFeatures
    b, d, _ = load()
    bt = b[b.timeofday.isin(TOD) & (b.enc >= 0)].copy()
    truth = ((bt.timeofday != "daytime") | (bt.enc == 1)).astype(int).values

    def run(x, th):
        out = []
        for r in x.itertuples():
            f = LightFeatures(t=0, sky_y=r.sky_y, sky_u=r.sky_u, sky_v=r.sky_v,
                              sky_chroma=r.sky_chroma, sky_clip=r.sky_clip, road_y=r.road_y)
            dd = AutoLightModel(th).update(f, None, t=0)
            out.append((int(dd.lights), dd.reason))
        return out
    for th in (LightThresholds(),):
        res = run(bt, th)
        report(f"LightThresholds({th.sky_y_on:.0f}/{th.sky_y_off:.0f}, chroma {th.chroma_on:.0f}/"
               f"{th.chroma_off:.0f}) per frame vs BDD (ON = night | dusk | enclosed)",
               truth, [r[0] for r in res], [0, 1])
        print(pd.crosstab(bt.timeofday.values, [r[1] for r in res]).to_string())
        dl = run(d, th)
        print(pd.crosstab(d.scene.values, [r[1] for r in dl]).to_string())
    print("\nsky_y_on / sky_y_off sweep (chroma 8/12 kept), per-frame accuracy (dead band = ON):")
    for on, off in ((100, 130), (110, 140), (120, 150), (130, 160), (140, 170)):
        th = LightThresholds(sky_y_on=on, sky_y_off=off)
        p = [r[0] for r in run(bt, th)]
        dl = [r[0] for r in run(d, th)]
        print(f"  {on}/{off}: BDD acc {(np.array(p) == truth).mean():.3f}; 70mai garage ON "
              f"{np.mean([x for x, s in zip(dl, d.scene) if s == 'garage']):.2f}, outdoor ON "
              f"{np.mean([x for x, s in zip(dl, d.scene) if s == 'outdoor']):.2f}")


def run_runtime(x, windowed=False):
    from cyberpunk_computer.vision import environment as env
    from cyberpunk_computer.vision.scene_features import from_row
    model = env.EnvironmentModel()
    out = []
    for r in x.to_dict("records"):
        f = from_row({k: str(v) for k, v in r.items()})
        out.append(model.update(f, t=f.t) if windowed else model.classify(f))
    x = x.copy()
    x["p_tod"] = [e.time_of_day for e in out]
    x["p_enc"] = [e.enclosed for e in out]
    x["p_wx"] = [e.weather or "-" for e in out]
    x["p_env"] = [e.label for e in out]
    x["p_conf"] = [e.confidence for e in out]
    return x


def cmd_runtime(a):
    b, d, w = load()
    b = run_runtime(b)
    bt = b[b.timeofday.isin(TOD)]
    report("runtime time of day, all BDD val (includes the fit split)", bt.timeofday.map(TOD),
           bt.p_tod, TOD_LABELS)
    bb = b[b.enc >= 0]
    print("\nenclosed: BDD enclosed", bb[bb.enc == 1].p_enc.mean().round(3),
          "| BDD not enclosed", bb[bb.enc == 0].p_enc.mean().round(4),
          "| by time of day", bb[bb.enc == 0].groupby("timeofday").p_enc.mean().round(3).to_dict())
    truth = ((bb.timeofday != "daytime") | (bb.enc == 1))
    keep = bb.timeofday.isin(TOD) | (bb.enc == 1)
    report("lights ON from the environment, BDD (ON = night | dusk | enclosed)",
           truth[keep].astype(int).values, bb[keep].p_env.isin(["night", "dusk_dawn", "enclosed"]).astype(int).values,
           [0, 1])
    print("\nweather (daytime BDD):", pd.crosstab(b[b.timeofday == "daytime"].weather,
                                                b[b.timeofday == "daytime"].p_wx).to_string())
    for windowed in (False, True):
        dd = run_runtime(d, windowed)
        print(f"\n70mai drive ({'10 s window' if windowed else 'per frame'}):")
        print(pd.crosstab(dd.segment, dd.p_env).to_string())
        print("weather:", dd.p_wx.value_counts().to_dict())
    if w is not None:
        ww = run_runtime(w)
        print("\nDAWN:", pd.crosstab(ww.weather, ww.p_env).to_dict(), pd.crosstab(ww.weather, ww.p_wx).to_dict())
    if a.dump:
        b.to_csv(a.dump, index=False)


def cmd_fixture(a):
    """Stratified BDD sample (features + labels + runtime outputs) for the unit tests."""
    b, _, _ = load()
    b = run_runtime(b)
    parts = []
    for tod, n in (("daytime", 120), ("dawn/dusk", 60), ("night", 80)):
        parts.append(b[(b.timeofday == tod) & (b.enc == 0)].sample(n, random_state=0))
    parts.append(b[b.enc == 1])
    s = pd.concat(parts)
    from cyberpunk_computer.vision.scene_features import FIELDS
    cols = ["file", "timeofday", "weather", "scene", "enc"] + list(FIELDS) + ["p_env", "p_wx"]
    s[cols].to_csv(a.out, index=False)
    print(len(s), "rows ->", a.out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit")
    f.add_argument("--emit")
    sub.add_parser("lights")
    r = sub.add_parser("runtime")
    r.add_argument("--dump")
    x = sub.add_parser("fixture")
    x.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)
    {"fit": cmd_fit, "lights": cmd_lights, "runtime": cmd_runtime, "fixture": cmd_fixture}[a.cmd](a)


if __name__ == "__main__":
    main()
