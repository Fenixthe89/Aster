"""Estrazione condivisa di query di ricerca da testo libero."""

import re


def estrai_query_da_testo(
    testo: str,
    stopword: set[str],
) -> list[str]:
    """
    Estrae query semplici e conservative da un testo, per i fallback
    di ricerca della memoria persistente (recall e delete-per-query).

    Algoritmo invariato rispetto alle implementazioni originarie in
    modules/chat.py e modules/memory_tools.py: tokenizzazione via
    regex, rimozione delle stopword del chiamante, filtro di
    lunghezza minima, poi bigrammi consecutivi sulle parole
    significative seguiti dalle parole singole, nello stesso ordine
    di apparizione nel testo originale.
    """

    parole = re.findall(
        r"[a-zA-ZÀ-ÿ0-9_+-]+",
        testo.casefold(),
    )

    significative = [
        parola
        for parola in parole
        if parola not in stopword
        and len(parola) >= 4
    ]

    query = []

    for indice in range(len(significative) - 1):
        query.append(
            f"{significative[indice]} "
            f"{significative[indice + 1]}"
        )

    query.extend(significative)

    return query
