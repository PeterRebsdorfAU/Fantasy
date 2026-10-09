#!/bin/bash
# Dobbeltklik på denne fil for at starte FPL Liga Dashboard.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 er ikke installeret. Hent det fra https://www.python.org/downloads/"
  read -n 1 -s -r -p "Tryk på en tast for at lukke ..."
  exit 1
fi
python3 fpl_liga.py "$@"
