"""
0.7.2b - modules/gpu_info.py: enumerazione adapter grafici via DXGI.

Copre precondizioni (piattaforma, 64 bit, layout struct), caricamento
sicuro di dxgi.dll, l'algoritmo di enumerazione tutto-o-niente con una
API finta in puro Python (filtro software, ordine, cap, errori e
Release su ogni percorso) e, solo su Windows a 64 bit, lo strato ctypes
con oggetti COM finti (vtable costruite con callback ctypes) più un
unico smoke reale senza valori hardware hardcodati.

Nessuna dipendenza esterna, nessun subprocess, nessun file in data/.

Esecuzione:
    python -m unittest discover -s tests -v
"""

import ctypes
import importlib.util
import json
import struct
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import modules.gpu_info as gpu_info
from modules.gpu_info import AdattatoreGrafico, GpuNonDisponibile

WINDOWS_64 = sys.platform == "win32" and struct.calcsize("P") == 8

S_OK = 0
E_FAIL = 0x80004005
NOT_FOUND = gpu_info._DXGI_ERROR_NOT_FOUND
SOFTWARE = gpu_info._DXGI_ADAPTER_FLAG_SOFTWARE


# =====================================================================
# API DXGI finta in puro Python (nessun ctypes): registra ogni chiamata
# =====================================================================

class _ApiFinta:
    """
    Doppio di _ApiDxgi. adapter: lista di (nome, memoria, flags).
    Gli handle sono interi fittizi: factory 1, adapter 100 + indice.
    """

    FACTORY = 1

    def __init__(
        self,
        adapter=(),
        hr_factory=S_OK,
        factory=FACTORY,
        errori_enum=None,
        errori_desc=None,
        infinito=False,
    ):
        self._adapter = list(adapter)
        self._hr_factory = hr_factory
        self._factory = factory
        # indice -> (hr, handle restituito)
        self._errori_enum = errori_enum or {}
        # indice -> hr di GetDesc1
        self._errori_desc = errori_desc or {}
        self._infinito = infinito
        self.enum_chiamati = []
        self.rilasciati = []

    def crea_factory(self):
        return self._hr_factory, self._factory

    def enum_adapters1(self, factory, indice):
        assert factory == self._factory
        self.enum_chiamati.append(indice)
        if indice in self._errori_enum:
            return self._errori_enum[indice]
        if self._infinito or indice < len(self._adapter):
            return S_OK, 100 + indice
        return NOT_FOUND, None

    def get_desc1(self, adapter):
        indice = adapter - 100
        hr = self._errori_desc.get(indice, S_OK)
        if self._infinito:
            nome, memoria, flags = (f"GPU {indice}", 1024, 0)
        else:
            nome, memoria, flags = self._adapter[indice]
        return hr, nome, memoria, flags

    def release(self, oggetto):
        self.rilasciati.append(oggetto)


def _hw(nome, memoria):
    return (nome, memoria, 0)


def _sw(nome="Software Adapter", memoria=0):
    return (nome, memoria, SOFTWARE)


