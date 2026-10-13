"""
Applicazioni installate per la macchina e per l'utente corrente, in sola
lettura (usata da list_installed_applications).

Fonte: le chiavi Uninstall del registro di Windows (vista nativa a 64
bit di HKLM, vista a 32 bit di HKLM, HKCU per le installazioni
per-utente). Le due viste di HKLM si ottengono dallo stesso percorso
fisico con KEY_WOW64_64KEY / KEY_WOW64_32KEY passati esplicitamente a
OpenKey, mai dal percorso letterale WOW6432Node: il risultato è così
corretto sia con Python a 64 bit sia a 32 bit su Windows a 64 bit,
senza dipendere dalla bitness del processo che esegue Aster. Nessuna
esecuzione di programmi, processi o comandi; nessun subprocess,
PowerShell o WMI; nessuna lettura di filesystem. Il modulo si importa
su ogni sistema: winreg viene usato solo dentro
leggi_applicazioni_installate, dopo il controllo di piattaforma.

L'inventario non è né può essere completo: applicazioni Microsoft Store
/ UWP e programmi portatili senza installer non passano da queste
chiavi e non compaiono mai qui. Oltre a questo limite strutturale,
sorgenti_non_disponibili segnala quando una radice non è stata
enumerata per intero (radice non apribile, errore reale durante
l'enumerazione, o limite di sicurezza raggiunto): un elenco vuoto o
filtrato a vuoto con sorgenti_non_disponibili > 0 non è un'assenza
verificata, solo un'assenza osservata su dati incompleti.
"""

import sys
import unicodedata
from typing import NamedTuple

try:
    import winreg
except ImportError:
    winreg = None

# Circuit breaker difensivo sull'enumerazione grezza di una singola
# radice: un registro anomalo non deve produrre un ciclo arbitrariamente
# lungo. Ben oltre qualunque installazione Windows reale. Il
# raggiungimento del limite rende la radice incompleta (vedi
# _enumera_radice): non viene mai presentato come scansione completa.
_MAX_SUBKEY_PER_RADICE = 4096

# WinError 259 (ERROR_NO_MORE_ITEMS): fine naturale di EnumKey quando
# l'indice supera l'ultima sottochiave. Qualunque altro OSError durante
# l'enumerazione è un errore reale, non una fine naturale.
_ERROR_NO_MORE_ITEMS = 259

_MAX_LUNGHEZZA_NOME = 200
_MAX_LUNGHEZZA_CAMPO = 200

_PERCORSO_UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

# (nome hive, percorso, vista WOW64 esplicita): le due viste pertinenti
# di HKLM più le installazioni per-utente. "64"/"32" selezionano
# KEY_WOW64_64KEY/KEY_WOW64_32KEY sullo STESSO percorso fisico (vedi
# _flag_vista); None per HKCU, dove non si applica la stessa
# redirezione. L'assenza di una radice (es. nessuna installazione
# per-utente) è normale e non viene mai contata come non disponibile;
# un errore reale nell'aprirla sì.
_RADICI_UNINSTALL = (
    ("HKEY_LOCAL_MACHINE", _PERCORSO_UNINSTALL, "64"),
    ("HKEY_LOCAL_MACHINE", _PERCORSO_UNINSTALL, "32"),
    ("HKEY_CURRENT_USER", _PERCORSO_UNINSTALL, None),
)


class PiattaformaNonSupportata(Exception):
    """Backend applicazioni installate non disponibile su questo sistema."""


class _VoceNonLeggibile(Exception):
    """
    Segnale interno (mai propagato fuori dal modulo): una sottochiave
    Uninstall esiste ma non è stata esaminabile per un errore reale
    (accesso negato, I/O), distinto da una voce normale senza
    DisplayName o marcata SystemComponent=1, e distinto da una
    sottochiave sparita tra EnumKey e l'apertura (FileNotFoundError,
    normale concorrenza, non un errore).
    """


class RisultatoApplicazioni(NamedTuple):
    """Applicazioni deduplicate e ordinate (non ancora filtrate/troncate dal tool)."""

    applicazioni: list
    sorgenti_non_disponibili: int


_CATEGORIE_UNICODE_VIETATE = frozenset({
    "Cc",  # Caratteri di controllo.
    "Cs",  # Surrogati isolati: mai un carattere valido in una stringa
           # ben formata, ma possibile se il registro restituisce dati
           # UTF-16 malformati; senza questo controllo, un successivo
           # json.dumps(..., ensure_ascii=False) può sollevare
           # UnicodeEncodeError in encode("utf-8") (verificato).
    "Cf",  # Marcatori di formattazione invisibili/bidirezionali (es.
           # LRM, RLO, ZWJ): possono alterare la visualizzazione del
           # testo senza essere visibili essi stessi.
})


