"""
Versão budget de um deck: troca cartas caras por alternativas parecidas e mais baratas.

Para cada carta cara (acima de um preço-limite, em USD), o módulo procura:
  1. A MESMA carta numa impressão mais barata (jogabilidade idêntica).
  2. Cartas DIFERENTES com efeito parecido: busca na Scryfall (formato, identidade de cor,
     tipo, CMC próximo, preço menor) usando trechos de texto que descrevem o efeito, e
     ranqueia localmente pela semelhança do texto de regras.

Este módulo não imprime nada e não depende de interface: gerar_deck_budget() devolve um
dicionário com as trocas, os totais e a lista final.
"""
import re
import time

import requests

from analisador import (
    FORMATOS,
    HEADERS,
    TIMEOUT_API,
    ErroScryfall,
    _preco_usd,
    cores_da_carta,
    identidade_do_comandante,
    obter_comandantes,
    obter_texto_oracle,
)

API_SEARCH_URL = "https://api.scryfall.com/cards/search"

LIMITE_TROCA_PADRAO = 1.00      # só procura substitutos para cartas acima deste preço (USD)
MAX_CARTAS_ANALISADAS = 60      # teto de cartas pesquisadas (as mais caras primeiro)
ECONOMIA_MINIMA = 0.80          # a substituta precisa custar no máximo 80% da original
PREFERIR_IMPRESSAO_ATE = 0.60   # impressão mais barata que isto (60% do preço) vence a troca por outra carta
MAX_ALTERNATIVAS = 3

# Rigor da semelhança (nota mínima de 0 a 1 para aceitar uma substituta)
RIGOR = {"Flexível": 0.18, "Equilibrado": 0.30, "Rigoroso": 0.45}

# Trechos do texto de regras que indicam a função da carta -> filtro de busca na Scryfall.
# A ordem importa: padrões mais específicos primeiro.
PADROES_EFEITO = [
    (r"counter target", 'o:"counter target"'),
    (r"destroy all|destroy each", 'o:"destroy all"'),
    (r"exile all", 'o:"exile all"'),
    (r"destroy target", 'o:"destroy target"'),
    (r"exile target", 'o:"exile target"'),
    (r"gain control of", 'o:"gain control"'),
    (r"to (its|their) owner'?s hand", 'o:"owner\'s hand"'),
    (r"search your library for [^.]*land", 'o:"search your library" o:"land card"'),
    (r"search your library for", 'o:"search your library for"'),
    (r"\{t\}: add|add \{", 'o:"add {"'),
    (r"\bdraws? (a|two|three|four|x|\d+) cards?", "o:draw"),
    (r"create [^.]*tokens?", 'o:create o:token'),
    (r"each opponent", 'o:"each opponent"'),
    (r"damage to (any target|each|target)", 'o:"damage to"'),
    (r"\+1/\+1 counter", 'o:"+1/+1 counter"'),
    (r"from your graveyard", 'o:"from your graveyard"'),
    (r"\bsacrifice\b", "o:sacrifice"),
    (r"\bscry\b", "o:scry"),
    (r"proliferate", "o:proliferate"),
    (r"\buntap\b", "o:untap"),
    (r"\bdiscard", "o:discard"),
]

TIPOS_PRINCIPAIS = ["Creature", "Planeswalker", "Instant", "Sorcery", "Artifact",
                    "Enchantment", "Land", "Battle"]

PALAVRAS_VAZIAS = {"the", "a", "an", "of", "to", "you", "your", "it", "its", "this", "that",
                   "and", "or", "in", "on", "at", "for", "with", "from", "is", "are", "be",
                   "can", "may", "if", "then", "has", "have", "as", "by"}


# ---------------------------------------------------------------------------
# Acesso à Scryfall
# ---------------------------------------------------------------------------

