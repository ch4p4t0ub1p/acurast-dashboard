#!/usr/bin/env bash
# Installe les trois services (utilisateur) du tableau de bord Acurast.
# À lancer depuis le dossier du projet, après avoir rempli config.json :
#   cp config.example.json config.json && nano config.json && ./installer.sh
set -euo pipefail

DOSSIER="$(cd "$(dirname "$0")" && pwd)"
cd "$DOSSIER"

if [ ! -f config.json ]; then
    echo "config.json absent : copiez config.example.json en config.json et mettez vos adresses." >&2
    exit 1
fi

# Vérifie les adresses avant d'aller plus loin
python3 - <<'EOF'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("releve", "acurast_releve.py")
m = importlib.util.module_from_spec(spec); sys.argv = ["x"]; spec.loader.exec_module(m)
c = json.load(open("config.json", encoding="utf-8"))
mauvaises = [a for a in [c["manager"]] + [t["adresse"] for t in c["telephones"]] if not m.adresse_valide(a)]
if mauvaises:
    sys.exit("config.json : adresse(s) invalide(s) : " + ", ".join(mauvaises))
print("config.json : OK,", len(c["telephones"]), "téléphone(s)")
EOF

echo "Environnement Python (.venv)…"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "Services systemd (utilisateur)…"
mkdir -p "$HOME/.config/systemd/user"
for s in releve evenements classement; do
    sed "s|@DOSSIER@|$DOSSIER|g" "systemd/acurast-$s.service" > "$HOME/.config/systemd/user/acurast-$s.service"
done
systemctl --user daemon-reload
systemctl --user enable --now acurast-releve acurast-evenements acurast-classement

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo
    echo "Pour que les services tournent aussi quand vous n'êtes pas connecté, lancez une fois :"
    echo "  sudo loginctl enable-linger $USER"
fi

PORT=$(python3 -c "import json; print(json.load(open('config.json')).get('port_http', 8090))")
echo
echo "C'est prêt. Tableau de bord : http://$(hostname -I | awk '{print $1}'):$PORT/"
