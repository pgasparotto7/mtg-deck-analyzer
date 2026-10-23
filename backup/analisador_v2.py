"""
MTG Deck Analyzer V2

Novidades da V2:
  - Validação de identidade de cor do comandante (Commander, Brawl e Historic Brawl)
  - Verificação se o comandante pode, de fato, ser comandante
  - Seção "Comandante" no relatório, listando cartas fora da identidade de cor
"""
import re
import time
import requests

VERSAO = "2.1"

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


def carregar_deck(caminho_arquivo):
    """
    Lê o deck.txt e marca a zona de cada carta: "main", "commander" ou "side".
    Retorna (cartas, nome_deck).

    Formatos aceitos (Moxfield, MTG Arena, MTGO e texto simples):
      - Seções por cabeçalho: About / Commander / Companion / Deck / Sideboard / Maybeboard
        (com ou sem '//', '#', '==' ou ':' -- ex.: 'SIDEBOARD:')
      - Prefixo 'SB:' em linhas individuais
      - SEM seções: uma linha em branco separa o deck principal do sideboard
      - Quantidade '4', '4x' ou ausente; '(SET) 123' e marcador de foil são ignorados
      - Tags do Archidekt: [Commander], [Sideboard], [Maybeboard]
    Maybeboard é ignorado.
    """
    try:
        with open(caminho_arquivo, "r", encoding="utf-8-sig") as f:
            linhas = [l.strip() for l in f]
    except FileNotFoundError:
        print(f"Erro: O arquivo '{caminho_arquivo}' não foi encontrado.")
        return [], None

    # O arquivo tem seções explícitas? Se não, a linha em branco separa main e sideboard.
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
        print(f"ℹ️ {ignoradas} linha(s) ignorada(s) (Maybeboard ou blocos extras).")
    return cartas, nome_deck


def limpar_nome_carta(nome):
    """Se a carta tiver '//' (dupla face), pega apenas a face principal da frente."""
    if "//" in nome:
        return nome.split("//")[0].strip()
    return nome


# ---------------------------------------------------------------------------
# Scryfall
# ---------------------------------------------------------------------------

def buscar_cartas_em_lote(cartas):
    """
    Busca cada carta ÚNICA uma vez (mesmo que apareça no main e no sideboard).
    Retorna:
      - dados_por_nome: {nome_em_minusculas: dados_da_scryfall}
      - nao_encontrados: {nome_em_minusculas: nome_limpo} das cartas que falharam
    """
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
        response = requests.post(API_COLLECTION_URL, json={"identifiers": fatia}, headers=HEADERS)

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
            print(f"⚠️ Erro no servidor da API: Status {response.status_code}")
            # Marca o lote inteiro para tentar o Plano B
            for ident in fatia:
                nao_encontrados[ident["name"].lower()] = ident["name"]

    return dados_por_nome, nao_encontrados


