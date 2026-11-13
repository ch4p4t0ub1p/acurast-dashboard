#!/usr/bin/env python3
"""Relevé automatique des comptes Acurast (manager + téléphones).

Toutes les INTERVALLE secondes, lit la blockchain Acurast et le prix ACU,
ajoute une ligne à data/releves.csv et réécrit data/releves.json.
Sert aussi le dossier en HTTP pour consulter le tableau de bord depuis le réseau local.

Uniquement la bibliothèque standard Python.
"""
import csv, hashlib, ipaddress, json, os, subprocess, sys, threading, time, urllib.request
from datetime import datetime
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ICI = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ICI, "data")
CONFIG_PATH = os.path.join(ICI, "config.json")
CONFIG = json.load(open(CONFIG_PATH, encoding="utf-8"))

RPC = CONFIG.get("rpc", "https://public-rpc.mainnet.acurast.com")
INTERVALLE = int(CONFIG.get("intervalle_secondes", 300))
PORT = int(CONFIG.get("port_http", 8090))
DEC = 10**12
# twox128("System") + twox128("Account") et twox128("Balances") + twox128("Holds")
PREFIXE_COMPTE = "26aa394eea5630e07c48ae0c9558cef7b99d880ec681799c0cf30e8886371da9"
PREFIXE_HOLDS = "c2261276cc9d1f8598ea4b6a74b15c2f811187ede3c61f3269344d168d3e6337"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CSV_PATH = os.path.join(DATA, "releves.csv")
JSON_PATH = os.path.join(DATA, "releves.json")
BASE = ["horodatage", "t_ms", "bloc", "disponible_acu", "reserve_acu", "prix_usd"]
# une colonne de transactions par adresse de téléphone (tx_<adresse>) ; les colonnes des téléphones
# retirés sont gardées pour ne pas perdre l'historique
CHAMPS = BASE + [f"tx_{t['adresse']}" for t in CONFIG["telephones"]]


def log(*a):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def cle(adresse, prefixe):
    n = 0
    for c in adresse:
        n = n * 58 + B58.index(c)
    pk = n.to_bytes(35, "big")[1:33]
    return "0x" + prefixe + hashlib.blake2b(pk, digest_size=16).hexdigest() + pk.hex()


def preparer_csv():
    """Adapte l'en-tête du CSV à la liste actuelle des téléphones (ajout de colonnes, ancien format tx_1, tx_2…)."""
    global CHAMPS
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        lignes = list(csv.DictReader(f))
        f.seek(0)
        entete = next(csv.reader(f), [])
    anciens = [c for c in entete if c.startswith("tx_")]
    renomme = {}
    for c in anciens:   # ancien format : tx_1, tx_2… dans l'ordre de config.json
        if c[3:].isdigit() and int(c[3:]) <= len(CONFIG["telephones"]):
            renomme[c] = "tx_" + CONFIG["telephones"][int(c[3:]) - 1]["adresse"]
    colonnes = BASE + list(dict.fromkeys([renomme.get(c, c) for c in anciens] + CHAMPS[len(BASE):]))
    if colonnes == entete:
        CHAMPS = colonnes
        return
    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=colonnes)
        w.writeheader()
        for l in lignes:
            w.writerow({renomme.get(k, k): v for k, v in l.items() if k is not None})
    os.replace(tmp, CSV_PATH)
    CHAMPS = colonnes
    log("colonnes du CSV mises à jour :", ", ".join(c[:12] + "…" for c in colonnes[len(BASE):]))


def adresse_valide(a):
    """Vérifie une adresse Acurast (SS58, préfixe 42, somme de contrôle)."""
    if not isinstance(a, str) or not 46 <= len(a) <= 48:
        return False
    try:
        n = 0
        for c in a:
            n = n * 58 + B58.index(c)
        b = n.to_bytes(35, "big")
    except (ValueError, OverflowError):
        return False
    return b[0] == 42 and hashlib.blake2b(b"SS58PRE" + b[:33], digest_size=64).digest()[:2] == b[33:35]