class TestEnumerazione(unittest.TestCase):

    def _enumera(self, api):
        return gpu_info._enumera(api)

    def test_una_gpu(self):
        api = _ApiFinta([_hw("Example GPU", 8_589_934_592)])
        self.assertEqual(
            self._enumera(api),
            [AdattatoreGrafico("Example GPU", 8_589_934_592)],
        )

    def test_piu_gpu_ordine_dxgi_preservato(self):
        api = _ApiFinta([
            _hw("Zeta Integrated", 134_217_728),
            _hw("Alpha Discrete", 17_179_869_184),
        ])
        risultato = self._enumera(api)
        self.assertEqual(
            [adattatore.name for adattatore in risultato],
            ["Zeta Integrated", "Alpha Discrete"],
        )

    def test_adapter_identici_entrambi_mantenuti(self):
        api = _ApiFinta([_hw("Same GPU", 1), _hw("Same GPU", 1)])
        self.assertEqual(len(self._enumera(api)), 2)

    def test_software_filtrato_solo_tramite_flag(self):
        api = _ApiFinta([
            _hw("Example GPU", 8),
            _sw("Microsoft Basic Render Driver"),
        ])
        self.assertEqual(
            self._enumera(api),
            [AdattatoreGrafico("Example GPU", 8)],
        )

    def test_nomi_microsoft_hardware_non_filtrati(self):
        nomi = [
            "Microsoft Basic Display Adapter",
            "Microsoft Remote Display Adapter",
            "Microsoft Hyper-V Video",
        ]
        api = _ApiFinta([_hw(nome, 0) for nome in nomi])
        self.assertEqual(
            [adattatore.name for adattatore in self._enumera(api)],
            nomi,
        )

    def test_nome_qualsiasi_con_flag_software_filtrato(self):
        api = _ApiFinta([_sw("NVIDIA GeForce Example", 8_589_934_592)])
        self.assertEqual(self._enumera(api), [])

    def test_solo_software_lista_vuota(self):
        api = _ApiFinta([_sw()])
        self.assertEqual(self._enumera(api), [])
        self.assertEqual(api.rilasciati, [100, _ApiFinta.FACTORY])

    def test_nessun_adapter_lista_vuota(self):
        api = _ApiFinta([])
        self.assertEqual(self._enumera(api), [])
        self.assertEqual(api.enum_chiamati, [0])
        self.assertEqual(api.rilasciati, [_ApiFinta.FACTORY])

    def test_fine_lista_su_not_found(self):
        api = _ApiFinta([_hw("A", 1), _hw("B", 2)])
        self._enumera(api)
        self.assertEqual(api.enum_chiamati, [0, 1, 2])

    def test_valori_grezzi_non_normalizzati(self):
        api = _ApiFinta([_hw("  Example GPU  ", 8_413_773_824)])
        self.assertEqual(
            self._enumera(api),
            [AdattatoreGrafico("  Example GPU  ", 8_413_773_824)],
        )

    def test_release_di_ogni_adapter_e_della_factory(self):
        api = _ApiFinta([_hw("A", 1), _sw(), _hw("B", 2)])
        self._enumera(api)
        self.assertEqual(api.rilasciati, [100, 101, 102, _ApiFinta.FACTORY])

    def test_output_espone_solo_nome_e_memoria(self):
        api = _ApiFinta([_hw("Example GPU", 8)])
        adattatore = self._enumera(api)[0]
        self.assertEqual(
            adattatore._fields,
            ("name", "dedicated_memory_bytes"),
        )


class TestEnumerazioneCap(unittest.TestCase):

    def test_esattamente_sedici_adapter_consentiti(self):
        api = _ApiFinta([_hw(f"GPU {i}", i) for i in range(16)])
        risultato = gpu_info._enumera(api)
        self.assertEqual(len(risultato), 16)
        self.assertEqual(api.enum_chiamati[-1], 16)

    def test_diciassettesimo_adapter_anomalia(self):
        api = _ApiFinta([_hw(f"GPU {i}", i) for i in range(17)])
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)

    def test_cap_senza_not_found_nessuna_lista_parziale(self):
        api = _ApiFinta(infinito=True)
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.enum_chiamati, list(range(17)))

    def test_cap_rilascia_anche_l_adapter_in_eccesso(self):
        api = _ApiFinta(infinito=True)
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(
            api.rilasciati,
            [100 + i for i in range(17)] + [_ApiFinta.FACTORY],
        )

    def test_valore_del_cap(self):
        self.assertEqual(gpu_info.MAX_GPU_ADAPTERS, 16)