def buscar_carta_fallback(nome_carta):
    """Plano B: Busca individual utilizando o parâmetro 'fuzzy' caso o lote falhe."""
    nome_limpo = limpar_nome_carta(nome_carta)
    response = requests.get(API_FUZZY_URL, params={"fuzzy": nome_limpo}, headers=HEADERS)
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
    """
    if obter_comandantes(entradas):
        return entradas

    candidatos = [e for e in entradas if e["zona"] == "side"]
    total_side = sum(e["qtd"] for e in candidatos)
    total_geral = sum(e["qtd"] for e in entradas)

    if total_side not in (1, 2) or total_geral not in (60, 100):
        return entradas
    if not all(pode_ser_comandante(e["data"], True) for e in candidatos):
        return entradas

    for e in candidatos:
        e["zona"] = "commander"
    nomes = " + ".join(e["data"]["name"] for e in candidatos)
    print(f"ℹ️ Comandante não declarado: usando a carta do sideboard como comandante ({nomes}).\n")
    return entradas


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

    print("\n" + "=" * 40)
    print("✔️  VALIDAÇÃO DE FORMATOS")
    print("=" * 40)

    if houve_falhas:
        print("⚠️ Algumas cartas não foram encontradas; o resultado pode estar incorreto.\n")

    formatos_validos = []
    for chave, regras in FORMATOS.items():
        problemas, avisos = validar_formato(
            chave, regras, agregadas, total_main, total_side, comandantes, entradas
        )
        if not problemas:
            formatos_validos.append(regras["nome"])
            print(f"✅ {regras['nome']}")
        else:
            print(f"❌ {regras['nome']} ({len(problemas)} problema(s))")
            for p in problemas[:3]:
                print(f"     - {p}")
            if len(problemas) > 3:
                print(f"     ... e mais {len(problemas) - 3}")
        for aviso in avisos:
            print(f"     ⚠️ {aviso}")

    print()
    if formatos_validos:
        print(f"🏆 Deck válido em: {', '.join(formatos_validos)}")
    else:
        print("🚫 O deck não é válido em nenhum dos formatos verificados.")


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def imprimir_secao_comandante(entradas):
    """Mostra comandante, identidade de cor e cartas fora da identidade."""
    comandantes = obter_comandantes(entradas)
    if not comandantes:
        return

    cores_cmd = identidade_do_comandante(comandantes)
    nomes = " + ".join(e["data"]["name"] for e in comandantes)

    print("\n👑 Comandante e Identidade de Cor:")
    print(f"  Comandante: {nomes}")
    print(f"  Identidade de cor: {formatar_cores(cores_cmd)}")

    fora = cartas_fora_da_identidade(entradas, cores_cmd)
    if not fora:
        print("  ✅ Todas as cartas respeitam a identidade de cor do comandante.")
    else:
        print(f"  ❌ {len(fora)} carta(s) fora da identidade de cor:")
        for nome, excedentes in fora:
            print(f"     - {nome} (cores não permitidas: {formatar_cores(excedentes)})")


def analisar_deck(cartas, nome_deck=None):
    total_cartas = 0
    curva_mana = {}
    cores_count = {}
    preco_total = 0.0
    tipos_count = {"Creature": 0, "Instant": 0, "Sorcery": 0, "Land": 0,
                   "Artifact": 0, "Enchantment": 0, "Planeswalker": 0}

    print("🔎 Buscando informações do deck na Scryfall API...\n")

    dados_por_nome, nao_encontrados = buscar_cartas_em_lote(cartas)

    # Executa o Plano B para qualquer carta que tenha falhado no lote
    for chave, nome in nao_encontrados.items():
        print(f"🔄 Tentando busca individual (fallback) para: {nome}...")
        card_data = buscar_carta_fallback(nome)
        if card_data:
            dados_por_nome[chave] = card_data
        time.sleep(0.1)

    # Associa cada linha do deck.txt aos dados da Scryfall
    entradas = []
    falhas_finais = []
    for c in cartas:
        data = (dados_por_nome.get(limpar_nome_carta(c["name"]).lower())
                or dados_por_nome.get(c["name"].lower()))
        if data:
            entradas.append({"qtd": c["qtd"], "zona": c["zona"], "data": data})
        else:
            falhas_finais.append(c)

    entradas = inferir_comandante(entradas)

    principal = [e for e in entradas if e["zona"] != "side"]
    sideboard = [e for e in entradas if e["zona"] == "side"]

    # Preço considera tudo o que você precisa ter (main + sideboard)
    for e in entradas:
        prices = e["data"].get("prices", {})
        usd_price = prices.get("usd") or prices.get("usd_foil")
        if usd_price:
            preco_total += float(usd_price) * e["qtd"]

    # Estatísticas do deck principal (sideboard fica de fora)
    for e in principal:
        qtd, data = e["qtd"], e["data"]
        nome = data.get("name")
        total_cartas += qtd

        # 1. Curva de Mana (CMC)
        cmc = int(data.get("cmc", 0))
        curva_mana[cmc] = curva_mana.get(cmc, 0) + qtd

        # 2. Cores
        cores = data.get("colors", [])
        if not cores and "card_faces" in data:
            # Em cartas dupla face, pega as cores da primeira face se estiver vazio
            cores = data["card_faces"][0].get("colors", [])
        if not cores:
            cores = data.get("color_identity", [])

        if not cores:
            cores_count["Incolor/Terreno"] = cores_count.get("Incolor/Terreno", 0) + qtd
        else:
            for cor in cores:
                cores_count[cor] = cores_count.get(cor, 0) + qtd

        # 3. Tipos de carta (em dupla face, considera apenas a face da frente)
        type_frente = data.get("type_line", "").split("//")[0]
        for tipo in tipos_count:
            if tipo in type_frente:
                tipos_count[tipo] += qtd

        marca = " [Comandante]" if e["zona"] == "commander" else ""
        print(f"✅ {qtd}x {nome} (CMC: {cmc}){marca}")

    for e in sideboard:
        print(f"🗂️ {e['qtd']}x {e['data'].get('name')} (CMC: {int(e['data'].get('cmc', 0))}) [Sideboard]")

    if falhas_finais:
        print("\n" + "x" * 40)
        for c in falhas_finais:
            print(f"❌ Carta realmente não encontrada na API: {c['qtd']}x {c['name']}")

    total_side = sum(e["qtd"] for e in sideboard)

    print("\n" + "=" * 40)
    print(f"📊 RELATÓRIO DO DECK (V{VERSAO})")
    print("=" * 40)
    if nome_deck:
        print(f"Deck: {nome_deck}")
    print(f"Total de cartas (deck principal): {total_cartas}")
    print(f"Total de cartas (sideboard): {total_side}")
    print(f"Preço estimado total (main + sideboard): ${preco_total:.2f} USD")

    print("\n📈 Curva de Mana (CMC):")
    for cmc in sorted(curva_mana.keys()):
        barras = "█" * curva_mana[cmc]
        print(f"  CMC {cmc}: {barras} ({curva_mana[cmc]})")

    print("\n🎨 Distribuição de Cores:")
    for cor, count in cores_count.items():
        nome_exibicao = NOMES_CORES.get(cor, cor)
        print(f"  {nome_exibicao}: {count} cartas")

    print("\n🃏 Tipos de Carta:")
    nomes_tipos = {
        "Creature": "Criaturas",
        "Instant": "Mágicas Instantâneas",
        "Sorcery": "Feitiços",
        "Land": "Terrenos",
        "Artifact": "Artefatos",
        "Enchantment": "Encantamentos",
        "Planeswalker": "Planeswalkers",
    }
    for tipo, count in tipos_count.items():
        print(f"  {nomes_tipos[tipo]}: {count}")

    imprimir_secao_comandante(entradas)

    print(f"\n🗂️ Sideboard ({total_side} cartas):")
    if not sideboard:
        print("  (vazio)")
    for e in sorted(sideboard, key=lambda e: (int(e["data"].get("cmc", 0)), e["data"]["name"])):
        data = e["data"]
        print(f"  {e['qtd']}x {data['name']} (CMC: {int(data.get('cmc', 0))}) - {data.get('type_line', '')}")

    validar_deck(entradas, houve_falhas=bool(falhas_finais))


if __name__ == "__main__":
    deck, nome_deck = carregar_deck("deck.txt")
    if deck:
        analisar_deck(deck, nome_deck)
