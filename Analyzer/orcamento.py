"""
Versão budget de um deck: troca cartas caras por alternativas mais baratas que preservem
a sinergia do deck original.

Para cada carta cara (acima de um preço-limite, em USD):
  1. Se a MESMA carta existir numa impressão muito mais barata, sugere essa impressão
     (sinergia 100% preservada; informa a coleção). "Muito mais barata" = economiza pelo
     menos X dólares E custa no máximo metade do preço original.
  2. Senão, procura cartas DIFERENTES. O tipo da carta NÃO precisa ser o mesmo (um
     encantamento pode virar uma mágica, por exemplo; só terrenos continuam sendo
     trocados por terrenos). Cada candidata recebe uma nota:
         50% sinergia com o deck  +  35% semelhança de função  +  15% economia
     A sinergia vem do EDHREC (quando há comandante e o EDHREC responde) combinada com uma
     estimativa feita a partir do próprio deck; ver sinergia.py.

Este módulo não imprime nada e não depende de interface.
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
    buscar_cartas_em_lote,
    cores_da_carta,
    identidade_do_comandante,
    obter_comandantes,
    obter_texto_oracle,
)
from sinergia import (
    carregar_sinergia_edhrec,
    buscar_no_edhrec,
    jaccard,
    perfil_do_deck,
    sinergia_final,
    subtipos,
    tokens_texto,
)

API_SEARCH_URL = "https://api.scryfall.com/cards/search"

LIMITE_TROCA_PADRAO = 1.00           # só procura substitutas para cartas acima deste preço (USD)
MAX_CARTAS_ANALISADAS = 60           # teto de cartas pesquisadas (as mais caras primeiro)
ECONOMIA_MINIMA = 0.80               # carta diferente precisa custar no máximo 80% da original
ECONOMIA_MINIMA_IMPRESSAO_USD = 3.00  # impressão mais barata: economizar pelo menos US$ 3,00...
IMPRESSAO_PRECO_MAX_REL = 0.50       # ...e custar no máximo 50% do preço original
JANELA_CMC = 2                       # candidatas com CMC até 2 acima/abaixo da original
MAX_ALTERNATIVAS = 3
TAMANHO_POOL_EDHREC = 250            # quantas cartas populares do EDHREC são consideradas

PESOS = {"sinergia": 0.50, "funcao": 0.35, "economia": 0.15}

# Rigor: (nota mínima total, semelhança de função mínima), ambas de 0 a 1
RIGOR = {"Flexível": (0.25, 0.0), "Equilibrado": (0.38, 0.12), "Rigoroso": (0.50, 0.25)}

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
    (r"create [^.]*tokens?", "o:create o:token"),
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
# Semelhança de função
# ---------------------------------------------------------------------------

def similaridade(original, candidata):
    """
    Nota de 0 a 1 para o quanto 'candidata' FAZ o mesmo que 'original' (texto de regras).
    Não exige o mesmo tipo de carta; subtipos em comum só dão um pequeno bônus.
    """
    ta, tb = tokens_texto(original), tokens_texto(candidata)
    texto = jaccard(ta, tb) if (ta or tb) else 0.5   # ambas sem texto: neutro
    nota = 0.90 * texto + 0.10 * jaccard(subtipos(original), subtipos(candidata))
    nota -= 0.03 * abs(int(original.get("cmc", 0)) - int(candidata.get("cmc", 0)))
    pa, pb = set(original.get("produced_mana", [])), set(candidata.get("produced_mana", []))
    if pa and pb:
        nota += 0.20 * jaccard(pa, pb)
    return max(0.0, min(1.0, nota))


# ---------------------------------------------------------------------------
# Montagem das buscas
# ---------------------------------------------------------------------------

def _eh_terreno(data):
    return "Land" in data.get("type_line", "").split("//")[0]


def _eh_terreno_basico(data):
    return "Basic Land" in data.get("type_line", "")


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
    """Consultas da mais específica à mais ampla (relaxando o filtro de texto). Sem filtro de tipo."""
    cmc = int(data.get("cmc", 0))
    base = ["game:paper", "usd>0", f"usd<{preco * ECONOMIA_MINIMA:.2f}",
            f"cmc>={max(0, cmc - JANELA_CMC)}", f"cmc<={cmc + JANELA_CMC}",
            _filtro_identidade(identidade)]
    if formato:
        base.append(f"f:{formato}")
    if _eh_terreno(data):
        base.append("t:land")
        produzidas = sorted(set(data.get("produced_mana", [])) & set("WUBRG"), key="WUBRG".index)
        if produzidas:
            base.append("produces>=" + "".join(produzidas).lower())
    else:
        base.append("-t:land")   # terreno só é trocado por terreno (e vice-versa)

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
        "colecao": data.get("set_name"),
        "colecao_codigo": (data.get("set") or "").upper(),
        "numero": data.get("collector_number"),
        **extra,
    }


def _impressao_mais_barata(data, preco, economia_minima_usd):
    """
    Mesma carta numa impressão de papel MUITO mais barata, ou None.
    Só vale se economizar pelo menos 'economia_minima_usd' E custar no máximo
    IMPRESSAO_PRECO_MAX_REL do preço original.
    """
    consulta = f'!"{data["name"]}" game:paper lang:en usd>0 -is:oversized'
    achados = [c for c in _buscar(consulta, ordem="usd", direcao="asc", unico="prints")
               if _preco_usd(c)]
    if not achados:
        return None
    melhor = min(achados, key=_preco_usd)
    novo = _preco_usd(melhor)
    if (preco - novo) >= economia_minima_usd and novo <= preco * IMPRESSAO_PRECO_MAX_REL:
        return melhor
    return None


# ---------------------------------------------------------------------------
# Candidatas populares (EDHREC) e pontuação
# ---------------------------------------------------------------------------

def _montar_pool(edhrec, nomes_no_deck, formato, identidade_deck, avisos):
    """
    Cartas que o EDHREC mostra para este comandante (as de maior sinergia/inclusão),
    com dados da Scryfall (preço, formato, identidade), já filtradas por legalidade e cor.
    """
    if not edhrec:
        return []
    vistos, candidatas = set(), []
    for entrada in sorted(edhrec.values(),
                          key=lambda e: -((e.get("synergy") or 0) + (e.get("inclusao") or 0))):
        nome = entrada["nome"]
        if nome.lower() in vistos or nome in nomes_no_deck:
            continue
        vistos.add(nome.lower())
        candidatas.append({"name": nome})
        if len(candidatas) >= TAMANHO_POOL_EDHREC:
            break

    dados_por_nome, _ = buscar_cartas_em_lote(candidatas, avisos)
    pool, nomes = [], set()
    for d in dados_por_nome.values():
        nome = d.get("name")
        if nome in nomes or nome in nomes_no_deck or _eh_terreno_basico(d) or not _preco_usd(d):
            continue
        if "paper" not in d.get("games", ["paper"]):
            continue
        if formato and d.get("legalities", {}).get(formato) != "legal":
            continue
        if identidade_deck is not None and not set(d.get("color_identity", [])) <= identidade_deck:
            continue
        nomes.add(nome)
        pool.append(d)
    return pool


def _avaliar(original, candidata, preco, edhrec, perfil):
    """Nota total (0 a 1), semelhança de função (0 a 1) e detalhes de sinergia."""
    funcao = similaridade(original, candidata)
    sinergia, detalhes = sinergia_final(candidata, edhrec, perfil)
    economia = max(0.0, min(1.0, 1 - _preco_usd(candidata) / preco))
    nota = (PESOS["sinergia"] * sinergia + PESOS["funcao"] * funcao
            + PESOS["economia"] * economia)
    return nota, funcao, detalhes


def _compativel(original, candidata, preco, nomes_no_deck, identidade):
    """Filtros locais (para candidatas do pool do EDHREC, que não passaram pela busca filtrada)."""
    p = _preco_usd(candidata)
    return (candidata.get("name") not in nomes_no_deck
            and p and p <= preco * ECONOMIA_MINIMA
            and abs(int(candidata.get("cmc", 0)) - int(original.get("cmc", 0))) <= JANELA_CMC
            and _eh_terreno(candidata) == _eh_terreno(original)
            and set(candidata.get("color_identity", [])) <= identidade)


def _melhores_candidatas(data, preco, formato, identidade, nomes_no_deck,
                         pool, edhrec, perfil, rigor, buscar_na_scryfall=True):
    """Candidatas diferentes aprovadas pelo rigor, da melhor para a pior nota."""
    por_nome = {}
    for c in pool:
        if _compativel(data, c, preco, nomes_no_deck, identidade):
            por_nome[c["name"]] = c

    if buscar_na_scryfall:
        ordem, direcao = (("edhrec", "asc") if formato in ("commander", "brawl", "standardbrawl")
                          else ("usd", "desc"))
        achadas = 0
        for consulta in _consultas_candidatas(data, preco, formato, identidade):
            for c in _buscar(consulta, ordem=ordem, direcao=direcao):
                nome = c.get("name")
                if nome in nomes_no_deck or not _preco_usd(c) or nome in por_nome:
                    continue
                por_nome[nome] = c
                achadas += 1
            if achadas >= 8:
                break

    nota_min, funcao_min = rigor
    pontuadas = []
    for c in por_nome.values():
        nota, funcao, detalhes = _avaliar(data, c, preco, edhrec, perfil)
        if nota >= nota_min and funcao >= funcao_min:
            pontuadas.append((nota, funcao, detalhes, c))
    pontuadas.sort(key=lambda x: (-x[0], _preco_usd(x[3])))
    return pontuadas[:MAX_ALTERNATIVAS + 1]


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------

def gerar_deck_budget(entradas, formato=None, limite_troca=LIMITE_TROCA_PADRAO,
                      rigor=RIGOR["Equilibrado"], progresso=None,
                      economia_minima_impressao=ECONOMIA_MINIMA_IMPRESSAO_USD,
                      usar_edhrec=True):
    """
    Gera a versão budget do deck.

    entradas: lista [{"qtd", "zona", "data"}] (resultado de analisador.resolver_cartas)
    formato:  chave Scryfall ("commander", "modern"...) ou None para não filtrar por formato
    limite_troca: só procura substitutas para cartas acima deste preço (USD)
    rigor: (nota mínima, semelhança de função mínima); veja RIGOR
    progresso: função opcional progresso(fracao_0_a_1, mensagem)
    economia_minima_impressao: a mesma carta só é sugerida em outra impressão se economizar
        pelo menos este valor (USD) e custar no máximo metade do preço original

    Comandantes e terrenos básicos nunca são trocados. Com comandante, as substitutas
    respeitam a identidade de cor dele; sem comandante, a da carta original.
    """
    def avisar(fracao, mensagem):
        if progresso:
            progresso(fracao, mensagem)

    comandantes = obter_comandantes(entradas)
    identidade_deck = identidade_do_comandante(comandantes)
    nomes_no_deck = {e["data"]["name"] for e in entradas}
    avisos = []

    # Sinergia: EDHREC (se houver comandante) + estimativa a partir do deck
    edhrec, slug_edhrec, aviso_edhrec = None, None, None
    if usar_edhrec and comandantes:
        avisar(0.0, "Consultando o EDHREC...")
        edhrec, slug_edhrec, aviso_edhrec = carregar_sinergia_edhrec(comandantes)
        if aviso_edhrec:
            avisos.append(aviso_edhrec)
    elif not comandantes:
        avisos.append("Deck sem comandante: a sinergia é estimada a partir do próprio deck.")
    perfil = perfil_do_deck(entradas)

    pool = []
    if edhrec:
        avisar(0.0, "Buscando as cartas populares com este comandante...")
        pool = _montar_pool(edhrec, nomes_no_deck, formato, identidade_deck, avisos)

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
        avisar(i / len(elegiveis), f"Procurando alternativas ({i}/{len(elegiveis)}): {data['name']}")

        identidade = (identidade_deck if identidade_deck is not None
                      else set(cores_da_carta(data)[1]))

        impressao = _impressao_mais_barata(data, preco, economia_minima_impressao)
        candidatas = _melhores_candidatas(
            data, preco, formato, identidade, nomes_no_deck, pool, edhrec, perfil, rigor,
            buscar_na_scryfall=impressao is None)   # com impressão ótima, poupa requisições

        opcoes = [_resumo(c, tipo_troca="similar", nota=round(n, 2), funcao=round(f, 2), **det)
                  for n, f, det, c in candidatas]
        escolha = None
        if impressao:
            # A mesma carta preserva 100% da sinergia: vence qualquer carta diferente.
            sin_orig = buscar_no_edhrec(edhrec, data)
            escolha = _resumo(impressao, tipo_troca="impressao", nota=1.0, funcao=1.0,
                              edhrec_pct=None if not sin_orig or sin_orig.get("synergy") is None
                              else round(sin_orig["synergy"] * 100),
                              inclusao_pct=None, estimada_pct=100)
        elif opcoes:
            escolha = opcoes[0]

        if escolha and escolha["tipo_troca"] == "similar":
            nomes_no_deck.add(escolha["nome"])        # não escolhe a mesma substituta duas vezes
        alternativas = [o for o in opcoes if o is not escolha][:MAX_ALTERNATIVAS]

        original_edh = buscar_no_edhrec(edhrec, data)
        trocas[id(e)] = {
            "original": _resumo(
                data, qtd=e["qtd"],
                edhrec_pct=(None if not original_edh or original_edh.get("synergy") is None
                            else round(original_edh["synergy"] * 100))),
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
            esc = troca["escolha"]
            impressao_txt = (f"({esc['colecao_codigo']}) {esc['numero']}"
                             if esc["tipo_troca"] == "impressao" and esc["colecao_codigo"] else "")
            lista.append({"qtd": e["qtd"], "nome": esc["nome"], "zona": e["zona"],
                          "impressao": impressao_txt})
            total_budget += esc["preco"] * e["qtd"]
        else:
            lista.append({"qtd": e["qtd"], "nome": e["data"]["name"], "zona": e["zona"],
                          "impressao": ""})
            total_budget += preco * e["qtd"]

    lista_trocas = sorted(trocas.values(), key=lambda t: -t["economia"])
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
        "edhrec": {"usado": bool(edhrec), "slug": slug_edhrec},
    }


def formatar_lista(resultado):
    """
    Lista final em texto (Commander / Deck / Sideboard), pronta para colar no Moxfield etc.
    Quando a troca é só de impressão, a linha indica a coleção: '1 Sol Ring (C21) 263'.
    """
    secoes = {"commander": [], "main": [], "side": []}
    for item in resultado["lista"]:
        linha = f"{item['qtd']} {item['nome']}"
        if item.get("impressao"):
            linha += f" {item['impressao']}"
        secoes[item["zona"] if item["zona"] in secoes else "main"].append(linha)
    blocos = []
    for chave, titulo in (("commander", "Commander"), ("main", "Deck"), ("side", "Sideboard")):
        if secoes[chave]:
            blocos.append(titulo + "\n" + "\n".join(secoes[chave]))
    return "\n\n".join(blocos) + "\n"


def formato_automatico(entradas):
    """Commander se há comandante declarado; senão None (sem filtro de formato)."""
    return "commander" if obter_comandantes(entradas) else None


NOMES_FORMATOS = {dados["nome"]: chave for chave, dados in FORMATOS.items()}