def _contiene_caratteri_controllo(testo: str) -> bool:
    """True se il testo contiene caratteri di controllo (Cc), surrogati isolati (Cs) o marcatori di formattazione invisibili (Cf)."""

    return any(unicodedata.category(ch) in _CATEGORIE_UNICODE_VIETATE for ch in testo)


def _stringa_valida(valore, max_lunghezza: int) -> str | None:
    """Stringa ripulita (strip); None se non stringa, vuota, con Cc/Cs/Cf o oltre il limite."""

    if not isinstance(valore, str):
        return None

    testo = valore.strip()
    if not testo or _contiene_caratteri_controllo(testo) or len(testo) > max_lunghezza:
        return None

    return testo


def _flag_vista(vista: str | None) -> int:
    """KEY_WOW64_64KEY/32KEY per "64"/"32", nessun flag aggiuntivo altrimenti."""

    if vista == "64":
        return winreg.KEY_WOW64_64KEY
    if vista == "32":
        return winreg.KEY_WOW64_32KEY
    return 0


def _leggi_valore(chiave, nome: str):
    """
    QueryValueEx su un singolo nome; None solo se il valore non esiste
    (FileNotFoundError). Un altro OSError (es. PermissionError) non è
    un valore assente: si propaga, così _leggi_voce lo tratta come
    _VoceNonLeggibile invece di confonderlo con un campo mancante.
    """

    try:
        valore, _tipo = winreg.QueryValueEx(chiave, nome)
    except FileNotFoundError:
        return None
    return valore


def _componente_di_sistema(chiave) -> bool:
    """True solo se SystemComponent è esattamente l'intero 1 (bool escluso)."""

    valore = _leggi_valore(chiave, "SystemComponent")
    return type(valore) is int and valore == 1


def _leggi_voce(hkey, percorso_radice: str, nome_subkey: str, flag_accesso: int) -> dict | None:
    """
    Una singola applicazione da una sottochiave Uninstall, o None se la
    voce è normale ma non è un'applicazione utente (DisplayName
    mancante/non valido, o componente di sistema). Solleva
    _VoceNonLeggibile se la sottochiave esiste ma un errore reale ne
    impedisce l'esame affidabile: i due casi non vanno confusi, un
    errore di accesso non equivale a "non è un'app".

    flag_accesso va passato identico a quello usato per aprire la
    radice: la redirezione WOW64 si applica a ogni apertura assoluta
    sotto il percorso redirezionato, non solo alla prima (verificato
    empiricamente: una sottochiave nella sola vista a 32 bit non si
    apre con un percorso assoluto HKLM senza KEY_WOW64_32KEY, anche se
    la radice Uninstall era stata aperta con quel flag).
    """

    percorso_completo = f"{percorso_radice}\\{nome_subkey}"

    try:
        chiave = winreg.OpenKey(hkey, percorso_completo, 0, winreg.KEY_READ | flag_accesso)
    except FileNotFoundError:
        # Sparita tra EnumKey e questa apertura: concorrenza normale
        # (es. disinstallata nel frattempo), non un errore di lettura.
        return None
    except OSError:
        raise _VoceNonLeggibile(
            "Sottochiave applicazione non apribile per un errore reale."
        )

    try:
        with chiave:
            if _componente_di_sistema(chiave):
                return None

            nome = _stringa_valida(
                _leggi_valore(chiave, "DisplayName"), _MAX_LUNGHEZZA_NOME
            )
            if nome is None:
                return None

            versione = _stringa_valida(
                _leggi_valore(chiave, "DisplayVersion"), _MAX_LUNGHEZZA_CAMPO
            )
            produttore = _stringa_valida(
                _leggi_valore(chiave, "Publisher"), _MAX_LUNGHEZZA_CAMPO
            )
    except OSError:
        raise _VoceNonLeggibile(
            "Sottochiave applicazione non leggibile per un errore reale."
        )

    return {"name": nome, "version": versione, "publisher": produttore}


