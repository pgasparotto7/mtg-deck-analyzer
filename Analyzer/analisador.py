"""
MTG Deck Analyzer V2 -- módulo de lógica

Este módulo não imprime nada por conta própria (exceto em imprimir_relatorio() e no
bloco __main__). analisar_deck() devolve um dicionário com todos os resultados, para
que qualquer interface (terminal, Streamlit, Flask...) possa apresentá-los.

Uso no terminal:      python analisador.py          (lê o arquivo deck.txt)
Uso em outra interface:
    cartas, nome_deck, avisos = carregar_deck_de_texto(texto)
    resultado = analisar_deck(cartas, nome_deck)

Novidades da V2:
  - Validação de identidade de cor do comandante (Commander, Brawl e Historic Brawl)
  - Verificação se o comandante pode, de fato, ser comandante
  - Seção "Comandante" no relatório, listando cartas fora da identidade de cor
  - Cartas incolores contadas à parte; comandante incolor identificado explicitamente
  - Distribuição de cores por custo de mana OU por identidade de cor (custo + habilidades)
"""
import re
import time
import requests

VERSAO = "2.3"

TIMEOUT_API = 30  # segundos


class ErroScryfall(Exception):
    """Falha de comunicação com a API da Scryfall."""


API_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
API_FUZZY_URL = "https://api.scryfall.com/cards/named"

HEADERS = {
    "User-Agent": f"MTGDeckAnalyzer/{VERSAO}",
    "Accept": "application/json"
}

NOMES_CORES = {'W': 'Branco (W)', 'U': 'Azul (U)', 'B': 'Preto (B)', 'R': 'Vermelho (R)', 'G': 'Verde (G)'}
ORDEM_CORES = "WUBRG"

# Chaves = nomes usados no campo "legalities" da Scryfall
# "side"            = tamanho máximo do sideboard (None = não verificar)
# "identidade_cor"  = formato com comandante e regra de identidade de cor
# "cmd_planeswalker"= planeswalkers lendários podem ser comandante (Brawl)
FORMATOS = {
    "standard":      {"nome": "Standard",       "min": 60,  "max": None, "copias": 4, "side": 15},
    "alchemy":       {"nome": "Alchemy",        "min": 60,  "max": None, "copias": 4, "side": 15},
    "pioneer":       {"nome": "Explorer",       "min": 60,  "max": None, "copias": 4, "side": 15},
    "historic":      {"nome": "Historic",       "min": 60,  "max": None, "copias": 4, "side": 15},
    "timeless":      {"nome": "Timeless",       "min": 60,  "max": None, "copias": 4, "side": 15},
    "modern":        {"nome": "Modern",         "min": 60,  "max": None, "copias": 4, "side": 15},
    "legacy":        {"nome": "Legacy",         "min": 60,  "max": None, "copias": 4, "side": 15},
    "pauper":        {"nome": "Pauper",         "min": 60,  "max": None, "copias": 4, "side": 15},
    "penny":         {"nome": "Penny Dreadful", "min": 60,  "max": None, "copias": 4, "side": 15},
    "standardbrawl": {"nome": "Brawl",          "min": 60,  "max": 60,   "copias": 1, "side": None,
                      "identidade_cor": True, "cmd_planeswalker": True},
    "brawl":         {"nome": "Historic Brawl", "min": 100, "max": 100,  "copias": 1, "side": None,
                      "identidade_cor": True, "cmd_planeswalker": True},
    "commander":     {"nome": "Commander",      "min": 100, "max": 100,  "copias": 1, "side": None,
                      "identidade_cor": True, "cmd_planeswalker": False},
}

# Cabeçalhos de seção reconhecidos no deck.txt (export da Arena, Moxfield, etc.)
CABECALHOS_ZONA = {
    "deck": "main",
    "main": "main",
    "mainboard": "main",
    "maindeck": "main",
    "main deck": "main",
    "commander": "commander",
    "sideboard": "side",
    "side": "side",
    "side board": "side",
    "companion": "side",
    "about": "meta",  # bloco de metadados (Name ...), não contém cartas
    "maybeboard": "ignorar",
    "considering": "ignorar",
}


