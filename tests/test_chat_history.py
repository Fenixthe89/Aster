"""
0.6.8 - Cronologia e rollback del turno (modules/chat.py).

Verifica limita_cronologia: compattazione dei risultati tool
voluminosi, compattazione della risposta assistant lunga nei soli
turni tool pesanti, limite per numero di messaggi e per unità pesate,
tagli sempre per turni interi (nessun messaggio orfano).

History Token Budget v2 (policy E16): cifra ASCII 3, punteggiatura
ASCII 2, altro ASCII 1, ogni carattere non ASCII 2 unità per byte UTF-8,
+1 per lettera in sequenze [A-Za-z0-9] di almeno 16 caratteri. I test
verificano pesi esatti e proprietà deterministiche, mai conteggi di
token di un modello.

Verifica il rollback del turno in avvia_chat: su eccezione o
interruzione la cronologia torna esattamente allo stato pre-turno.

0.7.1b: avvia_chat riceve il ContestoFilesystem già preparato da
aster.py e non legge configurazione né percorsi runtime.

Nessuna chiamata a Ollama (tutte sostituite da doppi di test).
Nessun file in data/ viene letto o scritto.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import ast
import builtins
import contextlib
import io
import json
import shutil
import string
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
import modules.config as modulo_config
import modules.file_tools as file_tools
import modules.runtime_paths as runtime_paths
import modules.tool_response as tool_response
from modules.chat import (
    MAX_UNITA_CRONOLOGIA,
    PLACEHOLDER_RISPOSTA_OMESSA,
    PLACEHOLDER_TOOL_OMESSO,
    SOGLIA_COMPATTAZIONE_RISPOSTA,
    SOGLIA_COMPATTAZIONE_TOOL,
    limita_cronologia,
)
from modules.file_tools import ContestoFilesystem
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


def _costo_totale(messaggi):
    """Unità pesate della cronologia (sistema escluso), come le conta limita_cronologia."""

    return sum(chat._costo_cronologia(m) for m in messaggi[1:])


def _turni_precedenti(messaggi):
    """Turni completi conservati prima del turno corrente."""

    return sum(1 for m in messaggi[1:] if _ruolo(m) == "user") - 1


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
        # Prosa con spazi: una sola sequenza di 2500 lettere ora pesa il
        # doppio (regola delle sequenze lunghe) e supererebbe il budget.
        risposta = ("risposta " * 300)[:SOGLIA_COMPATTAZIONE_RISPOSTA + 500]
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

    def test_09_tetto_unita_applicato(self):
        conversazione = []
        for indice in range(8):
            conversazione += [_user(f"domanda {indice}"), _assistant("a" * 1500)]
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione, limite=100)

        self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)
        self.assertLess(len(messaggi), len(conversazione) + 1)

    def test_10_ultimo_user_preservato_anche_se_enorme(self):
        enorme = "u" * (MAX_UNITA_CRONOLOGIA * 3)
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
            sum(chat._unita_testo(c) for c in contenuti), MAX_UNITA_CRONOLOGIA
        )


# =====================================================================
# Costo pesato della cronologia (context hardening)
# =====================================================================

class TestCostoCronologia(unittest.TestCase):

    def test_lettere_un_unita_ciascuna(self):
        self.assertEqual(chat._unita_testo("abc"), 3)

    def test_cifre_tre_unita_ciascuna(self):
        self.assertEqual(chat._unita_testo("123"), 9)

    def test_misto(self):
        self.assertEqual(chat._unita_testo("a1b2"), 8)

    def test_zero(self):
        self.assertEqual(chat._unita_testo("0"), 3)

    def test_tutte_le_cifre_ascii(self):
        self.assertEqual(chat._unita_testo("0123456789"), 30)

    def test_vuoto(self):
        self.assertEqual(chat._unita_testo(""), 0)

    def test_lettere_e_spazi_uguali_alla_lunghezza(self):
        for testo in (
            "Preferisco Linux per i server",
            "   \t\n  ",
            "SELECT nome FROM utenti WHERE attivo TRUE",
        ):
            with self.subTest(testo=testo):
                self.assertEqual(chat._unita_testo(testo), len(testo))

    def test_punteggiatura_due_unita(self):
        # E16: la punteggiatura ASCII (string.punctuation) pesa 2.
        punteggiatura = ".,;:!?()[]{}<>'\"-_/\\|@#$%^&*+=~`"
        self.assertEqual(chat._unita_testo(punteggiatura), 2 * len(punteggiatura))
        codice = "def f(x):\n\treturn {'k': [x]}  # ok!"
        self.assertEqual(chat._unita_testo(codice), len(codice) + 12)

    def test_unicode_due_unita_per_byte_utf8(self):
        # E16: à è ì ò ù ñ ß 2 byte -> 4; € 漢 字 3 byte -> 6; 😀 4 byte -> 8.
        testo = "àèìòù €漢字 ñ ß 😀"
        self.assertEqual(chat._unita_testo(testo), 5 * 4 + 6 + 2 * 6 + 4 + 4 + 8 + 4)
        self.assertEqual(
            chat._unita_testo(testo),
            2 * len(testo.encode("utf-8")) - sum(1 for c in testo if c.isascii()),
        )

    def test_cifre_unicode_non_ascii_non_pesate(self):
        # Apici, cifre arabo-indiane e devanagari non sono cifre ASCII:
        # seguono la regola Unicode (2 unità per byte), non il peso 3.
        self.assertEqual(chat._unita_testo("²³٣४"), 4 + 4 + 4 + 6)

    def test_messaggio_dict(self):
        self.assertEqual(chat._costo_cronologia(_user("a1")), 4)

    def test_messaggio_senza_contenuto(self):
        self.assertEqual(chat._costo_cronologia({"role": "assistant", "content": None}), 0)
        self.assertEqual(chat._costo_cronologia(_assistant("")), 0)

    def test_tool_call_nome_e_argomenti_pesati(self):
        messaggio = _tool_call("elimina_memoria", {"memory_id": 123})
        # nome: 14 lettere + "_" (2); argomenti "{'memory_id': 123}":
        # 18 caratteri, 6 di punteggiatura (+1 ciascuno), 3 cifre (+2 ciascuna).
        self.assertEqual(chat._costo_cronologia(messaggio), (14 + 2) + (18 + 6 + 2 * 3))

    def test_vecchi_nomi_rimossi(self):
        self.assertFalse(hasattr(chat, "MAX_CARATTERI_CRONOLOGIA"))
        self.assertFalse(hasattr(chat, "_dimensione_messaggio"))


class TestPesiE16(unittest.TestCase):
    """Pesi esatti della policy E16 per carattere e per sequenza."""

    def test_ascii_altro_un_unita(self):
        for carattere in ("a", "Z", " ", "\n", "\t", "\r", "\x00", "\x7f"):
            with self.subTest(carattere=repr(carattere)):
                self.assertEqual(chat._unita_testo(carattere), 1)

    def test_ogni_punteggiatura_ascii_due_unita(self):
        for carattere in string.punctuation:
            with self.subTest(carattere=carattere):
                self.assertEqual(chat._unita_testo(carattere), 2)

    def test_ogni_cifra_ascii_tre_unita(self):
        for carattere in "0123456789":
            with self.subTest(carattere=carattere):
                self.assertEqual(chat._unita_testo(carattere), 3)

    def test_non_ascii_due_unita_per_byte(self):
        casi = {
            "é": 4,            # 2 byte
            "ж": 4,            # cirillico, 2 byte
            "ب": 4,            # arabo, 2 byte
            "\u0301": 4,       # segno combinante, 2 byte
            "²": 4,            # cifra non ASCII, 2 byte
            "٣": 4,            # cifra arabo-indiana, 2 byte
            "漢": 6,           # CJK, 3 byte
            "€": 6,            # 3 byte
            "४": 6,            # cifra devanagari, 3 byte
            "１": 6,            # cifra a larghezza piena, 3 byte
            "\ue000": 6,       # uso privato BMP, 3 byte
            "😀": 8,           # emoji, 4 byte
            "\U00020000": 8,   # CJK ext B, 4 byte
            "\U000f0000": 8,   # uso privato astrale, 4 byte
        }
        for carattere, atteso in casi.items():
            with self.subTest(carattere=repr(carattere)):
                self.assertEqual(len(carattere.encode("utf-8")) * 2, atteso)
                self.assertEqual(chat._unita_testo(carattere), atteso)

    def test_surrogato_isolato_contato_senza_errori(self):
        # Una stringa Python può contenere surrogati isolati: 3 byte, mai un'eccezione.
        self.assertEqual(chat._unita_testo("\ud83d"), 6)
        self.assertEqual(chat._unita_testo("a\udc00b"), 1 + 6 + 1)

    def test_sequenza_di_15_senza_bonus(self):
        self.assertEqual(chat._unita_testo("a" * 15), 15)
        self.assertEqual(chat._unita_testo("Ab3" * 5), 10 + 5 * 3)

    def test_sequenza_di_16_bonus_sulle_lettere(self):
        self.assertEqual(chat._unita_testo("a" * 16), 32)
        self.assertEqual(chat._unita_testo("a" * 40), 80)

    def test_sequenza_mista_bonus_solo_sulle_lettere(self):
        # 8 lettere (1 + bonus 1) e 8 cifre (3, nessun bonus).
        self.assertEqual(chat._unita_testo("abcdefgh12345678"), 8 * 2 + 8 * 3)

    def test_sequenza_di_sole_cifre_nessun_bonus(self):
        self.assertEqual(chat._unita_testo("1" * 16), 48)
        self.assertEqual(chat._unita_testo("9" * 100), 300)

    def test_sequenze_separate_da_punteggiatura(self):
        self.assertEqual(chat._unita_testo("a" * 16 + "-" + "b" * 16), 32 + 2 + 32)
        # Ognuna sotto 16: nessun bonus, anche se insieme superano 16.
        self.assertEqual(chat._unita_testo("a" * 10 + "_" + "b" * 10), 10 + 2 + 10)
        self.assertEqual(chat._unita_testo("a" * 10 + " " + "b" * 10), 21)

    def test_non_ascii_interrompe_la_sequenza(self):
        self.assertEqual(chat._unita_testo("a" * 10 + "é" + "a" * 10), 10 + 4 + 10)

    def test_hash_e_base64_pesati_oltre_la_lunghezza(self):
        sha = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        base64 = "QXN0ZXIgaGlzdG9yeSB0b2tlbiBidWRnZXQgdjIgdGVzdCBjb3JwdXM"
        for testo in (sha, base64):
            with self.subTest(testo=testo[:12]):
                self.assertGreater(chat._unita_testo(testo), 1.5 * len(testo))

    def test_tool_call_dict_e_oggetto_ollama_stesso_costo(self):
        argomenti = {"path": "Documenti/项目/报告_2026.txt"}
        oggetto = _tool_call("read_file", argomenti)
        dizionario = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": argomenti}}],
        }
        atteso = chat._unita_testo("read_file") + chat._unita_testo(str(argomenti))
        self.assertEqual(chat._costo_cronologia(oggetto), atteso)
        self.assertEqual(chat._costo_cronologia(dizionario), atteso)
        # Nome e argomenti pesati con E16: i caratteri CJK del path valgono 6.
        self.assertGreater(atteso, len("read_file") + len(str(argomenti)) + 4 * 5)

    def test_contenuto_e_tool_call_sommati(self):
        messaggio = SimpleNamespace(
            role="assistant",
            content="漢",
            tool_calls=[SimpleNamespace(function=SimpleNamespace(name="x", arguments={"k": 1}))],
        )
        self.assertEqual(chat._costo_cronologia(messaggio), 6 + 1 + chat._unita_testo("{'k': 1}"))


# Corpus deterministico (nessun modello): una voce per classe.
CORPUS_E16 = {
    "italiano": "Ieri ho sistemato il server: però la cartella delle foto è stata saltata, perché?",
    "codice": "def media(valori):\n    return sum(valori) / len(valori) if valori else None\n",
    "numeri": "Fattura 2026-0915: totale 1.284,50 euro, IVA 22% pari a 231,63 euro.",
    "cjk comune": "今天我修改了服务器的配置，并且更新了项目的文档。",
    "cjk raro": "龘靐齉爩鱻麤龖龗驫灥飝厵癵籱鸝鬱",
    "emoji": "😀😂👍🎉🔥💡🚀✅🙏🤔",
    "astrale": "\U00020000\U00020001\U0002a6d0\U00020b9f",
    "arabo": "قمت اليوم بتعديل إعدادات الخادم وتحديث وثائق المشروع",
    "cirillico": "Сегодня я изменил настройки сервера и обновил документацию",
    "accenti europei": "Ich habe gestern einen spannenden Artikel über künstliche Intelligenz gelesen",
    "zalgo": "a\u0301\u0302\u0303\u0304\u0305e\u0306\u0307\u0308\u0309\u030a",
    "uso privato": "\ue000\ue001\uf8ff\U000f0000\U000f0001",
    "base64/hash": "QXN0ZXIgaGlzdG9yeSB0b2tlbiBidWRnZXQ 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822c",
}


def _byte_non_ascii(testo):
    return sum(len(c.encode("utf-8")) for c in testo if not c.isascii())


class TestCorpusE16(unittest.TestCase):
    """Proprietà deterministiche E16 su un corpus rappresentativo (nessun token Qwen hardcoded)."""

    def test_ogni_byte_non_ascii_pesa_almeno_due_unita(self):
        # Base del limite: un tokenizer BPE a byte usa al più un token per
        # byte, quindi il testo non ASCII resta sotto 0.5 token per unità.
        for classe, testo in CORPUS_E16.items():
            with self.subTest(classe=classe):
                self.assertGreaterEqual(chat._unita_testo(testo), 2 * _byte_non_ascii(testo))
                self.assertGreaterEqual(chat._unita_testo(testo), len(testo))

    def test_testo_interamente_non_ascii_esatto(self):
        for classe in ("cjk raro", "emoji", "astrale", "zalgo", "uso privato"):
            testo = CORPUS_E16[classe]
            with self.subTest(classe=classe):
                ascii_ = sum(1 for c in testo if c.isascii())
                self.assertEqual(chat._unita_testo(testo), ascii_ + 2 * _byte_non_ascii(testo))

    def test_cjk_e_astrale_pesano_molto_piu_della_prosa(self):
        italiano = chat._unita_testo(CORPUS_E16["italiano"]) / len(CORPUS_E16["italiano"])
        for classe, minimo in (("cjk comune", 5.5), ("cjk raro", 6), ("astrale", 8)):
            testo = CORPUS_E16[classe]
            with self.subTest(classe=classe):
                self.assertGreaterEqual(chat._unita_testo(testo) / len(testo), minimo)
                self.assertGreater(chat._unita_testo(testo) / len(testo), 4 * italiano)

    def test_cirillico_e_arabo_quattro_unita_per_lettera(self):
        for classe in ("cirillico", "arabo"):
            testo = CORPUS_E16[classe]
            lettere = sum(1 for c in testo if not c.isascii())
            spazi = sum(1 for c in testo if c.isascii())
            with self.subTest(classe=classe):
                self.assertEqual(chat._unita_testo(testo), 4 * lettere + spazi)

    def test_base64_e_hash_con_bonus(self):
        testo = CORPUS_E16["base64/hash"]
        senza_bonus = len(testo) + 2 * sum(testo.count(c) for c in "0123456789")
        self.assertGreater(chat._unita_testo(testo), senza_bonus)


class TestNonRegressioneItaliano(unittest.TestCase):
    """La prosa italiana normale cresce solo per accenti e punteggiatura, non come il CJK."""

    PROSA = (
        "Ieri sera ho finalmente sistemato la configurazione del server di casa: "
        "adesso il backup parte da solo ogni notte. Però non sono sicuro che la "
        "cartella delle foto venga inclusa, perché l'ultima volta è stata saltata. "
        "Domani proverò a controllare i log e, se serve, cambierò le impostazioni "
        "del programma. Mi ricordi anche di comprare il caffè e di chiamare Giulia "
        "per la cena di venerdì?"
    )

    def test_aumento_solo_da_accenti_e_punteggiatura(self):
        punteggiatura = sum(1 for c in self.PROSA if c in string.punctuation)
        accentati = sum(1 for c in self.PROSA if not c.isascii())
        cifre = sum(self.PROSA.count(c) for c in "0123456789")
        prima = len(self.PROSA) + 2 * cifre

        # Ogni accento passa da 1 a 4 unità, ogni segno da 1 a 2; nient'altro.
        self.assertEqual(chat._unita_testo(self.PROSA), prima + punteggiatura + 3 * accentati)
        self.assertLessEqual(chat._unita_testo(self.PROSA), 1.15 * prima)

    def test_conversazione_italiana_conserva_gli_stessi_turni(self):
        conversazione = []
        for indice in range(30):
            conversazione += [_user(f"Domanda {indice}: come procedo?"), _assistant(self.PROSA)]
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione)

        # Con budget 4000 restano 8 turni da ~430 unità: la prosa non viene
        # moltiplicata come il testo CJK.
        self.assertEqual(_turni_precedenti(messaggi), 8)
        self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def test_cjk_della_stessa_lunghezza_conserva_meno_turni(self):
        cjk = (CORPUS_E16["cjk comune"] * 20)[:len(self.PROSA)]
        italiano = [x for i in range(30) for x in (_user(f"d{i}"), _assistant(self.PROSA))]
        cinese = [x for i in range(30) for x in (_user(f"d{i}"), _assistant(cjk))]

        turni_italiano = _turni_precedenti(_limita([*italiano, _user("corrente")]))
        turni_cinese = _turni_precedenti(_limita([*cinese, _user("corrente")]))

        self.assertLess(turni_cinese, turni_italiano)
        self.assertEqual(turni_cinese, 1)


class TestBudgetPesato(unittest.TestCase):

    def test_costanti(self):
        self.assertEqual(MAX_UNITA_CRONOLOGIA, 4000)
        self.assertEqual(SOGLIA_COMPATTAZIONE_TOOL, 1000)
        self.assertEqual(SOGLIA_COMPATTAZIONE_RISPOSTA, 2000)

    def _conversazione(self, carattere, turni=6, lunghezza=700):
        conversazione = []
        for indice in range(turni):
            conversazione += [_user(f"domanda {indice}"), _assistant(carattere * lunghezza)]
        conversazione.append(_user("corrente"))
        return conversazione

    def test_prosa_mantiene_piu_storia_dei_numeri(self):
        prosa = _limita(self._conversazione("a"), limite=100)
        numeri = _limita(self._conversazione("7"), limite=100)

        self.assertGreater(_turni_precedenti(prosa), _turni_precedenti(numeri))
        self.assertEqual(_turni_precedenti(numeri), 1)

    def test_numeri_potati_anche_sotto_il_budget_in_caratteri(self):
        conversazione = self._conversazione("1", turni=3, lunghezza=600)
        caratteri = sum(len(m["content"]) for m in conversazione)
        self.assertLess(caratteri, MAX_UNITA_CRONOLOGIA)

        messaggi = _limita(conversazione, limite=100)

        self.assertLess(_turni_precedenti(messaggi), 3)
        self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def _turni_tool_con_argomenti(self, query, turni=4):
        conversazione = []
        for indice in range(turni):
            conversazione += [
                _user(f"cerca {indice}"),
                _tool_call("cerca_memoria", {"query": query}),
                _tool({"ok": True, "operation": "search", "status": "searched"}),
                _assistant("fatto"),
            ]
        conversazione.append(_user("corrente"))
        return conversazione

    def test_cifre_negli_argomenti_tool_pesate(self):
        lettere = _limita(self._turni_tool_con_argomenti("a" * 500), limite=100)
        cifre = _limita(self._turni_tool_con_argomenti("9" * 500), limite=100)

        self.assertGreater(_turni_precedenti(lettere), _turni_precedenti(cifre))
        for messaggi in (lettere, cifre):
            _assert_nessun_orfano(self, messaggi)
            self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def test_budget_rispettato_salvo_il_solo_ultimo_turno(self):
        casi = [
            self._conversazione("a"),
            self._conversazione("5"),
            self._conversazione("a1", turni=10, lunghezza=300),
            self._turni_tool_con_argomenti("42" * 300),
            [*_turno_tool(_risultato_processi(), nome="list_processes"),
             *_turno_tool({"ok": True, "operation": "x", "status": "success", "data": "8" * 900}),
             _user("corrente")],
        ]
        for conversazione in casi:
            messaggi = _limita(conversazione, limite=100)
            with self.subTest(turni=_turni_precedenti(messaggi)):
                _assert_nessun_orfano(self, messaggi)
                self.assertEqual(messaggi[-1], _user("corrente"))
                if _turni_precedenti(messaggi) > 0:
                    self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def test_taglio_per_turni_interi(self):
        conversazione = []
        for indice in range(5):
            conversazione += _turno_tool(
                {"ok": True, "operation": "x", "status": "success", "data": "3" * 400},
                risposta="0" * 300,
                domanda=f"d{indice}",
            )
        conversazione.append(_user("corrente"))

        messaggi = _limita(conversazione, limite=100)

        _assert_nessun_orfano(self, messaggi)
        # Ogni turno conservato è completo: user, tool call, tool, assistant.
        corpo = messaggi[1:-1]
        self.assertEqual(len(corpo) % 4, 0)
        for inizio in range(0, len(corpo), 4):
            self.assertEqual(
                [_ruolo(m) for m in corpo[inizio:inizio + 4]],
                ["user", "assistant", "tool", "assistant"],
            )
        self.assertLessEqual(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def test_ultimo_user_numerico_enorme_preservato(self):
        # Contratto attuale (debito noto): il turno corrente non viene mai
        # rimosso né troncato, anche se da solo supera il budget.
        enorme = "9" * MAX_UNITA_CRONOLOGIA
        conversazione = [_user("vecchia"), _assistant("ok"), _user(enorme)]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi, [SISTEMA, _user(enorme)])
        self.assertGreater(_costo_totale(messaggi), MAX_UNITA_CRONOLOGIA)

    def test_ultimo_turno_con_tool_preservato_anche_oltre_budget(self):
        ultimo = _turno_tool(
            {"ok": True, "operation": "x", "status": "success"},
            risposta="1" * MAX_UNITA_CRONOLOGIA,
            domanda="corrente",
        )
        conversazione = [_user("vecchia"), _assistant("ok"), *ultimo]

        messaggi = _limita(conversazione)

        self.assertEqual(messaggi[1:], ultimo)

    def test_soglie_di_compattazione_restano_in_caratteri(self):
        base = {"ok": True, "operation": "x", "status": "success", "data": ""}
        cifre = SOGLIA_COMPATTAZIONE_TOOL - len(json.dumps(base, ensure_ascii=False))
        risultato = dict(base, data="7" * cifre)
        contenuto = json.dumps(risultato, ensure_ascii=False)
        self.assertEqual(len(contenuto), SOGLIA_COMPATTAZIONE_TOOL)

        messaggi = _limita([*_turno_tool(risultato), _user("corrente")])

        # 1000 caratteri ma ~2900 unità: non compattato (soglia in caratteri).
        self.assertEqual(_contenuti_tool(messaggi), [contenuto])


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
                    ContestoFilesystem(allowed_roots=[], reserved_roots=()),
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


# =====================================================================
# 0.7.1b: nessuna lettura di configurazione in chat.py
# =====================================================================

SORGENTE_CHAT = BASE_DIR / "modules" / "chat.py"


class TestAvviaChatSenzaConfig(unittest.TestCase):

    def test_nessun_import_di_config_o_runtime_paths(self):
        albero = ast.parse(SORGENTE_CHAT.read_text(encoding="utf-8"))
        moduli = set()
        nomi = set()
        for nodo in ast.walk(albero):
            if isinstance(nodo, ast.Import):
                moduli.update(alias.name for alias in nodo.names)
            elif isinstance(nodo, ast.ImportFrom):
                moduli.add(nodo.module or "")
                nomi.update(alias.name for alias in nodo.names)

        self.assertNotIn("modules.config", moduli)
        self.assertNotIn("modules.runtime_paths", moduli)
        for nome in ("config", "runtime_paths", "carica_config",
                     "carica_config_runtime", "prepara_contesto_filesystem",
                     "percorsi_runtime"):
            with self.subTest(nome=nome):
                self.assertNotIn(nome, nomi)
                self.assertFalse(hasattr(chat, nome))

    def test_nessuna_apertura_di_file_di_configurazione(self):
        sorgente = SORGENTE_CHAT.read_text(encoding="utf-8")
        self.assertNotIn("config.json", sorgente)
        self.assertNotIn("config.default.json", sorgente)

        for nodo in ast.walk(ast.parse(sorgente)):
            if not isinstance(nodo, ast.Call):
                continue
            funzione = nodo.func
            nome = getattr(funzione, "id", None) or getattr(funzione, "attr", None)
            with self.subTest(riga=nodo.lineno):
                self.assertNotIn(nome, {"open", "read_text", "read_bytes"})

    def test_avvia_chat_usa_il_contesto_ricevuto(self):
        tmp = Path(tempfile.mkdtemp(prefix="aster_test_contesto_"))
        self.addCleanup(shutil.rmtree, tmp, True)

        contesto_ricevuto = ContestoFilesystem(allowed_roots=[tmp / "root"], reserved_roots=())
        contesti_visti = []

        def handler_filesystem(argomenti, contesto):
            contesti_visti.append(contesto)
            return {"ok": True, "operation": "tool_fs", "status": "success"}

        def registro_filesystem():
            registro = RegistroStrumenti()
            registro.registra(ToolSpec(nome="tool_fs", schema=_schema("tool_fs"),
                                       handler=handler_filesystem, livello="READ_ONLY",
                                       dominio="filesystem"))
            return registro

        def vietato(*args, **kwargs):
            raise AssertionError("avvia_chat non deve leggere la configurazione")

        aperti = []
        open_originale = builtins.open

        def open_spia(file, *args, **kwargs):
            aperti.append(file)
            return open_originale(file, *args, **kwargs)

        risposte = iter([_risposta_tool("tool_fs")])
        ingressi = iter(["usa il tool", "esci"])

        patch_attive = [
            mock.patch.object(chat, "crea_registro_memoria", registro_filesystem),
            mock.patch.object(chat, "registra_tool_sistema", lambda registro: None),
            mock.patch.object(chat, "registra_tool_filesystem", lambda registro: None),
            mock.patch.object(chat, "esegui_turno_con_tools", lambda *a: next(risposte)),
            mock.patch.object(chat, "genera_risposta_post_tool", lambda **kw: "Fatto."),
            mock.patch.object(modulo_config, "carica_config", vietato),
            mock.patch.object(modulo_config, "carica_config_runtime", vietato),
            mock.patch.object(modulo_config, "_leggi_json", vietato),
            mock.patch.object(runtime_paths, "percorsi_runtime", vietato),
            mock.patch.object(file_tools, "prepara_contesto_filesystem", vietato),
            mock.patch.object(builtins, "input", lambda prompt="": next(ingressi)),
            mock.patch.object(builtins, "open", open_spia),
        ]
        with contextlib.ExitStack() as pila:
            for patch in patch_attive:
                pila.enter_context(patch)
            pila.enter_context(contextlib.redirect_stdout(io.StringIO()))
            chat.avvia_chat(
                "prompt di sistema", "qwen3:8b", LIMITE,
                "http://localhost:11434", 60, 8192,
                StatoMemoria(modalita=MODALITA_NORMALE, memoria=None),
                tmp / "memory.json", 5,
                contesto_ricevuto,
            )

        self.assertEqual(len(contesti_visti), 1)
        self.assertIs(contesti_visti[0], contesto_ricevuto)
        self.assertEqual(aperti, [])


if __name__ == "__main__":
    unittest.main()
