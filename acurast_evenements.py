#!/usr/bin/env python3
"""Journal des événements Acurast qui concernent vos comptes.

Parcourt chaque bloc de la blockchain Acurast, garde les événements qui citent
le compte principal ou un téléphone, les classe (signal de présence, frais,
remboursement, récompense...) et écrit :
  data/evenements.jsonl  : tous les événements, une ligne par événement
  data/evenements.json   : les 1500 derniers + totaux par jour, pour le tableau de bord
  data/evenements_etat.json : dernier bloc traité (reprise après redémarrage)

Nécessite substrate-interface (installé dans .venv).
"""
import json, os, sys, time
from collections import defaultdict
from datetime import datetime
from substrateinterface import SubstrateInterface

ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ICI, "data")
CONFIG = json.load(open(os.path.join(ICI, "config.json"), encoding="utf-8"))
WSS = CONFIG.get("rpc", "https://public-rpc.mainnet.acurast.com").replace("https://", "wss://")
DEPUIS = int(CONFIG.get("evenements_depuis_bloc", 0))
DEC = 10**12
JSONL = os.path.join(DATA, "evenements.jsonl")
JSON_OUT = os.path.join(DATA, "evenements.json")
ETAT = os.path.join(DATA, "evenements_etat.json")

NOMS = {CONFIG["manager"]: "Compte principal"}
for t in CONFIG["telephones"]:
    NOMS[t["adresse"]] = t["nom"]
PHONES = {t["adresse"] for t in CONFIG["telephones"]}


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def comptes_dans(obj):
    s = json.dumps(obj, default=str)
    return [a for a in NOMS if a in s]


def classer(mod, ev, attr, frais_tx):
    """Retourne (catégorie, compte concerné, montant signé en ACU vu de vos comptes)."""
    a = attr if isinstance(attr, dict) else {}
    qui = a.get("who") or a.get("to") or a.get("from")
    montant = a.get("amount") or a.get("actual_fee")
    m = (montant or 0) / DEC
    if mod == "AcurastProcessorManager" and ev.startswith("ProcessorHeartbeat"):
        return "presence", (attr[0] if isinstance(attr, (list, tuple)) and attr else None), None
    if mod == "AcurastProcessorManager" and ev == "ProcessorRewardSent":
        return "recompense", (comptes_dans(attr) or [None])[0], None
    if mod == "AcurastMarketplace":
        phone = next((p for p in comptes_dans(attr) if p in PHONES), None)
        if ev.startswith(("JobRegistrationAssigned", "JobExecutionMatched", "JobRegistrationMatched")):
            return "tache_attribuee", phone, None
        if ev.startswith("Reported"):
            return "tache_rapport", phone, None
        if ev == "ExecutionSuccess":
            return "tache_ok", phone, None
        if ev == "ExecutionFailure":
            return "tache_echec", phone, None
        if ev == "JobFinalized":
            return "tache_fin", phone, None
    if mod == "TransactionPayment" and ev == "TransactionFeePaid":
        return "frais", qui, -m
    if mod == "Balances":
        if ev == "Released":
            return "reserve_liberee", qui, None   # passe de la réserve au solde, puis sert aux frais
        if ev == "Withdraw":
            return "prelevement", qui, None       # déjà compté dans « frais »
        if ev == "Deposit":
            if frais_tx:
                return "remboursement", qui, m    # trop-perçu des frais rendu
            return "credit", qui, m
        if ev == "Transfer":
            if a.get("to") in NOMS and a.get("from") not in NOMS:
                return "recompense" if a.get("from") not in NOMS else "transfert", a.get("to"), m
            if a.get("from") in NOMS and a.get("to") not in NOMS:
                return "envoi", a.get("from"), -m
            return "transfert", qui, None
        if ev in ("Held", "Reserved"):
            return "reserve_ajoutee", qui, None
    if any(k in (mod + ev).lower() for k in ("reward", "payout", "claim")):
        return "recompense", qui, m if montant else None
    return "autre", qui, None


def charger_etat():
    try:
        return json.load(open(ETAT))["dernier_bloc"]
    except Exception:
        return None


def debut_suivi():
    try:
        return json.load(open(ETAT)).get("debut_t")
    except Exception:
        return None


def sauver_etat(b, debut=None):
    tmp = ETAT + ".tmp"
    json.dump({"dernier_bloc": b, "debut_t": debut or debut_suivi() or int(time.time() * 1000)}, open(tmp, "w"))
    os.replace(tmp, ETAT)


def heures_suivies(jour):
    """Heures du jour couvertes par le suivi (journée partielle le premier jour et aujourd'hui)."""
    d0 = datetime.fromisoformat(jour).timestamp()
    debut = (debut_suivi() or 0) / 1000
    a, b = max(d0, debut), min(d0 + 86400, time.time())
    return max(0.0, (b - a) / 3600)


