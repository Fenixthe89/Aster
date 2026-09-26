"""
Caricamento e validazione della configurazione di Aster.

Dalla v0.7.1b la configurazione è a due livelli:
- default app-owned (config.default.json nelle risorse): completo e
  obbligatorio;
- override utente opzionale (config.json nella directory dati): può
  sovrascrivere soltanto le foglie elencate in FOGLIE_UTENTE.

Il modulo non conosce le posizioni dei file: le riceve come parametri
(le determina runtime_paths, tramite aster.py). Legge soltanto: non
crea, non scrive, non rinomina e non migra nulla.

I messaggi di errore non riportano mai i valori letti dai file di
configurazione, né nomi di chiave scritti dall'utente: solo nomi di
campo noti ad Aster e percorsi dei file coinvolti.
"""

import copy
import json
import math
import os
import stat
import unicodedata
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath


class ErroreConfig(ValueError):
    """Configurazione assente, illeggibile o non valida: avvio bloccato."""


class ErroreTipoConfig(ErroreConfig, TypeError):
    """Campo di tipo errato: resta anche un TypeError per i chiamanti esistenti."""


class _ErroreJsonStretto(Exception):
    """JSON sintatticamente valido ma rifiutato dal parsing stretto."""


# File opzionale assente. Distinto da None, che è un contenuto JSON
# valido ("null") e va rifiutato come qualsiasi non-oggetto.
_FILE_ASSENTE = object()


# Valori legacy (v0.6.8) di files.prompt/files.memory. Dalla v0.7.1a le
# posizioni di prompt e memoria sono gestite da runtime_paths: queste
# chiavi restano tollerate solo se equivalenti ai valori legacy.
FILES_LEGACY = {
    "prompt": ("prompt.txt",),
    "memory": ("data", "memory.json"),
}


# =====================================================================
# Validazione delle singole foglie
# =====================================================================


def _valida_testo(valore, nome: str, origine: str) -> None:
    if not isinstance(valore, str):
        raise ErroreTipoConfig(
            f"Il campo '{nome}' in {origine} deve essere una stringa."
        )

    if not valore:
        raise ErroreConfig(
            f"Il campo '{nome}' in {origine} non può essere vuoto."
        )

    if any(unicodedata.category(carattere) == "Cc" for carattere in valore):
        raise ErroreConfig(
            f"Il campo '{nome}' in {origine} non può contenere caratteri "
            "di controllo."
        )


def _valida_timeout(valore, nome: str, origine: str) -> None:
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        raise ErroreTipoConfig(
            f"Il campo '{nome}' in {origine} deve essere un numero "
            "(int o float)."
        )

    if isinstance(valore, float) and not math.isfinite(valore):
        raise ErroreConfig(
            f"Il campo '{nome}' in {origine} deve essere un numero finito."
        )

    if valore <= 0:
        raise ErroreConfig(
            f"Il campo '{nome}' in {origine} deve essere maggiore di 0."
        )


def _valida_num_ctx(valore, nome: str, origine: str) -> None:
    # Solo la forma (intero positivo): nessun limite legato a un
    # modello specifico. Il minimo rispetto al default lo impone
    # l'override utente (_unisci_override).
    if isinstance(valore, bool) or not isinstance(valore, int):
        raise ErroreTipoConfig(
            f"Il campo '{nome}' in {origine} deve essere un numero intero."
        )

    if valore <= 0:
        raise ErroreConfig(
            f"Il campo '{nome}' in {origine} deve essere maggiore di 0."
        )


def _valida_allowed_roots(valore, nome: str, origine: str) -> None:
    # Solo la forma grezza: la semantica di dominio (assoluti/relativi,
    # esistenza, containment) resta in file_tools/filesystem_policy.
    # "" è rifiutata perché verrebbe risolta come app_root in silenzio;
    # "." resta ammessa (app_root dichiarata esplicitamente).
    if not isinstance(valore, list):
        raise ErroreTipoConfig(
            f"Il campo '{nome}' in {origine} deve essere una lista."
        )

    for elemento in valore:
        if not isinstance(elemento, str):
            raise ErroreTipoConfig(
                f"Ogni elemento di '{nome}' in {origine} deve essere "
                "una stringa."
            )

        if not elemento:
            raise ErroreConfig(
                f"Gli elementi di '{nome}' in {origine} non possono essere "
                "stringhe vuote."
            )

        if "\x00" in elemento:
            raise ErroreConfig(
                f"Gli elementi di '{nome}' in {origine} non possono "
                "contenere il carattere NUL."
            )