def _enumera_radice(hkey, percorso_radice: str, flag_accesso: int) -> tuple[list, bool]:
    """
    (voci, radice_incompleta) per una singola radice Uninstall.

    FileNotFoundError nell'aprire la radice stessa è normale (vista non
    applicabile su questo sistema, es. nessuna installazione
    per-utente): voci vuote, incompleta=False. Qualunque altro OSError
    nell'aprire la radice la rende incompleta.

    Una volta aperta, l'enumerazione distingue la fine naturale di
    EnumKey (WinError 259, ERROR_NO_MORE_ITEMS) da un errore reale a
    metà: solo il secondo rende la radice incompleta, mai la prima. Il
    raggiungimento del circuit breaker (_MAX_SUBKEY_PER_RADICE) rende
    la radice incompleta allo stesso modo: oltre quel punto la
    scansione non è più garantita esaustiva, anche senza un errore.
    In ogni caso le voci già lette restano valide: un'interruzione non
    scarta mai i dati raccolti fino a quel momento.

    Una singola sottochiave normale (senza DisplayName valido, o
    componente di sistema) viene esclusa senza alcun effetto sulla
    radice. Una sottochiave che invece non si può esaminare per un
    errore reale (_VoceNonLeggibile) non ferma l'enumerazione delle
    altre, ma rende la radice incompleta: le voci già raccolte restano
    valide, non vengono mai scartate.
    """

    try:
        chiave_radice = winreg.OpenKey(hkey, percorso_radice, 0, winreg.KEY_READ | flag_accesso)
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], True

    voci = []
    incompleta = False
    try:
        indice = 0
        while True:
            try:
                nome_subkey = winreg.EnumKey(chiave_radice, indice)
            except OSError as errore:
                if getattr(errore, "winerror", None) != _ERROR_NO_MORE_ITEMS:
                    incompleta = True
                break

            if indice >= _MAX_SUBKEY_PER_RADICE:
                # Esiste almeno un'altra sottochiave oltre il limite:
                # la si lascia non letta (mai aperta/interrogata) e la
                # radice è incompleta. Il controllo avviene DOPO aver
                # confermato che EnumKey restituisce ancora un nome (e
                # non la fine naturale): un registro con esattamente
                # _MAX_SUBKEY_PER_RADICE sottochiavi non è mai segnalato
                # come incompleto per errore.
                incompleta = True
                break
            indice += 1

            try:
                voce = _leggi_voce(hkey, percorso_radice, nome_subkey, flag_accesso)
            except _VoceNonLeggibile:
                # Voce esistente ma non esaminabile in modo affidabile:
                # non invalida le altre, ma la radice resta incompleta.
                # L'enumerazione continua (le sottochiavi successive
                # restano leggibili).
                incompleta = True
                continue

            if voce is not None:
                voci.append(voce)
    finally:
        chiave_radice.Close()

    return voci, incompleta


def _chiave_dedup(voce: dict) -> tuple:
    """Identità per la deduplicazione: nome/versione/produttore normalizzati per confronto."""

    return (
        voce["name"].casefold(),
        (voce["version"] or "").casefold(),
        (voce["publisher"] or "").casefold(),
    )


def _deduplica(voci: list) -> list:
    """
    Mantiene la prima occorrenza per ogni tripla (nome, versione,
    produttore) normalizzata: solo repliche effettive della stessa
    installazione riflesse in più viste del registro vengono unite.
    Nomi simili con versione o produttore diversi (o entrambi assenti
    ma comunque diversi tra loro) restano applicazioni distinte.
    """

    viste = set()
    risultato = []
    for voce in voci:
        chiave = _chiave_dedup(voce)
        if chiave in viste:
            continue
        viste.add(chiave)
        risultato.append(voce)
    return risultato


def _piattaforma() -> str:
    return sys.platform


def leggi_applicazioni_installate() -> RisultatoApplicazioni:
    """
    Applicazioni installate secondo le chiavi Uninstall del registro,
    per macchina (viste 64/32 bit via KEY_WOW64_64KEY/32KEY) e per
    utente corrente, deduplicate e ordinate in modo deterministico
    (nome casefold, poi nome, versione, produttore). Non applica
    filtro né limite di risultati: è compito del chiamante
    (list_installed_applications).

    PiattaformaNonSupportata: sistema diverso da Windows o winreg
    assente. Nessuna altra eccezione: radici non apribili, enumerazioni
    interrotte da un errore reale o dal circuit breaker, e singole
    sottochiavi non esaminabili per un errore reale contribuiscono
    tutte a sorgenti_non_disponibili. Una sottochiave normale (senza
    DisplayName valido, o componente di sistema) viene invece
    semplicemente esclusa, senza alcun effetto sul conteggio. Nulla di
    tutto questo viene mai propagato come eccezione.
    """

    if winreg is None or _piattaforma() != "win32":
        raise PiattaformaNonSupportata(
            "Applicazioni installate disponibili solo su Windows."
        )

    hive_per_nome = {
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    }

    tutte_le_voci = []
    sorgenti_non_disponibili = 0

    for nome_hive, percorso, vista in _RADICI_UNINSTALL:
        voci, incompleta = _enumera_radice(
            hive_per_nome[nome_hive], percorso, _flag_vista(vista)
        )
        tutte_le_voci.extend(voci)
        if incompleta:
            sorgenti_non_disponibili += 1

    deduplicate = _deduplica(tutte_le_voci)
    deduplicate.sort(
        key=lambda voce: (
            voce["name"].casefold(),
            voce["name"],
            voce["version"] or "",
            voce["publisher"] or "",
        )
    )

    return RisultatoApplicazioni(
        applicazioni=deduplicate,
        sorgenti_non_disponibili=sorgenti_non_disponibili,
    )