class TestEnumerazioneErrori(unittest.TestCase):

    def test_factory_fallita_nessuna_release(self):
        api = _ApiFinta([_hw("A", 1)], hr_factory=E_FAIL, factory=None)
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [])
        self.assertEqual(api.enum_chiamati, [])

    def test_factory_fallita_con_puntatore_rilasciata(self):
        api = _ApiFinta([_hw("A", 1)], hr_factory=E_FAIL)
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [_ApiFinta.FACTORY])

    def test_factory_nulla_con_successo(self):
        api = _ApiFinta([_hw("A", 1)], factory=None)
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [])

    def test_errore_enum_nessuna_lista_parziale(self):
        api = _ApiFinta(
            [_hw("A", 1), _hw("B", 2)],
            errori_enum={1: (E_FAIL, None)},
        )
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [100, _ApiFinta.FACTORY])

    def test_errore_enum_con_adapter_non_nullo_rilasciato(self):
        api = _ApiFinta([_hw("A", 1)], errori_enum={0: (E_FAIL, 555)})
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [555, _ApiFinta.FACTORY])

    def test_enum_riuscita_ma_adapter_nullo(self):
        api = _ApiFinta([_hw("A", 1)], errori_enum={0: (S_OK, None)})
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [_ApiFinta.FACTORY])

    def test_not_found_con_adapter_non_nullo_anomalo(self):
        api = _ApiFinta([_hw("A", 1)], errori_enum={0: (NOT_FOUND, 777)})
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [777, _ApiFinta.FACTORY])

    def test_errore_get_desc1(self):
        api = _ApiFinta(
            [_hw("A", 1), _hw("B", 2), _hw("C", 3)],
            errori_desc={1: E_FAIL},
        )
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        # L'adapter in errore è rilasciato, i successivi mai ottenuti.
        self.assertEqual(api.rilasciati, [100, 101, _ApiFinta.FACTORY])
        self.assertEqual(api.enum_chiamati, [0, 1])

    def test_eccezione_imprevista_rilascia_tutto(self):
        api = _ApiFinta([_hw("A", 1), _hw("B", 2)])

        def get_desc1_esplode(adapter):
            raise RuntimeError(r"C:\Users\Secret\dxgi")

        api.get_desc1 = get_desc1_esplode
        with self.assertRaises(RuntimeError):
            gpu_info._enumera(api)
        self.assertEqual(api.rilasciati, [100, _ApiFinta.FACTORY])

    def test_ogni_oggetto_rilasciato_una_sola_volta(self):
        api = _ApiFinta(
            [_hw("A", 1), _sw(), _hw("B", 2)],
            errori_desc={2: E_FAIL},
        )
        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(api)
        self.assertEqual(len(api.rilasciati), len(set(api.rilasciati)))


# =====================================================================
# Precondizioni di leggi_adattatori_grafici e caricamento della DLL
# =====================================================================

def _caricatore_vietato():
    raise AssertionError("dxgi.dll non deve essere caricata")


class TestPrecondizioni(unittest.TestCase):

    def _leggi(self, piattaforma="win32", bit=64, dimensione=None, api=None):
        """
        Esegue leggi_adattatori_grafici con piattaforma/bit/layout simulati.
        Senza api, caricare dxgi.dll è un errore del test: le precondizioni
        devono fermarsi prima. dimensione=None simula il layout atteso.
        """

        if dimensione is None:
            dimensione = ctypes.sizeof(gpu_info._DXGI_ADAPTER_DESC1)

        with (
            mock.patch.object(gpu_info, "_piattaforma", return_value=piattaforma),
            mock.patch.object(gpu_info, "_bit_processo", return_value=bit),
            mock.patch.object(gpu_info, "_DIMENSIONE_DESC1_ATTESA", dimensione),
            mock.patch.object(
                gpu_info,
                "_carica_crea_factory",
                _caricatore_vietato if api is None else (lambda: None),
            ),
            mock.patch.object(gpu_info, "_ApiDxgi", lambda crea: api),
        ):
            return gpu_info.leggi_adattatori_grafici()

    def test_linux_non_disponibile(self):
        with self.assertRaises(GpuNonDisponibile):
            self._leggi(piattaforma="linux")

    def test_macos_non_disponibile(self):
        with self.assertRaises(GpuNonDisponibile):
            self._leggi(piattaforma="darwin")

    def test_windows_32_bit_non_disponibile(self):
        with self.assertRaises(GpuNonDisponibile):
            self._leggi(bit=32)

    def test_dimensione_struct_inattesa_non_disponibile(self):
        with self.assertRaises(GpuNonDisponibile):
            self._leggi(dimensione=ctypes.sizeof(gpu_info._DXGI_ADAPTER_DESC1) + 8)

    def test_precondizioni_ok_usa_l_enumerazione(self):
        api = _ApiFinta([_hw("Example GPU", 8), _sw()])
        self.assertEqual(
            self._leggi(api=api),
            [AdattatoreGrafico("Example GPU", 8)],
        )

    @unittest.skipUnless(WINDOWS_64, "layout verificabile solo su Windows a 64 bit")
    def test_sizeof_desc1_reale_64_bit(self):
        self.assertEqual(ctypes.sizeof(gpu_info._DXGI_ADAPTER_DESC1), 312)
        self.assertEqual(gpu_info._DIMENSIONE_DESC1_ATTESA, 312)

    def test_bit_processo_coerente(self):
        self.assertEqual(gpu_info._bit_processo(), struct.calcsize("P") * 8)


