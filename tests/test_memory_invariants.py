"""
0.5.3.3 - Memory Safety Invariants Test Suite.

Trasforma in test di regressione automatici le protezioni conservative
gia' esistenti nel layer memoria (modules/memory.py, modules/memory_tools.py),
senza introdurre nuovo codice produttivo.

Ogni test lavora esclusivamente su una directory temporanea isolata,
creata ed eliminata per ogni singolo metodo di test (setUp/tearDown):
nessun file in data/ viene mai letto o scritto, nessun test dipende
dall'ordine di esecuzione degli altri.

Nessuna chiamata a Ollama: questi test esercitano solo il layer
Python di memoria/pending, mai modules/chat.py o modules/ollama_manager.py.

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

from modules.memory import StatoMemoria, MODALITA_NORMALE, carica_archivio, carica_cestino
from modules.memory_session import MemorySessionState, PendingAction, imposta_pending
from modules.memory_tools import esegui_tool_memoria

LIMITE_RICERCA = 5


class MemoriaTestCase(unittest.TestCase):
    """Base comune: directory temporanea isolata per ogni test."""

    def setUp(self):
        self.base_dir = Path(tempfile.mkdtemp(prefix="aster_test_invariants_"))
        self.percorso_memoria = self.base_dir / "memory.json"
        self.percorso_cestino = self.base_dir / "deleted_memories.json"

    def tearDown(self):
        shutil.rmtree(self.base_dir, ignore_errors=True)

    # ---------------------------------------------------------------
    # Helper di seeding e lettura
    # ---------------------------------------------------------------

    @staticmethod
    def _archivio(memories, next_id):
        return {
            "meta": {"schema_version": 2, "next_memory_id": next_id},
            "memories": memories,
        }

    @staticmethod
    def _cestino(deleted):
        return {
            "meta": {"schema_version": 2},
            "deleted_memories": deleted,
        }

    @staticmethod
    def _ricordo(id_, content, ts="2026-01-01T10:00:00+01:00"):
        return {
            "id": id_,
            "content": content,
            "created_at": ts,
            "updated_at": ts,
        }

    @staticmethod
    def _ricordo_eliminato(id_, content, ts="2026-01-01T10:00:00+01:00", deleted_ts="2026-01-01T11:00:00+01:00"):
        return {
            "id": id_,
            "content": content,
            "created_at": ts,
            "updated_at": ts,
            "deleted_at": deleted_ts,
        }

    def _scrivi(self, percorso, contenuto):
        percorso.write_text(
            json.dumps(contenuto, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )

    def _seed_memoria(self, memories, next_id):
        self._scrivi(self.percorso_memoria, self._archivio(memories, next_id))

    def _seed_cestino(self, deleted):
        self._scrivi(self.percorso_cestino, self._cestino(deleted))

    def _hash(self, percorso):
        if not percorso.exists():
            return None
        return hashlib.sha256(percorso.read_bytes()).hexdigest()

    def _hash_memoria(self):
        return self._hash(self.percorso_memoria)

    def _hash_cestino(self):
        return self._hash(self.percorso_cestino)

    def _nuovo_stato(self):
        """Nuovo StatoMemoria (NORMALE) + MemorySessionState puliti."""
        return (
            StatoMemoria(modalita=MODALITA_NORMALE, memoria=None),
            MemorySessionState(),
        )

    def _tool(self, nome_tool, argomenti, stato_memoria, stato_sessione):
        return esegui_tool_memoria(
            nome_tool=nome_tool,
            argomenti=argomenti,
            stato_memoria=stato_memoria,
            stato_sessione=stato_sessione,
            percorso_memoria=self.percorso_memoria,
            limite_ricerca=LIMITE_RICERCA,
        )


# =====================================================================
# INVARIANTE A - ZERO TARGET CERTO -> ZERO MUTAZIONE
# =====================================================================

class TestInvarianteA_ZeroTargetZeroMutazione(MemoriaTestCase):

    def test_delete_id_inesistente_nessuna_mutazione(self):
        self._seed_memoria([self._ricordo(1, "Ricordo attivo")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool("elimina_memoria", {"memory_id": 999}, sm, ss)

        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)

    def test_update_id_inesistente_nessuna_mutazione(self):
        self._seed_memoria([self._ricordo(1, "Ricordo attivo")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool(
            "modifica_memoria",
            {"memory_id": 999, "new_content": "Nuovo contenuto"},
            sm, ss,
        )

        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)

    def test_restore_id_inesistente_nessuna_mutazione(self):
        self._seed_memoria([], next_id=1)
        self._seed_cestino([self._ricordo_eliminato(5, "Ricordo eliminato")])
        hash_memoria_prima = self._hash_memoria()
        hash_cestino_prima = self._hash_cestino()
        sm, ss = self._nuovo_stato()

        r = self._tool("ripristina_memoria", {"memory_id": 999}, sm, ss)

        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_memoria_prima)
        self.assertEqual(self._hash_cestino(), hash_cestino_prima)
        self.assertIsNone(ss.pending_action)

    def test_delete_per_query_zero_match_nessuna_mutazione(self):
        self._seed_memoria([self._ricordo(1, "Ricordo su un altro argomento")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool(
            "elimina_memoria_per_query",
            {"query": "TermineCheNonEsisteAffatto"},
            sm, ss,
        )

        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)


# =====================================================================
# INVARIANTE B - PIU' TARGET POSSIBILI -> PENDING_SELECTION
# =====================================================================

class TestInvarianteB_PendingSelection(MemoriaTestCase):

    def setUp(self):
        super().setUp()
        self.ricordi = [
            self._ricordo(1, "Per modificare Aster uso VS Code."),
            self._ricordo(2, "Per i test di Aster uso una memoria separata."),
        ]
        self._seed_memoria(self.ricordi, next_id=3)

    def test_query_ambigua_produce_pending_selection_con_candidati_reali(self):
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)

        self.assertEqual(r["status"], "pending_selection")
        self.assertEqual(self._hash_memoria(), hash_prima, "nessuna mutazione durante la selezione")

        candidati = r["candidates"]
        self.assertEqual(len(candidati), 2)

        attesi = {(x["id"], x["content"]) for x in self.ricordi}
        ottenuti = {(c["id"], c["content"]) for c in candidati}
        self.assertEqual(ottenuti, attesi, "i candidati devono provenire dall'archivio reale")

        self.assertIsNotNone(ss.pending_action)
        self.assertEqual(ss.pending_action.phase, "selection")
        self.assertEqual(len(ss.pending_action.candidates), 2)

    def test_nessun_id_scelto_automaticamente(self):
        sm, ss = self._nuovo_stato()
        r = self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)

        self.assertEqual(r["status"], "pending_selection")
        self.assertNotIn("memory_id", r, "con piu' candidati non deve mai comparire un memory_id gia' scelto")
        self.assertIsNone(ss.pending_action.target_id, "nessun target deve essere gia' fissato in fase di selection")

    def test_select_id_fuori_dai_candidati(self):
        sm, ss = self._nuovo_stato()
        self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)
        hash_prima = self._hash_memoria()

        r = self._tool(
            "gestisci_pending_memoria",
            {"decision": "select", "memory_id": 999},
            sm, ss,
        )

        self.assertEqual(r["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNotNone(ss.pending_action)
        self.assertEqual(ss.pending_action.phase, "selection", "il pending deve restare in fase di selezione")
        self.assertEqual(len(ss.pending_action.candidates), 2, "i candidati non devono essere alterati")


# =====================================================================
# INVARIANTE C - MUTAZIONI CONFERMABILI -> PENDING_CONFIRMATION
# =====================================================================

class TestInvarianteC_PendingConfirmation(MemoriaTestCase):

    def test_update_richiede_conferma_applica_una_sola_volta(self):
        self._seed_memoria([self._ricordo(1, "Contenuto originale")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r1 = self._tool(
            "modifica_memoria",
            {"memory_id": 1, "new_content": "Contenuto aggiornato"},
            sm, ss,
        )
        self.assertEqual(r1["status"], "pending_confirmation")
        self.assertEqual(self._hash_memoria(), hash_prima, "nessuna mutazione prima della conferma")

        r2 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r2["status"], "updated")
        hash_dopo_confirm = self._hash_memoria()
        self.assertNotEqual(hash_dopo_confirm, hash_prima)

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Contenuto aggiornato")

        # un secondo confirm non deve poter riapplicare nulla: il pending
        # e' gia' stato azzerato dal primo confirm.
        r3 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r3["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_dopo_confirm, "nessuna doppia applicazione")

    def test_delete_richiede_conferma_applica_una_sola_volta(self):
        self._seed_memoria([self._ricordo(1, "Da eliminare")], next_id=2)
        hash_memoria_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r1 = self._tool("elimina_memoria", {"memory_id": 1}, sm, ss)
        self.assertEqual(r1["status"], "pending_confirmation")
        self.assertEqual(self._hash_memoria(), hash_memoria_prima)
        self.assertFalse(self.percorso_cestino.exists(), "nessun cestino creato prima della conferma")

        r2 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r2["status"], "deleted")

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"], [])
        cestino = carica_cestino(self.percorso_cestino)
        self.assertEqual(len(cestino["deleted_memories"]), 1)
        hash_memoria_dopo = self._hash_memoria()
        hash_cestino_dopo = self._hash_cestino()

        r3 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r3["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_memoria_dopo)
        self.assertEqual(self._hash_cestino(), hash_cestino_dopo)

        cestino_finale = carica_cestino(self.percorso_cestino)
        self.assertEqual(len(cestino_finale["deleted_memories"]), 1, "nessuna doppia voce nel cestino")

    def test_restore_richiede_conferma_applica_una_sola_volta(self):
        self._seed_memoria([], next_id=1)
        self._seed_cestino([self._ricordo_eliminato(7, "Ricordo da ripristinare")])
        sm, ss = self._nuovo_stato()

        r1 = self._tool("ripristina_memoria", {"memory_id": 7}, sm, ss)
        self.assertEqual(r1["status"], "pending_confirmation")

        archivio_prima = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio_prima["memories"], [])

        r2 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r2["status"], "restored")

        archivio_dopo = carica_archivio(self.percorso_memoria)
        self.assertEqual(len(archivio_dopo["memories"]), 1)
        self.assertEqual(archivio_dopo["memories"][0]["id"], 7)
        cestino_dopo = carica_cestino(self.percorso_cestino)
        self.assertEqual(cestino_dopo["deleted_memories"], [])

        hash_memoria_dopo = self._hash_memoria()
        hash_cestino_dopo = self._hash_cestino()

        r3 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)
        self.assertEqual(r3["status"], "not_found")
        self.assertEqual(self._hash_memoria(), hash_memoria_dopo)
        self.assertEqual(self._hash_cestino(), hash_cestino_dopo)

        archivio_finale = carica_archivio(self.percorso_memoria)
        self.assertEqual(len(archivio_finale["memories"]), 1, "nessuna duplicazione del ricordo ripristinato")

    def test_cancel_non_modifica_nulla(self):
        self._seed_memoria([self._ricordo(1, "Contenuto")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        self._tool(
            "modifica_memoria",
            {"memory_id": 1, "new_content": "Tentativo di modifica"},
            sm, ss,
        )
        r = self._tool("gestisci_pending_memoria", {"decision": "cancel"}, sm, ss)

        self.assertEqual(r["status"], "cancelled")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Contenuto")

    def test_confirm_durante_fase_selection_e_validation_error(self):
        self._seed_memoria(
            [
                self._ricordo(1, "Per modificare Aster uso VS Code."),
                self._ricordo(2, "Per i test di Aster uso una memoria separata."),
            ],
            next_id=3,
        )
        sm, ss = self._nuovo_stato()

        r1 = self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)
        self.assertEqual(r1["status"], "pending_selection")
        hash_prima = self._hash_memoria()

        r2 = self._tool("gestisci_pending_memoria", {"decision": "confirm"}, sm, ss)

        self.assertEqual(r2["status"], "validation_error")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNotNone(ss.pending_action)
        self.assertEqual(ss.pending_action.phase, "selection")


# =====================================================================
# INVARIANTE D - CREATE EXPLICIT (solo contratto reale, non intento)
# =====================================================================

class TestInvarianteD_CreateExplicitContrattoReale(MemoriaTestCase):
    """
    NOTA IMPORTANTE:

    mode="explicit" presuppone che il chiamante abbia gia' risolto
    l'intento dell'utente come esplicito e il contenuto come completo.
    Questa precondizione diventera' testabile quando/esclusivamente se
    verra' introdotto un fallback Python per create.

    Qui si testa SOLO il contratto tecnico gia' reale oggi
    (content non vuoto + mode="explicit" -> scrittura immediata),
    non una validazione dell'intento linguistico, che Python non
    possiede.
    """

    def test_explicit_con_contenuto_valido_crea_subito_senza_pending(self):
        self._seed_memoria([], next_id=1)
        sm, ss = self._nuovo_stato()

        r = self._tool(
            "crea_memoria",
            {"content": "Uso VS Code per modificare Aster.", "mode": "explicit"},
            sm, ss,
        )

        self.assertEqual(r["status"], "created")
        self.assertIsNone(ss.pending_action, "explicit non deve mai lasciare un pending")

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(len(archivio["memories"]), 1)
        self.assertEqual(archivio["memories"][0]["content"], "Uso VS Code per modificare Aster.")


# =====================================================================
# INVARIANTE E - SENSITIVE PRIMA DI PENDING/SCRITTURA
# =====================================================================

class TestInvarianteE_SensitivePrimaDiPendingOScrittura(MemoriaTestCase):

    VALORE_SEGRETO = "Pippo123!"

    def test_create_explicit_sensitive_nessuna_scrittura(self):
        self._seed_memoria([], next_id=1)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool(
            "crea_memoria",
            {"content": f"La password di Gmail e' {self.VALORE_SEGRETO}", "mode": "explicit"},
            sm, ss,
        )

        self.assertEqual(r["status"], "blocked_sensitive")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)
        self.assertNotIn(self.VALORE_SEGRETO, json.dumps(r, ensure_ascii=False))

    def test_create_proposal_sensitive_nessun_pending(self):
        self._seed_memoria([], next_id=1)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        token_di_test = "1234567890abcdefghijklmnop"
        r = self._tool(
            "crea_memoria",
            {"content": f"Il token GitHub e' ghp_{token_di_test}", "mode": "proposal"},
            sm, ss,
        )

        self.assertEqual(r["status"], "blocked_sensitive")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)
        self.assertNotIn(token_di_test, json.dumps(r, ensure_ascii=False))

    def test_update_sensitive_nessun_pending(self):
        self._seed_memoria([self._ricordo(1, "Contenuto originale")], next_id=2)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool(
            "modifica_memoria",
            {"memory_id": 1, "new_content": f"Il PIN della carta e' 1234"},
            sm, ss,
        )

        self.assertEqual(r["status"], "blocked_sensitive")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertIsNone(ss.pending_action)
        self.assertNotIn("1234", json.dumps(r, ensure_ascii=False))

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"][0]["content"], "Contenuto originale")


# =====================================================================
# ALTRI TEST CONSERVATIVI
# =====================================================================

class TestAltriControlliConservativi(MemoriaTestCase):

    def test_pending_esistente_blocca_nuova_azione_incompatibile(self):
        self._seed_memoria(
            [self._ricordo(1, "Primo"), self._ricordo(2, "Secondo")],
            next_id=3,
        )
        sm, ss = self._nuovo_stato()

        imposta_pending(
            ss,
            PendingAction(
                operation="update",
                phase="confirmation",
                target_id=1,
                before="Primo",
                after="Primo modificato",
            ),
        )
        hash_prima = self._hash_memoria()

        r = self._tool("elimina_memoria", {"memory_id": 2}, sm, ss)

        self.assertEqual(r["status"], "conflict")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertEqual(ss.pending_action.operation, "update")
        self.assertEqual(ss.pending_action.target_id, 1)

    def test_cancel_pending_create_proposal_non_scrive_nulla(self):
        self._seed_memoria([], next_id=1)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r1 = self._tool(
            "crea_memoria",
            {"content": "Proposta mai confermata", "mode": "proposal"},
            sm, ss,
        )
        self.assertEqual(r1["status"], "pending_confirmation")

        r2 = self._tool("gestisci_pending_memoria", {"decision": "cancel"}, sm, ss)
        self.assertEqual(r2["status"], "cancelled")
        self.assertEqual(self._hash_memoria(), hash_prima)

        archivio = carica_archivio(self.percorso_memoria)
        self.assertEqual(archivio["memories"], [])

    def test_delete_ambiguo_con_tre_candidati_nessuna_scelta_arbitraria(self):
        ricordi = [
            self._ricordo(1, "Nota su Aster uno"),
            self._ricordo(2, "Nota su Aster due"),
            self._ricordo(3, "Nota su Aster tre"),
        ]
        self._seed_memoria(ricordi, next_id=4)
        hash_prima = self._hash_memoria()
        sm, ss = self._nuovo_stato()

        r = self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)

        self.assertEqual(r["status"], "pending_selection")
        self.assertEqual(len(r["candidates"]), 3)
        self.assertEqual(self._hash_memoria(), hash_prima)

        # un secondo tentativo con la stessa query, a pending gia' attivo,
        # non deve creare un nuovo pending_selection ne' avanzare da solo.
        r2 = self._tool("elimina_memoria_per_query", {"query": "Aster"}, sm, ss)
        self.assertEqual(r2["status"], "conflict")
        self.assertEqual(self._hash_memoria(), hash_prima)
        self.assertEqual(len(ss.pending_action.candidates), 3, "i candidati originali non devono essere alterati")


if __name__ == "__main__":
    unittest.main()
