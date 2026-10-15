"""
0.7.4 - conferma Python di set_app_preference nel ciclo reale di
modules/chat.py (loop avvia_chat, Ollama e registry sostituiti dove
non pertinente, registra_tool_preferenze REALE).

Copre il requisito centrale del task: set_app_preference non scrive
mai da solo (tests/test_preferences_tools.py lo dimostra a livello di
funzione); qui si dimostra che avvia_chat intercetta
status="pending_confirmation" SOLO per questo tool, presenta una
domanda sì/no tramite input() diretto (nessun coinvolgimento del
modello, nessun secondo giro Ollama: genera_risposta_post_tool viene
reso esplosivo per far fallire il test se venisse mai chiamato per
questo tool), applica la scrittura solo su conferma esplicita e
inequivocabile, e mantiene una cronologia coerente (nessuna tool call
orfana, la proposta non è mai presentata come una scrittura riuscita).

Solo directory temporanee: nessun file in data/ reale viene mai letto
o scritto da questi test.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import builtins
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.chat as chat
from modules.app_preferences import leggi_preferenze_o_vuote
from modules.file_tools import ContestoFilesystem
from modules.memory import MODALITA_NORMALE, StatoMemoria
from modules.preferences_tools import ContestoPreferenze, applica_preferenza_confermata
from modules.tool_registry import RegistroStrumenti

LIMITE = 20


def _tool_call(nome, argomenti=None):
    return SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name=nome, arguments=argomenti or {}))
        ],
    )


def _risposta_tool(nome, argomenti=None):
    return SimpleNamespace(message=_tool_call(nome, argomenti))


class _Interrompi:
    """Sentinella: il prossimo input() solleva KeyboardInterrupt invece di restituire un valore."""


class _InterrompiEOF:
    """Sentinella: il prossimo input() solleva EOFError invece di restituire un valore."""


def _registro_vuoto():
    return RegistroStrumenti()


def _verifica_nessuna_tool_call_orfana(messaggi):
    """Ogni assistant con tool_calls e' seguito da un messaggio role=tool, e viceversa."""

    for indice, messaggio in enumerate(messaggi):
        ha_tool_calls = bool(
            getattr(messaggio, "tool_calls", None)
            if not isinstance(messaggio, dict)
            else messaggio.get("tool_calls")
        )
        if ha_tool_calls:
            assert indice + 1 < len(messaggi), "tool_calls senza alcun messaggio successivo"
            successivo = messaggi[indice + 1]
            ruolo = successivo.get("role") if isinstance(successivo, dict) else getattr(successivo, "role", None)
            assert ruolo == "tool", f"tool_calls non seguito da role=tool (trovato {ruolo!r})"

        ruolo_corrente = messaggio.get("role") if isinstance(messaggio, dict) else getattr(messaggio, "role", None)
        if ruolo_corrente == "tool":
            assert indice > 0, "role=tool senza alcun messaggio precedente"
            precedente = messaggi[indice - 1]
            tool_calls_precedente = (
                precedente.get("tool_calls") if isinstance(precedente, dict) else getattr(precedente, "tool_calls", None)
            )
            assert tool_calls_precedente, "role=tool non preceduto da una tool_calls"