# Foglie che il config utente può sovrascrivere: nessun'altra.
VALIDATORI_FOGLIE_UTENTE = {
    ("ollama", "model"): _valida_testo,
    ("ollama", "host"): _valida_testo,
    ("ollama", "timeout"): _valida_timeout,
    ("ollama", "num_ctx"): _valida_num_ctx,
    ("tools", "filesystem", "allowed_roots"): _valida_allowed_roots,
}

FOGLIE_UTENTE = tuple(VALIDATORI_FOGLIE_UTENTE)

# Foglie che il default deve sempre contenere: app-owned più whitelist.
FOGLIE_DEFAULT = (
    ("assistant", "name"),
    ("assistant", "version"),
    ("chat", "history_limit"),
    ("memory", "search_max_results"),
    *FOGLIE_UTENTE,
)


def _albero_foglie(foglie) -> dict:
    """Albero delle sezioni ammesse: dict per le sezioni, None per le foglie."""

    albero = {}
    for percorso in foglie:
        nodo = albero
        for chiave in percorso[:-1]:
            nodo = nodo.setdefault(chiave, {})
        nodo[percorso[-1]] = None
    return albero


SCHEMA_UTENTE = _albero_foglie(FOGLIE_UTENTE)


def _nome_foglia(percorso: tuple) -> str:
    return ".".join(percorso)


def _elenco_foglie_utente() -> str:
    return ", ".join(_nome_foglia(percorso) for percorso in FOGLIE_UTENTE)


# =====================================================================
# Lettura JSON stretta
# =====================================================================


def _rifiuta_chiavi_duplicate(coppie: list) -> dict:
    risultato = {}
    for chiave, valore in coppie:
        if chiave in risultato:
            raise _ErroreJsonStretto("chiave duplicata")
        risultato[chiave] = valore
    return risultato


def _rifiuta_costante(nome: str):
    # NaN, Infinity, -Infinity: estensioni non standard di json.
    raise _ErroreJsonStretto("valore numerico non finito")


def _float_finito(testo: str) -> float:
    # Un letterale come 1e999 diventerebbe inf senza passare da NaN/Infinity.
    valore = float(testo)
    if not math.isfinite(valore):
        raise _ErroreJsonStretto("valore numerico non finito")
    return valore


def _file_regolare_presente(percorso: Path, obbligatorio: bool) -> bool:
    """
    True se percorso è un file regolare (anche tramite symlink valido).
    False solo se non esiste nulla a quel percorso e il file non è
    obbligatorio. Qualsiasi altro caso (directory, symlink rotto,
    errore di accesso) è un errore: mai un fallback silenzioso.
    """

    try:
        os.lstat(percorso)
    except FileNotFoundError:
        if obbligatorio:
            raise ErroreConfig(
                f"File di configurazione non trovato: {percorso}"
            ) from None
        return False
    except OSError:
        raise ErroreConfig(
            f"Impossibile accedere al file di configurazione: {percorso}"
        ) from None

    try:
        informazioni = os.stat(percorso)
    except OSError:
        raise ErroreConfig(
            f"Il file di configurazione non è leggibile: {percorso}"
        ) from None

    if not stat.S_ISREG(informazioni.st_mode):
        raise ErroreConfig(
            f"Il percorso di configurazione non è un file regolare: {percorso}"
        )

    return True