# ---------------------------------------------------------------------------
# Leitura do deck
# ---------------------------------------------------------------------------

def eh_cabecalho(linha):
    """Se a linha for um cabeçalho de seção (Deck, SIDEBOARD:, // Commander, == COMMANDER ==...),
    retorna a zona correspondente; caso contrário, None."""
    return CABECALHOS_ZONA.get(linha.strip("#/=: ").lower())


def interpretar_linha(linha):
    """
    Interpreta uma linha de carta. Retorna (qtd, nome, zona_forcada).
    Aceita: '4 Nome', '4x Nome', 'Nome', '4 Nome (SET) 123', '... *F*',
    'SB: 2 Nome' e tags do Archidekt ([Commander{top}], [Sideboard], [Maybeboard]).
    """
    zona_forcada = None

    if linha.upper().startswith("SB:"):
        zona_forcada = "side"
        linha = linha[3:].strip()

    # Tags do Archidekt
    for tag in re.findall(r"\[([^\]]+)\]", linha):
        tokens = {t.strip() for t in re.split(r"[,{}]", tag.lower())}
        if "commander" in tokens:
            zona_forcada = "commander"
        elif "sideboard" in tokens:
            zona_forcada = "side"
        elif "maybeboard" in tokens:
            zona_forcada = "ignorar"
    linha = re.sub(r"\s*\[[^\]]*\]", "", linha)      # remove [tags]
    linha = re.sub(r"\s*\^[^^]*\^", "", linha)       # remove ^categorias^
    linha = re.sub(r"\s+\*[A-Za-z]\*$", "", linha)   # remove marcador de foil (*F*, *E*)
    linha = linha.strip()

    m = re.match(r"^(\d+)(?:[xX]|\s)\s*(.+)$", linha)
    if m:
        qtd = int(m.group(1))
        nome = m.group(2).strip()
    else:
        qtd = 1
        nome = linha

    # Remove "(SET) 123" do final
    nome = re.sub(r"\s+\([A-Za-z0-9]{2,6}\)(?:\s+\S+)?$", "", nome).strip()
    return qtd, nome, zona_forcada

def carregar_deck_de_texto(texto):
    """
    Lê o texto de um deck e marca a zona de cada carta: "main", "commander" ou "side".
    Retorna (cartas, nome_deck, avisos).

    Formatos aceitos (Moxfield, MTG Arena, MTGO e texto simples):
      - Seções por cabeçalho: About / Commander / Companion / Deck / Sideboard / Maybeboard
        (com ou sem '//', '#', '==' ou ':' -- ex.: 'SIDEBOARD:')
      - Prefixo 'SB:' em linhas individuais
      - SEM seções: uma linha em branco separa o deck principal do sideboard
      - Quantidade '4', '4x' ou ausente; '(SET) 123' e marcador de foil são ignorados
      - Tags do Archidekt: [Commander], [Sideboard], [Maybeboard]
    Maybeboard é ignorado.
    """
    linhas = [l.strip() for l in texto.lstrip("\ufeff").splitlines()]
    avisos = []

    # O texto tem seções explícitas? Se não, a linha em branco separa main e sideboard.
    usa_secoes = (any(eh_cabecalho(l) for l in linhas if l)
                  or any(l.upper().startswith("SB:") for l in linhas))

    cartas = []
    nome_deck = None
    zona = "main"
    bloco = 0
    bloco_tem_cartas = False
    ignoradas = 0

    for linha in linhas:
        if not linha:
            if not usa_secoes and bloco_tem_cartas:
                bloco += 1
                bloco_tem_cartas = False
                zona = "side" if bloco == 1 else "ignorar"
            continue

        nova_zona = eh_cabecalho(linha)
        if nova_zona:
            zona = nova_zona
            continue

        # Seção de metadados (ex.: "About / Name ..." do Moxfield/Arena)
        if zona == "meta":
            if linha.lower().startswith("name "):
                nome_deck = linha[5:].strip()
            continue

        # Comentários
        if linha.startswith("#") or linha.startswith("//"):
            continue

        qtd, nome, zona_forcada = interpretar_linha(linha)
        zona_linha = zona_forcada or zona
        bloco_tem_cartas = True

        if zona_linha == "ignorar":
            ignoradas += 1
            continue

        cartas.append({"qtd": qtd, "name": nome, "zona": zona_linha})

    if ignoradas:
        avisos.append(f"{ignoradas} linha(s) ignorada(s) (Maybeboard ou blocos extras).")
    return cartas, nome_deck, avisos


