#!/usr/bin/env python3
"""Classement des métriques Compute de vos téléphones face à tout le réseau Acurast.

Toutes les INTERVALLE secondes (3 h par défaut), lit toutes les métriques du réseau
(AcurastCompute.Metrics, ~100 000 téléphones × 4 métriques, ~7 min de lecture),
calcule le rang de chaque téléphone par métrique et écrit :
  data/classement.json   : dernier classement (pour le tableau de bord)
  data/classement.jsonl  : historique des classements
"""
import json, os, sys, time
from datetime import datetime
from substrateinterface import SubstrateInterface

ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ICI, "data")
CONFIG = json.load(open(os.path.join(ICI, "config.json"), encoding="utf-8"))
WSS = CONFIG.get("rpc", "https://public-rpc.mainnet.acurast.com").replace("https://", "wss://")
INTERVALLE = int(CONFIG.get("classement_intervalle_secondes", 3 * 3600))
OUT = os.path.join(DATA, "classement.json")
HIST = os.path.join(DATA, "classement.jsonl")
MIENS = {t["adresse"]: t["nom"] for t in CONFIG["telephones"]}


RPC = CONFIG.get("rpc", "https://public-rpc.mainnet.acurast.com")
# twox128("AcurastCompute") + twox128("Metrics")
PREFIXE_METRICS = "0x56c55973ec96db3a5ef84bbd48553aebfc6d1549f281e53ef10743af2f4f8495"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def pubkey_hex(adresse):
    n = 0
    for c in adresse:
        n = n * 58 + B58.index(c)
    return n.to_bytes(35, "big")[1:33].hex()


def rpc(methode, params):
    import urllib.request
    req = urllib.request.Request(RPC, json.dumps({"id": 1, "jsonrpc": "2.0", "method": methode, "params": params}).encode(),
                                 {"Content-Type": "application/json"})
    for essai in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                rep = json.load(r)
            if "error" in rep:
                raise RuntimeError(rep["error"])
            return rep["result"]
        except Exception:
            if essai == 3:
                raise
            time.sleep(5 * (essai + 1))


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def classement():
    s = SubstrateInterface(url=WSS)
    epoque = s.query("AcurastCompute", "CurrentCycle").value["epoch"]
    validite = int(s.get_constant("AcurastCompute", "MetricValidity").value)   # en époques
    pools = {}
    for k, v in s.query_map("AcurastCompute", "MetricPools"):
        p = v.value
        pools[k.value] = {"nom": bytes.fromhex(p["name"][2:]).decode(errors="ignore").rstrip("_"),
                          "poids": p["reward"]["current"] / 1e18}
    s.close()
    valeurs = {pid: [] for pid in pools}
    miens = {pid: {} for pid in pools}
    mes_cles = {pubkey_hex(a): nom for a, nom in MIENS.items()}
    t0, n = time.time(), 0
    # lecture brute, page par page : clé = préfixe + twox64(compte) + compte (32 octets) + n° de métrique (1 octet),
    # valeur = époque (u32) + mesure (u128), tout en little-endian
    debut = None
    while True:
        cles = rpc("state_getKeysPaged", [PREFIXE_METRICS, 1000] + ([debut] if debut else []))
        if not cles:
            break
        debut = cles[-1]
        for bloc in rpc("state_queryStorageAt", [cles]):
            for cle, val in bloc["changes"]:
                if not val:
                    continue
                n += 1
                h = cle[2 + len(PREFIXE_METRICS) - 2:]
                compte, pid = h[16:80], int(h[80:82], 16)   # twox64 (8 octets) + compte (32) + métrique (1)
                b = bytes.fromhex(val[2:])
                ep, metrique = int.from_bytes(b[0:4], "little"), int.from_bytes(b[4:20], "little")
                if pid not in valeurs or ep < epoque - validite:   # ignore les mesures périmées
                    continue
                valeurs[pid].append(metrique)
                if compte in mes_cles:
                    miens[pid][mes_cles[compte]] = metrique
        if len(cles) < 1000:
            break
    log(f"{n} mesures lues en {time.time() - t0:.0f} s")
    res = {"t": int(time.time() * 1000), "epoque": epoque, "validite_epoques": validite, "mesures_lues": n, "metriques": {}}
    for pid, vals in valeurs.items():
        vals.sort(reverse=True)
        total = len(vals)
        if not total:
            continue
        q = lambda f: vals[min(total - 1, int(total * f))] / 1e18
        tels = {}
        for nom, val in miens[pid].items():
            # rang = nombre de téléphones strictement meilleurs + 1 (recherche dichotomique sur la liste triée)
            lo, hi = 0, total
            while lo < hi:
                mid = (lo + hi) // 2
                if vals[mid] > val:
                    lo = mid + 1
                else:
                    hi = mid
            rang = lo + 1
            tels[nom] = {"valeur": val / 1e18, "rang": rang, "meilleur_que_pct": 100 * (1 - rang / total)}
        res["metriques"][pools[pid]["nom"]] = {"poids": pools[pid]["poids"], "actifs": total,
                                              "max": vals[0] / 1e18, "top10": q(0.10), "mediane": q(0.5),
                                              "telephones": tels}
    return res


def ecrire(res):
    os.makedirs(DATA, exist_ok=True)
    # garde le classement précédent pour afficher la tendance (le téléphone monte ou descend)
    try:
        prec = json.load(open(OUT, encoding="utf-8"))
        res["precedent"] = {"t": prec["t"], "rangs": {m: {n: t["rang"] for n, t in v["telephones"].items()}
                                                      for m, v in prec["metriques"].items()},
                            "valeurs": {m: {n: t["valeur"] for n, t in v["telephones"].items()}
                                        for m, v in prec["metriques"].items()}}
    except Exception:
        pass
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    with open(HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


def main():
    while True:
        debut = time.time()
        try:
            res = classement()
            ecrire(res)
            for nom_m, m in res["metriques"].items():
                log(nom_m, f"{m['actifs']} actifs", ", ".join(f"{n} n°{t['rang']}" for n, t in m["telephones"].items()))
        except Exception as e:
            log("erreur :", repr(e)[:300])
            time.sleep(300)
            continue
        time.sleep(max(600, INTERVALLE - (time.time() - debut)))


if __name__ == "__main__":
    if "--une-fois" in sys.argv:
        ecrire(classement())
    else:
        main()
