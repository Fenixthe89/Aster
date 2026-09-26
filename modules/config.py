"""Caricamento e validazione della configurazione di Aster."""

import json
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

# Valori legacy (v0.6.8) di files.prompt/files.memory. Dalla v0.7.1a le
# posizioni di prompt e memoria sono gestite da runtime_paths: queste
# chiavi restano tollerate solo se equivalenti ai valori legacy.
FILES_LEGACY = {
    "prompt": ("prompt.txt",),
    "memory": ("data", "memory.json"),
}


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


def carica_config(config_file: Path) -> dict:
    """
    Legge la configurazione di Aster dal file indicato.

    Il percorso arriva come parametro perché il modulo non conosce
    la posizione del progetto: quella la determina runtime_paths.
    """

    if not config_file.exists():
        raise FileNotFoundError(
            "Il file config.json non è stato trovato.\n"
            f"Percorso previsto: {config_file}"
        )

    with open(config_file, "r", encoding="utf-8") as file:
        config = json.load(file)

    # Validazione introdotta nella v0.3.1: un history_limit di tipo
    # errato non causerebbe un errore all'avvio, ma solo durante la
    # conversazione, dentro limita_cronologia().
    if not isinstance(config["chat"]["history_limit"], int):
        raise TypeError(
            "Il campo 'chat.history_limit' in config.json deve essere "
            "un numero intero."
        )

    search_max_results = config["memory"]["search_max_results"]

    if type(search_max_results) is not int:
        raise TypeError(
            "Il campo 'memory.search_max_results' in config.json "
            "deve essere un numero intero."
        )

    if search_max_results < 1:
        raise ValueError(
            "Il campo 'memory.search_max_results' in config.json "
            "deve essere maggiore o uguale a 1."
        )

    # Il campo timeout è opzionale per compatibilità con config.json
    # precedenti: se manca, il chiamante userà il default (60).
    if "timeout" in config["ollama"]:
        timeout_ollama = config["ollama"]["timeout"]

        if (
            isinstance(timeout_ollama, bool)
            or not isinstance(timeout_ollama, (int, float))
        ):
            raise TypeError(
                "Il campo 'ollama.timeout' in config.json deve essere "
                "un numero (int o float)."
            )

        if timeout_ollama <= 0:
            raise ValueError(
                "Il campo 'ollama.timeout' in config.json deve essere "
                "maggiore di 0."
            )

    # Il campo num_ctx è opzionale (compatibilità con config.json
    # precedenti): se manca, il chiamante userà il default (8192). Qui
    # si valida solo la forma (intero positivo): nessun limite legato a
    # un modello specifico, per restare utilizzabile con modelli futuri.
    if "num_ctx" in config["ollama"]:
        num_ctx = config["ollama"]["num_ctx"]

        if (
            isinstance(num_ctx, bool)
            or not isinstance(num_ctx, int)
        ):
            raise TypeError(
                "Il campo 'ollama.num_ctx' in config.json deve essere "
                "un numero intero."
            )

        if num_ctx <= 0:
            raise ValueError(
                "Il campo 'ollama.num_ctx' in config.json deve essere "
                "maggiore di 0."
            )

    # Il campo tools.filesystem.allowed_roots è opzionale (compatibilità
    # con config.json precedenti): se assente equivale a lista vuota,
    # cioè nessun accesso filesystem (default deny). Qui si valida solo
    # la forma grezza (lista di stringhe): la semantica di dominio
    # (path assoluti/relativi, esistenza, containment) non appartiene a
    # questo modulo.
    tools_config = config.get("tools", {})
    if not isinstance(tools_config, dict):
        tools_config = {}

    filesystem_config = tools_config.get("filesystem", {})
    if not isinstance(filesystem_config, dict):
        filesystem_config = {}

    allowed_roots_raw = filesystem_config.get("allowed_roots")

    if allowed_roots_raw is not None:
        if not isinstance(allowed_roots_raw, list):
            raise TypeError(
                "Il campo 'tools.filesystem.allowed_roots' in config.json "
                "deve essere una lista."
            )

        for elemento in allowed_roots_raw:
            if not isinstance(elemento, str):
                raise TypeError(
                    "Ogni elemento di 'tools.filesystem.allowed_roots' in "
                    "config.json deve essere una stringa."
                )

    # I campi files.prompt/files.memory non possono più spostare prompt
    # o memoria: i path reali arrivano da runtime_paths. Un valore
    # personalizzato blocca l'avvio invece di essere ignorato in
    # silenzio (Aster creerebbe una memoria nuova e vuota altrove).
    # Assenti o legacy -> accettati. La sezione sparirà in 0.7.1b.
    files_config = config.get("files")

    if files_config is not None:
        if not isinstance(files_config, dict):
            raise TypeError(
                "Il campo 'files' in config.json deve essere un oggetto."
            )

        for chiave, parti_attese in FILES_LEGACY.items():
            if chiave not in files_config:
                continue

            valore = files_config[chiave]

            if not isinstance(valore, str):
                raise TypeError(
                    f"Il campo 'files.{chiave}' in config.json deve essere "
                    "una stringa."
                )

            if not _path_legacy_equivalente(valore, parti_attese):
                raise ValueError(
                    f"Il campo 'files.{chiave}' in config.json non può più "
                    "essere personalizzato: la posizione del file è gestita "
                    "da Aster. Ripristina il valore "
                    f"\"{'/'.join(parti_attese)}\" oppure rimuovi il campo."
                )

    return config