def _buscar(consulta, ordem="usd", direcao="asc", unico="cards", pausa=0.1):
    """Busca na Scryfall (1 página, até 175 cartas). Sem resultados -> lista vazia."""
    time.sleep(pausa)  # a Scryfall pede de 50 a 100 ms entre requisições
    params = {"q": consulta, "order": ordem, "dir": direcao, "unique": unico}
    for tentativa in range(2):
        try:
            resposta = requests.get(API_SEARCH_URL, params=params,
                                    headers=HEADERS, timeout=TIMEOUT_API)
        except requests.RequestException as erro:
            raise ErroScryfall("Não foi possível conectar à Scryfall. "
                               "Verifique sua conexão com a internet e tente novamente.") from erro
        if resposta.status_code == 429 and tentativa == 0:
            time.sleep(1.0)
            continue
        if resposta.status_code == 200:
            return resposta.json().get("data", [])
        return []  # 404 = nenhum resultado; 422 = consulta inválida
    return []


# ---------------------------------------------------------------------------
# Semelhança entre cartas
# ---------------------------------------------------------------------------

def _tokens(data):
    """Palavras e pares de palavras do texto de regras (nome -> ~, números -> N)."""
    texto = obter_texto_oracle(data).lower()
    nome = data.get("name", "").lower().split("//")[0].strip()
    if nome:
        texto = texto.replace(nome, "~")
    texto = re.sub(r"\([^)]*\)", " ", texto)          # remove texto de lembrete
    texto = re.sub(r"\d+", "N", texto)
    palavras = [p for p in re.findall(r"~|\{[^}]+\}|[a-z+/\-']+", texto)
                if p not in PALAVRAS_VAZIAS]
    return set(palavras) | {f"{a} {b}" for a, b in zip(palavras, palavras[1:])}


def _subtipos(data):
    linha = data.get("type_line", "").split("//")[0]
    return set(linha.split("—")[1].split()) if "—" in linha else set()


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def similaridade(original, candidata):
    """Nota de 0 a 1 para o quanto 'candidata' se parece com 'original' (dados Scryfall)."""
    ta, tb = _tokens(original), _tokens(candidata)
    texto = _jaccard(ta, tb) if (ta or tb) else 0.5   # ambas sem texto: neutro
    subtipos = _jaccard(_subtipos(original), _subtipos(candidata))
    nota = 0.85 * texto + 0.15 * subtipos
    nota -= 0.03 * abs(int(original.get("cmc", 0)) - int(candidata.get("cmc", 0)))
    pa, pb = set(original.get("produced_mana", [])), set(candidata.get("produced_mana", []))
    if pa and pb:
        nota += 0.20 * _jaccard(pa, pb)
    return max(0.0, min(1.0, nota))


# ---------------------------------------------------------------------------
# Montagem das buscas
# ---------------------------------------------------------------------------

def _tipo_principal(data):
    frente = data.get("type_line", "").split("//")[0]
    for tipo in TIPOS_PRINCIPAIS:
        if tipo in frente:
            return tipo.lower()
    return None


def _clausulas_de_efeito(data):
    """Filtros de texto que descrevem a função da carta (no máximo 2, do mais ao menos específico)."""
    texto = obter_texto_oracle(data).lower()
    texto = re.sub(r"\([^)]*\)", " ", texto)
    clausulas = []
    for padrao, clausula in PADROES_EFEITO:
        if re.search(padrao, texto) and clausula not in clausulas:
            clausulas.append(clausula)
    if not clausulas:  # sem padrão conhecido: usa uma palavra-chave (Flying, Trample...)
        clausulas = [f"keyword:{k.lower().replace(' ', '')}" for k in data.get("keywords", [])[:2]]
    return clausulas[:2]


def _filtro_identidade(letras):
    return "id<=" + ("".join(sorted(letras, key="WUBRG".index)) or "c")