def carregar_deck(caminho_arquivo):
    """Igual a carregar_deck_de_texto(), mas lê de um arquivo. Levanta FileNotFoundError."""
    with open(caminho_arquivo, "r", encoding="utf-8-sig") as f:
        return carregar_deck_de_texto(f.read())


def limpar_nome_carta(nome):
    """Se a carta tiver '//' (dupla face), pega apenas a face principal da frente."""
    if "//" in nome:
        return nome.split("//")[0].strip()
    return nome


# ---------------------------------------------------------------------------
# Scryfall
# ---------------------------------------------------------------------------

def buscar_cartas_em_lote(cartas, avisos=None):
    """
    Busca cada carta ÚNICA uma vez (mesmo que apareça no main e no sideboard).
    Mensagens sobre problemas são acrescentadas à lista 'avisos' (se fornecida).
    Retorna:
      - dados_por_nome: {nome_em_minusculas: dados_da_scryfall}
      - nao_encontrados: {nome_em_minusculas: nome_limpo} das cartas que falharam
    Levanta ErroScryfall se não for possível conectar à API.
    """
    if avisos is None:
        avisos = []

    nomes_unicos = {}
    for c in cartas:
        nome_limpo = limpar_nome_carta(c["name"])
        nomes_unicos.setdefault(nome_limpo.lower(), nome_limpo)

    identificadores = [{"name": n} for n in nomes_unicos.values()]
    dados_por_nome = {}
    nao_encontrados = {}

    # Envia em lotes de até 75 cartas
    lote_tamanho = 75
    for i in range(0, len(identificadores), lote_tamanho):
        fatia = identificadores[i:i + lote_tamanho]
        try:
            response = requests.post(API_COLLECTION_URL, json={"identifiers": fatia},
                                     headers=HEADERS, timeout=TIMEOUT_API)
        except requests.RequestException as erro:
            raise ErroScryfall("Não foi possível conectar à Scryfall. "
                               "Verifique sua conexão com a internet e tente novamente.") from erro

        if response.status_code == 200:
            data = response.json()

            for card in data.get("data", []):
                nome_completo = card.get("name", "").lower()
                dados_por_nome[nome_completo] = card
                dados_por_nome[nome_completo.split("//")[0].strip()] = card

            for not_found in data.get("not_found", []):
                nome_falhou = not_found.get("name", "")
                nao_encontrados[nome_falhou.lower()] = nome_falhou
        else:
            avisos.append(f"Erro no servidor da Scryfall (status {response.status_code}) "
                          f"em um lote de {len(fatia)} cartas; tentando busca individual.")
            # Marca o lote inteiro para tentar o Plano B
            for ident in fatia:
                nao_encontrados[ident["name"].lower()] = ident["name"]

    return dados_por_nome, nao_encontrados


def buscar_carta_fallback(nome_carta):
    """Plano B: Busca individual utilizando o parâmetro 'fuzzy' caso o lote falhe."""
    nome_limpo = limpar_nome_carta(nome_carta)
    try:
        response = requests.get(API_FUZZY_URL, params={"fuzzy": nome_limpo},
                                headers=HEADERS, timeout=TIMEOUT_API)
    except requests.RequestException:
        return None
    if response.status_code == 200:
        return response.json()
    return None


