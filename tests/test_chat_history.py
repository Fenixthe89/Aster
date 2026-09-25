"""
0.6.8 - Cronologia e rollback del turno (modules/chat.py).

Verifica limita_cronologia: compattazione dei risultati tool
voluminosi, compattazione della risposta assistant lunga nei soli
turni tool pesanti, limite per numero di messaggi e per caratteri,
tagli sempre per turni interi (nessun messaggio orfano).

Verifica il rollback del turno in avvia_chat: su eccezione o
interruzione la cronologia torna esattamente allo stato pre-turno.

Nessuna chiamata a Ollama (tutte sostituite da doppi di test).
Nessun file in data/ viene letto o scritto.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.chat as chat
import modules.tool_response as tool_response
from modules.chat import (
    MAX_CARATTERI_CRONOLOGIA,
    PLACEHOLDER_RISPOSTA_OMESSA,
    PLACEHOLDER_TOOL_OMESSO,
    SOGLIA_COMPATTAZIONE_RISPOSTA,
    SOGLIA_COMPATTAZIONE_TOOL,
    limita_cronologia,
)
from modules.memory import MODALITA_NORMALE, StatoMemoria
from modules.tool_registry import RegistroStrumenti, ToolSpec

SISTEMA = {"role": "system", "content": "prompt di sistema"}
LIMITE = 20


def _user(testo="domanda"):
    return {"role": "user", "content": testo}


def _assistant(testo="risposta"):
    return {"role": "assistant", "content": testo}


def _tool_call(nome="read_file", argomenti=None):
    # Come risposta.message di ollama: oggetto con role/content/tool_calls.
    return SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(name=nome, arguments=argomenti or {})
            )
        ],
    )


def _tool(risultato):
    contenuto = risultato if isinstance(risultato, str) else json.dumps(
        risultato, ensure_ascii=False
    )
    return {"role": "tool", "content": contenuto}


def _turno_tool(risultato, risposta="Ecco il risultato.", domanda="usa un tool", nome="read_file"):
    return [_user(domanda), _tool_call(nome), _tool(risultato), _assistant(risposta)]


def _risultato_read_file(caratteri=1500):
    return {
        "ok": True,
        "operation": "read_file",
        "status": "success",
        "data": {"content": "SEGRETO_CONTENUTO " + "x" * caratteri, "encoding": "utf-8"},
    }


def _risultato_processi(quanti=50):
    return {
        "ok": True,
        "operation": "list_processes",
        "status": "success",
        "data": {
            "processes": [{"pid": i, "name": f"processo_{i}.exe"} for i in range(quanti)],
            "name_filter": None,
            "total": 362,
            "truncated": True,
        },
    }


def _risultato_directory(quante=100):
    return {
        "ok": True,
        "operation": "list_directory",
        "status": "success",
        "data": {
            "entries": [{"name": f"file_{i:03d}.txt", "type": "file"} for i in range(quante)],
            "truncated": False,
        },
    }


def _risultato_memoria():
    return {
        "ok": True,
        "operation": "search",
        "status": "searched",
        "results": [{"id": 3, "content": "Preferisco Linux"}],
        "returned": 1,
    }


def _limita(conversazione, limite=LIMITE):
    messaggi = [SISTEMA, *conversazione]
    limita_cronologia(messaggi, limite)
    return messaggi


def _ruolo(messaggio):
    return messaggio["role"] if isinstance(messaggio, dict) else messaggio.role


def _contenuti_tool(messaggi):
    return [m["content"] for m in messaggi if isinstance(m, dict) and m.get("role") == "tool"]


def _assert_nessun_orfano(test, messaggi):
    """Ogni tool segue una tool call o un tool; ogni tool call è seguita da un tool."""

    test.assertEqual(messaggi[0], SISTEMA)
    conversazione = messaggi[1:]
    if not conversazione:
        return
    test.assertEqual(_ruolo(conversazione[0]), "user")

    for indice, messaggio in enumerate(conversazione):
        ruolo = _ruolo(messaggio)
        if ruolo == "tool":
            precedente = conversazione[indice - 1]
            test.assertTrue(
                getattr(precedente, "tool_calls", None) or _ruolo(precedente) == "tool",
                msg=f"role=tool orfano in posizione {indice}",
            )
        if ruolo == "assistant" and getattr(messaggio, "tool_calls", None):
            test.assertLess(indice + 1, len(conversazione), msg="tool call finale orfana")
            test.assertEqual(_ruolo(conversazione[indice + 1]), "tool")


# =====================================================================
# Compattazione
# =====================================================================

class TestCompattazione(unittest.TestCase):

    def test_01_history_corta_invariata(self):
        conversazione = [_user("ciao"), _assistant("ciao!"), _user("come va?")]
        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, *conversazione])

    def test_02_tool_piccolo_invariato(self):
        turno = _turno_tool({"ok": True, "operation": "get_disk_usage", "status": "success",
                             "data": {"free_bytes": 1}})
        messaggi = _limita([*turno, _user("altro")])

        self.assertEqual(messaggi[1:], [*turno, _user("altro")])

    def test_03_tool_oltre_1000_compattato(self):
        turno = _turno_tool(_risultato_read_file())
        self.assertGreater(len(turno[2]["content"]), SOGLIA_COMPATTAZIONE_TOOL)

        messaggi = _limita([*turno, _user("altro")])

        contenuto = _contenuti_tool(messaggi)[0]
        self.assertLessEqual(len(contenuto), SOGLIA_COMPATTAZIONE_TOOL)
        self.assertTrue(json.loads(contenuto)["omitted_from_history"])

    def test_03b_esattamente_1000_non_compattato(self):
        contenuto = "y" * SOGLIA_COMPATTAZIONE_TOOL
        messaggi = _limita([*_turno_tool(contenuto), _user("altro")])

        self.assertEqual(_contenuti_tool(messaggi), [contenuto])

    def test_04_json_conserva_solo_ok_operation_status(self):
        messaggi = _limita([*_turno_tool(_risultato_read_file()), _user("altro")])

        dati = json.loads(_contenuti_tool(messaggi)[0])
        self.assertEqual(
            dati,
            {"ok": True, "operation": "read_file", "status": "success",
             "omitted_from_history": True},
        )

    def test_05_data_content_processes_path_error_non_restano(self):
        risultato = {
            "ok": False,
            "operation": "read_file",
            "status": "tool_error",
            "error": "C:\\Users\\Secret " + "e" * 1200,
            "path": "C:\\Users\\Secret\\file.txt",
            "data": {"content": "x", "processes": [], "entries": []},
        }
        messaggi = _limita([*_turno_tool(risultato), _user("altro")])

        contenuto = _contenuti_tool(messaggi)[0]
        dati = json.loads(contenuto)
        self.assertEqual(set(dati), {"ok", "operation", "status", "omitted_from_history"})
        for chiave in ("data", "content", "processes", "entries", "path", "error"):
            self.assertNotIn(chiave, dati, msg=chiave)
        self.assertNotIn("Secret", contenuto)
        self.assertNotIn("eeee", contenuto)

    def test_06_tool_non_json_placeholder_sicuro(self):
        grezzo = "testo non json con SEGRETO " + "z" * 1200
        messaggi = _limita([*_turno_tool(grezzo), _user("altro")])

        contenuto = _contenuti_tool(messaggi)[0]
        self.assertEqual(contenuto, PLACEHOLDER_TOOL_OMESSO)
        self.assertNotIn("SEGRETO", contenuto)

    def test_06b_json_non_dizionario_placeholder_sicuro(self):
        grezzo = json.dumps(["x" * 1200])
        messaggi = _limita([*_turno_tool(grezzo), _user("altro")])

        self.assertEqual(_contenuti_tool(messaggi)[0], PLACEHOLDER_TOOL_OMESSO)

    def test_07_assistant_post_tool_lungo_compattato(self):
        risposta = "Contenuto del file: " + "r" * SOGLIA_COMPATTAZIONE_RISPOSTA
        turno = _turno_tool(_risultato_read_file(), risposta=risposta)
        messaggi = _limita([*turno, _user("altro")])

        self.assertEqual(messaggi[4]["content"], PLACEHOLDER_RISPOSTA_OMESSA)

    def test_07b_assistant_post_tool_corto_mantenuto(self):
        turno = _turno_tool(_risultato_read_file(), risposta="Il file parla di Aster.")
        messaggi = _limita([*turno, _user("altro")])

        self.assertEqual(messaggi[4]["content"], "Il file parla di Aster.")

    def test_07c_assistant_lungo_dopo_tool_piccolo_non_compattato(self):
        risposta = "r" * (SOGLIA_COMPATTAZIONE_RISPOSTA + 500)
        turno = _turno_tool({"ok": True, "operation": "get_system_info", "status": "success"},
                            risposta=risposta)
        messaggi = _limita([*turno, _user("altro")])

        self.assertEqual(messaggi[4]["content"], risposta)

    def test_08_assistant_normale_lungo_non_compattato(self):
        risposta = "spiegazione " * 200
        self.assertGreater(len(risposta), SOGLIA_COMPATTAZIONE_RISPOSTA)
        conversazione = [_user("spiegami qualcosa"), _assistant(risposta), _user("grazie")]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi[2]["content"], risposta)

    def test_16_memoria_piccola_integra(self):
        turno = _turno_tool(_risultato_memoria(), nome="cerca_memoria")
        messaggi = _limita([*turno, _user("elimina quel ricordo")])

        self.assertEqual(json.loads(_contenuti_tool(messaggi)[0]), _risultato_memoria())

    def test_17_read_file_content_non_resta(self):
        messaggi = _limita([*_turno_tool(_risultato_read_file()), _user("altro")])

        self.assertNotIn("SEGRETO_CONTENUTO", json.dumps(_contenuti_tool(messaggi)))

    def test_18_list_processes_pesante_non_resta(self):
        turno = _turno_tool(_risultato_processi(), nome="list_processes")
        messaggi = _limita([*turno, _user("altro")])

        contenuto = _contenuti_tool(messaggi)[0]
        self.assertNotIn("processo_", contenuto)
        self.assertEqual(json.loads(contenuto)["operation"], "list_processes")

    def test_19_list_directory_pesante_non_resta(self):
        turno = _turno_tool(_risultato_directory(), nome="list_directory")
        messaggi = _limita([*turno, _user("altro")])

        contenuto = _contenuti_tool(messaggi)[0]
        self.assertNotIn("file_0", contenuto)
        self.assertEqual(json.loads(contenuto)["operation"], "list_directory")

    def test_compattazione_idempotente(self):
        messaggi = _limita([*_turno_tool(_risultato_read_file()), _user("altro")])
        prima = [m if not isinstance(m, dict) else dict(m) for m in messaggi]

        limita_cronologia(messaggi, LIMITE)

        self.assertEqual(_contenuti_tool(messaggi), _contenuti_tool(prima))

    def test_nessuna_mutazione_in_place(self):
        turno = _turno_tool(_risultato_read_file())
        originale_tool = dict(turno[2])

        _limita([*turno, _user("altro")])

        self.assertEqual(turno[2], originale_tool)


# =====================================================================
# Limiti di dimensione e numero
# =====================================================================

class TestLimiti(unittest.TestCase):

    def test_09_tetto_caratteri_applicato(self):
        conversazione = []
        for indice in range(8):
            conversazione += [_user(f"domanda {indice}"), _assistant("a" * 1500)]
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione, limite=100)

        totale = sum(len(m["content"]) for m in messaggi[1:])
        self.assertLessEqual(totale, MAX_CARATTERI_CRONOLOGIA)
        self.assertLess(len(messaggi), len(conversazione) + 1)

    def test_10_ultimo_user_preservato_anche_se_enorme(self):
        enorme = "u" * (MAX_CARATTERI_CRONOLOGIA * 3)
        conversazione = [_user("vecchia"), _assistant("ok"), _user(enorme)]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user(enorme)])

    def test_limite_numero_messaggi_per_turni_interi(self):
        conversazione = []
        for indice in range(6):
            conversazione += _turno_tool({"ok": True, "operation": "x", "status": "success"},
                                         domanda=f"d{indice}")
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione, limite=9)

        self.assertLessEqual(len(messaggi) - 1, 9)
        _assert_nessun_orfano(self, messaggi)
        self.assertEqual(messaggi[-1], _user("corrente"))

    def test_15_storie_con_piu_tool(self):
        conversazione = [
            *_turno_tool(_risultato_processi(), nome="list_processes", domanda="processi?"),
            *_turno_tool(_risultato_read_file(), nome="read_file", domanda="leggi"),
            *_turno_tool(_risultato_memoria(), nome="cerca_memoria", domanda="ricordi?"),
            *_turno_tool(_risultato_directory(), nome="list_directory", domanda="cartella?"),
            _user("corrente"),
        ]

        messaggi = _limita(conversazione)

        _assert_nessun_orfano(self, messaggi)
        contenuti = _contenuti_tool(messaggi)
        self.assertEqual(len(contenuti), 4)
        compattati = [json.loads(c).get("omitted_from_history") is True for c in contenuti]
        self.assertEqual(compattati, [True, True, False, True])
        self.assertLessEqual(
            sum(len(json.dumps(c)) for c in contenuti), MAX_CARATTERI_CRONOLOGIA
        )


# =====================================================================
# Messaggi orfani / tagli per turni
# =====================================================================

class TestOrfani(unittest.TestCase):

    def test_11_nessun_tool_iniziale(self):
        conversazione = [_tool({"ok": True}), _assistant("resto"), _user("corrente")]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user("corrente")])

    def test_12_nessuna_tool_call_orfana(self):
        conversazione = [_user("vecchia"), _tool_call(), _user("corrente")]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user("corrente")])

    def test_12b_tool_call_iniziale_scartata(self):
        conversazione = [_tool_call(), _tool({"ok": True}), _assistant("x"), _user("corrente")]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user("corrente")])

    def test_13_nessun_tool_result_orfano(self):
        conversazione = [_user("vecchia"), _tool({"ok": True}), _assistant("x"), _user("corrente")]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user("corrente")])

    def test_14_taglio_per_turni_coerenti(self):
        conversazione = []
        for indice in range(10):
            conversazione += _turno_tool(
                {"ok": True, "operation": "get_disk_usage", "status": "success"},
                domanda=f"d{indice}",
            )
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione, limite=20)

        _assert_nessun_orfano(self, messaggi)
        self.assertEqual((len(messaggi) - 2) % 4, 0)
        self.assertEqual(messaggi[-1], _user("corrente"))

    def test_ultimo_turno_incoerente_ridotto_al_solo_user(self):
        conversazione = [_user("corrente"), _tool({"ok": True})]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user("corrente")])

    def test_solo_sistema(self):
        messaggi = [SISTEMA]
        limita_cronologia(messaggi, LIMITE)
        self.assertEqual(messaggi, [SISTEMA])


# =====================================================================
# Rollback del turno in avvia_chat (loop reale, Ollama sostituito)
# =====================================================================

def _risposta_testo(testo):
    return SimpleNamespace(message=SimpleNamespace(role="assistant", content=testo, tool_calls=None))


def _risposta_tool(nome, argomenti=None):
    return SimpleNamespace(message=_tool_call(nome, argomenti))


def _handler_esplode(argomenti, contesto):
    raise RuntimeError("handler esploso C:\\Users\\Secret")


def _handler_ok(argomenti, contesto):
    return {"ok": True, "operation": "tool_ok", "status": "success", "data": {"v": 1}}


def _schema(nome):
    return {"type": "function", "function": {"name": nome, "parameters": {"type": "object", "properties": {}}}}


def _registro_di_test():
    registro = RegistroStrumenti()
    registro.registra(ToolSpec(nome="tool_esplosivo", schema=_schema("tool_esplosivo"),
                               handler=_handler_esplode, livello="READ_ONLY", dominio="system"))
    registro.registra(ToolSpec(nome="tool_ok", schema=_schema("tool_ok"),
                               handler=_handler_ok, livello="READ_ONLY", dominio="system"))
    return registro


class TestRollbackTurno(unittest.TestCase):
    """Esegue avvia_chat con input, Ollama e registry sostituiti."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aster_test_rollback_"))
        self._originali = {}
        self._patch(chat, "crea_registro_memoria", _registro_di_test)
        self._patch(chat, "registra_tool_sistema", lambda registro: None)
        self._patch(chat, "registra_tool_filesystem", lambda registro: None)
        self._patch(chat, "carica_config", lambda percorso: {})
        self._patch(chat, "prepara_contesto_filesystem", lambda config, base: None)

    def tearDown(self):
        for (modulo, nome), valore in self._originali.items():
            setattr(modulo, nome, valore)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, modulo, nome, valore):
        self._originali.setdefault((modulo, nome), getattr(modulo, nome))
        setattr(modulo, nome, valore)

    def _esegui(self, domande, risposte_primo_giro):
        """Esegue il loop; restituisce la lista messaggi reale usata da avvia_chat."""

        ingressi = iter([*domande, "esci"])
        risposte = iter(risposte_primo_giro)
        catturati = {}

        def primo_giro_finto(modello, messaggi, tools, host, timeout, num_ctx):
            catturati["messaggi"] = messaggi
            catturati.setdefault("snapshot", []).append(list(messaggi))
            return next(risposte)

        self._patch(chat, "esegui_turno_con_tools", primo_giro_finto)

        import builtins
        input_originale = builtins.input
        builtins.input = lambda prompt="": next(ingressi)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                chat.avvia_chat(
                    "prompt di sistema", "qwen3:8b", LIMITE,
                    "http://localhost:11434", 60, 8192,
                    StatoMemoria(modalita=MODALITA_NORMALE, memoria=None),
                    self.tmp / "memory.json", 5,
                )
        finally:
            builtins.input = input_originale

        return catturati["messaggi"]

    def _stato_dopo_primo_turno(self):
        return [
            {"role": "system", "content": "prompt di sistema"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "Ciao!"},
        ]

    def test_handler_exception_rollback(self):
        messaggi = self._esegui(
            ["ciao", "fai qualcosa"],
            [_risposta_testo("Ciao!"), _risposta_tool("tool_esplosivo")],
        )

        self.assertEqual(messaggi, self._stato_dopo_primo_turno())

    def test_post_tool_exception_rollback(self):
        def post_tool_esplode(**kwargs):
            raise RuntimeError("fallback esploso")

        self._patch(chat, "genera_risposta_post_tool", post_tool_esplode)

        messaggi = self._esegui(
            ["ciao", "usa il tool"],
            [_risposta_testo("Ciao!"), _risposta_tool("tool_ok")],
        )

        self.assertEqual(messaggi, self._stato_dopo_primo_turno())

    def test_keyboard_interrupt_post_tool_rollback(self):
        def post_tool_interrotto(**kwargs):
            raise KeyboardInterrupt

        self._patch(chat, "genera_risposta_post_tool", post_tool_interrotto)

        messaggi = self._esegui(
            ["ciao", "usa il tool"],
            [_risposta_testo("Ciao!"), _risposta_tool("tool_ok")],
        )

        self.assertEqual(messaggi, self._stato_dopo_primo_turno())

    def test_primo_giro_exception_rollback(self):
        def primo_giro_ok_poi_errore():
            yield _risposta_testo("Ciao!")
            raise ConnectionError("Ollama non raggiungibile")

        generatore = primo_giro_ok_poi_errore()

        class RisposteConErrore:
            def __iter__(self):
                return self

            def __next__(self):
                return next(generatore)

        messaggi = self._esegui(["ciao", "altro"], RisposteConErrore())

        self.assertEqual(messaggi, self._stato_dopo_primo_turno())

    def test_secondo_giro_exception_usa_fallback_turno_completo(self):
        # Il secondo giro Ollama che fallisce è gestito dalla pipeline
        # (fallback deterministico): il turno si completa, senza orfani.
        def esegui_risposta_finale_fallisce(*args, **kwargs):
            raise ConnectionError("Ollama non raggiungibile")

        self._patch(tool_response, "esegui_risposta_finale", esegui_risposta_finale_fallisce)

        messaggi = self._esegui(
            ["ciao", "usa il tool"],
            [_risposta_testo("Ciao!"), _risposta_tool("tool_ok")],
        )

        self.assertEqual(messaggi[:3], self._stato_dopo_primo_turno())
        self.assertEqual([_ruolo(m) for m in messaggi[3:]], ["user", "assistant", "tool", "assistant"])
        _assert_nessun_orfano(self, messaggi)

    def test_turno_riuscito_dopo_rollback_resta_coerente(self):
        messaggi = self._esegui(
            ["ciao", "fai qualcosa", "ancora ciao"],
            [_risposta_testo("Ciao!"), _risposta_tool("tool_esplosivo"), _risposta_testo("Eccomi.")],
        )

        self.assertEqual(
            messaggi,
            [*self._stato_dopo_primo_turno(), _user("ancora ciao"), _assistant("Eccomi.")],
        )


if __name__ == "__main__":
    unittest.main()
