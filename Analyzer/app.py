"""
Interface Streamlit do MTG Deck Analyzer.

Para rodar:  streamlit run app.py
"""
import pandas as pd
import streamlit as st

from analisador import (
    NOMES_CORES,
    NOMES_TIPOS,
    VERSAO,
    ErroScryfall,
    analisar_deck,
    carregar_deck_de_texto,
    distribuicao_cores,
)

st.set_page_config(page_title="MTG - Deck Analyzer", page_icon="🃏", layout="wide")

PLACEHOLDER = """EX:
Commander
1 Krenko, Mob Boss

Deck
1 Lightning Bolt
1 Sol Ring
35 Mountain
..."""


@st.cache_data(show_spinner=False, ttl=1800)
def analisar_em_cache(cartas, nome_deck):
    """Guarda o resultado por 30 min: reanalisar o mesmo deck não refaz as consultas."""
    return analisar_deck(cartas, nome_deck)


def texto_identidade(cores):
    """['R', 'G'] -> 'RG'; lista vazia -> 'Incolor'."""
    return "".join(cores) if cores else "Incolor"


def tabela_cartas(itens):
    return pd.DataFrame([
        {"Qtd": e["qtd"], "Carta": e["nome"], "CMC": e["cmc"],
         "Identidade": texto_identidade(e["cores_identidade"]),
         "Tipo": e["tipo"], "Preço (USD)": e["preco"]}
        for e in itens
    ])


CONFIG_COLUNAS = {"Preço (USD)": st.column_config.NumberColumn(format="$%.2f")}