# ---------------------------------------------------------------------------
# Regras de cópias
# ---------------------------------------------------------------------------

def obter_texto_oracle(data):
    """Pega o texto da carta, inclusive de cartas dupla face."""
    texto = data.get("oracle_text", "")
    for face in data.get("card_faces", []):
        texto += " " + face.get("oracle_text", "")
    return texto


def limite_de_copias(data, limite_padrao):
    """Retorna o limite de cópias de uma carta (None = sem limite)."""
    if "Basic Land" in data.get("type_line", ""):
        return None
    texto = obter_texto_oracle(data)
    if "any number of cards named" in texto:
        return None
    if "up to seven cards named" in texto:
        return 7
    if "up to nine cards named" in texto:
        return 9
    return limite_padrao


# ---------------------------------------------------------------------------
# Comandante e identidade de cor
# ---------------------------------------------------------------------------

def formatar_cores(cores):
    """Converte um conjunto de letras de cor em texto legível, na ordem WUBRG."""
    if not cores:
        return "Incolor"
    ordenadas = sorted(cores, key=lambda c: ORDEM_CORES.index(c) if c in ORDEM_CORES else 99)
    return ", ".join(NOMES_CORES.get(c, c) for c in ordenadas)


def obter_comandantes(entradas):
    """Retorna as entradas que estão na zona de comandante."""
    return [e for e in entradas if e["zona"] == "commander"]


def identidade_do_comandante(comandantes):
    """
    Identidade de cor do(s) comandante(s): união das identidades de cada um
    (cobre Partner). Retorna None se não houver comandante declarado.
    """
    if not comandantes:
        return None
    cores = set()
    for e in comandantes:
        cores.update(e["data"].get("color_identity", []))
    return cores


def cartas_fora_da_identidade(entradas, cores_cmd):
    """Lista [(nome, {cores_fora})] das cartas cuja identidade não cabe no comandante."""
    fora = {}
    for e in entradas:
        nome = e["data"]["name"]
        excedentes = set(e["data"].get("color_identity", [])) - cores_cmd
        if excedentes:
            fora[nome] = excedentes
    return sorted(fora.items())


def pode_ser_comandante(data, permitir_planeswalker):
    """Confere se uma carta pode ser comandante."""
    tipo = data.get("type_line", "")
    if "can be your commander" in obter_texto_oracle(data):
        return True
    return "Legendary" in tipo and (
        "Creature" in tipo
        or "Vehicle" in tipo
        or "Spacecraft" in tipo
        or ("Planeswalker" in tipo and permitir_planeswalker)
    )


def validar_comandante(comandantes, regras):
    """Confere se as cartas declaradas como comandante podem ser comandante."""
    problemas = []

    total = sum(e["qtd"] for e in comandantes)
    if total > 2:
        problemas.append(f"{total} comandantes declarados (máximo: 2, apenas com Partner)")

    for e in comandantes:
        if not pode_ser_comandante(e["data"], regras.get("cmd_planeswalker")):
            problemas.append(f"{e['data']['name']}: não pode ser comandante neste formato")

    return problemas


def inferir_comandante(entradas):
    """
    Alguns exports (ex.: formato Moxfield) colocam o comandante no sideboard.
    Se nenhum comandante foi declarado e o sideboard tem só 1 ou 2 cartas que podem
    ser comandante, num deck de 60 ou 100 cartas no total, elas viram o comandante.
    Retorna (entradas, aviso); aviso é None se nada foi inferido.
    """
    if obter_comandantes(entradas):
        return entradas, None

    candidatos = [e for e in entradas if e["zona"] == "side"]
    total_side = sum(e["qtd"] for e in candidatos)
    total_geral = sum(e["qtd"] for e in entradas)

    if total_side not in (1, 2) or total_geral not in (60, 100):
        return entradas, None
    if not all(pode_ser_comandante(e["data"], True) for e in candidatos):
        return entradas, None

    for e in candidatos:
        e["zona"] = "commander"
    nomes = " + ".join(e["data"]["name"] for e in candidatos)
    return entradas, (f"Comandante não declarado: usando a carta do sideboard "
                      f"como comandante ({nomes}).")


