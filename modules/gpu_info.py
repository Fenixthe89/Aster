"""Enumerazione in sola lettura degli adattatori grafici (usata da get_system_info)."""

import ctypes
import struct
import sys
from typing import NamedTuple

# Unico backend 0.7.2b: DXGI su Windows a 64 bit, con sola stdlib ctypes.
# Nessun subprocess, PowerShell, WMI, device D3D o DLL del produttore:
# l'enumerazione DXGI non carica il driver user-mode della GPU.
# Il modulo si importa su ogni sistema: dxgi.dll e i prototipi
# WINFUNCTYPE (esistenti solo su Windows) vengono toccati soltanto
# dentro leggi_adattatori_grafici, dopo i controlli di piattaforma.

# Sanity cap: oltre questo numero di adapter l'enumerazione è anomala.
MAX_GPU_ADAPTERS = 16

# Cerca dxgi.dll solo in System32: nessun path da config, cwd o PATH.
_LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x00000800

_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2

# Indici vtable COM (ABI immutabile): IUnknown::Release,
# IDXGIFactory1::EnumAdapters1, IDXGIAdapter1::GetDesc1.
_INDICE_RELEASE = 2
_INDICE_ENUM_ADAPTERS1 = 12
_INDICE_GET_DESC1 = 10

# sizeof(DXGI_ADAPTER_DESC1) su Windows a 64 bit: se il layout ctypes
# non coincide, nessuna chiamata COM (un layout errato è un crash, non
# un'eccezione intercettabile).
_DIMENSIONE_DESC1_ATTESA = 312


class GpuNonDisponibile(Exception):
    """Enumerazione GPU non supportata o non riuscita (tutto o niente)."""


class AdattatoreGrafico(NamedTuple):
    """Adapter hardware con i soli dati grezzi usati da system_tools."""

    name: str
    dedicated_memory_bytes: int


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


# {770aae78-f26f-4dba-a829-253c83d1b387}
_IID_IDXGIFACTORY1 = _GUID(
    0x770AAE78,
    0xF26F,
    0x4DBA,
    (ctypes.c_uint8 * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87),
)


class _LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.c_uint32),
        ("HighPart", ctypes.c_int32),
    ]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    # Layout completo richiesto dall'ABI; di questi campi si leggono solo
    # Description, DedicatedVideoMemory (SIZE_T) e Flags.
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint32),
        ("DeviceId", ctypes.c_uint32),
        ("SubSysId", ctypes.c_uint32),
        ("Revision", ctypes.c_uint32),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", ctypes.c_uint32),
    ]


def _fallito(hr: int) -> bool:
    """True se l'HRESULT (già a 32 bit senza segno) indica un errore."""

    return bool(hr & 0x80000000)


def _metodo_com(oggetto: int, indice: int, prototipo):
    """Funzione della vtable COM di oggetto all'indice dato."""

    vtable = ctypes.cast(
        oggetto,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    )[0]
    return prototipo(vtable[indice])


class _ApiDxgi:
    """Strato ctypes minimo: chiamate COM grezze, nessuna decisione."""

    def __init__(self, crea_factory):
        prototipo = ctypes.WINFUNCTYPE
        self._crea_factory = crea_factory
        self._release = prototipo(ctypes.c_ulong, ctypes.c_void_p)
        self._enum_adapters1 = prototipo(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_desc1 = prototipo(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_DXGI_ADAPTER_DESC1),
        )

    def crea_factory(self) -> tuple[int, int | None]:
        factory = ctypes.c_void_p()
        hr = self._crea_factory(
            ctypes.byref(_IID_IDXGIFACTORY1),
            ctypes.byref(factory),
        )
        return hr & 0xFFFFFFFF, factory.value

    def enum_adapters1(self, factory: int, indice: int) -> tuple[int, int | None]:
        adapter = ctypes.c_void_p()
        metodo = _metodo_com(factory, _INDICE_ENUM_ADAPTERS1, self._enum_adapters1)
        hr = metodo(factory, indice, ctypes.byref(adapter))
        return hr & 0xFFFFFFFF, adapter.value

    def get_desc1(self, adapter: int) -> tuple[int, str, int, int]:
        descrizione = _DXGI_ADAPTER_DESC1()
        metodo = _metodo_com(adapter, _INDICE_GET_DESC1, self._get_desc1)
        hr = metodo(adapter, ctypes.byref(descrizione))
        return (
            hr & 0xFFFFFFFF,
            descrizione.Description,
            descrizione.DedicatedVideoMemory,
            descrizione.Flags,
        )

    def release(self, oggetto: int) -> None:
        _metodo_com(oggetto, _INDICE_RELEASE, self._release)(oggetto)