def verifier_reglages(d):
    """Contrôle les réglages envoyés par la page ; renvoie (réglages propres, erreur)."""
    try:
        prix = float(d["electricite"]["prix_kwh_eur"])
        watts = float(d["electricite"]["watts_par_telephone"])
    except (KeyError, TypeError, ValueError):
        return None, "prix du kWh et watts par téléphone obligatoires"
    if not 0 <= prix <= 5:
        return None, "le prix du kWh doit être entre 0 et 5 €"
    if not 0 <= watts <= 50:
        return None, "la consommation doit être entre 0 et 50 W par téléphone"
    tels = d.get("telephones")
    if not isinstance(tels, list) or not 1 <= len(tels) <= 20:
        return None, "il faut entre 1 et 20 téléphones"
    propres, vus = [], set()
    for i, t in enumerate(tels, 1):
        nom = str((t or {}).get("nom", "")).strip()
        adr = str((t or {}).get("adresse", "")).strip()
        if not nom or len(nom) > 40 or any(ord(c) < 32 for c in nom):
            return None, f"téléphone {i} : nom vide ou trop long (40 caractères max)"
        if not adresse_valide(adr):
            return None, f"« {nom} » : adresse invalide (une adresse Acurast commence par 5 et fait 48 caractères)"
        if adr == CONFIG["manager"]:
            return None, f"« {nom} » : c'est l'adresse du compte principal, pas celle d'un téléphone"
        if adr in vus:
            return None, f"« {nom} » : adresse déjà utilisée par un autre téléphone"
        vus.add(adr)
        propres.append({"nom": nom, "adresse": adr})
    feu = d.get("feu_artifice", True)
    if not isinstance(feu, bool):
        return None, "feu d'artifice : vrai ou faux attendu"
    alertes, err = verifier_alertes(d.get("alertes") or {})
    if err:
        return None, err
    return {"prix_kwh_eur": round(prix, 6), "watts_par_telephone": round(watts, 3), "telephones": propres,
            "feu_artifice": feu, "alertes": alertes}, None


def verifier_alertes(a):
    actif = a.get("actif", False)
    if not isinstance(actif, bool):
        return None, "alertes : vrai ou faux attendu"
    try:
        minutes = int(a.get("silence_minutes", 75))
    except (TypeError, ValueError):
        return None, "délai d'alerte invalide"
    if not 40 <= minutes <= 720:
        return None, "le délai d'alerte doit être entre 40 et 720 minutes (un signal arrive toutes les 30 min environ)"
    url = str(a.get("webhook", "")).strip()
    if url and (not url.startswith(("http://", "https://")) or "/api/webhook/" not in url or len(url) > 300 or any(c in url for c in " \"'<>")):
        return None, "adresse du webhook invalide (forme attendue : http://homeassistant.local:8123/api/webhook/…)"
    if actif and not url:
        return None, "indiquez l'adresse du webhook Home Assistant pour activer les alertes"
    return {"actif": actif, "silence_minutes": minutes, "webhook": url}, None