# ---------------------------------------------------------------------------
# Validação de formatos
# ---------------------------------------------------------------------------

def validar_formato(chave, regras, agregadas, total_main, total_side, comandantes, entradas):
    """Retorna (problemas, avisos)."""
    problemas = []
    avisos = []

    # Tamanho do deck principal (sideboard não conta)
    if total_main < regras["min"]:
        problemas.append(f"Deck com {total_main} cartas (mínimo: {regras['min']})")
    if regras["max"] and total_main > regras["max"]:
        problemas.append(f"Deck com {total_main} cartas (máximo: {regras['max']})")

    # Tamanho do sideboard
    if regras.get("side") is not None and total_side > regras["side"]:
        problemas.append(f"Sideboard com {total_side} cartas (máximo: {regras['side']})")

    # Comandante e identidade de cor
    if regras.get("identidade_cor"):
        if not comandantes:
            avisos.append("comandante não informado: identidade de cor não verificada "
                          "(use a seção 'Commander' no deck.txt)")
        else:
            problemas.extend(validar_comandante(comandantes, regras))

            cores_cmd = identidade_do_comandante(comandantes)
            fora = cartas_fora_da_identidade(entradas, cores_cmd)
            if fora:
                exemplos = ", ".join(nome for nome, _ in fora[:3])
                extra = f" e mais {len(fora) - 3}" if len(fora) > 3 else ""
                problemas.append(
                    f"{len(fora)} carta(s) fora da identidade de cor do comandante "
                    f"({formatar_cores(cores_cmd)}): {exemplos}{extra}"
                )

    # Legalidade e cópias
    for nome, item in agregadas.items():
        data = item["data"]
        qtd_total = item["main"] + item["side"]  # o limite de cópias vale para main + side
        status = data.get("legalities", {}).get(chave, "not_legal")

        if status == "banned":
            problemas.append(f"{nome}: banida")
            continue
        if status == "not_legal":
            problemas.append(f"{nome}: não é legal neste formato")
            continue

        limite = limite_de_copias(data, regras["copias"])
        if status == "restricted":
            limite = 1 if limite is None else min(limite, 1)

        if limite is not None and qtd_total > limite:
            problemas.append(f"{nome}: {qtd_total} cópias somando main e sideboard (máximo: {limite})")

    return problemas, avisos

def validar_deck(entradas, houve_falhas=False):
    """Valida o deck em todos os formatos. Retorna um dicionário (nada é impresso)."""
    # Junta linhas repetidas da mesma carta, separando main e sideboard
    agregadas = {}
    for e in entradas:
        data = e["data"]
        nome = data["name"]
        if nome not in agregadas:
            agregadas[nome] = {"main": 0, "side": 0, "data": data}
        chave_zona = "side" if e["zona"] == "side" else "main"
        agregadas[nome][chave_zona] += e["qtd"]

    total_main = sum(i["main"] for i in agregadas.values())
    total_side = sum(i["side"] for i in agregadas.values())
    comandantes = obter_comandantes(entradas)

    formatos = []
    for chave, regras in FORMATOS.items():
        problemas, avisos = validar_formato(
            chave, regras, agregadas, total_main, total_side, comandantes, entradas
        )
        formatos.append({
            "chave": chave,
            "nome": regras["nome"],
            "valido": not problemas,
            "problemas": problemas,
            "avisos": avisos,
        })

    return {
        "formatos": formatos,
        "validos": [f["nome"] for f in formatos if f["valido"]],
        "houve_falhas": houve_falhas,
    }


# ---------------------------------------------------------------------------
# Análise (retorna dados) e relatório (imprime no terminal)
# ---------------------------------------------------------------------------

def _ordenar_cores(cores):
    return sorted(cores, key=lambda c: ORDEM_CORES.index(c) if c in ORDEM_CORES else 99)