def _carica_crea_factory():
    """CreateDXGIFactory1 da dxgi.dll di System32."""

    dxgi = ctypes.WinDLL("dxgi.dll", winmode=_LOAD_LIBRARY_SEARCH_SYSTEM32)
    funzione = dxgi.CreateDXGIFactory1
    # c_long e non ctypes.HRESULT: niente OSError automatico, i codici
    # (incluso DXGI_ERROR_NOT_FOUND) si controllano esplicitamente.
    funzione.restype = ctypes.c_long
    funzione.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    return funzione


def _enumera(api) -> list[AdattatoreGrafico]:
    """
    Enumera gli adapter DXGI: tutto o niente.

    Ordine DXGI preservato, nessuna deduplicazione, adapter software
    esclusi solo tramite DXGI_ADAPTER_FLAG_SOFTWARE (mai per nome).
    Qualsiasi errore, o più di MAX_GPU_ADAPTERS adapter senza
    DXGI_ERROR_NOT_FOUND, solleva GpuNonDisponibile: mai una lista
    parziale. Ogni adapter ottenuto e la factory vengono sempre rilasciati.
    """

    hr, factory = api.crea_factory()
    if _fallito(hr) or not factory:
        if factory:
            api.release(factory)
        raise GpuNonDisponibile("Factory DXGI non disponibile.")

    try:
        adattatori = []
        indice = 0

        while True:
            hr, adapter = api.enum_adapters1(factory, indice)

            if hr == _DXGI_ERROR_NOT_FOUND and not adapter:
                return adattatori

            try:
                if _fallito(hr) or not adapter:
                    raise GpuNonDisponibile("Enumerazione DXGI non riuscita.")

                if indice >= MAX_GPU_ADAPTERS:
                    raise GpuNonDisponibile("Enumerazione DXGI anomala.")

                hr, nome, memoria_dedicata, flags = api.get_desc1(adapter)
                if _fallito(hr):
                    raise GpuNonDisponibile("Descrizione DXGI non disponibile.")
            finally:
                if adapter:
                    api.release(adapter)

            if not flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
                adattatori.append(AdattatoreGrafico(nome, memoria_dedicata))

            indice += 1
    finally:
        api.release(factory)


def _piattaforma() -> str:
    return sys.platform


def _bit_processo() -> int:
    return struct.calcsize("P") * 8


def leggi_adattatori_grafici() -> list[AdattatoreGrafico]:
    """
    Adapter grafici hardware con nome e DedicatedVideoMemory grezzi.

    Lista vuota: enumerazione riuscita ma nessun adapter hardware.
    GpuNonDisponibile: sistema non supportato (non Windows, processo a
    32 bit, layout ctypes inatteso) o enumerazione non riuscita.
    I valori non sono normalizzati né convertiti: DedicatedVideoMemory
    è riportata così come la fornisce il sistema.
    """

    if _piattaforma() != "win32":
        raise GpuNonDisponibile("Backend GPU non disponibile su questo sistema.")

    # A 32 bit SIZE_T non rappresenta più di 4 GiB: niente valori saturati.
    if _bit_processo() != 64:
        raise GpuNonDisponibile("Backend GPU disponibile solo a 64 bit.")

    if ctypes.sizeof(_DXGI_ADAPTER_DESC1) != _DIMENSIONE_DESC1_ATTESA:
        raise GpuNonDisponibile("Layout DXGI_ADAPTER_DESC1 inatteso.")

    return _enumera(_ApiDxgi(_carica_crea_factory()))