def enregistrer_reglages(r):
    """Écrit config.json (copie de sécurité config.json.bak) et redémarre les services concernés."""
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    tel_change = cfg["telephones"] != r["telephones"]
    elec = {"prix_kwh_eur": r["prix_kwh_eur"], "watts_par_telephone": r["watts_par_telephone"]}
    elec_change = any(cfg.get("electricite", {}).get(k) != v for k, v in elec.items())
    cfg.setdefault("electricite", {}).update(elec)
    cfg["telephones"] = r["telephones"]
    cfg["feu_artifice"] = r["feu_artifice"]   # lu directement par la page, pas besoin de redémarrer
    cfg["alertes"] = r["alertes"]
    CONFIG["alertes"] = r["alertes"]           # pris en compte au prochain relevé, sans redémarrage
    with open(CONFIG_PATH + ".bak", "w", encoding="utf-8") as f:
        f.write(open(CONFIG_PATH, encoding="utf-8").read())
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    # le classement ne dépend que des téléphones ; ce service-ci en dernier, car son propre redémarrage
    # arrête aussi la commande systemctl lancée depuis lui
    services = (["acurast-classement"] if tel_change else []) + \
               (["acurast-evenements", "acurast-releve"] if tel_change or elec_change else [])
    log("réglages enregistrés" + (", redémarrage de " + ", ".join(services) if services else ""))
    if not services:
        return services

    def relancer():
        time.sleep(1)   # laisse partir la réponse HTTP avant que ce service ne redémarre lui-même
        env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
        subprocess.run(["systemctl", "--user", "--no-block", "restart", *services], env=env, check=False)
    threading.Thread(target=relancer, daemon=True).start()
    return services


def compte_actif(adresse):
    try:
        return compte(rpc("state_getStorage", [cle(adresse, PREFIXE_COMPTE)]))[0] > 0
    except Exception:
        return None


def rpc(methode, params):
    req = urllib.request.Request(RPC, json.dumps({"id": 1, "jsonrpc": "2.0", "method": methode, "params": params}).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rep = json.load(r)
    if "error" in rep:
        raise RuntimeError(rep["error"])
    return rep["result"]


def le(b):
    return int.from_bytes(b, "little")


def compte(hexa):
    if not hexa:
        return 0, 0
    b = bytes.fromhex(hexa[2:])
    return le(b[0:4]), le(b[16:32])


def holds(hexa):
    if not hexa:
        return 0
    b = bytes.fromhex(hexa[2:])
    n = b[0] >> 2  # longueur du vecteur (compact, < 64 entrées)
    return sum(le(b[1 + i * 18 + 2: 1 + i * 18 + 18]) for i in range(n))


def prix_usd():
    try:
        with urllib.request.urlopen("https://api.coingecko.com/api/v3/simple/price?ids=acurast&vs_currencies=usd", timeout=15) as r:
            return json.load(r)["acurast"]["usd"]
    except Exception as e:
        log("prix indisponible:", e)
        return None


def releve():
    m = CONFIG["manager"]
    _, libre = compte(rpc("state_getStorage", [cle(m, PREFIXE_COMPTE)]))
    reserve = holds(rpc("state_getStorage", [cle(m, PREFIXE_HOLDS)]))
    tx = [compte(rpc("state_getStorage", [cle(t["adresse"], PREFIXE_COMPTE)]))[0] for t in CONFIG["telephones"]]
    bloc = int(rpc("chain_getHeader", [])["number"], 16)
    now = time.time()
    return {"horodatage": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "t_ms": int(now * 1000),
            "bloc": bloc, "disponible_acu": libre / DEC, "reserve_acu": reserve / DEC, "prix_usd": prix_usd(),
            **{f"tx_{t['adresse']}": n for t, n in zip(CONFIG["telephones"], tx)}}


def ecrire(ligne):
    os.makedirs(DATA, exist_ok=True)
    neuf = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CHAMPS)
        if neuf:
            w.writeheader()
        w.writerow(ligne)
    # JSON pour le tableau de bord (format attendu par la page)
    hist = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            hist.append({"t": int(r["t_ms"]), "block": int(r["bloc"]) if r["bloc"] else None, "free": float(r["disponible_acu"]),
                         "hold": float(r["reserve_acu"]), "price": float(r["prix_usd"]) if r["prix_usd"] else None,
                         "n": [int(r[f"tx_{t['adresse']}"]) if r.get(f"tx_{t['adresse']}") else None
                               for t in CONFIG["telephones"]]})
    tmp = JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"maj": ligne["t_ms"], "intervalle_s": INTERVALLE, "manager": CONFIG["manager"],
                   "telephones": CONFIG["telephones"], "electricite": CONFIG.get("electricite"),
                   "comptes": {
                       "manager": {"adresse": CONFIG["manager"], "cle_compte": cle(CONFIG["manager"], PREFIXE_COMPTE),
                                   "cle_reserve": cle(CONFIG["manager"], PREFIXE_HOLDS)},
                       "telephones": [{"nom": t["nom"], "adresse": t["adresse"], "cle_compte": cle(t["adresse"], PREFIXE_COMPTE)}
                                      for t in CONFIG["telephones"]]},
                   "releves": hist}, f)
    os.replace(tmp, JSON_PATH)
    return hist