class TestCaricamentoDll(unittest.TestCase):

    def test_dxgi_solo_da_system32(self):
        chiamate = []
        funzione = SimpleNamespace()

        def windll_finto(nome, **kwargs):
            chiamate.append((nome, kwargs))
            return SimpleNamespace(CreateDXGIFactory1=funzione)

        with mock.patch.object(ctypes, "WinDLL", windll_finto, create=True):
            risultato = gpu_info._carica_crea_factory()

        self.assertIs(risultato, funzione)
        self.assertEqual(chiamate, [("dxgi.dll", {"winmode": 0x00000800})])
        self.assertIs(funzione.restype, ctypes.c_long)
        self.assertEqual(len(funzione.argtypes), 2)


class TestImportMultipiattaforma(unittest.TestCase):

    def test_import_non_tocca_dll_ne_winfunctype(self):
        def vietato(*args, **kwargs):
            raise AssertionError("nessun accesso Windows-only all'import")

        spec = importlib.util.spec_from_file_location(
            "gpu_info_import_isolato",
            BASE_DIR / "modules" / "gpu_info.py",
        )
        modulo = importlib.util.module_from_spec(spec)
        with (
            mock.patch.object(ctypes, "WinDLL", vietato, create=True),
            mock.patch.object(ctypes, "WINFUNCTYPE", vietato, create=True),
        ):
            spec.loader.exec_module(modulo)

        self.assertTrue(callable(modulo.leggi_adattatori_grafici))

    def test_sorgente_senza_primitive_vietate(self):
        sorgente = (BASE_DIR / "modules" / "gpu_info.py").read_text(encoding="utf-8")
        # Il docstring del modulo nomina ciò che NON usa: si cercano usi reali.
        for vietato in (
            "import subprocess",
            "subprocess.",
            "os.system",
            "os.popen",
            "import wmi",
            "win32com",
            "pynvml",
            "nvidia-smi",
            "CoInitialize",
            "CreateDevice",
            "eval(",
            "exec(",
        ):
            self.assertNotIn(vietato, sorgente, msg=vietato)


# =====================================================================
# Strato ctypes con oggetti COM finti (solo Windows a 64 bit): verifica
# indici vtable, IID, layout della struct e Release reali via ctypes.
# =====================================================================