def _leggi_json(percorso: Path, encoding: str, obbligatorio: bool = True):
    """
    Legge un file JSON in modo stretto: chiavi duplicate, NaN e
    Infinity sono rifiutati. Restituisce _FILE_ASSENTE solo se il file
    non è obbligatorio e non esiste. Gli errori OS/JSON diventano
    ErroreConfig senza il dettaglio nativo (può contenere dati).
    """

    if not _file_regolare_presente(percorso, obbligatorio):
        return _FILE_ASSENTE

    try:
        with open(percorso, "r", encoding=encoding) as file:
            testo = file.read()
    except UnicodeDecodeError:
        raise ErroreConfig(
            f"Il file di configurazione non è testo UTF-8 valido: {percorso}"
        ) from None
    except OSError:
        raise ErroreConfig(
            f"Impossibile leggere il file di configurazione: {percorso}"
        ) from None

    try:
        return json.loads(
            testo,
            object_pairs_hook=_rifiuta_chiavi_duplicate,
            parse_constant=_rifiuta_costante,
            parse_float=_float_finito,
        )
    except _ErroreJsonStretto as errore:
        raise ErroreConfig(
            f"JSON non valido in {percorso}: {errore}."
        ) from None
    except json.JSONDecodeError as errore:
        # Solo posizione e motivo generico: mai il testo del file.
        raise ErroreConfig(
            f"JSON non valido in {percorso} "
            f"(riga {errore.lineno}, colonna {errore.colno})."
        ) from None
    except (ValueError, RecursionError):
        raise ErroreConfig(f"JSON non valido in {percorso}.") from None


# =====================================================================
# Validazione di una configurazione
# =====================================================================


def _path_legacy_equivalente(
    valore: str,
    parti_attese: tuple,
    classe_path: type[PurePath] = PurePath,
) -> bool:
    """
    True se valore indica, secondo le regole di path del sistema
    (classe_path, di default quello corrente), lo stesso path relativo
    legacy: accetta per esempio "./data/memory.json". Rifiuta sempre
    path vuoti, assoluti o ancorati (root/drive, in qualsiasi
    convenzione) e qualsiasi componente "..".
    """

    if not valore or "\x00" in valore:
        return False

    if PurePosixPath(valore).anchor or PureWindowsPath(valore).anchor:
        return False

    candidato = classe_path(valore)

    if ".." in candidato.parts:
        return False

    return candidato == classe_path(*parti_attese)


def _valida_config(config, origine: str = "config.json") -> None:
    """
    Validazione di forma dei campi noti. Le chiavi ollama.timeout,
    ollama.num_ctx e tools.filesystem.allowed_roots sono opzionali qui
    (compatibilità di carica_config): la completezza del default e
    della configurazione finale la impone _valida_config_completa.
    """

    if not isinstance(config, dict):
        raise ErroreTipoConfig(
            f"Il contenuto di {origine} deve essere un oggetto JSON."
        )

    # Validazione introdotta nella v0.3.1: un history_limit di tipo
    # errato non causerebbe un errore all'avvio, ma solo durante la
    # conversazione, dentro limita_cronologia(). bool è escluso anche
    # se sottoclasse di int.
    history_limit = config["chat"]["history_limit"]

    if type(history_limit) is not int:
        raise ErroreTipoConfig(
            f"Il campo 'chat.history_limit' in {origine} deve essere "
            "un numero intero."
        )

    if history_limit < 1:
        raise ErroreConfig(
            f"Il campo 'chat.history_limit' in {origine} deve essere "
            "maggiore o uguale a 1."
        )

    search_max_results = config["memory"]["search_max_results"]

    if type(search_max_results) is not int:
        raise ErroreTipoConfig(
            f"Il campo 'memory.search_max_results' in {origine} "
            "deve essere un numero intero."
        )

    if search_max_results < 1:
        raise ErroreConfig(
            f"Il campo 'memory.search_max_results' in {origine} "
            "deve essere maggiore o uguale a 1."
        )

    ollama_config = config["ollama"]

    for chiave in ("model", "host"):
        if chiave in ollama_config:
            _valida_testo(ollama_config[chiave], f"ollama.{chiave}", origine)

    if "timeout" in ollama_config:
        _valida_timeout(ollama_config["timeout"], "ollama.timeout", origine)

    if "num_ctx" in ollama_config:
        _valida_num_ctx(ollama_config["num_ctx"], "ollama.num_ctx", origine)

    # Se assente equivale a lista vuota, cioè nessun accesso filesystem
    # (default deny).
    tools_config = config.get("tools", {})
    if not isinstance(tools_config, dict):
        tools_config = {}

    filesystem_config = tools_config.get("filesystem", {})
    if not isinstance(filesystem_config, dict):
        filesystem_config = {}

    allowed_roots_raw = filesystem_config.get("allowed_roots")

    if allowed_roots_raw is not None:
        _valida_allowed_roots(
            allowed_roots_raw,
            "tools.filesystem.allowed_roots",
            origine,
        )

    # I campi files.prompt/files.memory non possono più spostare prompt
    # o memoria: i path reali arrivano da runtime_paths. Un valore
    # personalizzato blocca l'avvio invece di essere ignorato in
    # silenzio (Aster creerebbe una memoria nuova e vuota altrove).
    # Dalla v0.7.1b la sezione non esiste più nel default e il config
    # utente non può contenerla (whitelist).
    files_config = config.get("files")

    if files_config is not None:
        if not isinstance(files_config, dict):
            raise ErroreTipoConfig(
                f"Il campo 'files' in {origine} deve essere un oggetto."
            )

        for chiave, parti_attese in FILES_LEGACY.items():
            if chiave not in files_config:
                continue

            valore = files_config[chiave]

            if not isinstance(valore, str):
                raise ErroreTipoConfig(
                    f"Il campo 'files.{chiave}' in {origine} deve essere "
                    "una stringa."
                )

            if not _path_legacy_equivalente(valore, parti_attese):
                raise ErroreConfig(
                    f"Il campo 'files.{chiave}' in {origine} non può più "
                    "essere personalizzato: la posizione del file è gestita "
                    "da Aster. Ripristina il valore "
                    f"\"{'/'.join(parti_attese)}\" oppure rimuovi il campo."
                )