def secao_comandante(entradas):
    """Dados do comandante e das cartas fora da identidade. None se não há comandante."""
    comandantes = obter_comandantes(entradas)
    if not comandantes:
        return None

    cores_cmd = identidade_do_comandante(comandantes)
    fora = cartas_fora_da_identidade(entradas, cores_cmd)
    return {
        "nomes": [e["data"]["name"] for e in comandantes],
        "identidade": _ordenar_cores(cores_cmd),
        "identidade_texto": formatar_cores(cores_cmd),
        "incolor": not cores_cmd,
        "fora_da_identidade": [
            {"nome": nome, "cores": _ordenar_cores(exc), "cores_texto": formatar_cores(exc)}
            for nome, exc in fora
        ],
    }


def _preco_usd(data):
    prices = data.get("prices", {})
    valor = prices.get("usd") or prices.get("usd_foil")
    return float(valor) if valor else None


def cores_da_carta(data):
    """
    Retorna (cores_custo, cores_identidade), listas na ordem WUBRG.
      - cores_custo: cores da carta em si (custo de mana; em dupla face, a face da frente)
      - cores_identidade: identidade de cor da Scryfall, que também inclui símbolos de mana
        escritos nas habilidades (ex.: "{T}: Add {G}" ou "{2}{R}: ...") e a outra face
    """
    custo = data.get("colors")
    if not custo and data.get("card_faces"):
        custo = data["card_faces"][0].get("colors", [])
    return (_ordenar_cores(set(custo or [])),
            _ordenar_cores(set(data.get("color_identity", []))))


def distribuicao_cores(principal, base="identidade", incluir_terrenos=True):
    """
    Conta quantas cartas do deck principal têm cada cor.
      base="custo":      usa só as cores do custo de mana
      base="identidade": usa a identidade de cor (custo + habilidades + outra face)
    Cartas sem cor entram em "Incolor". Carta multicolorida conta em cada uma das suas cores.
    """
    campo = "cores_custo" if base == "custo" else "cores_identidade"
    contagem = {}
    for e in principal:
        if e["terreno"] and not incluir_terrenos:
            continue
        for cor in (e[campo] or ["Incolor"]):
            contagem[cor] = contagem.get(cor, 0) + e["qtd"]
    ordem = list(ORDEM_CORES) + ["Incolor"]
    return {cor: contagem[cor] for cor in ordem if cor in contagem}