@unittest.skipUnless(WINDOWS_64, "oggetti COM finti richiedono WINFUNCTYPE a 64 bit")
class TestStratoCtypesComFinto(unittest.TestCase):

    NUMERO_SLOT = 16

    # Indici dall'ABI documentata (dxgi.h), volutamente letterali e non
    # presi dal modulo: un indice sbagliato in gpu_info finisce su una
    # trappola invece di essere replicato anche nel doppio.
    ABI_RELEASE = 2
    ABI_GET_DESC1 = 10
    ABI_ENUM_ADAPTERS1 = 12

    def setUp(self):
        self.proto_release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        self.proto_enum = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self.proto_desc = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(gpu_info._DXGI_ADAPTER_DESC1),
        )
        self.proto_trappola = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        self.proto_crea = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.POINTER(gpu_info._GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._vivi = []
        self.release = {}
        self.trappole = []
        self.iid_ricevuti = []

    def _oggetto_com(self, metodi):
        """Oggetto COM finto: il primo campo punta a una vtable di callback."""

        vtable = (ctypes.c_void_p * self.NUMERO_SLOT)()
        for indice in range(self.NUMERO_SLOT):
            def trappola(this, indice=indice):
                self.trappole.append(indice)
                return E_FAIL - (1 << 32)
            callback = self.proto_trappola(trappola)
            self._vivi.append(callback)
            vtable[indice] = ctypes.cast(callback, ctypes.c_void_p).value

        def release(this):
            self.release[this] = self.release.get(this, 0) + 1
            return 0

        metodi = {self.ABI_RELEASE: self.proto_release(release), **metodi}
        for indice, callback in metodi.items():
            self._vivi.append(callback)
            vtable[indice] = ctypes.cast(callback, ctypes.c_void_p).value

        oggetto = ctypes.c_void_p(ctypes.addressof(vtable))
        self._vivi.extend([vtable, oggetto])
        return ctypes.addressof(oggetto)

    def _adapter(self, nome, memoria, flags=0, hr=S_OK):
        def get_desc1(this, descrizione):
            if hr == S_OK:
                descrizione[0].Description = nome
                descrizione[0].DedicatedVideoMemory = memoria
                descrizione[0].DedicatedSystemMemory = 111
                descrizione[0].SharedSystemMemory = 222
                descrizione[0].VendorId = 0x1234
                descrizione[0].Flags = flags
                return 0
            return hr - (1 << 32)

        return self._oggetto_com(
            {self.ABI_GET_DESC1: self.proto_desc(get_desc1)}
        )

    def _factory(self, adapter):
        def enum_adapters1(this, indice, uscita):
            if indice < len(adapter):
                uscita[0] = adapter[indice]
                return 0
            return NOT_FOUND - (1 << 32)

        return self._oggetto_com(
            {self.ABI_ENUM_ADAPTERS1: self.proto_enum(enum_adapters1)}
        )

    def _crea(self, factory):
        def crea(iid, uscita):
            self.iid_ricevuti.append(bytes(iid[0]))
            uscita[0] = factory
            return 0

        callback = self.proto_crea(crea)
        self._vivi.append(callback)
        return callback

    def test_enumerazione_via_ctypes(self):
        gpu = self._adapter("Example GPU", 8_413_773_824)
        warp = self._adapter("Example Software", 0, flags=SOFTWARE)
        factory = self._factory([gpu, warp])

        risultato = gpu_info._enumera(gpu_info._ApiDxgi(self._crea(factory)))

        self.assertEqual(
            risultato,
            [AdattatoreGrafico("Example GPU", 8_413_773_824)],
        )
        self.assertEqual(self.trappole, [])
        self.assertEqual(self.release, {gpu: 1, warp: 1, factory: 1})
        self.assertEqual(
            self.iid_ricevuti,
            [bytes(gpu_info._IID_IDXGIFACTORY1)],
        )

    def test_memoria_oltre_4_gib_non_troncata(self):
        gpu = self._adapter("Big GPU", 25_769_803_776)
        factory = self._factory([gpu])

        risultato = gpu_info._enumera(gpu_info._ApiDxgi(self._crea(factory)))

        self.assertEqual(risultato[0].dedicated_memory_bytes, 25_769_803_776)

    def test_get_desc1_fallita_via_ctypes(self):
        gpu = self._adapter("Example GPU", 1, hr=E_FAIL)
        factory = self._factory([gpu])

        with self.assertRaises(GpuNonDisponibile):
            gpu_info._enumera(gpu_info._ApiDxgi(self._crea(factory)))

        self.assertEqual(self.trappole, [])
        self.assertEqual(self.release, {gpu: 1, factory: 1})

    def test_iid_idxgifactory1(self):
        self.assertEqual(
            bytes(gpu_info._IID_IDXGIFACTORY1),
            bytes.fromhex("78ae0a776ff2ba4da829253c83d1b387"),
        )


# =====================================================================
# Unico smoke reale (Windows a 64 bit): nessun modello, VRAM o numero
# di adapter atteso, solo assenza di crash e tipi validi.
# =====================================================================

@unittest.skipUnless(WINDOWS_64, "backend DXGI disponibile solo su Windows a 64 bit")
class TestSmokeRealeDxgi(unittest.TestCase):

    def test_enumerazione_reale_tipi_validi(self):
        try:
            adattatori = gpu_info.leggi_adattatori_grafici()
        except GpuNonDisponibile:
            self.skipTest("DXGI non disponibile in questo ambiente")

        self.assertIsInstance(adattatori, list)
        self.assertLessEqual(len(adattatori), gpu_info.MAX_GPU_ADAPTERS)
        for adattatore in adattatori:
            self.assertIsInstance(adattatore, AdattatoreGrafico)
            self.assertIsInstance(adattatore.name, str)
            self.assertIs(type(adattatore.dedicated_memory_bytes), int)
            self.assertGreaterEqual(adattatore.dedicated_memory_bytes, 0)
        json.dumps([list(adattatore) for adattatore in adattatori])


if __name__ == "__main__":
    unittest.main()