def mostrar_resultado(resultado, avisos_leitura):
    st.divider()
    st.header(resultado["nome_deck"] or "Resultado da análise")

    for aviso in avisos_leitura + resultado["avisos"]:
        st.info(aviso)
    if resultado["falhas"]:
        nomes = ", ".join(f"{f['qtd']}x {f['nome']}" for f in resultado["falhas"])
        st.error(f"Cartas não encontradas na Scryfall: {nomes}. "
                 "Confira a grafia; o resultado pode estar incompleto.")

    validacao = resultado["validacao"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Deck principal", resultado["total_main"])
    c2.metric("Sideboard", resultado["total_side"])
    c3.metric("Preço estimado (USD)", f"${resultado['preco_total']:,.2f}")
    c4.metric("Formatos válidos", f"{len(validacao['validos'])} de {len(validacao['formatos'])}")

    nomes_abas = (["Visão geral"]
                  + (["Comandante"] if resultado["comandante"] else [])
                  + ["Formatos", "Cartas"])
    abas = dict(zip(nomes_abas, st.tabs(nomes_abas)))

    # --- Visão geral ---------------------------------------------------------
    with abas["Visão geral"]:
        sem_terrenos = st.checkbox("Ocultar terrenos nos gráficos de curva e cores", value=True)
        col_a, col_b = st.columns(2)

        with col_a:
            st.subheader("Curva de mana")
            curva = {}
            for e in resultado["principal"]:
                if sem_terrenos and e["terreno"]:
                    continue
                curva[e["cmc"]] = curva.get(e["cmc"], 0) + e["qtd"]
            if curva:
                cmcs = list(range(0, max(curva) + 1))
                df_curva = pd.DataFrame({"CMC": cmcs, "Cartas": [curva.get(i, 0) for i in cmcs]})
                st.bar_chart(df_curva, x="CMC", y="Cartas")
            else:
                st.caption("Sem cartas para mostrar.")

        with col_b:
            st.subheader("Distribuição de cores")
            base_cores = st.radio(
                "Contar cores por",
                ["Identidade de cor (custo + habilidades)", "Só custo de mana"],
                horizontal=True,
            )
            base = "identidade" if base_cores.startswith("Identidade") else "custo"
            cores = {NOMES_CORES.get(c, c): n for c, n in
                     distribuicao_cores(resultado["principal"], base, not sem_terrenos).items()}
            if cores:
                st.bar_chart(pd.Series(cores, name="Cartas"))
                st.caption("Carta com mais de uma cor conta em cada uma delas. "
                           "A identidade inclui cores que só aparecem em habilidades.")
            else:
                st.caption("Sem cartas para mostrar.")

            st.subheader("Tipos de carta")
            tipos = {NOMES_TIPOS[t]: n for t, n in resultado["tipos"].items() if n}
            if tipos:
                st.bar_chart(pd.Series(tipos, name="Cartas"))

    # --- Comandante ----------------------------------------------------------
    if resultado["comandante"]:
        cmd = resultado["comandante"]
        with abas["Comandante"]:
            st.markdown(f"**Comandante:** {' + '.join(cmd['nomes'])}")
            st.markdown(f"**Identidade de cor:** {cmd['identidade_texto']}")
            if cmd["incolor"]:
                st.info("Comandante incolor: o deck só pode ter cartas sem identidade de cor "
                        "(artefatos incolores, Wastes etc.). Terrenos básicos coloridos e "
                        "cartas com símbolos de mana de qualquer cor, inclusive em "
                        "habilidades, ficam de fora.")
            fora = cmd["fora_da_identidade"]
            if not fora:
                st.success("Todas as cartas respeitam a identidade de cor do comandante.")
            else:
                st.error(f"{len(fora)} carta(s) fora da identidade de cor do comandante:")
                st.dataframe(
                    pd.DataFrame([{"Carta": f["nome"], "Cores não permitidas": f["cores_texto"]}
                                  for f in fora]),
                    hide_index=True,
                )

    # --- Formatos ------------------------------------------------------------
    with abas["Formatos"]:
        if validacao["houve_falhas"]:
            st.warning("Algumas cartas não foram encontradas; "
                       "os resultados abaixo podem estar incorretos.")
        if validacao["validos"]:
            st.success("Deck válido em: " + ", ".join(validacao["validos"]))
        else:
            st.error("O deck não é válido em nenhum dos formatos verificados.")

        for f in validacao["formatos"]:
            if f["valido"]:
                st.markdown(f"✅ **{f['nome']}**")
                for aviso in f["avisos"]:
                    st.caption(f"⚠️ {aviso}")
            else:
                with st.expander(f"❌ {f['nome']} ({len(f['problemas'])} problema(s))"):
                    for problema in f["problemas"]:
                        st.markdown(f"- {problema}")
                    for aviso in f["avisos"]:
                        st.caption(f"⚠️ {aviso}")

    # --- Cartas --------------------------------------------------------------
    with abas["Cartas"]:
        st.subheader(f"Deck principal ({resultado['total_main']})")
        st.dataframe(tabela_cartas(resultado["principal"]), hide_index=True,
                     column_config=CONFIG_COLUNAS)

        st.subheader(f"Sideboard ({resultado['total_side']})")
        if resultado["sideboard"]:
            st.dataframe(tabela_cartas(resultado["sideboard"]), hide_index=True,
                         column_config=CONFIG_COLUNAS)
        else:
            st.caption("Vazio.")


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------

st.title("MTG - Deck Analyzer")
st.caption("Cole a lista do seu deck (Moxfield, Arena, MTGO ou texto simples) e veja "
           "curva de mana, cores, preço e legalidade em cada formato.")

modo = st.radio("Como quer enviar o deck?", ["Colar a lista", "Enviar arquivo .txt"],
                horizontal=True)

texto = ""
if modo == "Colar a lista":
    texto = st.text_area("Lista do deck", height=280, placeholder=PLACEHOLDER)
else:
    arquivo = st.file_uploader("Arquivo do deck", type=["txt"])
    if arquivo is not None:
        texto = arquivo.getvalue().decode("utf-8-sig", errors="replace")

if st.button("Analisar deck", type="primary"):
    st.session_state.pop("analise", None)

    if not texto.strip():
        st.warning("Cole a lista ou envie um arquivo antes de analisar.")
    else:
        cartas, nome_deck, avisos_leitura = carregar_deck_de_texto(texto)
        if not cartas:
            st.error("Não encontrei nenhuma carta na lista. "
                     "Use uma carta por linha, por exemplo: 4 Lightning Bolt.")
        else:
            try:
                with st.spinner("Consultando a Scryfall (pode levar alguns segundos)..."):
                    resultado = analisar_em_cache(cartas, nome_deck)
            except ErroScryfall as erro:
                st.error(str(erro))
            else:
                st.session_state["analise"] = {
                    "resultado": resultado,
                    "avisos_leitura": avisos_leitura,
                }

analise = st.session_state.get("analise")
if analise:
    mostrar_resultado(**analise)

st.caption(f"Dados de cartas e preços: Scryfall · Analisador v{VERSAO}")