class _BaseIntegrazione(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aster_test_pref_confirm_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.percorso_preferenze = self.tmp / "app_preferences.json"
        self.contesto_preferenze = ContestoPreferenze(percorso_preferenze=self.percorso_preferenze)

        self.chiamate_turno = []
        self.chiamate_post_tool = []

    def _preferenze_attuali(self) -> dict:
        return leggi_preferenze_o_vuote(self.percorso_preferenze)["preferences"]

    def _esegui(self, ingressi_lista, risposte_ollama):
        """Esegue avvia_chat col loop reale; restituisce la cronologia messaggi."""

        ingressi = iter(ingressi_lista)
        risposte = iter(risposte_ollama)
        catturati = {}

        def primo_giro_finto(modello, messaggi, tools, host, timeout, num_ctx):
            self.chiamate_turno.append(1)
            catturati["messaggi"] = messaggi
            return next(risposte)

        def post_tool_esplosivo(**kwargs):
            self.chiamate_post_tool.append(1)
            raise AssertionError(
                "set_app_preference non deve mai raggiungere genera_risposta_post_tool "
                "(nessun secondo giro Ollama per questo tool)."
            )

        def input_fittizio(prompt=""):
            valore = next(ingressi)
            if valore is _Interrompi:
                raise KeyboardInterrupt()
            if valore is _InterrompiEOF:
                raise EOFError()
            return valore

        patch_attive = [
            mock.patch.object(chat, "crea_registro_memoria", _registro_vuoto),
            mock.patch.object(chat, "registra_tool_sistema", lambda registro: None),
            mock.patch.object(chat, "registra_tool_filesystem", lambda registro: None),
            # registra_tool_preferenze NON patchata: tool reali, scrittura reale.
            mock.patch.object(chat, "esegui_turno_con_tools", primo_giro_finto),
            mock.patch.object(chat, "genera_risposta_post_tool", post_tool_esplosivo),
            mock.patch.object(builtins, "input", input_fittizio),
        ]

        with contextlib.ExitStack() as pila:
            for patch in patch_attive:
                pila.enter_context(patch)
            stdout = io.StringIO()
            pila.enter_context(contextlib.redirect_stdout(stdout))

            chat.avvia_chat(
                "prompt di sistema", "qwen3:8b", LIMITE,
                "http://localhost:11434", 60, 8192,
                StatoMemoria(modalita=MODALITA_NORMALE, memoria=None),
                self.tmp / "memory.json",
                self.percorso_preferenze,
                5,
                ContestoFilesystem(allowed_roots=[], reserved_roots=()),
            )

        self.output = stdout.getvalue()
        return catturati["messaggi"]


class TestConfermaPositiva(_BaseIntegrazione):

    def test_conferma_scrive_e_cronologia_coerente(self):
        messaggi = self._esegui(
            ["Imposta Firefox come browser preferito", "si", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        self.assertEqual(self._preferenze_attuali()["browser"], "Firefox")
        self.assertEqual(self.chiamate_turno, [1])
        self.assertEqual(self.chiamate_post_tool, [])

        _verifica_nessuna_tool_call_orfana(messaggi)

        self.assertEqual(messaggi[-1]["role"], "assistant")
        self.assertIn("Firefox", messaggi[-1]["content"])
        self.assertIn("salvata", messaggi[-1]["content"].lower())

    def test_tool_result_non_presentato_come_riuscito_prima_della_conferma(self):
        messaggi = self._esegui(
            ["Imposta Firefox come browser preferito", "si", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        messaggi_tool = [m for m in messaggi if isinstance(m, dict) and m.get("role") == "tool"]
        self.assertEqual(len(messaggi_tool), 1)
        self.assertIn('"pending_confirmation"', messaggi_tool[0]["content"])
        self.assertNotIn('"status": "success"', messaggi_tool[0]["content"])

    def test_varianti_affermative_accettate(self):
        for risposta in ("si", "Si", "sì", "yes", "  si  "):
            with self.subTest(risposta=risposta):
                self.chiamate_turno = []
                self.chiamate_post_tool = []
                percorso = self.tmp / f"prefs_{hash(risposta)}.json"
                self.percorso_preferenze = percorso
                self._esegui(
                    ["Imposta Chrome come browser", risposta, "esci"],
                    [_risposta_tool("set_app_preference", {"category": "browser", "value": "Chrome"})],
                )
                self.assertEqual(self._preferenze_attuali()["browser"], "Chrome")


class TestRispostaNegativaOAmbigua(_BaseIntegrazione):

    def test_no_esplicito_non_scrive(self):
        messaggi = self._esegui(
            ["Imposta Firefox come browser", "no", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        self.assertFalse(self.percorso_preferenze.exists())
        self.assertIn("annullat", messaggi[-1]["content"].lower())

    def test_risposta_ambigua_non_scrive(self):
        self._esegui(
            ["Imposta Firefox come browser", "forse più tardi", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )
        self.assertFalse(self.percorso_preferenze.exists())

    def test_risposta_vuota_non_scrive(self):
        self._esegui(
            ["Imposta Firefox come browser", "", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )
        self.assertFalse(self.percorso_preferenze.exists())


class TestInterruzioneDuranteConferma(_BaseIntegrazione):

    def test_keyboardinterrupt_annulla_senza_scrivere_e_senza_rollback_intero(self):
        messaggi = self._esegui(
            ["Imposta Firefox come browser", _Interrompi, "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        self.assertFalse(self.percorso_preferenze.exists())
        # Nessun rollback dell'intero turno: la tool call e il suo
        # risultato restano in cronologia, con la risposta di
        # cancellazione in coda (non l'errore generico di
        # comunicazione con Ollama, che qui non e' mai stato chiamato).
        self.assertNotIn("Errore nella comunicazione", self.output)
        self.assertEqual(messaggi[-1]["role"], "assistant")
        self.assertIn("annullat", messaggi[-1]["content"].lower())
        _verifica_nessuna_tool_call_orfana(messaggi)

    def test_eof_annulla_senza_scrivere(self):
        messaggi = self._esegui(
            ["Imposta Firefox come browser", _InterrompiEOF, "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        self.assertFalse(self.percorso_preferenze.exists())
        self.assertIn("annullat", messaggi[-1]["content"].lower())


class TestCancellazioneConNull(_BaseIntegrazione):

    def test_cancellazione_confermata(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto_preferenze)

        messaggi = self._esegui(
            ["Dimentica il browser preferito", "si", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": None})],
        )

        self.assertIsNone(self._preferenze_attuali()["browser"])
        self.assertIn("cancellat", messaggi[-1]["content"].lower())

    def test_cancellazione_rifiutata_mantiene_il_valore(self):
        applica_preferenza_confermata("browser", "Firefox", self.contesto_preferenze)

        self._esegui(
            ["Dimentica il browser preferito", "no", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": None})],
        )

        self.assertEqual(self._preferenze_attuali()["browser"], "Firefox")


class TestProposteConsecutive(_BaseIntegrazione):

    def test_conferme_separate_non_si_influenzano(self):
        messaggi = self._esegui(
            [
                "Imposta Firefox come browser",
                "si",
                "Anzi, imposta Chrome come browser",
                "no",
                "esci",
            ],
            [
                _risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"}),
                _risposta_tool("set_app_preference", {"category": "browser", "value": "Chrome"}),
            ],
        )

        # Prima proposta confermata, seconda rifiutata: resta Firefox.
        self.assertEqual(self._preferenze_attuali()["browser"], "Firefox")
        self.assertEqual(self.chiamate_turno, [1, 1])
        _verifica_nessuna_tool_call_orfana(messaggi)

    def test_seconda_proposta_non_eredita_la_conferma_della_prima(self):
        """Un singolo "si" vale solo per la proposta a cui risponde."""

        self._esegui(
            [
                "Imposta Firefox come browser",
                "si",
                "Imposta VS Code come editor",
                "no",
                "esci",
            ],
            [
                _risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"}),
                _risposta_tool("set_app_preference", {"category": "editor", "value": "VS Code"}),
            ],
        )

        preferenze = self._preferenze_attuali()
        self.assertEqual(preferenze["browser"], "Firefox")
        self.assertIsNone(preferenze["editor"])


class TestErrorePersistenzaDopoConferma(_BaseIntegrazione):

    def test_file_corrotto_blocca_la_scrittura_anche_dopo_si(self):
        self.percorso_preferenze.write_text("{non json", encoding="utf-8")

        messaggi = self._esegui(
            ["Imposta Firefox come browser", "si", "esci"],
            [_risposta_tool("set_app_preference", {"category": "browser", "value": "Firefox"})],
        )

        # Il file corrotto non viene mai sovrascritto, nemmeno dopo conferma.
        self.assertEqual(
            self.percorso_preferenze.read_text(encoding="utf-8"), "{non json"
        )
        self.assertNotIn("Preferenza salvata", messaggi[-1]["content"])


if __name__ == "__main__":
    unittest.main()
