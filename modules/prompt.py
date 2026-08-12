"""Caricamento del prompt di sistema di Aster."""

from pathlib import Path


def carica_prompt(prompt_file: Path) -> str:
    """
    Legge e controlla il prompt di sistema.

    Il percorso arriva come parametro perché il modulo non conosce
    la posizione del progetto: quella la determina aster.py.
    """

    if not prompt_file.exists():
        raise FileNotFoundError(
            f"Il file prompt.txt non è stato trovato.\n"
            f"Percorso previsto: {prompt_file}"
        )

    prompt = prompt_file.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError(
            "Il file prompt.txt esiste, ma è vuoto."
        )

    return prompt