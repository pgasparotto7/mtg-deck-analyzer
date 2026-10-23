import time
import requests

API_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
API_FUZZY_URL = "https://api.scryfall.com/cards/named?fuzzy="

HEADERS = {
    "User-Agent": "MTGDeckAnalyzer/1.0",
    "Accept": "application/json"
}

# Chaves = nomes usados no campo "legalities" da Scryfall
FORMATOS = {
    "standard":      {"nome": "Standard",       "min": 60,  "max": None, "copias": 4},
    "alchemy":       {"nome": "Alchemy",        "min": 60,  "max": None, "copias": 4},
    "pioneer":       {"nome": "Explorer",       "min": 60,  "max": None, "copias": 4},
    "historic":      {"nome": "Historic",       "min": 60,  "max": None, "copias": 4},
    "timeless":      {"nome": "Timeless",       "min": 60,  "max": None, "copias": 4},
    "modern":        {"nome": "Modern",         "min": 60,  "max": None, "copias": 4},
    "legacy":        {"nome": "Legacy",         "min": 60,  "max": None, "copias": 4},
    "pauper":        {"nome": "Pauper",         "min": 60,  "max": None, "copias": 4},
    "penny":         {"nome": "Penny Dreadful", "min": 60,  "max": None, "copias": 4},
    "standardbrawl": {"nome": "Brawl",          "min": 60,  "max": 60,   "copias": 1},
    "brawl":         {"nome": "Historic Brawl", "min": 100, "max": 100,  "copias": 1},
    "commander":     {"nome": "Commander",      "min": 100, "max": 100,  "copias": 1},
}

