"""
0.5.3.4 - Memory Query Normalization Consolidation: test di regressione.

Fissa il comportamento PRE-REFACTOR (gia' verificato manualmente prima
della consolidazione) di:

- modules.chat.genera_query_memoria
- modules.memory_tools._genera_query_eliminazione

dopo che entrambe sono diventate thin wrapper di
modules.memory_query.estrai_query_da_testo, e testa direttamente
l'helper condiviso.

Nessun file in data/ viene letto o scritto; nessuna chiamata a Ollama.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules.chat import genera_query_memoria, STOPWORD_RECALL_MEMORIA
from modules.memory_query import estrai_query_da_testo
from modules.memory_tools import (
    _genera_query_eliminazione,
    STOPWORD_ELIMINAZIONE_MEMORIA,
)


# =====================================================================
# Matrice di regressione: output atteso PRE-REFACTOR, verificato
# manualmente sull'implementazione originaria prima della
# consolidazione in modules/memory_query.py.
# =====================================================================

MATRICE_ATTESA = {
    "Che editor uso per modificare il progetto Aster?": {
        "recall": [
            "editor modificare", "modificare progetto", "progetto aster",
            "editor", "modificare", "progetto", "aster",
        ],
        "delete": [
            "editor modificare", "modificare progetto", "progetto aster",
            "editor", "modificare", "progetto", "aster",
        ],
    },
    "Elimina il ricordo su Aster": {
        "recall": ["elimina ricordo", "ricordo aster", "elimina", "ricordo", "aster"],
        "delete": ["aster"],
    },
    "Cancella la memoria relativa a VS Code": {
        "recall": [
            "cancella memoria", "memoria relativa", "relativa code",
            "cancella", "memoria", "relativa", "code",
        ],
        "delete": ["relativa code", "relativa", "code"],
    },
    "Cosa ricordi del progetto Aster?": {
        "recall": ["ricordi progetto", "progetto aster", "ricordi", "progetto", "aster"],
        "delete": ["cosa progetto", "progetto aster", "cosa", "progetto", "aster"],
    },
    # query breve
    "Aster": {
        "recall": ["aster"],
        "delete": ["aster"],
    },
    # query verbose
    (
        "Per favore, potresti eliminare definitivamente quel vecchio "
        "ricordo che avevo salvato tempo fa riguardo al progetto Aster "
        "e a VS Code?"
    ): {
        "recall": [
            "favore potresti", "potresti eliminare", "eliminare definitivamente",
            "definitivamente quel", "quel vecchio", "vecchio ricordo",
            "ricordo avevo", "avevo salvato", "salvato tempo", "tempo riguardo",
            "riguardo progetto", "progetto aster", "aster code",
            "favore", "potresti", "eliminare", "definitivamente", "quel",
            "vecchio", "ricordo", "avevo", "salvato", "tempo", "riguardo",
            "progetto", "aster", "code",
        ],
        "delete": [
            "favore potresti", "potresti definitivamente", "definitivamente quel",
            "quel vecchio", "vecchio avevo", "avevo salvato", "salvato tempo",
            "tempo riguardo", "riguardo progetto", "progetto aster", "aster code",
            "favore", "potresti", "definitivamente", "quel", "vecchio", "avevo",
            "salvato", "tempo", "riguardo", "progetto", "aster", "code",
        ],
    },
    # punteggiatura
    "Elimina, per favore, il ricordo su Aster!": {
        "recall": ["elimina favore", "favore ricordo", "ricordo aster", "elimina", "favore", "ricordo", "aster"],
        "delete": ["favore aster", "favore", "aster"],
    },
    # maiuscole/minuscole miste
    "ELIMINA IL RICORDO SU aster": {
        "recall": ["elimina ricordo", "ricordo aster", "elimina", "ricordo", "aster"],
        "delete": ["aster"],
    },
}


class TestRegressioneQueryRecall(unittest.TestCase):
    """genera_query_memoria deve produrre esattamente l'output pre-refactor."""

    def test_matrice_recall(self):
        for testo, atteso in MATRICE_ATTESA.items():
            with self.subTest(testo=testo):
                self.assertEqual(genera_query_memoria(testo), atteso["recall"])