def analisar_deck(cartas, nome_deck=None, progresso=None):
    """
    Analisa o deck e devolve um dicionário com todos os resultados.

    'progresso' é uma função opcional que recebe uma mensagem (str) a cada etapa
    demorada (ex.: busca individual de uma carta). Levanta ErroScryfall se a API
    estiver inacessível.
    """
    def avisar_progresso(msg):
        if progresso:
            progresso(msg)

    avisos = []
    total_cartas = 0
    curva_mana = {}
    preco_total = 0.0
    tipos_count = {"Creature": 0, "Instant": 0, "Sorcery": 0, "Land": 0,
                   "Artifact": 0, "Enchantment": 0, "Planeswalker": 0}

    avisar_progresso("Buscando informações do deck na Scryfall API...")
    dados_por_nome, nao_encontrados = buscar_cartas_em_lote(cartas, avisos)

    # Executa o Plano B para qualquer carta que tenha falhado no lote
    for chave, nome in nao_encontrados.items():
        avisar_progresso(f"Tentando busca individual (fallback) para: {nome}...")
        card_data = buscar_carta_fallback(nome)
        if card_data:
            dados_por_nome[chave] = card_data
        time.sleep(0.1)

    # Associa cada linha do deck aos dados da Scryfall
    entradas = []
    falhas_finais = []
    for c in cartas:
        data = (dados_por_nome.get(limpar_nome_carta(c["name"]).lower())
                or dados_por_nome.get(c["name"].lower()))
        if data:
            entradas.append({"qtd": c["qtd"], "zona": c["zona"], "data": data})
        else:
            falhas_finais.append({"qtd": c["qtd"], "nome": c["name"]})

    entradas, aviso_cmd = inferir_comandante(entradas)
    if aviso_cmd:
        avisos.append(aviso_cmd)

    principal_entradas = [e for e in entradas if e["zona"] != "side"]
    sideboard_entradas = [e for e in entradas if e["zona"] == "side"]

    # Preço considera tudo o que você precisa ter (main + sideboard)
    for e in entradas:
        preco = _preco_usd(e["data"])
        if preco:
            preco_total += preco * e["qtd"]

    # Estatísticas do deck principal (sideboard fica de fora)
    principal = []
    for e in principal_entradas:
        qtd, data = e["qtd"], e["data"]
        total_cartas += qtd

        # 1. Curva de Mana (CMC)
        cmc = int(data.get("cmc", 0))
        curva_mana[cmc] = curva_mana.get(cmc, 0) + qtd

        # 2. Cores: do custo de mana e da identidade (inclui símbolos nas habilidades)
        cores_custo, cores_identidade = cores_da_carta(data)

        # 3. Tipos de carta (em dupla face, considera apenas a face da frente)
        type_frente = data.get("type_line", "").split("//")[0]
        for tipo in tipos_count:
            if tipo in type_frente:
                tipos_count[tipo] += qtd

        principal.append({
            "qtd": qtd,
            "nome": data.get("name"),
            "cmc": cmc,
            "tipo": data.get("type_line", ""),
            "terreno": "Land" in type_frente,
            "comandante": e["zona"] == "commander",
            "preco": _preco_usd(data),
            "cores_custo": cores_custo,
            "cores_identidade": cores_identidade,
            # cores que a carta só tem por causa das habilidades (ou da outra face)
            "cores_habilidade": [c for c in cores_identidade if c not in cores_custo],
        })

    sideboard = [{
        "qtd": e["qtd"],
        "nome": e["data"].get("name"),
        "cmc": int(e["data"].get("cmc", 0)),
        "tipo": e["data"].get("type_line", ""),
        "preco": _preco_usd(e["data"]),
        "cores_identidade": cores_da_carta(e["data"])[1],
    } for e in sideboard_entradas]

    return {
        "versao": VERSAO,
        "nome_deck": nome_deck,
        "total_main": total_cartas,
        "total_side": sum(e["qtd"] for e in sideboard),
        "preco_total": preco_total,
        "curva_mana": curva_mana,
        "cores": distribuicao_cores(principal, "identidade"),
        "cores_custo": distribuicao_cores(principal, "custo"),
        "tipos": tipos_count,
        "principal": principal,
        "sideboard": sideboard,
        "falhas": falhas_finais,
        "avisos": avisos,
        "comandante": secao_comandante(entradas),
        "validacao": validar_deck(entradas, houve_falhas=bool(falhas_finais)),
    }


NOMES_TIPOS = {
    "Creature": "Criaturas",
    "Instant": "Mágicas Instantâneas",
    "Sorcery": "Feitiços",
    "Land": "Terrenos",
    "Artifact": "Artefatos",
    "Enchantment": "Encantamentos",
    "Planeswalker": "Planeswalkers",
}