def _verifica_foglie_presenti(config, foglie, origine: str) -> None:
    if not isinstance(config, dict):
        raise ErroreTipoConfig(
            f"Il contenuto di {origine} deve essere un oggetto JSON."
        )

    for percorso in foglie:
        nodo = config
        for profondita, chiave in enumerate(percorso):
            if not isinstance(nodo, dict):
                raise ErroreTipoConfig(
                    f"La sezione '{_nome_foglia(percorso[:profondita])}' "
                    f"in {origine} deve essere un oggetto."
                )
            if chiave not in nodo:
                raise ErroreConfig(
                    f"Il campo '{_nome_foglia(percorso)}' manca in {origine}."
                )
            nodo = nodo[chiave]


def _valida_config_completa(config, origine: str) -> None:
    """Default o configurazione finale: ogni foglia presente e valida."""

    _verifica_foglie_presenti(config, FOGLIE_DEFAULT, origine)
    _valida_config(config, origine)

    for chiave in ("name", "version"):
        _valida_testo(config["assistant"][chiave], f"assistant.{chiave}", origine)

    # Le foglie sovrascrivibili devono rispettare nel default le stesse
    # regole imposte al config utente.
    for percorso, validatore in VALIDATORI_FOGLIE_UTENTE.items():
        valore = config
        for chiave in percorso:
            valore = valore[chiave]
        validatore(valore, _nome_foglia(percorso), origine)


def carica_config(config_file: Path) -> dict:
    """
    Legge e valida un singolo file di configurazione, senza
    stratificazione. Il percorso arriva come parametro perché il modulo
    non conosce la posizione del progetto.
    """

    config = _leggi_json(config_file, "utf-8")
    _valida_config(config, str(config_file))
    return config


# =====================================================================
# Configurazione a livelli: default + override utente
# =====================================================================


def _stesso_file(primo: Path, secondo: Path) -> bool:
    if Path(primo) == Path(secondo):
        return True

    try:
        return os.path.samefile(primo, secondo)
    except OSError:
        return False