def _consultas_candidatas(data, preco, formato, identidade):
    """Lista de consultas, da mais específica à mais ampla (relaxando o filtro de texto)."""
    cmc = int(data.get("cmc", 0))
    base = ["game:paper", "usd>0", f"usd<{preco * ECONOMIA_MINIMA:.2f}",
            f"cmc>={max(0, cmc - 1)}", f"cmc<={cmc + 1}", _filtro_identidade(identidade)]
    if formato:
        base.append(f"f:{formato}")
    tipo = _tipo_principal(data)
    if tipo:
        base.append(f"t:{tipo}")
    if tipo == "land" and data.get("produced_mana"):
        base.append("produces>=" + "".join(sorted(set(data["produced_mana"]) & set("WUBRG"))).lower())

    clausulas = _clausulas_de_efeito(data)
    niveis = []
    if clausulas:
        niveis.append(clausulas)
        if len(clausulas) > 1:
            niveis.append(clausulas[:1])
    niveis.append([])
    return [" ".join(base + n) for n in niveis]


def _resumo(data, **extra):
    return {
        "nome": data.get("name"),
        "preco": _preco_usd(data),
        "tipo": data.get("type_line", ""),
        "cmc": int(data.get("cmc", 0)),
        "texto": obter_texto_oracle(data).strip(),
        **extra,
    }


def _impressao_mais_barata(data, preco):
    """Mesma carta, impressão de papel mais barata. None se não houver ganho."""
    consulta = f'!"{data["name"]}" game:paper usd>0 -is:oversized'
    achados = _buscar(consulta, ordem="usd", direcao="asc", unico="prints")
    achados = [c for c in achados if _preco_usd(c)]
    if not achados:
        return None
    melhor = min(achados, key=_preco_usd)
    if _preco_usd(melhor) < preco * ECONOMIA_MINIMA:
        return melhor
    return None


def _melhores_similares(data, preco, formato, identidade, nomes_no_deck, similaridade_minima):
    """Candidatas diferentes, ordenadas por semelhança (já filtradas pela nota mínima)."""
    ordem, direcao = (("edhrec", "asc") if formato in ("commander", "brawl", "standardbrawl")
                      else ("usd", "desc"))
    vistos, candidatas = set(), []
    for consulta in _consultas_candidatas(data, preco, formato, identidade):
        for c in _buscar(consulta, ordem=ordem, direcao=direcao):
            nome = c.get("name")
            if nome in vistos or nome in nomes_no_deck or not _preco_usd(c):
                continue
            vistos.add(nome)
            candidatas.append(c)
        if len(candidatas) >= 8:
            break

    pontuadas = [(similaridade(data, c), c) for c in candidatas]
    pontuadas = [(n, c) for n, c in pontuadas if n >= similaridade_minima]
    pontuadas.sort(key=lambda x: (-x[0], _preco_usd(x[1])))
    return pontuadas[:MAX_ALTERNATIVAS]


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------

def _eh_terreno_basico(data):
    return "Basic Land" in data.get("type_line", "")