def ecrire_resume(jours, compte):
    elec = CONFIG.get("electricite") or {}
    w = float(elec.get("watts_par_telephone", 2.0)) * len(CONFIG["telephones"])
    prix = float(elec.get("prix_kwh_eur", 0.195))
    lignes = ["jour,heures_suivies,signaux,frais_acu,recompenses_acu,bilan_acu,electricite_kwh,electricite_eur"]
    for j in sorted(compte):
        h = heures_suivies(j)
        frais = -jours[j].get("frais", 0.0)
        rec = jours[j].get("recompense", 0.0) + jours[j].get("credit", 0.0)
        kwh = w * h / 1000
        lignes.append(f"{j},{h:.2f},{compte[j].get('presence', 0)},{frais:.6f},{rec:.6f},{rec - frais:.6f},{kwh:.4f},{kwh * prix:.4f}")
    tmp = os.path.join(DATA, "resume_quotidien.csv.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")
    os.replace(tmp, os.path.join(DATA, "resume_quotidien.csv"))


def reconstruire_json():
    evs = []
    if os.path.exists(JSONL):
        with open(JSONL, encoding="utf-8") as f:
            evs = [json.loads(l) for l in f if l.strip()]
    jours = defaultdict(lambda: defaultdict(float))
    compte = defaultdict(lambda: defaultdict(int))
    for e in evs:
        j = e["horodatage"][:10]
        if e["montant_acu"] is not None:
            jours[j][e["categorie"]] += e["montant_acu"]
        compte[j][e["categorie"]] += 1
    ecrire_resume(jours, compte)
    out = {"maj": int(time.time() * 1000), "debut_t": debut_suivi(), "total": len(evs), "noms": NOMS,
           "par_jour": {j: {"montants": jours[j], "nombre": compte[j]} for j in sorted(compte)},
           # chaque paiement reçu, sur toute la période (pour le graphique des récompenses)
           "recompenses": [[e["t"], e["montant_acu"], e["bloc"]] for e in evs
                           if e["categorie"] in ("recompense", "credit") and e["montant_acu"]],
           "evenements": evs[-1500:]}
    tmp = JSON_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, JSON_OUT)


def traiter_bloc(s, b):
    h = s.get_block_hash(b)
    evs = s.get_events(h)
    lignes = []
    brut = [e.value for e in evs]
    # extrinsics dans lesquels un de vos comptes a payé des frais
    frais_ext = {v.get("extrinsic_idx") for v in brut
                 if v["event"]["module_id"] == "TransactionPayment" and comptes_dans(v["event"]["attributes"])}
    ts = None
    for v in brut:
        attr = v["event"]["attributes"]
        if not comptes_dans(attr):
            continue
        mod, ev = v["event"]["module_id"], v["event"]["event_id"]
        cat, qui, montant = classer(mod, ev, attr, v.get("extrinsic_idx") in frais_ext)
        if ts is None:
            ts = s.query("Timestamp", "Now", block_hash=h).value
        lignes.append({"horodatage": datetime.fromtimestamp(ts / 1000).isoformat(timespec="seconds"), "t": ts,
                       "bloc": b, "extrinsic": v.get("extrinsic_idx"), "module": mod, "evenement": ev,
                       "categorie": cat, "compte": NOMS.get(qui, qui), "montant_acu": montant,
                       "details": json.loads(json.dumps(attr, default=str))})
    return lignes


COMPUTE = os.path.join(DATA, "compute.json")
COMPUTE_HIST = os.path.join(DATA, "compute.jsonl")


COMPUTE_SERIE = os.path.join(DATA, "compute_serie.json")


def ecrire_serie_compute():
    """Version compacte de compute.jsonl pour les graphiques : [t, {nom: ACU estimés par époque}]."""
    serie = []
    with open(COMPUTE_HIST, encoding="utf-8") as f:
        for l in f:
            try:
                c = json.loads(l)
            except ValueError:
                continue
            serie.append([c["t"], {t["nom"]: round(t["estimation_acu_epoque"], 6) for t in c["telephones"]}])
    tmp = COMPUTE_SERIE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"epoques_par_jour": 16, "serie": serie}, f, ensure_ascii=False)
    os.replace(tmp, COMPUTE_SERIE)


