"""
Métricas de sinergia entre uma carta e um deck.

Duas fontes, sempre identificadas como tais na interface:

  1. EDHREC (só com comandante). A sinergia do EDHREC é:
         (% dos decks daquele comandante que usam a carta)
       - (% dos decks da mesma identidade de cor que usam a carta)
     Os dados vêm do endpoint JSON público, mas NÃO oficial, do EDHREC (o EDHREC não tem
     API oficial). Ele pode mudar ou sair do ar sem aviso; por isso qualquer falha aqui
     nunca derruba o app: o módulo devolve None e a estimativa local assume.

  2. Estimativa local (sempre disponível). Mede o quanto o vocabulário da carta (mecânicas,
     palavras-chave, tipos de criatura) é típico do deck e do comandante. É uma aproximação
     nossa, não uma estatística de decks reais.
"""
import re
import statistics
import unicodedata
from collections import Counter

import requests

from analisador import HEADERS, TIMEOUT_API, obter_texto_oracle

EDHREC_URL = "https://json.edhrec.com/pages/commanders/{slug}.json"

PALAVRAS_VAZIAS = {"the", "a", "an", "of", "to", "you", "your", "it", "its", "this", "that",
                   "and", "or", "in", "on", "at", "for", "with", "from", "is", "are", "be",
                   "can", "may", "if", "then", "has", "have", "as", "by"}

_CACHE_EDHREC = {}   # slug -> dicionário de cartas (ou None se falhou)


# ---------------------------------------------------------------------------
# Vocabulário das cartas (compartilhado com a medida de semelhança de função)
# ---------------------------------------------------------------------------

def tokens_texto(data):
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


def subtipos(data):
    linha = data.get("type_line", "").split("//")[0]
    return set(linha.split("—")[1].split()) if "—" in linha else set()


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def _tokens_sinergia(data):
    """Vocabulário temático: texto + tipos de criatura + palavras-chave."""
    return (tokens_texto(data)
            | {"tipo:" + s.lower() for s in subtipos(data)}
            | {"kw:" + k.lower() for k in data.get("keywords", [])})


# ---------------------------------------------------------------------------
# EDHREC
# ---------------------------------------------------------------------------

def slug_edhrec(nome):
    """'Krenko, Mob Boss' -> 'krenko-mob-boss' (formato usado nas URLs do EDHREC)."""
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    nome = re.sub(r"[^a-z0-9\s-]", "", nome)
    return re.sub(r"[\s-]+", "-", nome).strip("-")


def _slugs_possiveis(comandantes):
    """Slugs a tentar. Partners viram um slug só, em ordem alfabética, ligados por hífen."""
    nomes = [e["data"]["name"] for e in comandantes]
    frente = "-".join(sorted(slug_edhrec(n.split(" // ")[0]) for n in nomes))
    completo = "-".join(sorted(slug_edhrec(n) for n in nomes))
    return [frente] if frente == completo else [frente, completo]


def _fracao(a, b):
    try:
        return float(a) / float(b) if a is not None and b else None
    except (TypeError, ValueError):
        return None


def _extrair_cartas(json_edhrec):
    """
    Percorre o JSON da página do comandante procurando listas 'cardviews'. A busca é
    recursiva de propósito: tolera mudanças na profundidade da estrutura.
    Retorna {nome_minusculo: {"nome", "synergy", "inclusao", "decks", "potencial"}}.
    """
    cartas = {}

    def visitar(no):
        if isinstance(no, dict):
            for chave, valor in no.items():
                if chave == "cardviews" and isinstance(valor, list):
                    for cv in valor:
                        registrar(cv)
                else:
                    visitar(valor)
        elif isinstance(no, list):
            for item in no:
                visitar(item)

    def registrar(cv):
        nome = cv.get("name") if isinstance(cv, dict) else None
        if not nome:
            return
        decks, potencial = cv.get("num_decks"), cv.get("potential_decks")
        inclusao = _fracao(decks if decks is not None else cv.get("inclusion"), potencial)
        entrada = {"nome": nome, "synergy": cv.get("synergy"), "inclusao": inclusao,
                   "decks": decks, "potencial": potencial}
        for chave in {nome.lower(), nome.lower().split(" // ")[0]}:
            antiga = cartas.get(chave)
            if antiga is None:
                cartas[chave] = entrada
            else:  # a carta aparece em várias listas: junta o que cada uma sabe
                if antiga.get("synergy") is None and entrada["synergy"] is not None:
                    antiga["synergy"] = entrada["synergy"]
                if (entrada["inclusao"] or 0) > (antiga["inclusao"] or 0):
                    antiga["inclusao"] = entrada["inclusao"]
                    antiga["decks"], antiga["potencial"] = decks, potencial

    visitar(json_edhrec)
    return cartas


