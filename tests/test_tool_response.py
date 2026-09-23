"""
0.6.2 - Generic Post-Tool Pipeline: test di regressione.

Verifica che modules/tool_response.py sia una pipeline post-tool
realmente generica: nessuna conoscenza di "memory", STATUS_MEMORIA,
blocked_sensitive o di qualunque altro dominio, e nessuna dipendenza
(diretta o indiretta) da modules.chat.

Copre anche la parita' con il comportamento pre-refactor per il
dominio memory (blocked_sensitive e caso normale), simulato tramite
gli stessi due parametri (salta_secondo_giro, fallback_deterministico)
che chat.py calcola nel routing.

Nessun file in data/ viene mai letto o scritto. Nessuna chiamata reale
a Ollama: esegui_risposta_finale viene sempre sostituita con un doppio
di test (funzione o eccezione), mai con la rete.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.tool_response as tool_response
from modules.tool_response import (
    genera_risposta_post_tool,
    pulisci_testo_modello,
    raccogli_risposta_finale,
)


def _messaggio(contenuto: str):
    """Simula un oggetto risposta.message.content di ollama."""

    return SimpleNamespace(message=SimpleNamespace(content=contenuto))


class ContatoreChiamate:
    """Doppio di test per esegui_risposta_finale: conta le invocazioni."""

    def __init__(self, comportamento):
        self.chiamate = 0
        self._comportamento = comportamento

    def __call__(self, modello, messaggi, host_ollama, timeout_ollama):
        self.chiamate += 1
        return self._comportamento()


# =====================================================================
# Import isolato: tool_response.py non deve dipendere da modules.chat.
# =====================================================================

class TestImportIsolato(unittest.TestCase):

    def test_tool_response_importabile_autonomamente(self):
        # Se questo import fallisse (ImportError/AttributeError), il
        # test fallirebbe: non solleva nulla -> importabile da solo.
        import importlib
        modulo = importlib.import_module("modules.tool_response")
        self.assertTrue(hasattr(modulo, "genera_risposta_post_tool"))

    def test_import_tool_response_non_importa_modules_chat(self):
        # Processo Python pulito e separato: importa SOLO tool_response
        # e verifica che modules.chat non sia mai stato caricato come
        # effetto collaterale.
        codice = (
            "import sys; "
            "import modules.tool_response; "
            "sys.exit(1 if 'modules.chat' in sys.modules else 0)"
        )
        risultato = subprocess.run(
            [sys.executable, "-c", codice],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            risultato.returncode,
            0,
            msg=f"stdout={risultato.stdout!r} stderr={risultato.stderr!r}",
        )

    def test_modulo_non_referenzia_modules_chat_nel_sorgente(self):
        sorgente = (BASE_DIR / "modules" / "tool_response.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("modules.chat", sorgente)
        self.assertNotIn("modules import chat", sorgente)


# =====================================================================
# genera_risposta_post_tool: comportamento generico.
# =====================================================================

class TestGeneraRispostaPostTool(unittest.TestCase):

    def test_salta_secondo_giro_chiama_fallback_e_non_esegui_risposta_finale(self):
        chiamato_fallback = []

        def fallback(risultato_tool):
            chiamato_fallback.append(risultato_tool)
            return "risposta fallback"

        def esegui_risposta_finale_non_deve_essere_chiamata(*args, **kwargs):
            raise AssertionError(
                "esegui_risposta_finale non deve essere invocata "
                "quando salta_secondo_giro=True."
            )

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = (
            esegui_risposta_finale_non_deve_essere_chiamata
        )
        try:
            risultato_tool = {"ok": True, "status": "qualsiasi"}

            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=True,
                fallback_deterministico=fallback,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "risposta fallback")
        self.assertEqual(chiamato_fallback, [risultato_tool])

    def test_secondo_giro_riuscito_restituisce_testo_pulito(self):
        stream = [_messaggio("Ciao <think>ragionamento interno</think>mondo")]
        contatore = ContatoreChiamate(lambda: iter(stream))

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[{"role": "user", "content": "ciao"}],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool={"ok": True},
                salta_secondo_giro=False,
                fallback_deterministico=lambda r: "non deve essere usato",
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "Ciao mondo")
        self.assertEqual(contatore.chiamate, 1)

    def test_secondo_giro_fallito_chiama_fallback_senza_propagare(self):
        def esegui_risposta_finale_che_fallisce(*args, **kwargs):
            raise ConnectionError("Ollama non raggiungibile")

        chiamato_fallback = []

        def fallback(risultato_tool):
            chiamato_fallback.append(risultato_tool)
            return "risposta di ripiego"

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = esegui_risposta_finale_che_fallisce
        try:
            risultato_tool = {"ok": False, "status": "tool_error", "error": "x"}

            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=fallback,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "risposta di ripiego")
        self.assertEqual(chiamato_fallback, [risultato_tool])

    def test_esegui_risposta_finale_chiamata_al_massimo_una_volta(self):
        contatore = ContatoreChiamate(lambda: iter([_messaggio("ok")]))

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool={"ok": True},
                salta_secondo_giro=False,
                fallback_deterministico=lambda r: "fallback",
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(contatore.chiamate, 1)

    def test_nessun_retry_del_tool_originale(self):
        # genera_risposta_post_tool non riceve mai un riferimento al
        # dispatcher/registry: strutturalmente non puo' rieseguire il
        # tool. Verifichiamo che l'unica funzione esterna invocata sia
        # esegui_risposta_finale (o il fallback), mai altro.
        chiamate_esterne = []

        def esegui_risposta_finale_fallisce(*args, **kwargs):
            chiamate_esterne.append("esegui_risposta_finale")
            raise TimeoutError

        def fallback(risultato_tool):
            chiamate_esterne.append("fallback")
            return "ok"

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = esegui_risposta_finale_fallisce
        try:
            genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool={"ok": True},
                salta_secondo_giro=False,
                fallback_deterministico=fallback,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(
            chiamate_esterne,
            ["esegui_risposta_finale", "fallback"],
        )


# =====================================================================
# Parita' pre-refactor delle due utility spostate.
# =====================================================================

class TestPulisciTestoModello(unittest.TestCase):

    def test_stringa_vuota(self):
        self.assertEqual(pulisci_testo_modello(""), "")
        self.assertEqual(pulisci_testo_modello(None), "")

    def test_rimuove_think_chiuso(self):
        self.assertEqual(
            pulisci_testo_modello("prima<think>ragiono</think>dopo"),
            "primadopo",
        )

    def test_gestisce_think_residuo_senza_apertura(self):
        self.assertEqual(
            pulisci_testo_modello("scarto residuo</think>testo visibile"),
            "testo visibile",
        )

    def test_gestisce_think_aperto_senza_chiusura(self):
        self.assertEqual(
            pulisci_testo_modello("visibile<think>mai chiuso"),
            "visibile",
        )

    def test_strip_finale(self):
        self.assertEqual(pulisci_testo_modello("  ciao  "), "ciao")


class TestRaccogliRispostaFinale(unittest.TestCase):

    def test_concatena_e_pulisce(self):
        stream = [
            _messaggio("Ciao "),
            _messaggio("<think>interno</think>"),
            _messaggio("mondo"),
        ]

        self.assertEqual(raccogli_risposta_finale(iter(stream)), "Ciao mondo")

    def test_ignora_contenuto_vuoto(self):
        stream = [_messaggio(""), _messaggio(None), _messaggio("testo")]

        self.assertEqual(raccogli_risposta_finale(iter(stream)), "testo")


# =====================================================================
# Parita' con il comportamento precedente per il dominio memory,
# simulata con gli stessi parametri che chat.py calcola nel routing.
# =====================================================================

class TestParitaDominioMemory(unittest.TestCase):

    def _fallback_memoria_di_prova(self, risultato_tool):
        # Stand-in equivalente a genera_risposta_deterministica_memoria
        # per il solo status che serve a questo test.
        status = risultato_tool.get("status")
        if status == "blocked_sensitive":
            return (
                "Non ho salvato questo contenuto perché sembra "
                "includere un dato sensibile."
            )
        return f"fallback per status={status}"

    def test_blocked_sensitive_salta_secondo_giro_e_non_ripete_valore(self):
        risultato_tool = {
            "ok": False,
            "operation": "create",
            "status": "blocked_sensitive",
            "sensitive_category": "password",
        }

        def esegui_risposta_finale_non_deve_essere_chiamata(*args, **kwargs):
            raise AssertionError(
                "blocked_sensitive deve saltare il secondo giro Ollama."
            )

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = (
            esegui_risposta_finale_non_deve_essere_chiamata
        )
        try:
            salta_secondo_giro = risultato_tool.get("status") == "blocked_sensitive"

            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[{"role": "tool", "content": "..."}],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=salta_secondo_giro,
                fallback_deterministico=self._fallback_memoria_di_prova,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertTrue(salta_secondo_giro)
        self.assertEqual(
            risposta,
            "Non ho salvato questo contenuto perché sembra "
            "includere un dato sensibile.",
        )
        # Nessun valore sensibile nella risposta.
        self.assertNotIn("sensitive_category", risposta)

    def test_status_normale_non_salta_secondo_giro(self):
        risultato_tool = {"ok": True, "status": "searched", "returned": 0}

        contatore = ContatoreChiamate(lambda: iter([_messaggio("Nessun ricordo.")]))
        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = contatore
        try:
            salta_secondo_giro = risultato_tool.get("status") == "blocked_sensitive"

            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=salta_secondo_giro,
                fallback_deterministico=self._fallback_memoria_di_prova,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertFalse(salta_secondo_giro)
        self.assertEqual(contatore.chiamate, 1)
        self.assertEqual(risposta, "Nessun ricordo.")


# =====================================================================
# Dominio sconosciuto: fallback neutro, nessun testo "memoria".
# =====================================================================

class TestDominioSconosciuto(unittest.TestCase):

    def _fallback_minimo_di_prova(self, risultato_tool):
        error = risultato_tool.get("error")
        if error:
            return error
        return "Non riesco a gestire questa richiesta."

    def test_fallback_neutro_con_errore(self):
        risultato_tool = {
            "ok": False,
            "domain": None,
            "status": "tool_error",
            "error": "Tool non registrato: tool_fantasma.",
        }

        def esegui_risposta_finale_fallisce(*args, **kwargs):
            raise ConnectionError

        originale = tool_response.esegui_risposta_finale
        tool_response.esegui_risposta_finale = esegui_risposta_finale_fallisce
        try:
            risposta = genera_risposta_post_tool(
                modello="qwen3:8b",
                messaggi=[],
                host_ollama="http://localhost:11434",
                timeout_ollama=60,
                risultato_tool=risultato_tool,
                salta_secondo_giro=False,
                fallback_deterministico=self._fallback_minimo_di_prova,
            )
        finally:
            tool_response.esegui_risposta_finale = originale

        self.assertEqual(risposta, "Tool non registrato: tool_fantasma.")
        self.assertNotIn("memoria", risposta.lower())
        self.assertNotIn("ricordo", risposta.lower())

    def test_fallback_neutro_senza_errore(self):
        risposta = self._fallback_minimo_di_prova({"ok": False})
        self.assertEqual(risposta, "Non riesco a gestire questa richiesta.")
        self.assertNotIn("memoria", risposta.lower())


if __name__ == "__main__":
    unittest.main()