ALERTES_ETAT = os.path.join(DATA, "alertes_etat.json")


def envoyer_alerte(titre, message, url=None):
    """Appelle le webhook Home Assistant ; l'automatisation HA envoie la notification sur l'iPhone."""
    url = url or (CONFIG.get("alertes") or {}).get("webhook")
    if not url:
        return False, "pas de webhook"
    req = urllib.request.Request(url, json.dumps({"titre": titre, "message": message}, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as rep_:
            rep_.read()
        log("alerte envoyée :", titre)
        return True, None
    except Exception as e:
        log("alerte non envoyée :", titre, e)
        return False, str(e)[:200]


def duree_txt(s):
    m = int(s // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60:02d}"


def surveiller(hist):
    """Alerte quand le compteur de transactions d'un téléphone ne bouge plus depuis trop longtemps
    (il envoie normalement un signal de présence toutes les ~30 min), puis quand il repart."""
    a = CONFIG.get("alertes") or {}
    if not a.get("actif") or not a.get("webhook") or len(hist) < 2:
        return
    seuil = int(a.get("silence_minutes", 75)) * 60
    try:
        etat = json.load(open(ALERTES_ETAT, encoding="utf-8"))
    except Exception:
        etat = {}
    fin = hist[-1]["t"] / 1000   # heure du dernier relevé (pas l'heure actuelle : un relevé raté n'est pas une panne)
    for i, tel in enumerate(CONFIG["telephones"]):
        serie = [(h["t"] / 1000, h["n"][i]) for h in hist if h["n"][i] is not None]
        if not serie:
            continue
        actuel = serie[-1][1]
        depuis = serie[0][0]
        for (t_prec, n_prec), (t, n) in zip(reversed(serie[:-1]), reversed(serie[1:])):
            if n_prec != actuel:
                depuis = t   # premier relevé où le compteur a atteint sa valeur actuelle
                break
        silence = fin - depuis
        e = etat.get(tel["adresse"], {})
        heure = datetime.fromtimestamp(depuis).strftime("%H:%M")
        if silence > seuil and not e.get("panne"):
            ok, _ = envoyer_alerte(f"⚠️ Acurast : {tel['nom']} ne répond plus",
                                   f"Aucun signal depuis {duree_txt(silence)} (dernier vers {heure}). "
                                   "Vérifiez que l'app Acurast est ouverte, l'écran allumé, le Wi-Fi et la charge.")
            if ok:
                etat[tel["adresse"]] = {"panne": True, "depuis": depuis}
        elif silence <= seuil and e.get("panne"):
            ok, _ = envoyer_alerte(f"✅ Acurast : {tel['nom']} refonctionne",
                                   f"Signal reçu vers {heure} après {duree_txt(depuis - e.get('depuis', depuis))} sans signal.")
            if ok:
                etat[tel["adresse"]] = {"panne": False}
    tmp = ALERTES_ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f)
    os.replace(tmp, ALERTES_ETAT)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def repondre(self, code, obj):
        corps = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/config":
            cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
            return self.repondre(200, {"manager": cfg["manager"], "electricite": cfg.get("electricite", {}),
                                       "telephones": cfg["telephones"], "feu_artifice": cfg.get("feu_artifice", True),
                                       "alertes": {"actif": False, "silence_minutes": 75, "webhook": "", **cfg.get("alertes", {})}})
        return super().do_GET()

    def do_POST(self):
        chemin = self.path.split("?")[0]
        if chemin not in ("/api/config", "/api/test-alerte"):
            return self.repondre(404, {"erreur": "inconnu"})
        # uniquement depuis le réseau local, et seulement depuis la page elle-même (pas depuis un autre site)
        if not ipaddress.ip_address(self.client_address[0]).is_private:
            return self.repondre(403, {"erreur": "modification autorisée uniquement depuis le réseau local"})
        origine = self.headers.get("Origin")
        if origine and origine.split("://", 1)[-1] != self.headers.get("Host"):
            return self.repondre(403, {"erreur": "origine refusée"})
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.repondre(415, {"erreur": "JSON attendu"})
        try:
            taille = int(self.headers.get("Content-Length", 0))
            d = json.loads(self.rfile.read(min(taille, 20000)))
        except Exception:
            return self.repondre(400, {"erreur": "requête illisible"})
        if chemin == "/api/test-alerte":
            a, err = verifier_alertes({**d, "actif": False})
            if err or not a["webhook"]:
                return self.repondre(400, {"erreur": err or "indiquez d'abord l'adresse du webhook"})
            ok, detail = envoyer_alerte("🧪 Test Acurast", "Les alertes du Raspberry Pi arrivent bien sur ce téléphone.", a["webhook"])
            return self.repondre(200 if ok else 502, {"ok": ok, "erreur": None if ok else detail})
        r, err = verifier_reglages(d)
        if err:
            return self.repondre(400, {"erreur": err})
        connus = {t["adresse"] for t in CONFIG["telephones"]}
        inactifs = [t["nom"] for t in r["telephones"] if t["adresse"] not in connus and compte_actif(t["adresse"]) is False]
        services = enregistrer_reglages(r)
        self.repondre(200, {"ok": True, "services": services, "inactifs": inactifs})


def serveur():
    while True:   # le port peut rester occupé quelques secondes juste après un redémarrage
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", PORT), partial(Handler, directory=ICI))
            break
        except OSError as e:
            log("port HTTP indisponible, nouvel essai dans 5 s :", e)
            time.sleep(5)
    log(f"tableau de bord : http://<ip-du-pi>:{PORT}/")
    httpd.serve_forever()


def verifier_config():
    """Arrête le service avec un message clair si config.json contient encore les adresses d'exemple."""
    mauvaises = [a for a in [CONFIG["manager"]] + [t["adresse"] for t in CONFIG["telephones"]] if not adresse_valide(a)]
    if mauvaises:
        log("config.json : adresse(s) invalide(s) :", ", ".join(mauvaises),
            "— remplacez-les par vos adresses Acurast (elles commencent par 5).")
        sys.exit(1)


def main():
    verifier_config()
    preparer_csv()
    if PORT:
        threading.Thread(target=serveur, daemon=True).start()
    log(f"relevé toutes les {INTERVALLE} s, {len(CONFIG['telephones'])} téléphone(s)")
    while True:
        debut = time.time()
        try:
            l = releve()
            hist = ecrire(l)
            try:
                surveiller(hist)
            except Exception as e:
                log("surveillance :", repr(e)[:200])
            log(f"bloc {l['bloc']} dispo {l['disponible_acu']:.6f} réserve {l['reserve_acu']:.6f} "
                f"tx {[l['tx_' + t['adresse']] for t in CONFIG['telephones']]} prix {l['prix_usd']}")
        except Exception as e:
            log("erreur de relevé:", e)
        time.sleep(max(10, INTERVALLE - (time.time() - debut)))


if __name__ == "__main__":
    if "--une-fois" in sys.argv:
        PORT = 0
        preparer_csv()
        l = releve(); ecrire(l); print(json.dumps(l, ensure_ascii=False, indent=1))
    else:
        main()