def imprimir_relatorio(res):
    """Imprime no terminal o relatório a partir do dicionário de analisar_deck()."""
    for aviso in res["avisos"]:
        print(f"ℹ️ {aviso}")
    if res["avisos"]:
        print()

    for e in res["principal"]:
        marca = " [Comandante]" if e["comandante"] else ""
        print(f"✅ {e['qtd']}x {e['nome']} (CMC: {e['cmc']}){marca}")
    for e in res["sideboard"]:
        print(f"🗂️ {e['qtd']}x {e['nome']} (CMC: {e['cmc']}) [Sideboard]")

    if res["falhas"]:
        print("\n" + "x" * 40)
        for f in res["falhas"]:
            print(f"❌ Carta realmente não encontrada na API: {f['qtd']}x {f['nome']}")

    print("\n" + "=" * 40)
    print(f"📊 RELATÓRIO DO DECK (V{res['versao']})")
    print("=" * 40)
    if res["nome_deck"]:
        print(f"Deck: {res['nome_deck']}")
    print(f"Total de cartas (deck principal): {res['total_main']}")
    print(f"Total de cartas (sideboard): {res['total_side']}")
    print(f"Preço estimado total (main + sideboard): ${res['preco_total']:.2f} USD")

    print("\n📈 Curva de Mana (CMC):")
    for cmc in sorted(res["curva_mana"]):
        barras = "█" * res["curva_mana"][cmc]
        print(f"  CMC {cmc}: {barras} ({res['curva_mana'][cmc]})")

    print("\n🎨 Distribuição de Cores (por custo de mana):")
    for cor, count in res["cores_custo"].items():
        print(f"  {NOMES_CORES.get(cor, cor)}: {count} cartas")

    print("\n🎨 Distribuição de Cores (por identidade: custo + habilidades):")
    for cor, count in res["cores"].items():
        print(f"  {NOMES_CORES.get(cor, cor)}: {count} cartas")

    print("\n🃏 Tipos de Carta:")
    for tipo, count in res["tipos"].items():
        print(f"  {NOMES_TIPOS[tipo]}: {count}")

    cmd = res["comandante"]
    if cmd:
        print("\n👑 Comandante e Identidade de Cor:")
        print(f"  Comandante: {' + '.join(cmd['nomes'])}")
        print(f"  Identidade de cor: {cmd['identidade_texto']}")
        if cmd["incolor"]:
            print("  ℹ️ Comandante incolor: só entram cartas sem identidade de cor "
                  "(ex.: artefatos incolores, Wastes).")
        fora = cmd["fora_da_identidade"]
        if not fora:
            print("  ✅ Todas as cartas respeitam a identidade de cor do comandante.")
        else:
            print(f"  ❌ {len(fora)} carta(s) fora da identidade de cor:")
            for f in fora:
                print(f"     - {f['nome']} (cores não permitidas: {f['cores_texto']})")

    print(f"\n🗂️ Sideboard ({res['total_side']} cartas):")
    if not res["sideboard"]:
        print("  (vazio)")
    for e in sorted(res["sideboard"], key=lambda e: (e["cmc"], e["nome"])):
        print(f"  {e['qtd']}x {e['nome']} (CMC: {e['cmc']}) - {e['tipo']}")

    val = res["validacao"]
    print("\n" + "=" * 40)
    print("✔️  VALIDAÇÃO DE FORMATOS")
    print("=" * 40)
    if val["houve_falhas"]:
        print("⚠️ Algumas cartas não foram encontradas; o resultado pode estar incorreto.\n")

    for f in val["formatos"]:
        if f["valido"]:
            print(f"✅ {f['nome']}")
        else:
            print(f"❌ {f['nome']} ({len(f['problemas'])} problema(s))")
            for p in f["problemas"][:3]:
                print(f"     - {p}")
            if len(f["problemas"]) > 3:
                print(f"     ... e mais {len(f['problemas']) - 3}")
        for aviso in f["avisos"]:
            print(f"     ⚠️ {aviso}")

    print()
    if val["validos"]:
        print(f"🏆 Deck válido em: {', '.join(val['validos'])}")
    else:
        print("🚫 O deck não é válido em nenhum dos formatos verificados.")


if __name__ == "__main__":
    try:
        deck, nome_deck, avisos_leitura = carregar_deck("deck.txt")
    except FileNotFoundError:
        print("Erro: O arquivo 'deck.txt' não foi encontrado.")
    else:
        for aviso in avisos_leitura:
            print(f"ℹ️ {aviso}")
        if deck:
            try:
                resultado = analisar_deck(deck, nome_deck, progresso=lambda m: print(f"🔄 {m}"))
            except ErroScryfall as erro:
                print(f"❌ {erro}")
            else:
                print()
                imprimir_relatorio(resultado)