def carregar_deck(caminho_arquivo):
    cartas = []
    try:
        with open(caminho_arquivo, "r", encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#"):
                    continue
                partes = linha.split(" ", 1)
                if len(partes) == 2 and partes[0].isdigit():
                    qtd = int(partes[0])
                    nome = partes[1].strip()
                    cartas.append({"qtd": qtd, "name": nome})
                else:
                    cartas.append({"qtd": 1, "name": linha})
    except FileNotFoundError:
        print(f"Erro: O arquivo '{caminho_arquivo}' não foi encontrado.")
    return cartas

def limpar_nome_carta(nome):
    """Se a carta tiver '//' (dupla face), pega apenas a face principal da frente."""
    if "//" in nome:
        return nome.split("//")[0].strip()
    return nome

def buscar_cartas_em_lote(cartas):
    resultados = []
    nao_encontradas = []

    # Mapeamento para associar a quantidade original do deck.txt
    mapa_qtd = {}
    payload_identifiers = []

    for c in cartas:
        nome_limpo = limpar_nome_carta(c["name"])
        mapa_qtd[nome_limpo.lower()] = c["qtd"]
        mapa_qtd[c["name"].lower()] = c["qtd"]  # guarda nome original também
        payload_identifiers.append({"name": nome_limpo})

    # Envia em lotes de até 75 cartas
    lote_tamanho = 75
    for i in range(0, len(payload_identifiers), lote_tamanho):
        fatia_payload = payload_identifiers[i:i + lote_tamanho]
        fatia_originais = cartas[i:i + lote_tamanho]

        response = requests.post(API_COLLECTION_URL, json={"identifiers": fatia_payload}, headers=HEADERS)

        if response.status_code == 200:
            data = response.json()
            
            for card in data.get("data", []):
                nome_carta = card.get("name")
                # Tenta buscar a quantidade pelo nome retornado ou pela face
                nome_frente = nome_carta.split("//")[0].strip().lower()
                qtd = mapa_qtd.get(nome_frente, mapa_qtd.get(nome_carta.lower(), 1))
                resultados.append((qtd, card))

            for not_found in data.get("not_found", []):
                # Guarda o item que falhou para tentarmos o Plano B
                nome_falhou = not_found.get("name")
                # Acha o objeto original correspondente
                orig = next((c for c in fatia_originais if limpar_nome_carta(c["name"]).lower() == nome_falhou.lower()), None)
                if orig:
                    nao_encontradas.append(orig)
                else:
                    nao_encontradas.append({"qtd": 1, "name": nome_falhou})

        else:
            print(f"⚠️ Erro no servidor da API: Status {response.status_code}")

    return resultados, nao_encontradas

def buscar_carta_fallback(nome_carta):
    """Plano B: Busca individual utilizando o parâmetro 'fuzzy' caso o lote falhe."""
    nome_limpo = limpar_nome_carta(nome_carta)
    response = requests.get(f"{API_FUZZY_URL}{nome_limpo}", headers=HEADERS)
    if response.status_code == 200:
        return response.json()
    return None

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

def validar_formato(chave, regras, agregadas, total_cartas):
    problemas = []

    if total_cartas < regras["min"]:
        problemas.append(f"Deck com {total_cartas} cartas (mínimo: {regras['min']})")
    if regras["max"] and total_cartas > regras["max"]:
        problemas.append(f"Deck com {total_cartas} cartas (máximo: {regras['max']})")

    for nome, item in agregadas.items():
        qtd, data = item["qtd"], item["data"]
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

        if limite is not None and qtd > limite:
            problemas.append(f"{nome}: {qtd} cópias (máximo: {limite})")

    return problemas

def validar_deck(cartas_encontradas, total_cartas, houve_falhas=False):
    # Junta linhas repetidas da mesma carta
    agregadas = {}
    for qtd, data in cartas_encontradas:
        nome = data["name"]
        if nome in agregadas:
            agregadas[nome]["qtd"] += qtd
        else:
            agregadas[nome] = {"qtd": qtd, "data": data}

    print("\n" + "=" * 40)
    print("✔️  VALIDAÇÃO DE FORMATOS")
    print("=" * 40)

    if houve_falhas:
        print("⚠️ Algumas cartas não foram encontradas; o resultado pode estar incorreto.\n")

    formatos_validos = []
    for chave, regras in FORMATOS.items():
        problemas = validar_formato(chave, regras, agregadas, total_cartas)
        if not problemas:
            formatos_validos.append(regras["nome"])
            print(f"✅ {regras['nome']}")
        else:
            print(f"❌ {regras['nome']} ({len(problemas)} problema(s))")
            for p in problemas[:3]:
                print(f"     - {p}")
            if len(problemas) > 3:
                print(f"     ... e mais {len(problemas) - 3}")

    print()
    if formatos_validos:
        print(f"🏆 Deck válido em: {', '.join(formatos_validos)}")
    else:
        print("🚫 O deck não é válido em nenhum dos formatos verificados.")

def analisar_deck(cartas):
    total_cartas = 0
    curva_mana = {}
    cores_count = {}
    preco_total = 0.0
    tipos_count = {"Creature": 0, "Instant": 0, "Sorcery": 0, "Land": 0, "Artifact": 0, "Enchantment": 0, "Planeswalker": 0}


    print("🔎 Buscando informações do deck na Scryfall API...\n")

    cartas_encontradas, nao_encontradas = buscar_cartas_em_lote(cartas)

    # Executa o Plano B para qualquer carta que tenha falhado no lote
    falhas_finais = []
    if nao_encontradas:
        for item in nao_encontradas:
            qtd = item["qtd"]
            nome = item["name"]
            print(f"🔄 Tentando busca individual (fallback) para: {nome}...")
            card_data = buscar_carta_fallback(nome)
            if card_data:
                cartas_encontradas.append((qtd, card_data))
            else:
                falhas_finais.append((qtd, nome))
            time.sleep(0.1)

    for qtd, data in cartas_encontradas:
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

        # 3. Preço estimado (USD)
        prices = data.get("prices", {})
        usd_price = prices.get("usd") or prices.get("usd_foil")
        if usd_price:
            preco_total += float(usd_price) * qtd

        # 4. Tipos de carta
        type_line = data.get("type_line", "")
        # Em cartas dupla face, considera apenas a face da frente
        type_frente = type_line.split("//")[0]
        for tipo in tipos_count:
            if tipo in type_frente:
                tipos_count[tipo] += qtd   

        print(f"✅ {qtd}x {nome} (CMC: {cmc})")

    if falhas_finais:
        print("\n" + "x" * 40)
        for qtd, nome_falha in falhas_finais:
            print(f"❌ Carta realmente não encontrada na API: {qtd}x {nome_falha}")

    print("\n" + "=" * 40)
    print("📊 RELATÓRIO DO DECK")
    print("=" * 40)
    print(f"Total de cartas: {total_cartas}")
    print(f"Preço estimado total: ${preco_total:.2f} USD")
    
    print("\n📈 Curva de Mana (CMC):")
    for cmc in sorted(curva_mana.keys()):
        barras = "█" * curva_mana[cmc]
        print(f"  CMC {cmc}: {barras} ({curva_mana[cmc]})")

    print("\n🎨 Distribuição de Cores:")
    mapa_nomes_cores = {'W': 'Branco (W)', 'U': 'Azul (U)', 'B': 'Preto (B)', 'R': 'Vermelho (R)', 'G': 'Verde (G)'}
    for cor, count in cores_count.items():
        nome_exibicao = mapa_nomes_cores.get(cor, cor)
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

    validar_deck(cartas_encontradas, total_cartas, houve_falhas=bool(falhas_finais))    

if __name__ == "__main__":
    deck = carregar_deck("deck.txt")
    if deck:
        analisar_deck(deck)