def _verifica_legacy(legacy_file: Path, user_file: Path) -> None:
    """
    B+: un config.json legacy separato dal config utente blocca
    l'avvio. Nessuna migrazione: il file non viene letto, modificato,
    rinominato o copiato, e il config utente non viene creato.
    """

    if _stesso_file(legacy_file, user_file):
        return

    try:
        os.lstat(legacy_file)
    except FileNotFoundError:
        return
    except OSError:
        raise ErroreConfig(
            "Impossibile verificare l'assenza della configurazione legacy: "
            f"{legacy_file}"
        ) from None

    foglie = "\n".join(
        f"  - {_nome_foglia(percorso)}" for percorso in FOGLIE_UTENTE
    )

    raise ErroreConfig(
        f"Config legacy rilevata: {legacy_file}\n"
        "Questa versione di Aster non legge più quel file e non lo "
        "converte in automatico.\n"
        "Le sole impostazioni personalizzabili sono:\n"
        f"{foglie}\n"
        "Se ne avevi modificata qualcuna, riportala a mano (solo le "
        f"chiavi cambiate) in: {user_file}\n"
        "Poi rimuovi o sposta altrove il vecchio config.json e riavvia "
        "Aster.\n"
        "Nessun file è stato modificato."
    )


def _applica_sezione(
    sezione_utente,
    schema: dict,
    destinazione: dict,
    percorso: tuple,
    origine: str,
) -> None:
    """
    Visita soltanto le strutture ammesse da schema. Ogni chiave non
    prevista, a qualunque livello, è un errore. Le foglie valide
    sostituiscono per intero il valore di destinazione (liste incluse).
    """

    if not isinstance(sezione_utente, dict):
        if percorso:
            raise ErroreTipoConfig(
                f"La sezione '{_nome_foglia(percorso)}' in {origine} deve "
                "essere un oggetto."
            )
        raise ErroreTipoConfig(
            f"Il contenuto di {origine} deve essere un oggetto JSON."
        )

    for chiave, valore in sezione_utente.items():
        if chiave not in schema:
            # Il nome della chiave sconosciuta non viene riportato:
            # proviene dal file utente.
            posizione = (
                f"nella sezione '{_nome_foglia(percorso)}'"
                if percorso
                else "al livello principale"
            )
            raise ErroreConfig(
                f"{origine} contiene una chiave non consentita {posizione}. "
                f"Chiavi modificabili: {_elenco_foglie_utente()}."
            )

        percorso_figlio = percorso + (chiave,)
        figlio = schema[chiave]

        if isinstance(figlio, dict):
            _applica_sezione(
                valore,
                figlio,
                destinazione[chiave],
                percorso_figlio,
                origine,
            )
            continue

        VALIDATORI_FOGLIE_UTENTE[percorso_figlio](
            valore,
            _nome_foglia(percorso_figlio),
            origine,
        )
        destinazione[chiave] = valore


def _unisci_override(default: dict, utente, origine: str) -> dict:
    """Copia del default con le sole foglie utente consentite applicate."""

    finale = copy.deepcopy(default)
    _applica_sezione(utente, SCHEMA_UTENTE, finale, (), origine)

    # Il config utente può solo alzare num_ctx: un context più piccolo
    # del default degrada il routing dei tool (v0.6.6b).
    if finale["ollama"]["num_ctx"] < default["ollama"]["num_ctx"]:
        raise ErroreConfig(
            f"Il campo 'ollama.num_ctx' in {origine} non può essere "
            "inferiore al valore predefinito di Aster."
        )

    return finale


def carica_config_runtime(
    default_file: Path,
    user_file: Path,
    legacy_file: Path,
) -> dict:
    """
    Configurazione effettiva di Aster:

    A) un config legacy separato blocca l'avvio (nessuna migrazione);
    B) il default è obbligatorio, completo e valido;
    C) il config utente è opzionale: se assente non viene creato;
    D) merge per whitelist di foglie su una copia del default;
    E) la configurazione finale viene validata di nuovo.

    Qualsiasi problema solleva ErroreConfig: nessun fallback silenzioso.
    """

    _verifica_legacy(legacy_file, user_file)

    default = _leggi_json(default_file, "utf-8")
    _valida_config_completa(default, str(default_file))

    # utf-8-sig: il config utente può essere salvato con BOM da editor
    # come il Blocco note di Windows.
    utente = _leggi_json(user_file, "utf-8-sig", obbligatorio=False)

    if utente is _FILE_ASSENTE:
        finale = copy.deepcopy(default)
    else:
        finale = _unisci_override(default, utente, str(user_file))

    _valida_config_completa(finale, "configurazione finale")

    return finale