def etat_compute(s):
    """Statut Compute de chaque téléphone + récompense estimée par époque d'après ses métriques."""
    cst = lambda n: s.get_constant("AcurastCompute", n).value
    inflation = cst("InflationPerEpoch") / DEC
    part_metriques = cst("InflationMetricsRatio") / 1e18
    epoque_blocs = cst("Epoch")
    cycle = s.query("AcurastCompute", "CurrentCycle").value
    pools = {k.value: v.value for k, v in s.query_map("AcurastCompute", "MetricPools")}
    tels = []
    for t in CONFIG["telephones"]:
        a = t["adresse"]
        proc = s.query("AcurastCompute", "Processors", [a]).value
        hb = s.query("AcurastProcessorManager", "ProcessorHeartbeat", [a]).value
        ver = s.query("AcurastProcessorManager", "ProcessorVersion", [a]).value
        metriques, estim = {}, 0.0
        for k, v in s.query_map("AcurastCompute", "Metrics", [a]):
            p = pools.get(k.value)
            if not p:
                continue
            nom = bytes.fromhex(p["name"][2:]).decode(errors="ignore").rstrip("_")
            total = p["total"]["prev"] or p["total"]["cur"]
            part = v.value["metric"] / total if total else 0
            gain = inflation * part_metriques * p["reward"]["current"] / 1e18 * part
            metriques[nom] = {"epoque": v.value["epoch"], "part_reseau": part, "acu_par_epoque": gain}
            estim += gain
        tels.append({"nom": t["nom"], "adresse": a, "statut": (proc or {}).get("status"),
                     "engage_epoque": (proc or {}).get("committed"), "paye": ((proc or {}).get("paid") or 0) / DEC,
                     "reclame": (proc or {}).get("claimed"), "contribution": (proc or {}).get("reward_contribution"),
                     "dernier_signal_t": hb, "version": (ver or {}).get("build_number"),
                     "metriques": metriques, "estimation_acu_epoque": estim})
    out = {"t": int(time.time() * 1000), "epoque": cycle["epoch"], "epoque_debut_bloc": cycle["epoch_start"],
           "blocs_par_epoque": epoque_blocs, "epoques_par_jour": 14400 / epoque_blocs,
           "echauffement_blocs": cst("WarmupPeriod"), "telephones": tels}
    tmp = COMPUTE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, COMPUTE)
    with open(COMPUTE_HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    ecrire_serie_compute()
    log("compute : " + ", ".join(f"{t['nom']} {t['statut']} ~{t['estimation_acu_epoque']:.4f} ACU/époque payé {t['paye']}" for t in tels))


def main():
    os.makedirs(DATA, exist_ok=True)
    s = None
    dernier = charger_etat()
    dernier_compute = 0
    while True:
        try:
            if s is None:
                s = SubstrateInterface(url=WSS)
                log("connecté à", WSS)
            if time.time() - dernier_compute > 600:
                dernier_compute = time.time()
                try:
                    etat_compute(s)
                except Exception as e:
                    log("compute indisponible :", repr(e)[:200])
            # seulement les blocs finalisés : un bloc plus récent peut encore être remplacé (fork),
            # et ses événements disparaître (cas du 13-11 à 04:16 : un paiement compté deux fois)
            tete = s.get_block_number(s.get_chain_finalised_head())
            if dernier is None:
                dernier = (DEPUIS - 1) if DEPUIS else tete - 1
                log("premier démarrage, départ au bloc", dernier + 1)
            if dernier >= tete:
                time.sleep(6)
                continue
            fin = min(tete, dernier + 200)
            nouveaux = []
            for b in range(dernier + 1, fin + 1):
                nouveaux += traiter_bloc(s, b)
                dernier = b
            if nouveaux:
                with open(JSONL, "a", encoding="utf-8") as f:
                    for l in nouveaux:
                        f.write(json.dumps(l, ensure_ascii=False) + "\n")
            sauver_etat(dernier)
            if nouveaux or fin == tete:
                reconstruire_json()
            if tete - dernier > 10:
                log(f"rattrapage : bloc {dernier} / {tete}, {len(nouveaux)} événement(s)")
            elif nouveaux:
                log(f"bloc {dernier} : " + ", ".join(f"{l['categorie']} {l['compte']}" for l in nouveaux))
        except Exception as e:
            if "State already discarded" in str(e) and s is not None:
                # le nœud public ne garde que les blocs récents : on saute au plus ancien encore disponible
                try:
                    tete = s.get_block_number(s.get_chain_finalised_head())
                    log(f"bloc {dernier + 1} trop ancien pour ce nœud, reprise au bloc {tete - 150}")
                    dernier = tete - 151
                    sauver_etat(dernier, int(time.time() * 1000) - 150 * 6000)
                    continue
                except Exception:
                    pass
            log("erreur :", repr(e)[:300])
            try:
                s and s.close()
            except Exception:
                pass
            s = None
            time.sleep(20)


if __name__ == "__main__":
    if "--reconstruire" in sys.argv:
        reconstruire_json()
        if os.path.exists(COMPUTE_HIST):
            ecrire_serie_compute()
    else:
        main()