def carregar_sinergia_edhrec(comandantes):
    """
    Baixa a página do comandante no EDHREC (uma requisição, com cache).
    Retorna (cartas, slug, aviso):
      cartas: dicionário nome -> dados de sinergia, ou None se indisponível
      aviso:  texto para mostrar ao usuário quando não foi possível usar o EDHREC
    Nunca levanta exceção.
    """
    if not comandantes:
        return None, None, None
    ultima_falha = "não encontrei a página do comandante"
    for slug in _slugs_possiveis(comandantes):
        if slug in _CACHE_EDHREC:
            cartas = _CACHE_EDHREC[slug]
            if cartas:
                return cartas, slug, None
            continue
        try:
            resposta = requests.get(EDHREC_URL.format(slug=slug), headers=HEADERS,
                                    timeout=TIMEOUT_API)
            if resposta.status_code != 200:
                ultima_falha = f"o EDHREC respondeu com status {resposta.status_code}"
                _CACHE_EDHREC[slug] = None
                continue
            cartas = _extrair_cartas(resposta.json())
        except (requests.RequestException, ValueError):
            ultima_falha = "não foi possível acessar o EDHREC"
            continue            # falha de rede: não guarda no cache, pode funcionar depois
        _CACHE_EDHREC[slug] = cartas or None
        if cartas:
            return cartas, slug, None
        ultima_falha = "a página do EDHREC veio em um formato inesperado"
    return None, None, (f"Sinergia do EDHREC indisponível ({ultima_falha}); "
                        "usando só a sinergia estimada a partir do deck.")


def buscar_no_edhrec(edhrec, data):
    """Dados de sinergia de uma carta (dados Scryfall) no dicionário do EDHREC, ou None."""
    if not edhrec:
        return None
    nome = data.get("name", "").lower()
    return edhrec.get(nome) or edhrec.get(nome.split(" // ")[0])


# ---------------------------------------------------------------------------
# Estimativa local
# ---------------------------------------------------------------------------

def perfil_do_deck(entradas):
    """
    Resume os temas do deck: para cada termo, em que fração das cartas ele aparece.
    O comandante pesa 3x. Termos genéricos demais (em mais de 60% das cartas) e termos
    que só aparecem em uma carta são descartados.
    """
    documentos = []
    for e in entradas:
        d = e["data"]
        if "Basic Land" in d.get("type_line", ""):
            continue
        documentos.extend([_tokens_sinergia(d)] * (3 if e["zona"] == "commander" else 1))
    n = len(documentos)
    if n == 0:
        return {"fracao": {}, "mediana": 1.0}

    contagem = Counter(t for doc in documentos for t in doc)
    fracao = {t: c / n for t, c in contagem.items() if c >= 2 and c / n <= 0.6}

    tipicos = [sum(fracao.get(t, 0) for t in doc) / len(doc) for doc in documentos if doc]
    mediana = max(statistics.median(tipicos) if tipicos else 0.0, 0.02)
    return {"fracao": fracao, "mediana": mediana}


def sinergia_local(data, perfil):
    """0 a 1: o quão típica do deck é a carta (1 = tão temática quanto a carta mediana)."""
    tokens = _tokens_sinergia(data)
    if not tokens:
        return 0.0
    tipica = sum(perfil["fracao"].get(t, 0) for t in tokens) / len(tokens)
    return min(1.0, tipica / perfil["mediana"])


def _nota_edhrec(entrada):
    """0 a 1 a partir da sinergia (70%) e da inclusão (30%) da carta no EDHREC."""
    syn, inc = entrada.get("synergy"), entrada.get("inclusao")
    if syn is None and inc is None:
        return None
    s = 0.0 if syn is None else min(1.0, max(0.0, (syn + 0.05) / 0.45))   # 40% de sinergia = nota máxima
    i = 0.0 if inc is None else min(1.0, inc / 0.5)                        # 50% dos decks = nota máxima
    return 0.7 * s + 0.3 * i


def sinergia_final(data, edhrec, perfil):
    """
    Nota de sinergia (0 a 1) da carta com o deck, mais os números para exibir.
      - Carta no EDHREC: 70% EDHREC + 30% estimativa local.
      - EDHREC carregado, mas sem a carta: 60% da estimativa local (sem sinal do EDHREC).
      - Sem EDHREC: estimativa local.
    Retorna (nota, detalhes) com detalhes = {"edhrec_pct", "inclusao_pct", "estimada_pct"}.
    """
    local = sinergia_local(data, perfil)
    entrada = buscar_no_edhrec(edhrec, data)
    nota_edh = _nota_edhrec(entrada) if entrada else None

    if nota_edh is not None:
        nota = 0.7 * nota_edh + 0.3 * local
    elif edhrec:
        nota = 0.6 * local
    else:
        nota = local

    syn = entrada.get("synergy") if entrada else None
    inc = entrada.get("inclusao") if entrada else None
    return nota, {
        "edhrec_pct": None if syn is None else round(syn * 100),
        "inclusao_pct": None if inc is None else round(inc * 100),
        "estimada_pct": round(local * 100),
    }