class TestRegressioneQueryDelete(unittest.TestCase):
    """_genera_query_eliminazione deve produrre esattamente l'output pre-refactor."""

    def test_matrice_delete(self):
        for testo, atteso in MATRICE_ATTESA.items():
            with self.subTest(testo=testo):
                self.assertEqual(_genera_query_eliminazione(testo), atteso["delete"])


class TestStopwordInvariate(unittest.TestCase):
    """Le stopword dei due domini non devono essere state alterate."""

    def test_stopword_recall_invariate(self):
        attese = {
            "che", "chi", "cosa", "come", "dove", "quando", "quale", "quali", "qual",
            "uso", "usi", "usa", "usare",
            "per", "con",
            "del", "della", "dei", "delle",
            "nel", "nella", "nei", "nelle",
            "il", "lo", "la", "i", "gli", "le",
            "un", "uno", "una",
            "mio", "mia", "miei", "mie",
        }
        self.assertEqual(STOPWORD_RECALL_MEMORIA, attese)

    def test_stopword_delete_invariate(self):
        attese = {
            "il", "lo", "la", "i", "gli", "le",
            "un", "uno", "una",
            "ricordo", "ricordi", "memoria",
            "elimina", "eliminare", "cancella", "cancellare",
            "su", "di", "del", "della", "dei", "delle",
            "per", "che",
            "quello", "quella",
        }
        self.assertEqual(STOPWORD_ELIMINAZIONE_MEMORIA, attese)


class TestHelperCondiviso(unittest.TestCase):
    """Test diretto di modules.memory_query.estrai_query_da_testo."""

    def test_ordine_bigrammi_poi_singole(self):
        risultato = estrai_query_da_testo(
            "progetto aster memoria",
            stopword=set(),
        )
        # tre parole significative -> 2 bigrammi, poi 3 singole,
        # nello stesso ordine di apparizione nel testo.
        self.assertEqual(
            risultato,
            [
                "progetto aster",
                "aster memoria",
                "progetto",
                "aster",
                "memoria",
            ],
        )

    def test_filtro_lunghezza_minima(self):
        risultato = estrai_query_da_testo(
            "usa vs vai su vscode adesso",
            stopword=set(),
        )
        # "usa"(3), "vs"(2), "vai"(3), "su"(2) sotto soglia -> esclusi;
        # "vscode"(6) e "adesso"(6) restano.
        self.assertNotIn("usa", risultato)
        self.assertNotIn("vs", risultato)
        self.assertNotIn("vai", risultato)
        self.assertNotIn("su", risultato)
        self.assertIn("vscode", risultato)
        self.assertIn("adesso", risultato)

    def test_stopword_applicate_correttamente(self):
        risultato = estrai_query_da_testo(
            "il mio progetto preferito",
            stopword={"il", "mio", "preferito"},
        )
        # solo "progetto" (4+ lettere, non in stopword) resta;
        # una sola parola significativa -> nessun bigramma, una singola.
        self.assertEqual(risultato, ["progetto"])

    def test_stopword_diverse_producono_output_diversi_sullo_stesso_testo(self):
        testo = "Elimina il ricordo su Aster"
        risultato_senza_dominio = estrai_query_da_testo(testo, stopword=set())
        risultato_con_stopword_delete = estrai_query_da_testo(
            testo, STOPWORD_ELIMINAZIONE_MEMORIA
        )
        self.assertNotEqual(risultato_senza_dominio, risultato_con_stopword_delete)
        self.assertEqual(risultato_con_stopword_delete, ["aster"])

    def test_nessuna_deduplicazione(self):
        # una stessa parola significativa ripetuta produce voci ripetute
        # nell'output: nessuna deduplicazione e' stata introdotta.
        risultato = estrai_query_da_testo("aster aster", stopword=set())
        self.assertEqual(
            risultato,
            ["aster aster", "aster", "aster"],
        )


if __name__ == "__main__":
    unittest.main()
