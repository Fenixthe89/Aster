"""
0.7.2d - modules/process_info.py: uso CPU e RAM dei processi, per nome.

Copre identità all'enumerazione (PID + creation time: un PID riusato
prima o tra le passate non riceve mai metriche sotto il vecchio nome),
campionamento a due passate con un'unica attesa globale, delta
CPU normalizzato sui processori logici (somma dei tick grezzi del
gruppo, un solo arrotondamento .5 per eccesso in Decimal, limiti 0-100), RAM da
PrivateWorkingSetSize, esclusione del PID 0, processi terminati e PID
riusati, processi protetti e non disponibili, precedenza dei marcatori
nei gruppi misti, chiusura di ogni handle, finestra e numero di
processori non validi, backend non disponibile e import sicuro.

API, orologio e attesa sono sempre finti: nessuna sleep reale nei test
unitari. Lo strato ctypes è verificato con funzioni Python al posto
delle chiamate kernel32; su Windows a 64 bit un unico smoke reale sul
processo corrente, senza valori hardcodati.

Nessuna dipendenza esterna, nessun subprocess, nessun file in data/.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import ctypes
import importlib.util
import json
import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.process_info as process_info
from modules.process_info import (
    CPU_SAMPLE_INTERVAL_SECONDS,
    LEGGIBILE,
    NON_DISPONIBILE,
    PROCESSO_PROTETTO,
    GruppoProcessi,
    normalizza_cpu,
)

WINDOWS_64 = sys.platform == "win32" and struct.calcsize("P") == 8

STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_GENERICO = 31

TICK_PER_SECONDO = 10_000_000
# Un decimo di secondo di CPU in tick da 100 ns.
TICK_DECIMO = 1_000_000


class _Processo:
    """
    Comportamento finto di un PID nelle due passate.

    apertura: codice errore di OpenProcess per passata (None = riuscita).
    tempi: (creazione, tick) per passata, oppure None = GetProcessTimes fallita.
    """

    def __init__(self, tick=(0, 0), creazione=(1000, 1000), apertura=(None, None),
                 tempi_falliti=(False, False), codice=STILL_ACTIVE, memoria=0):
        self.tick = tick
        self.creazione = creazione
        self.apertura = apertura
        self.tempi_falliti = tempi_falliti
        self.codice = codice
        self.memoria = memoria


class _ApiFinta:
    """
    Doppio di _ApiKernel32. processi: pid -> _Processo (assente = PID
    inesistente). Registra aperture, chiusure e passate; un'eccezione
    passata in solleva_su (nome metodo) viene sollevata alla prima chiamata.
    """

    def __init__(self, processi, solleva_su=None):
        self._processi = processi
        self._solleva_su = solleva_su
        self._passata = {}
        self._handle_pid = {}
        self._prossimo = 1
        self.aperti = set()
        self.chiusure = []
        self.aperture = []

    def _forse_solleva(self, metodo):
        if self._solleva_su == metodo:
            raise OSError(r"C:\Users\mario\dettaglio interno")

    def apri(self, pid):
        self._forse_solleva("apri")
        self.aperture.append(pid)
        passata = self._passata.get(pid, 0)
        self._passata[pid] = passata + 1

        processo = self._processi.get(pid)
        if processo is None:
            return None, ERROR_INVALID_PARAMETER
        errore = processo.apertura[min(passata, 1)]
        if errore is not None:
            return None, errore

        handle = self._prossimo
        self._prossimo += 1
        self._handle_pid[handle] = (pid, min(passata, 1))
        self.aperti.add(handle)
        return handle, 0

    def chiudi(self, handle):
        self.chiusure.append(handle)
        self.aperti.discard(handle)

    def tempi(self, handle):
        self._forse_solleva("tempi")
        pid, passata = self._handle_pid[handle]
        processo = self._processi[pid]
        if processo.tempi_falliti[passata]:
            return None
        return processo.creazione[passata], processo.tick[passata]

    def codice_uscita(self, handle):
        self._forse_solleva("codice_uscita")
        pid, _passata = self._handle_pid[handle]
        return self._processi[pid].codice

    def memoria_privata(self, handle):
        self._forse_solleva("memoria_privata")
        pid, _passata = self._handle_pid[handle]
        return self._processi[pid].memoria


class _Orologio:
    """Orologio monotono finto: restituisce i valori indicati, in ordine."""

    def __init__(self, valori=(0.0, 0.0, 0.1, 0.1)):
        self._valori = list(valori)
        self.letture = 0

    def __call__(self):
        valore = self._valori[min(self.letture, len(self._valori) - 1)]
        self.letture += 1
        return valore


class _Attesa:
    def __init__(self):
        self.chiamate = []

    def __call__(self, secondi):
        self.chiamate.append(secondi)


class _OrologioSimulato:
    """Tempo simulato condiviso da orologio, attesa e API cronometrata."""

    def __init__(self):
        self.t = 0.0
        self.bordi = []

    def __call__(self):
        self.bordi.append(self.t)
        return self.t

    def attendi(self, secondi):
        self.t += secondi


class _ApiCronometrata(_ApiFinta):
    """
    Ogni chiamata API consuma passo secondi simulati; ogni processo usa
    un core per tutto il tempo (tick proporzionali al tempo simulato).
    Registra l'istante di ogni lettura dei tempi per PID.
    """

    def __init__(self, pids, orologio, passo):
        super().__init__({pid: _Processo() for pid in pids})
        self._orologio = orologio
        self._passo = passo
        self.letture = {}

    def _avanza(self):
        self._orologio.t += self._passo

    def apri(self, pid):
        self._avanza()
        return super().apri(pid)

    def chiudi(self, handle):
        self._avanza()
        super().chiudi(handle)

    def codice_uscita(self, handle):
        self._avanza()
        return super().codice_uscita(handle)

    def memoria_privata(self, handle):
        self._avanza()
        return super().memoria_privata(handle)

    def tempi(self, handle):
        self._avanza()
        pid, _passata = self._handle_pid[handle]
        istante = self._orologio.t
        self.letture.setdefault(pid, []).append(istante)
        return 1000, round(istante * TICK_PER_SECONDO)

    def durate_passate(self, orologio):
        inizio_prima, fine_prima, inizio_seconda, fine_seconda = orologio.bordi
        return fine_prima - inizio_prima, fine_seconda - inizio_seconda


def _con_identita(processi, api):
    """
    (pid, nome) -> (pid, nome, creation time enumerato). Di default è
    l'identità del processo finto alla prima passata (nessun riuso
    prima della misura); le terne esplicite restano invariate.
    """

    voci = []
    for voce in processi:
        if len(voce) == 3:
            voci.append(voce)
            continue
        pid, nome = voce
        processo = getattr(api, "_processi", {}).get(pid)
        voci.append((pid, nome, processo.creazione[0] if processo is not None else 1000))
    return voci


def _misura(processi, api, processori=1, orologio=None, attesa=None):
    """Esegue _misura con orologio (finestra 0.1 s) e attesa finti; restituisce (gruppi, attesa)."""

    attesa = attesa or _Attesa()
    gruppi = process_info._misura(
        _con_identita(processi, api),
        api,
        orologio or _Orologio(),
        attesa,
        processori,
    )
    return gruppi, attesa


def _per_nome(gruppi):
    return {gruppo.nome: gruppo for gruppo in gruppi}


# =====================================================================
# Normalizzazione CPU
# =====================================================================

class TestNormalizzaCpu(unittest.TestCase):

    def test_delta_un_core_pieno(self):
        # 0.1 s di CPU in 0.1 s su 1 processore logico = 100%.
        self.assertEqual(normalizza_cpu(TICK_DECIMO, 0.1, 1), 100)

    def test_normalizzazione_sui_processori_logici(self):
        self.assertEqual(normalizza_cpu(TICK_DECIMO, 0.1, 4), 25)
        self.assertEqual(normalizza_cpu(TICK_DECIMO, 0.1, 24), 4)

    def test_arrotondamento_mezzo_per_eccesso(self):
        # 0.5 s / 1 s / 4 = 12.5% -> 13 (il banker's rounding darebbe 12).
        self.assertEqual(normalizza_cpu(5_000_000, 1.0, 4), 13)
        # 1.25% -> 1
        self.assertEqual(normalizza_cpu(1_000_000, 1.0, 8), 1)

    def test_caso_della_review(self):
        # 35_000 tick in 0.1 s su 1 processore = 3.5% esatto -> 4.
        # int(x + 0.5) sul float dava 3 (3.4999999999999996).
        self.assertEqual(normalizza_cpu(35_000, 0.1, 1), 4)

    def test_soglie_mezzo_esatte(self):
        # In 0.1 s su 1 processore: percentuale = tick / 10_000.
        casi = {
            5_000: 1,      # 0.5
            15_000: 2,     # 1.5
            25_000: 3,     # 2.5
            35_000: 4,     # 3.5
            45_000: 5,     # 4.5
            145_000: 15,   # 14.5
            995_000: 100,  # 99.5
        }
        for tick, atteso in casi.items():
            self.assertEqual(normalizza_cpu(tick, 0.1, 1), atteso, msg=tick)

    def test_appena_sotto_e_sopra_la_meta(self):
        for tick, atteso in ((4_999, 0), (5_001, 1), (34_999, 3), (35_001, 4), (144_999, 14), (145_001, 15)):
            self.assertEqual(normalizza_cpu(tick, 0.1, 1), atteso, msg=tick)

    def test_soglie_con_piu_processori_e_finestre_reali(self):
        # 4 processori: 100_000 tick in 0.1 s = 2.5% -> 3; 140_000 = 3.5% -> 4.
        self.assertEqual(normalizza_cpu(100_000, 0.1, 4), 3)
        self.assertEqual(normalizza_cpu(140_000, 0.1, 4), 4)
        # 24 processori, finestra reale tipica 0.104 s: 3.5% esatto -> 4.
        self.assertEqual(normalizza_cpu(873_600, 0.104, 24), 4)
        self.assertEqual(normalizza_cpu(873_599, 0.104, 24), 3)
        # 0.104 s su 1 processore: 36_400 tick = 3.5% -> 4.
        self.assertEqual(normalizza_cpu(36_400, 0.104, 1), 4)

    def test_contesto_decimal_globale_invariato(self):
        import decimal

        prima = decimal.getcontext().copy()
        normalizza_cpu(35_000, 0.1, 1)
        dopo = decimal.getcontext()
        self.assertEqual((prima.prec, prima.rounding), (dopo.prec, dopo.rounding))

    def test_limite_cento(self):
        self.assertEqual(normalizza_cpu(TICK_DECIMO * 50, 0.1, 1), 100)

    def test_limite_zero(self):
        self.assertEqual(normalizza_cpu(-5, 0.1, 1), 0)
        self.assertEqual(normalizza_cpu(0, 0.1, 1), 0)

    def test_finestra_non_valida(self):
        for finestra in (0, 0.0, -0.1, float("nan"), float("inf"), None, True, "0.1"):
            self.assertIsNone(normalizza_cpu(TICK_DECIMO, finestra, 1), msg=repr(finestra))

    def test_processori_non_validi(self):
        for processori in (0, -1, None, True, 2.0, "8"):
            self.assertIsNone(normalizza_cpu(TICK_DECIMO, 0.1, processori), msg=repr(processori))

    def test_tick_non_intero(self):
        for tick in (None, 1.5, True, "10"):
            self.assertIsNone(normalizza_cpu(tick, 0.1, 1), msg=repr(tick))

    def test_restituisce_int(self):
        self.assertIs(type(normalizza_cpu(TICK_DECIMO, 0.1, 3)), int)


# =====================================================================
# Campionamento, delta e RAM
# =====================================================================

class TestCampionamento(unittest.TestCase):

    def test_delta_cpu_e_ram(self):
        api = _ApiFinta({10: _Processo(tick=(5_000, 5_000 + TICK_DECIMO), memoria=4096)})
        gruppi, _ = _misura([(10, "a.exe")], api, processori=4)

        self.assertEqual(
            gruppi,
            [GruppoProcessi("a.exe", 1, LEGGIBILE, 25, 4096)],
        )

    def test_una_sola_attesa_globale_da_un_decimo(self):
        api = _ApiFinta({pid: _Processo() for pid in range(1, 40)})
        _gruppi, attesa = _misura([(pid, f"p{pid}.exe") for pid in range(1, 40)], api)

        self.assertEqual(attesa.chiamate, [CPU_SAMPLE_INTERVAL_SECONDS])
        self.assertEqual(CPU_SAMPLE_INTERVAL_SECONDS, 0.1)

    def test_due_passate_stesso_ordine(self):
        api = _ApiFinta({3: _Processo(), 1: _Processo(), 2: _Processo()})
        _misura([(3, "c.exe"), (1, "a.exe"), (2, "b.exe")], api)

        self.assertEqual(api.aperture, [3, 1, 2, 3, 1, 2])

    def test_finestra_tra_i_punti_medi_delle_passate(self):
        # Passate da 0.02 s, attesa 0.1 s: finestra (0.12+0.14)/2 - (0+0.02)/2 = 0.12.
        # Con la sola attesa (0.10 s) risulterebbe 60% invece di 50%.
        api = _ApiFinta({1: _Processo(tick=(0, 1_200_000))})
        gruppi, _ = _misura(
            [(1, "a.exe")],
            api,
            processori=2,
            orologio=_Orologio((0.0, 0.02, 0.12, 0.14)),
        )

        self.assertEqual(gruppi[0].cpu_percent, 50)

    def test_finestra_midpoint_approssima_l_intervallo_reale(self):
        # Tempo simulato: ogni chiamata API costa 4 µs; la seconda passata
        # fa più chiamate per processo (come quella reale: ~2.8 ms vs ~4.5 ms).
        # Ogni processo usa 1 core per tutto il tempo: su 4 processori la
        # verità è 25% sul suo intervallo reale tra le due letture.
        orologio = _OrologioSimulato()
        pids = list(range(1, 301))
        api = _ApiCronometrata(pids, orologio, passo=4e-6)

        esiti, finestra = process_info._campiona(
            api, {pid: 1000 for pid in pids}, orologio, orologio.attendi
        )

        prima, seconda = api.durate_passate(orologio)
        limite = abs(seconda - prima) / 2 / finestra
        errori = [
            abs((letture[1] - letture[0]) - finestra) / finestra
            for letture in api.letture.values()
        ]
        self.assertLessEqual(max(errori), limite + 2 * 4e-6 / finestra)
        self.assertLess(max(errori), 0.02)

        gruppi = process_info._aggrega(
            [(pid, f"p{pid}.exe") for pid in pids], esiti, finestra, 4
        )
        self.assertEqual({gruppo.cpu_percent for gruppo in gruppi}, {25})

    def test_ram_somma_private_working_set(self):
        api = _ApiFinta({
            1: _Processo(memoria=100),
            2: _Processo(memoria=250),
            3: _Processo(memoria=0),
        })
        gruppi, _ = _misura([(1, "chrome.exe"), (2, "chrome.exe"), (3, "chrome.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("chrome.exe", 3, LEGGIBILE, 0, 350)])

    def test_ram_zero_resta_zero(self):
        api = _ApiFinta({1: _Processo(memoria=0)})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi[0].memory_bytes, 0)
        self.assertEqual(gruppi[0].metriche, LEGGIBILE)

    def test_delta_negativo_non_disponibile(self):
        api = _ApiFinta({1: _Processo(tick=(500, 400))})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)])

    def test_valori_non_interi_non_disponibili(self):
        casi = (
            _Processo(tick=(1.5, 3)),
            _Processo(tick=(0, True)),
            _Processo(creazione=(None, None)),
            _Processo(memoria=None),
            _Processo(memoria=-1),
            _Processo(memoria=2.5),
            _Processo(memoria=True),
        )
        for processo in casi:
            gruppi, _ = _misura([(1, "a.exe")], _ApiFinta({1: processo}))
            self.assertEqual(gruppi[0].metriche, NON_DISPONIBILE, msg=vars(processo))
            self.assertIsNone(gruppi[0].cpu_percent)
            self.assertIsNone(gruppi[0].memory_bytes)

    def test_zero_processi(self):
        api = _ApiFinta({})
        gruppi, attesa = _misura([], api)

        self.assertEqual(gruppi, [])
        self.assertEqual(attesa.chiamate, [])
        self.assertEqual(api.aperture, [])


class TestAggregazione(unittest.TestCase):

    def test_piu_istanze_stesso_nome(self):
        api = _ApiFinta({
            1: _Processo(tick=(0, TICK_DECIMO), memoria=10),
            2: _Processo(tick=(0, TICK_DECIMO), memoria=20),
            3: _Processo(tick=(0, 0), memoria=5),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe"), (3, "y.exe")], api, processori=4)

        self.assertEqual(
            _per_nome(gruppi),
            {
                "x.exe": GruppoProcessi("x.exe", 2, LEGGIBILE, 50, 30),
                "y.exe": GruppoProcessi("y.exe", 1, LEGGIBILE, 0, 5),
            },
        )

    def test_somma_tick_grezzi_prima_di_arrotondare(self):
        # Tre istanze da 0.4% ciascuna: arrotondate una per una farebbero 0.
        processi = {pid: _Processo(tick=(0, 40_000)) for pid in (1, 2, 3)}
        gruppi, _ = _misura(
            [(1, "x.exe"), (2, "x.exe"), (3, "x.exe")],
            _ApiFinta(processi),
            orologio=_Orologio((0.0, 0.0, 1.0, 1.0)),
        )
        self.assertEqual(gruppi[0].cpu_percent, 1)

    def test_somma_tick_grezzi_nessun_errore_cumulativo_per_eccesso(self):
        # Tre istanze da 0.5%: arrotondate una per una farebbero 3, la somma 1.5 -> 2.
        processi = {pid: _Processo(tick=(0, 50_000)) for pid in (1, 2, 3)}
        gruppi, _ = _misura(
            [(1, "x.exe"), (2, "x.exe"), (3, "x.exe")],
            _ApiFinta(processi),
            orologio=_Orologio((0.0, 0.0, 1.0, 1.0)),
        )
        self.assertEqual(gruppi[0].cpu_percent, 2)

    def test_gruppo_limitato_a_cento(self):
        processi = {pid: _Processo(tick=(0, TICK_DECIMO)) for pid in (1, 2)}
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe")], _ApiFinta(processi), processori=1)

        self.assertEqual(gruppi[0].cpu_percent, 100)

    def test_nome_completo_esatto_case_sensitive(self):
        api = _ApiFinta({1: _Processo(), 2: _Processo(), 3: _Processo()})
        gruppi, _ = _misura([(1, "steam.exe"), (2, "Steam.exe"), (3, "steam")], api)

        self.assertEqual([gruppo.nome for gruppo in gruppi], ["steam.exe", "Steam.exe", "steam"])
        self.assertTrue(all(gruppo.istanze == 1 for gruppo in gruppi))

    def test_ordine_di_prima_comparsa(self):
        api = _ApiFinta({1: _Processo(), 2: _Processo(), 3: _Processo()})
        gruppi, _ = _misura([(1, "b.exe"), (2, "a.exe"), (3, "b.exe")], api)

        self.assertEqual([gruppo.nome for gruppo in gruppi], ["b.exe", "a.exe"])


class TestPidZero(unittest.TestCase):

    def test_pid_zero_escluso_da_output_e_misure(self):
        api = _ApiFinta({0: _Processo(tick=(0, TICK_DECIMO * 50)), 4: _Processo()})
        gruppi, _ = _misura([(0, "System Idle Process"), (4, "System")], api)

        self.assertEqual([gruppo.nome for gruppo in gruppi], ["System"])
        self.assertNotIn(0, api.aperture)

    def test_solo_pid_zero(self):
        api = _ApiFinta({0: _Processo()})
        gruppi, attesa = _misura([(0, "System Idle Process")], api)

        self.assertEqual(gruppi, [])
        self.assertEqual(attesa.chiamate, [])

    def test_stesso_nome_di_un_processo_reale_non_conta_pid_zero(self):
        api = _ApiFinta({7: _Processo(memoria=9)})
        gruppi, _ = _misura([(0, "x.exe"), (7, "x.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("x.exe", 1, LEGGIBILE, 0, 9)])


class TestProcessiTerminatiERiusati(unittest.TestCase):

    def test_processo_uscito_durante_la_finestra(self):
        api = _ApiFinta({1: _Processo(codice=0), 2: _Processo(memoria=7)})
        gruppi, _ = _misura([(1, "a.exe"), (2, "b.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("b.exe", 1, LEGGIBILE, 0, 7)])

    def test_pid_non_piu_esistente_nella_seconda_passata(self):
        api = _ApiFinta({1: _Processo(apertura=(None, ERROR_INVALID_PARAMETER))})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi, [])

    def test_pid_non_piu_esistente_nella_prima_passata(self):
        api = _ApiFinta({})
        gruppi, attesa = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi, [])
        self.assertEqual(attesa.chiamate, [])

    def test_pid_riusato_creation_time_diverso(self):
        processo = _Processo(tick=(0, TICK_DECIMO * 9), creazione=(1000, 2000), memoria=999)
        api = _ApiFinta({1: processo, 2: _Processo(memoria=5)})
        gruppi, _ = _misura([(1, "vecchio.exe"), (2, "vecchio.exe")], api)

        # Le metriche del nuovo processo non vengono attribuite al vecchio nome.
        self.assertEqual(gruppi, [GruppoProcessi("vecchio.exe", 1, LEGGIBILE, 0, 5)])

    def test_gruppo_senza_istanze_rimaste_non_restituito(self):
        api = _ApiFinta({
            1: _Processo(codice=1),
            2: _Processo(creazione=(1, 2)),
            3: _Processo(memoria=1),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe"), (3, "y.exe")], api)

        self.assertEqual([gruppo.nome for gruppo in gruppi], ["y.exe"])

    def test_istanze_uscite_non_contate(self):
        api = _ApiFinta({
            1: _Processo(codice=0),
            2: _Processo(memoria=3),
            3: _Processo(memoria=4),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe"), (3, "x.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("x.exe", 2, LEGGIBILE, 0, 7)])

    def test_mai_trasformato_in_zero(self):
        api = _ApiFinta({1: _Processo(codice=0, memoria=0, tick=(0, 0))})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi, [])


class TestIdentitaEnumerazione(unittest.TestCase):
    """Il nome enumerato riceve metriche solo dal processo con lo stesso creation time."""

    def test_regressione_review_pid_riusato_prima_della_prima_passata(self):
        # Enumerazione: PID 123 old.exe con creation A. old.exe termina e il
        # PID viene riusato: entrambe le passate vedono il nuovo processo (B).
        creazione_a, creazione_b = 1000, 2000
        api = _ApiFinta({
            123: _Processo(tick=(0, TICK_DECIMO), creazione=(creazione_b, creazione_b), memoria=999),
        })
        gruppi, _ = _misura([(123, "old.exe", creazione_a)], api)

        self.assertEqual(gruppi, [])
        self.assertNotIn(999, [gruppo.memory_bytes for gruppo in gruppi])

    def test_riuso_prima_della_misura_non_contamina_il_gruppo(self):
        api = _ApiFinta({
            1: _Processo(creazione=(2000, 2000), memoria=999),
            2: _Processo(creazione=(3000, 3000), memoria=7),
        })
        gruppi, _ = _misura([(1, "old.exe", 1000), (2, "old.exe", 3000)], api)

        self.assertEqual(gruppi, [GruppoProcessi("old.exe", 1, LEGGIBILE, 0, 7)])

    def test_stesso_pid_nome_e_creation_time_valido(self):
        api = _ApiFinta({123: _Processo(tick=(0, TICK_DECIMO), creazione=(1000, 1000), memoria=999)})
        gruppi, _ = _misura([(123, "old.exe", 1000)], api, processori=4)

        self.assertEqual(gruppi, [GruppoProcessi("old.exe", 1, LEGGIBILE, 25, 999)])

    def test_identita_assente_e_processo_apribile_non_disponibile(self):
        # Creation time non letto all'enumerazione: nessuna metrica al nome.
        api = _ApiFinta({1: _Processo(memoria=999)})
        gruppi, _ = _misura([(1, "a.exe", None)], api)

        self.assertEqual(gruppi, [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)])

    def test_identita_assente_e_accesso_negato_resta_protetto(self):
        api = _ApiFinta({1: _Processo(apertura=(ERROR_ACCESS_DENIED, None))})
        gruppi, _ = _misura([(1, "MsMpEng.exe", None)], api)

        self.assertEqual(gruppi, [GruppoProcessi("MsMpEng.exe", 1, PROCESSO_PROTETTO, None, None)])

    def test_identita_assente_e_processo_sparito(self):
        api = _ApiFinta({})
        gruppi, _ = _misura([(1, "a.exe", None)], api)

        self.assertEqual(gruppi, [])

    def test_riuso_tra_le_passate_resta_verificato(self):
        api = _ApiFinta({1: _Processo(creazione=(1000, 2000), memoria=999)})
        gruppi, _ = _misura([(1, "a.exe", 1000)], api)

        self.assertEqual(gruppi, [])

    def test_identita_non_entra_nel_risultato(self):
        api = _ApiFinta({1: _Processo(creazione=(424242, 424242), memoria=5)})
        gruppi, _ = _misura([(1, "a.exe", 424242)], api)

        self.assertNotIn(424242, gruppi[0])
        self.assertEqual(GruppoProcessi._fields, ("nome", "istanze", "metriche", "cpu_percent", "memory_bytes"))


class TestLettoreCreazione(unittest.TestCase):

    def test_creation_time_letto_e_handle_chiuso(self):
        api = _ApiFinta({7: _Processo(creazione=(5555, 5555))})

        self.assertEqual(process_info._leggi_creazione(api, 7), 5555)
        self.assertEqual(api.aperti, set())
        self.assertEqual(len(api.chiusure), 1)

    def test_non_leggibile_none(self):
        casi = {
            "accesso negato": _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
            "errore generico": _Processo(apertura=(ERROR_GENERICO, None)),
            "tempi falliti": _Processo(tempi_falliti=(True, False)),
            "creation non intero": _Processo(creazione=(None, None)),
        }
        for etichetta, processo in casi.items():
            api = _ApiFinta({7: processo})
            self.assertIsNone(process_info._leggi_creazione(api, 7), msg=etichetta)
            self.assertEqual(api.aperti, set(), msg=etichetta)

        self.assertIsNone(process_info._leggi_creazione(_ApiFinta({}), 7))

    def test_eccezione_none_e_handle_chiuso(self):
        for metodo in ("apri", "tempi"):
            api = _ApiFinta({7: _Processo()}, solleva_su=metodo)
            self.assertIsNone(process_info._leggi_creazione(api, 7), msg=metodo)
            self.assertEqual(api.aperti, set(), msg=metodo)

    def test_pid_non_valido_nessuna_apertura(self):
        api = _ApiFinta({})
        for pid in (0, -1, 2 ** 32, True, "7", None):
            self.assertIsNone(process_info._leggi_creazione(api, pid), msg=repr(pid))
        self.assertEqual(api.aperture, [])

    def test_backend_non_disponibile(self):
        with mock.patch.object(process_info, "_piattaforma", return_value="linux"), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=AssertionError("kernel32")):
            leggi = process_info.crea_lettore_creazione()
            self.assertIsNone(leggi(1234))

    def test_lettore_usa_un_backend_per_chiamata(self):
        api = _ApiFinta({7: _Processo(creazione=(9, 9))})
        with mock.patch.object(process_info, "_crea_api", return_value=api):
            leggi = process_info.crea_lettore_creazione()
        self.assertEqual(leggi(7), 9)


class TestProtettiENonDisponibili(unittest.TestCase):

    def test_accesso_negato_processo_protetto(self):
        api = _ApiFinta({1: _Processo(apertura=(ERROR_ACCESS_DENIED, None))})
        gruppi, attesa = _misura([(1, "MsMpEng.exe")], api)

        self.assertEqual(
            gruppi,
            [GruppoProcessi("MsMpEng.exe", 1, PROCESSO_PROTETTO, None, None)],
        )
        # Nessun processo leggibile: nessuna attesa.
        self.assertEqual(attesa.chiamate, [])

    def test_errore_generico_apertura_non_disponibile(self):
        api = _ApiFinta({1: _Processo(apertura=(ERROR_GENERICO, None))})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)])

    def test_accesso_negato_solo_in_seconda_passata_non_disponibile(self):
        # Leggibile un attimo prima: non è "protetto", l'identità non è verificabile.
        api = _ApiFinta({1: _Processo(apertura=(None, ERROR_ACCESS_DENIED))})
        gruppi, _ = _misura([(1, "a.exe")], api)

        self.assertEqual(gruppi[0].metriche, NON_DISPONIBILE)

    def test_letture_fallite_non_disponibile(self):
        casi = (
            _Processo(tempi_falliti=(True, False)),
            _Processo(tempi_falliti=(False, True)),
            _Processo(codice=None),
            _Processo(memoria=None),
        )
        for processo in casi:
            gruppi, _ = _misura([(1, "a.exe")], _ApiFinta({1: processo}))
            self.assertEqual(
                gruppi,
                [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)],
                msg=vars(processo),
            )

    def test_pid_fuori_intervallo_non_disponibile(self):
        api = _ApiFinta({})
        gruppi, _ = _misura([(-3, "a.exe"), (2 ** 32, "b.exe")], api)

        self.assertEqual([gruppo.metriche for gruppo in gruppi], [NON_DISPONIBILE, NON_DISPONIBILE])
        self.assertEqual(api.aperture, [])

    def test_misto_leggibile_e_protetto(self):
        api = _ApiFinta({
            1: _Processo(memoria=100),
            2: _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
        })
        gruppi, _ = _misura([(1, "svchost.exe"), (2, "svchost.exe")], api)

        # Mai somme parziali: niente 100 byte "di tutto svchost".
        self.assertEqual(
            gruppi,
            [GruppoProcessi("svchost.exe", 2, PROCESSO_PROTETTO, None, None)],
        )

    def test_misto_leggibile_e_non_disponibile(self):
        api = _ApiFinta({
            1: _Processo(memoria=100),
            2: _Processo(memoria=None),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("x.exe", 2, NON_DISPONIBILE, None, None)])

    def test_precedenza_non_disponibile_su_protetto(self):
        api = _ApiFinta({
            1: _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
            2: _Processo(apertura=(ERROR_GENERICO, None)),
            3: _Processo(memoria=1),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe"), (3, "x.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("x.exe", 3, NON_DISPONIBILE, None, None)])

    def test_protetto_piu_uscito_resta_protetto(self):
        api = _ApiFinta({
            1: _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
            2: _Processo(codice=0),
        })
        gruppi, _ = _misura([(1, "x.exe"), (2, "x.exe")], api)

        self.assertEqual(gruppi, [GruppoProcessi("x.exe", 1, PROCESSO_PROTETTO, None, None)])


class TestFinestraEProcessoriNonValidi(unittest.TestCase):

    def test_finestra_nulla(self):
        api = _ApiFinta({1: _Processo(memoria=8)})
        gruppi, _ = _misura([(1, "a.exe")], api, orologio=_Orologio((5.0, 5.0, 5.0, 5.0)))

        self.assertEqual(gruppi, [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)])

    def test_finestra_negativa_o_nan(self):
        for valori in ((1.0, 1.0, 0.0, 0.0), (0.0, 0.0, float("nan"), float("nan"))):
            api = _ApiFinta({1: _Processo(memoria=8)})
            gruppi, _ = _misura([(1, "a.exe")], api, orologio=_Orologio(valori))
            self.assertEqual(gruppi[0].metriche, NON_DISPONIBILE, msg=valori)

    def test_finestra_non_valida_non_tocca_i_protetti(self):
        api = _ApiFinta({
            1: _Processo(),
            2: _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
        })
        gruppi, _ = _misura([(1, "a.exe"), (2, "b.exe")], api, orologio=_Orologio((0.0,) * 4))

        self.assertEqual(
            {gruppo.nome: gruppo.metriche for gruppo in gruppi},
            {"a.exe": NON_DISPONIBILE, "b.exe": PROCESSO_PROTETTO},
        )

    def test_processori_logici_non_validi(self):
        for processori in (None, 0, -2, True, 8.0):
            api = _ApiFinta({1: _Processo(memoria=8)})
            gruppi, attesa = _misura([(1, "a.exe")], api, processori=processori)

            self.assertEqual(
                gruppi,
                [GruppoProcessi("a.exe", 1, NON_DISPONIBILE, None, None)],
                msg=repr(processori),
            )
            # Nessuna misura possibile: niente handle, niente attesa.
            self.assertEqual(api.aperture, [], msg=repr(processori))
            self.assertEqual(attesa.chiamate, [], msg=repr(processori))


class TestBackendNonDisponibile(unittest.TestCase):

    def test_api_assente_tutti_not_available(self):
        attesa = _Attesa()
        gruppi = process_info._misura(
            [(1, "a.exe", None), (2, "a.exe", None), (3, "b.exe", None), (0, "System Idle Process", None)],
            None,
            _Orologio(),
            attesa,
            8,
        )

        self.assertEqual(
            gruppi,
            [
                GruppoProcessi("a.exe", 2, NON_DISPONIBILE, None, None),
                GruppoProcessi("b.exe", 1, NON_DISPONIBILE, None, None),
            ],
        )
        self.assertEqual(attesa.chiamate, [])

    def test_non_windows(self):
        with mock.patch.object(process_info, "_piattaforma", return_value="linux"), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=AssertionError("kernel32")), \
                mock.patch.object(process_info.time, "sleep", side_effect=AssertionError("sleep")):
            gruppi = process_info.misura_gruppi([(10, "bash", None), (11, "bash", None)], 8)

        self.assertEqual(gruppi, [GruppoProcessi("bash", 2, NON_DISPONIBILE, None, None)])

    def test_processo_32_bit(self):
        with mock.patch.object(process_info, "_piattaforma", return_value="win32"), \
                mock.patch.object(process_info, "_bit_processo", return_value=32), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=AssertionError("kernel32")):
            self.assertIsNone(process_info._crea_api())

    def test_layout_inatteso(self):
        with mock.patch.object(process_info, "_piattaforma", return_value="win32"), \
                mock.patch.object(process_info, "_bit_processo", return_value=64), \
                mock.patch.object(process_info, "_DIMENSIONE_PMC_EX2_ATTESA", 88), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=AssertionError("kernel32")):
            self.assertIsNone(process_info._crea_api())

        with mock.patch.object(process_info, "_piattaforma", return_value="win32"), \
                mock.patch.object(process_info, "_bit_processo", return_value=64), \
                mock.patch.object(process_info, "_DIMENSIONE_FILETIME_ATTESA", 16), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=AssertionError("kernel32")):
            self.assertIsNone(process_info._crea_api())

    def test_caricamento_kernel32_fallito(self):
        with mock.patch.object(process_info, "_piattaforma", return_value="win32"), \
                mock.patch.object(process_info, "_bit_processo", return_value=64), \
                mock.patch.object(process_info, "_ApiKernel32", side_effect=OSError("dll")):
            if ctypes.sizeof(ctypes.c_size_t) == 8:
                self.assertIsNone(process_info._crea_api())

    def test_errore_imprevisto_del_campionamento(self):
        for metodo in ("apri", "tempi", "codice_uscita", "memoria_privata"):
            api = _ApiFinta(
                {1: _Processo(), 2: _Processo(apertura=(ERROR_ACCESS_DENIED, None))},
                solleva_su=metodo,
            )
            gruppi, _ = _misura([(1, "a.exe"), (2, "b.exe")], api)

            self.assertEqual(
                [gruppo.metriche for gruppo in gruppi],
                [NON_DISPONIBILE, NON_DISPONIBILE],
                msg=metodo,
            )
            self.assertEqual(api.aperti, set(), msg=metodo)
            self.assertNotIn("mario", repr(gruppi))


class TestHandle(unittest.TestCase):

    def test_ogni_handle_chiuso_una_volta(self):
        processi = {
            1: _Processo(memoria=1),
            2: _Processo(codice=0),
            3: _Processo(creazione=(1, 2)),
            4: _Processo(tempi_falliti=(False, True)),
            5: _Processo(memoria=None),
            6: _Processo(apertura=(ERROR_ACCESS_DENIED, None)),
            7: _Processo(tempi_falliti=(True, False)),
            8: _Processo(codice=None),
        }
        api = _ApiFinta(processi)
        _misura([(pid, "x.exe") for pid in processi], api)

        self.assertEqual(api.aperti, set())
        self.assertEqual(len(api.chiusure), len(set(api.chiusure)))
        self.assertEqual(len(api.chiusure), api._prossimo - 1)

    def test_handle_chiusi_anche_con_eccezione(self):
        for metodo in ("tempi", "codice_uscita", "memoria_privata"):
            api = _ApiFinta({1: _Processo()}, solleva_su=metodo)
            _misura([(1, "x.exe")], api)
            self.assertEqual(api.aperti, set(), msg=metodo)

    def test_nessuno_stato_tra_chiamate(self):
        processi = {1: _Processo(tick=(0, TICK_DECIMO), memoria=5)}
        primo, _ = _misura([(1, "a.exe")], _ApiFinta(processi))
        secondo, _ = _misura([(1, "a.exe")], _ApiFinta(processi))

        self.assertEqual(primo, secondo)
        # Nessun contenitore mutabile a livello di modulo (cache, handle, campioni).
        stato_globale = [
            nome for nome, valore in vars(process_info).items()
            if not nome.startswith("__") and isinstance(valore, (dict, list, set))
        ]
        self.assertEqual(stato_globale, [])


# =====================================================================
# Strato ctypes (funzioni kernel32 sostituite da funzioni Python)
# =====================================================================

def _api_ctypes(**funzioni):
    """_ApiKernel32 senza kernel32: le funzioni ctypes sono doppi Python."""

    api = process_info._ApiKernel32.__new__(process_info._ApiKernel32)
    for nome, funzione in funzioni.items():
        setattr(api, nome, funzione)
    return api


class TestStratoCtypes(unittest.TestCase):

    def test_layout_strutture(self):
        self.assertEqual(ctypes.sizeof(process_info._FILETIME), 8)
        if ctypes.sizeof(ctypes.c_size_t) == 8:
            self.assertEqual(ctypes.sizeof(process_info._PROCESS_MEMORY_COUNTERS_EX2), 96)
            self.assertEqual(
                process_info._PROCESS_MEMORY_COUNTERS_EX2.PrivateWorkingSetSize.offset,
                80,
            )

    def test_filetime_parte_alta_e_bassa(self):
        def tempi(handle, creazione, uscita, kernel, utente):
            creazione._obj.dwHighDateTime, creazione._obj.dwLowDateTime = 1, 5
            kernel._obj.dwHighDateTime, kernel._obj.dwLowDateTime = 0, 100
            utente._obj.dwHighDateTime, utente._obj.dwLowDateTime = 2, 7
            return 1

        api = _api_ctypes(_get_process_times=tempi)
        self.assertEqual(api.tempi(1), ((1 << 32) | 5, 100 + ((2 << 32) | 7)))

    def test_tempi_falliti(self):
        api = _api_ctypes(_get_process_times=lambda *args: 0)
        self.assertIsNone(api.tempi(1))

    def test_memoria_private_working_set_e_cb(self):
        chiamate = []

        def memoria(handle, contatori, dimensione):
            chiamate.append(dimensione)
            contatori._obj.WorkingSetSize = 9000
            contatori._obj.PrivateUsage = 7000
            contatori._obj.PrivateWorkingSetSize = 4096
            return 1

        api = _api_ctypes(_get_process_memory_info=memoria)
        self.assertEqual(api.memoria_privata(1), 4096)
        self.assertEqual(chiamate, [ctypes.sizeof(process_info._PROCESS_MEMORY_COUNTERS_EX2)])

    def test_memoria_campo_non_compilato(self):
        # Sistema che non compila PrivateWorkingSetSize: resta la sentinella.
        def memoria(handle, contatori, dimensione):
            contatori._obj.WorkingSetSize = 9000
            return 1

        api = _api_ctypes(_get_process_memory_info=memoria)
        self.assertIsNone(api.memoria_privata(1))

    def test_memoria_struttura_ex_soltanto(self):
        # Sistema senza EX2: riscrive cb con la dimensione EX (80) e non
        # tocca PrivateWorkingSetSize. Nessun fallback a PrivateUsage.
        def memoria(handle, contatori, dimensione):
            contatori._obj.cb = 80
            contatori._obj.WorkingSetSize = 9000
            contatori._obj.PrivateUsage = 7000
            return 1

        api = _api_ctypes(_get_process_memory_info=memoria)
        self.assertIsNone(api.memoria_privata(1))

    def test_memoria_cb_diverso_anche_con_campo_scritto(self):
        # Caso che la sola sentinella non vedrebbe: buffer azzerato e cb EX.
        for cb in (0, 72, 80, 88):
            def memoria(handle, contatori, dimensione, cb=cb):
                contatori._obj.cb = cb
                contatori._obj.PrivateWorkingSetSize = 0
                return 1

            api = _api_ctypes(_get_process_memory_info=memoria)
            self.assertIsNone(api.memoria_privata(1), msg=cb)

    def test_memoria_cb_compilato_ex2(self):
        # Comportamento osservato su Windows 11: cb resta (o torna) 96.
        def memoria(handle, contatori, dimensione):
            contatori._obj.cb = 96
            contatori._obj.PrivateWorkingSetSize = 0
            return 1

        api = _api_ctypes(_get_process_memory_info=memoria)
        self.assertEqual(api.memoria_privata(1), 0)

    def test_memoria_fallita(self):
        api = _api_ctypes(_get_process_memory_info=lambda *args: 0)
        self.assertIsNone(api.memoria_privata(1))

    def test_codice_uscita(self):
        def codice(handle, valore):
            valore._obj.value = STILL_ACTIVE
            return 1

        self.assertEqual(_api_ctypes(_get_exit_code_process=codice).codice_uscita(1), STILL_ACTIVE)
        self.assertIsNone(_api_ctypes(_get_exit_code_process=lambda *a: 0).codice_uscita(1))

    def test_apertura_solo_query_limited_information(self):
        richieste = []

        def apri(accesso, eredita, pid):
            richieste.append((accesso, eredita, pid))
            return 1234

        api = _api_ctypes(_open_process=apri)
        self.assertEqual(api.apri(42), (1234, 0))
        self.assertEqual(richieste, [(0x1000, 0, 42)])

    def test_sorgente_senza_api_vietate(self):
        sorgente = (BASE_DIR / "modules" / "process_info.py").read_text(encoding="utf-8")
        for vietato in (
            "PROCESS_VM_READ =",
            "0x0010",
            "NtQuery",
            "ntdll",
            "psapi",
            "import subprocess",
            "import psutil",
            "os.system",
            "eval(",
            "exec(",
        ):
            self.assertNotIn(vietato, sorgente, msg=vietato)


class TestImport(unittest.TestCase):

    def test_import_non_carica_kernel32(self):
        def vietato(*args, **kwargs):
            raise AssertionError("nessun accesso a kernel32 all'import")

        spec = importlib.util.spec_from_file_location(
            "process_info_import_isolato",
            BASE_DIR / "modules" / "process_info.py",
        )
        modulo = importlib.util.module_from_spec(spec)
        with mock.patch.object(ctypes, "WinDLL", vietato, create=True):
            spec.loader.exec_module(modulo)

        self.assertTrue(hasattr(modulo, "misura_gruppi"))


# =====================================================================
# Smoke reale (solo Windows a 64 bit): processo corrente, nessun valore
# hardware hardcodato. Attende una volta ~0.1 s.
# =====================================================================

class _PMC_EX2_SONDA(ctypes.Structure):
    # Copia locale e volutamente indipendente di PROCESS_MEMORY_COUNTERS_EX2
    # (psapi.h): la sonda non usa nulla di process_info.
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
        ("PrivateWorkingSetSize", ctypes.c_size_t),
        ("SharedCommitUsage", ctypes.c_uint64),
    ]


_DIMENSIONE_PMC_BASE_SONDA = 72  # sizeof(PROCESS_MEMORY_COUNTERS) a 64 bit
_SENTINELLA_SONDA = (1 << 64) - 1


def _sonda_ex2_grezza():
    """
    Capacità EX2 reale per il processo corrente: (disponibile, PrivateWorkingSetSize).

    Chiamate kernel32 dirette, struttura e sentinella locali al test, stessi
    criteri documentati del backend (cb restituito 96 e campo compilato).
    Se la chiamata EX2 fallisce si riprova con la struttura base: "non
    supportata" solo se la base funziona. Ogni altro errore sul processo
    corrente fa fallire il test, mai "EX2 non disponibile".
    """

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True, winmode=0x00000800)
    apri = kernel32.OpenProcess
    apri.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    apri.restype = ctypes.c_void_p
    chiudi = kernel32.CloseHandle
    chiudi.argtypes = [ctypes.c_void_p]
    chiudi.restype = ctypes.c_int
    leggi_memoria = kernel32.K32GetProcessMemoryInfo
    leggi_memoria.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
    leggi_memoria.restype = ctypes.c_int

    if ctypes.sizeof(_PMC_EX2_SONDA) != 96:
        raise AssertionError("sonda EX2: layout locale inatteso")

    ctypes.set_last_error(0)
    handle = apri(0x1000, 0, os.getpid())
    if not handle:
        raise AssertionError(f"sonda EX2: OpenProcess fallita sul processo corrente ({ctypes.get_last_error()})")
    try:
        contatori = _PMC_EX2_SONDA()
        contatori.cb = ctypes.sizeof(contatori)
        contatori.PrivateWorkingSetSize = _SENTINELLA_SONDA
        ctypes.set_last_error(0)
        if not leggi_memoria(handle, ctypes.byref(contatori), ctypes.sizeof(contatori)):
            errore = ctypes.get_last_error()
            base = _PMC_EX2_SONDA()
            base.cb = _DIMENSIONE_PMC_BASE_SONDA
            if not leggi_memoria(handle, ctypes.byref(base), _DIMENSIONE_PMC_BASE_SONDA):
                raise AssertionError(f"sonda EX2: K32GetProcessMemoryInfo fallita anche in forma base ({errore})")
            return False, None
        disponibile = (
            contatori.cb == ctypes.sizeof(contatori)
            and contatori.PrivateWorkingSetSize != _SENTINELLA_SONDA
        )
        return disponibile, (contatori.PrivateWorkingSetSize if disponibile else None)
    finally:
        chiudi(handle)


def _verifica_smoke_ex2(caso, gruppo, nome, ex2_disponibile, memoria_sonda):
    """Asserzioni dello smoke reale, separate per poterne provare la severità."""

    caso.assertEqual(gruppo.nome, nome)
    caso.assertEqual(gruppo.istanze, 1)
    if ex2_disponibile:
        caso.assertEqual(gruppo.metriche, LEGGIBILE)
        caso.assertIs(type(gruppo.cpu_percent), int)
        caso.assertTrue(0 <= gruppo.cpu_percent <= 100)
        caso.assertIs(type(gruppo.memory_bytes), int)
        caso.assertGreater(gruppo.memory_bytes, 0)
        # Controllo incrociato indipendente: stesso ordine di grandezza
        # del PrivateWorkingSetSize letto dalla sonda grezza.
        caso.assertLess(gruppo.memory_bytes, memoria_sonda * 2)
        caso.assertGreater(gruppo.memory_bytes, memoria_sonda // 2)
    else:
        caso.assertEqual(gruppo, GruppoProcessi(nome, 1, NON_DISPONIBILE, None, None))


class TestVerificaSmokeEx2(unittest.TestCase):
    """La verifica dello smoke non accetta esiti incoerenti con la capacità reale."""

    NOME = "aster_test"

    def test_ex2_disponibile_ma_backend_non_disponibile_fallisce(self):
        # Caso del reviewer: memoria_privata() sempre None su un sistema con EX2.
        gruppo = GruppoProcessi(self.NOME, 1, NON_DISPONIBILE, None, None)
        with self.assertRaises(AssertionError):
            _verifica_smoke_ex2(self, gruppo, self.NOME, True, 10_000_000)

    def test_ex2_assente_ma_backend_misurabile_fallisce(self):
        gruppo = GruppoProcessi(self.NOME, 1, LEGGIBILE, 0, 10_000_000)
        with self.assertRaises(AssertionError):
            _verifica_smoke_ex2(self, gruppo, self.NOME, False, None)

    def test_memoria_di_un_altro_campo_fallisce(self):
        # Es. WorkingSetSize al posto del PrivateWorkingSetSize (circa 3 volte).
        gruppo = GruppoProcessi(self.NOME, 1, LEGGIBILE, 0, 30_000_000)
        with self.assertRaises(AssertionError):
            _verifica_smoke_ex2(self, gruppo, self.NOME, True, 10_000_000)

    def test_esiti_coerenti_accettati(self):
        _verifica_smoke_ex2(self, GruppoProcessi(self.NOME, 1, LEGGIBILE, 3, 11_000_000), self.NOME, True, 10_000_000)
        _verifica_smoke_ex2(self, GruppoProcessi(self.NOME, 1, NON_DISPONIBILE, None, None), self.NOME, False, None)


@unittest.skipUnless(WINDOWS_64, "backend Win32 disponibile solo su Windows a 64 bit")
class TestSmokeReale(unittest.TestCase):

    NOME = "aster_test_processo_corrente"

    def _misura_processo_corrente(self):
        creazione = process_info.crea_lettore_creazione()(os.getpid())
        gruppi = process_info.misura_gruppi(
            [(os.getpid(), self.NOME, creazione)], os.cpu_count() or 1
        )
        self.assertEqual(len(gruppi), 1)
        json.dumps(gruppi[0]._asdict())
        return gruppi[0]

    def test_identita_del_processo_corrente(self):
        creazione = process_info.crea_lettore_creazione()(os.getpid())

        self.assertIs(type(creazione), int)
        self.assertGreater(creazione, 0)

    def test_processo_corrente(self):
        # Windows a 64 bit non garantisce EX2 (Win10/11 22H2 + CU 09/2023):
        # l'esito atteso viene dalla sonda grezza, indipendente dal backend.
        ex2_disponibile, memoria_sonda = _sonda_ex2_grezza()
        gruppo = self._misura_processo_corrente()

        _verifica_smoke_ex2(self, gruppo, self.NOME, ex2_disponibile, memoria_sonda)

    def test_smoke_rileva_memoria_privata_sempre_none(self):
        ex2_disponibile, memoria_sonda = _sonda_ex2_grezza()
        if not ex2_disponibile:
            self.skipTest("EX2 non disponibile su questo sistema: mutazione non applicabile")

        with mock.patch.object(process_info._ApiKernel32, "memoria_privata", lambda self, handle: None):
            gruppo = self._misura_processo_corrente()

        with self.assertRaises(AssertionError):
            _verifica_smoke_ex2(self, gruppo, self.NOME, ex2_disponibile, memoria_sonda)


if __name__ == "__main__":
    unittest.main()
