"""
0.6.1 - Generic Tool Registry Foundation: test di regressione.

Verifica che modules/tool_registry.py registri correttamente i 7 tool
memoria esistenti come adapter su modules.memory_tools.esegui_tool_memoria
(invariato), senza introdurre alcuna nuova capacita' e senza alterare
il risultato restituito rispetto al dispatcher precedente.

Ogni test che esegue un dispatch reale lavora su una directory temporanea
isolata (setUp/tearDown): nessun file in data/ viene mai letto o scritto
da questi test. setUpModule/tearDownModule verificano inoltre che i file
reali in data/ non risultino modificati dall'intera esecuzione del file.

Nessuna chiamata a Ollama: questi test esercitano solo modules/tool_registry.py
e modules/memory_tools.py, mai modules/chat.py o modules/ollama_manager.py.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from modules.memory import MODALITA_NORMALE, StatoMemoria
from modules.memory_session import MemorySessionState
from modules.memory_tools import TOOLS_MEMORIA, esegui_tool_memoria
from modules.tool_registry import (
    ContestoMemoria,
    RegistroStrumenti,
    ToolSpec,
    crea_registro_memoria,
)

LIMITE_RICERCA = 5


# =====================================================================
# Verifica whole-file: i file reali in data/ non vengono mai mutati
# dall'esecuzione di questi test (stesso principio hash-based gia'
# usato nella regressione manuale delle serie 0.5.3.x).
# =====================================================================

_FILE_MEMORIA_REALE = BASE_DIR / "data" / "memory.json"
_FILE_CESTINO_REALE = BASE_DIR / "data" / "deleted_memories.json"

_hash_dati_reali_iniziale = None


def _hash_file(percorso: Path):
    if not percorso.exists():
        return None
    return hashlib.sha256(percorso.read_bytes()).hexdigest()


def setUpModule():
    global _hash_dati_reali_iniziale
    _hash_dati_reali_iniziale = (
        _hash_file(_FILE_MEMORIA_REALE),
        _hash_file(_FILE_CESTINO_REALE),
    )


def tearDownModule():
    hash_finale = (
        _hash_file(_FILE_MEMORIA_REALE),
        _hash_file(_FILE_CESTINO_REALE),
    )
    assert hash_finale == _hash_dati_reali_iniziale, (
        "I file reali in data/ risultano modificati dall'esecuzione "
        "dei test del tool registry."
    )


def _schema_fittizio(nome: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": nome,
            "description": "Tool fittizio usato solo nei test del registry.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


# =====================================================================
# Registrazione dei tool memoria: schema, lookup, nessuna sovrascrittura
# silenziosa.
# =====================================================================

class TestRegistrazioneToolMemoria(unittest.TestCase):

    def test_tutti_schema_memoria_registrati(self):
        registro = crea_registro_memoria()

        nomi_registrati = {
            schema["function"]["name"]
            for schema in registro.elenco_schema()
        }
        nomi_attesi = {
            schema["function"]["name"]
            for schema in TOOLS_MEMORIA
        }

        self.assertEqual(nomi_registrati, nomi_attesi)
        self.assertEqual(len(nomi_attesi), 7)

    def test_elenco_schema_esattamente_i_tool_attesi(self):
        registro = crea_registro_memoria()

        self.assertEqual(registro.elenco_schema(), TOOLS_MEMORIA)

    def test_lookup_tool_valido(self):
        registro = crea_registro_memoria()

        tool_spec = registro.trova("cerca_memoria")

        self.assertIsNotNone(tool_spec)
        self.assertEqual(tool_spec.nome, "cerca_memoria")
        self.assertEqual(tool_spec.dominio, "memory")
        self.assertEqual(tool_spec.livello, "READ_ONLY")

    def test_lookup_tool_inesistente(self):
        registro = crea_registro_memoria()

        self.assertIsNone(
            registro.trova("tool_che_non_esiste_mai")
        )

    def test_nome_duplicato_non_sovrascrive_silenziosamente(self):
        registro = RegistroStrumenti()

        handler_originale = lambda argomenti, contesto: {"ok": True}
        handler_sostitutivo = lambda argomenti, contesto: {"ok": False}

        registro.registra(
            ToolSpec(
                nome="dummy_tool",
                schema=_schema_fittizio("dummy_tool"),
                handler=handler_originale,
                livello="READ_ONLY",
                dominio="test",
            )
        )

        with self.assertRaises(ValueError):
            registro.registra(
                ToolSpec(
                    nome="dummy_tool",
                    schema=_schema_fittizio("dummy_tool"),
                    handler=handler_sostitutivo,
                    livello="READ_ONLY",
                    dominio="test",
                )
            )

        # Il tentativo fallito non deve aver alterato la registrazione
        # originale.
        self.assertIs(
            registro.trova("dummy_tool").handler,
            handler_originale,
        )


# =====================================================================
# dispatch(): tool sconosciuto, tool FORBIDDEN.
# =====================================================================

class TestDispatchGovernance(unittest.TestCase):

    def test_dispatch_tool_inesistente_errore_strutturato(self):
        registro = crea_registro_memoria()

        # contesto=None e' deliberato: se dispatch chiamasse per errore
        # un handler, questo esploderebbe cercando attributi su None.
        risultato = registro.dispatch(
            "tool_fantasma_non_registrato", {}, None
        )

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "tool_error")
        self.assertIsNone(risultato["domain"])
        self.assertIn("tool_fantasma_non_registrato", risultato["error"])

    def test_tool_forbidden_non_esegue_mai_handler(self):
        registro = RegistroStrumenti()
        chiamato = []

        def handler(argomenti, contesto):
            chiamato.append(True)
            return {"ok": True}

        registro.registra(
            ToolSpec(
                nome="tool_vietato",
                schema=_schema_fittizio("tool_vietato"),
                handler=handler,
                livello="FORBIDDEN",
                dominio="test",
            )
        )

        risultato = registro.dispatch("tool_vietato", {}, None)

        self.assertFalse(risultato["ok"])
        self.assertEqual(risultato["status"], "tool_error")
        self.assertEqual(chiamato, [])


# =====================================================================
# dispatch() sui tool memoria reali: argomenti correttamente inoltrati,
# risultato identico al dispatcher precedente (esegui_tool_memoria
# chiamato direttamente).
# =====================================================================

class TestDispatchMemoriaParita(unittest.TestCase):

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix="aster_test_registry_"))
        self.percorso_memoria = self.base_dir / "memory.json"

        archivio = {
            "meta": {"schema_version": 2, "next_memory_id": 2},
            "memories": [
                {
                    "id": 1,
                    "content": "Sem usa Visual Studio Code come editor",
                    "created_at": "2026-01-01T10:00:00+01:00",
                    "updated_at": "2026-01-01T10:00:00+01:00",
                }
            ],
        }

        with open(self.percorso_memoria, "w", encoding="utf-8") as file:
            json.dump(archivio, file, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    def _contesto(self):
        return ContestoMemoria(
            stato_memoria=StatoMemoria(memoria=None, modalita=MODALITA_NORMALE),
            stato_sessione=MemorySessionState(),
            percorso_memoria=self.percorso_memoria,
            limite_ricerca=LIMITE_RICERCA,
        )

    def test_dispatch_memoria_passa_argomenti_correttamente(self):
        registro = crea_registro_memoria()

        risultato = registro.dispatch(
            "cerca_memoria", {"query": "editor"}, self._contesto()
        )

        self.assertEqual(risultato["status"], "searched")
        self.assertGreaterEqual(risultato["returned"], 1)
        self.assertIn(
            "Visual Studio Code",
            risultato["results"][0]["content"],
        )

    def test_risultato_identico_al_dispatcher_precedente_ricerca(self):
        registro = crea_registro_memoria()
        argomenti = {"query": "editor"}

        risultato_diretto = esegui_tool_memoria(
            nome_tool="cerca_memoria",
            argomenti=argomenti,
            stato_memoria=StatoMemoria(memoria=None, modalita=MODALITA_NORMALE),
            stato_sessione=MemorySessionState(),
            percorso_memoria=self.percorso_memoria,
            limite_ricerca=LIMITE_RICERCA,
        )

        risultato_registry = registro.dispatch(
            "cerca_memoria", argomenti, self._contesto()
        )

        self.assertEqual(risultato_registry, risultato_diretto)

    def test_risultato_identico_al_dispatcher_precedente_proposta_creazione(self):
        registro = crea_registro_memoria()
        argomenti = {
            "content": "Nuovo fatto stabile di prova per il registry",
            "mode": "proposal",
        }

        risultato_diretto = esegui_tool_memoria(
            nome_tool="crea_memoria",
            argomenti=argomenti,
            stato_memoria=StatoMemoria(memoria=None, modalita=MODALITA_NORMALE),
            stato_sessione=MemorySessionState(),
            percorso_memoria=self.percorso_memoria,
            limite_ricerca=LIMITE_RICERCA,
        )

        risultato_registry = registro.dispatch(
            "crea_memoria", argomenti, self._contesto()
        )

        self.assertEqual(risultato_registry, risultato_diretto)
        # mode="proposal" non scrive su disco: solo pending in sessione.
        self.assertEqual(risultato_diretto["status"], "pending_confirmation")

    def test_dispatch_tool_sconosciuto_non_tocca_archivio_reale_di_test(self):
        registro = crea_registro_memoria()

        hash_prima = hashlib.sha256(
            self.percorso_memoria.read_bytes()
        ).hexdigest()

        registro.dispatch(
            "tool_inventato_dal_modello", {"query": "x"}, self._contesto()
        )

        hash_dopo = hashlib.sha256(
            self.percorso_memoria.read_bytes()
        ).hexdigest()

        self.assertEqual(hash_prima, hash_dopo)


if __name__ == "__main__":
    unittest.main()