def gerar_deck_budget(entradas, formato=None, limite_troca=LIMITE_TROCA_PADRAO,
                      similaridade_minima=RIGOR["Equilibrado"], progresso=None):
    """
    Gera a versão budget do deck.

    entradas: lista [{"qtd", "zona", "data"}] (resultado de analisador.resolver_cartas)
    formato:  chave Scryfall ("commander", "modern"...) ou None para não filtrar por formato
    limite_troca: só procura substitutas para cartas acima deste preço (USD)
    progresso: função opcional progresso(i, total, nome_da_carta)

    Regras: comandantes e terrenos básicos nunca são trocados. Com comandante, as
    substitutas respeitam a identidade de cor dele; sem comandante, a da carta original.
    Entre as opções, a impressão mais barata da mesma carta vence quando custa até 60% do
    preço original; senão vale a carta diferente mais parecida (se atingir a nota mínima).
    """
    comandantes = obter_comandantes(entradas)
    identidade_deck = identidade_do_comandante(comandantes)
    nomes_no_deck = {e["data"]["name"] for e in entradas}

    elegiveis = [e for e in entradas
                 if e["zona"] != "commander" and not _eh_terreno_basico(e["data"])
                 and (_preco_usd(e["data"]) or 0) > limite_troca]
    elegiveis.sort(key=lambda e: -(_preco_usd(e["data"]) or 0))
    ignoradas = max(0, len(elegiveis) - MAX_CARTAS_ANALISADAS)
    elegiveis = elegiveis[:MAX_CARTAS_ANALISADAS]

    trocas = {}
    for i, e in enumerate(elegiveis, start=1):
        data = e["data"]
        preco = _preco_usd(data)
        if progresso:
            progresso(i, len(elegiveis), data["name"])

        identidade = (identidade_deck if identidade_deck is not None
                      else set(cores_da_carta(data)[1]))

        impressao = _impressao_mais_barata(data, preco)
        similares = _melhores_similares(data, preco, formato, identidade,
                                        nomes_no_deck, similaridade_minima)

        opcoes = [_resumo(c, similaridade=round(n, 2), tipo_troca="similar") for n, c in similares]
        if impressao:
            opcoes.insert(0, _resumo(impressao, similaridade=1.0, tipo_troca="impressao"))

        escolha = None
        if impressao and _preco_usd(impressao) <= preco * PREFERIR_IMPRESSAO_ATE:
            escolha = opcoes[0]                       # mesma carta, bem mais barata
        elif similares:
            escolha = next(o for o in opcoes if o["tipo_troca"] == "similar")
        elif impressao:
            escolha = opcoes[0]

        if escolha and escolha["tipo_troca"] == "similar":
            nomes_no_deck.add(escolha["nome"])        # não escolhe a mesma substituta duas vezes
        alternativas = [o for o in opcoes if o is not escolha][:MAX_ALTERNATIVAS]

        trocas[id(e)] = {
            "original": _resumo(data, qtd=e["qtd"]),
            "zona": e["zona"],
            "escolha": escolha,
            "alternativas": alternativas,
            "economia": ((preco - escolha["preco"]) * e["qtd"]) if escolha else 0.0,
        }

    # Lista final e totais
    lista, total_original, total_budget = [], 0.0, 0.0
    for e in entradas:
        preco = _preco_usd(e["data"]) or 0.0
        total_original += preco * e["qtd"]
        troca = trocas.get(id(e))
        if troca and troca["escolha"]:
            lista.append({"qtd": e["qtd"], "nome": troca["escolha"]["nome"], "zona": e["zona"]})
            total_budget += troca["escolha"]["preco"] * e["qtd"]
        else:
            lista.append({"qtd": e["qtd"], "nome": e["data"]["name"], "zona": e["zona"]})
            total_budget += preco * e["qtd"]

    lista_trocas = sorted(trocas.values(), key=lambda t: -t["economia"])
    avisos = []
    if ignoradas:
        avisos.append(f"Só as {MAX_CARTAS_ANALISADAS} cartas mais caras foram analisadas; "
                      f"{ignoradas} carta(s) mais barata(s) ficaram como estão.")
    return {
        "formato": formato,
        "limite_troca": limite_troca,
        "trocas": [t for t in lista_trocas if t["escolha"]],
        "sem_troca": [t for t in lista_trocas if not t["escolha"]],
        "lista": lista,
        "total_original": total_original,
        "total_budget": total_budget,
        "economia": total_original - total_budget,
        "avisos": avisos,
    }


def formatar_lista(resultado):
    """Lista final em texto (Commander / Deck / Sideboard), pronta para colar no Moxfield etc."""
    secoes = {"commander": [], "main": [], "side": []}
    for item in resultado["lista"]:
        secoes[item["zona"] if item["zona"] in secoes else "main"].append(
            f"{item['qtd']} {item['nome']}")
    blocos = []
    for chave, titulo in (("commander", "Commander"), ("main", "Deck"), ("side", "Sideboard")):
        if secoes[chave]:
            blocos.append(titulo + "\n" + "\n".join(secoes[chave]))
    return "\n\n".join(blocos) + "\n"


def formato_automatico(entradas):
    """Commander se há comandante declarado; senão None (sem filtro de formato)."""
    return "commander" if obter_comandantes(entradas) else None


NOMES_FORMATOS = {dados["nome"]: chave for chave, dados in FORMATOS.items()}
