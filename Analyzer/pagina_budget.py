"""
Aba "Versão budget" do app: à esquerda o deck enviado, à direita a versão mais barata.
"""
import hashlib

import pandas as pd
import streamlit as st

from analisador import ErroScryfall, carregar_deck_de_texto, resolver_cartas
from orcamento import (
    ECONOMIA_MINIMA_IMPRESSAO_USD,
    LIMITE_TROCA_PADRAO,
    NOMES_FORMATOS,
    RIGOR,
    formato_automatico,
    formatar_lista,
    gerar_deck_budget,
)

ALTURA_BLOCOS = 560  # os dois blocos têm exatamente a mesma altura

CONFIG_COLUNAS = {
    "US$ orig.": st.column_config.NumberColumn(format="$%.2f"),
    "US$ sug.": st.column_config.NumberColumn(format="$%.2f"),
    "Economia": st.column_config.NumberColumn(format="$%.2f"),
}


def _pct(valor):
    return "—" if valor is None else f"{valor}%"


def _texto_colecao(o, e):
    nova = f"{e['colecao']} ({e['colecao_codigo']})" if e["colecao"] else "—"
    if e["tipo_troca"] == "impressao":
        antiga = f"{o['colecao']} ({o['colecao_codigo']})" if o["colecao"] else "?"
        return f"{nova} · antes: {antiga}"
    return nova


def _texto_sinergia_edhrec(o, e):
    if e["tipo_troca"] == "impressao":
        return "mesma carta"
    if e["edhrec_pct"] is None:
        return "—"
    antes = f"{o['edhrec_pct']}% → " if o["edhrec_pct"] is not None else ""
    return f"{antes}{e['edhrec_pct']}%"


def _mostrar_resultado(estado):
    r = estado["resultado"]

    m1, m2, m3 = st.columns(3)
    m1.metric("Preço original", f"${r['total_original']:,.2f}")
    m2.metric("Preço budget", f"${r['total_budget']:,.2f}",
              delta=f"-${r['economia']:,.2f}", delta_color="inverse")
    m3.metric("Cartas trocadas", len(r["trocas"]))

    for aviso in estado["avisos"] + r["avisos"]:
        st.info(aviso)
    if r["edhrec"]["usado"]:
        st.caption(f"Sinergia do EDHREC para o comandante ({r['edhrec']['slug']}).")
    if estado["falhas"]:
        nomes = ", ".join(f"{f['qtd']}x {f['nome']}" for f in estado["falhas"])
        st.warning(f"Não encontradas na Scryfall e fora desta lista: {nomes}.")

    if not r["trocas"] and not r["sem_troca"]:
        st.info(f"Nenhuma carta custa mais de US$ {r['limite_troca']:.2f}; não há o que trocar. "
                "Diminua o valor em 'Trocar cartas acima de' para procurar mais trocas.")
    elif not r["trocas"]:
        st.info("Não encontrei substitutas com a semelhança pedida. "
                "Tente 'Flexível' em 'Semelhança exigida'.")

    if r["trocas"]:
        st.markdown("**Trocas sugeridas**")
        linhas = []
        for t in r["trocas"]:
            o, e = t["original"], t["escolha"]
            linhas.append({
                "Qtd": o["qtd"],
                "Original": o["nome"],
                "US$ orig.": o["preco"],
                "Sugestão": e["nome"],
                "US$ sug.": e["preco"],
                "Tipo de troca": ("Mesma carta, outra impressão" if e["tipo_troca"] == "impressao"
                                  else "Carta diferente"),
                "Coleção": _texto_colecao(o, e),
                "Sinergia EDHREC": _texto_sinergia_edhrec(o, e),
                "Sinergia estimada": _pct(e["estimada_pct"]),
                "Função": f"{round(e['funcao'] * 100)}%",
                "Economia": t["economia"],
            })
        st.dataframe(pd.DataFrame(linhas), hide_index=True, column_config=CONFIG_COLUNAS)

        with st.expander("Comparar textos e ver alternativas"):
            for t in r["trocas"]:
                o, e = t["original"], t["escolha"]
                st.markdown(f"**{o['nome']}** → **{e['nome']}**")
                ca, cb = st.columns(2)
                ca.caption(f"{o['tipo']} · CMC {o['cmc']} · ${o['preco']:.2f} · {o['colecao']}")
                ca.text(o["texto"] or "(sem texto)")
                cb.caption(f"{e['tipo']} · CMC {e['cmc']} · ${e['preco']:.2f} · {e['colecao']}")
                cb.text(e["texto"] or "(sem texto)")
                if t["alternativas"]:
                    st.caption("Outras opções: " + "; ".join(
                        f"{a['nome']} (${a['preco']:.2f}; sinergia EDHREC {_pct(a['edhrec_pct'])}, "
                        f"estimada {_pct(a['estimada_pct'])}; função {round(a['funcao'] * 100)}%)"
                        for a in t["alternativas"]))
                st.divider()

    if r["sem_troca"]:
        with st.expander(f"Sem substituta adequada ({len(r['sem_troca'])})"):
            for t in r["sem_troca"]:
                o = t["original"]
                st.markdown(f"- {o['qtd']}x {o['nome']} (${o['preco']:.2f})")

    st.markdown("**Lista budget**")
    st.code(formatar_lista(r), language=None)
    with st.expander("Como ler as métricas"):
        st.markdown(
            "- **Sinergia EDHREC:** % dos decks do comandante que usam a carta menos % dos "
            "decks da mesma identidade de cor que a usam. Vem do EDHREC e só existe com comandante. "
            "Em trocas por outra carta, aparece 'antes → depois'.\n"
            "- **Sinergia estimada:** o quanto a carta combina com os temas do seu deck e do "
            "comandante (mecânicas, palavras-chave, tipos de criatura). É uma estimativa nossa, "
            "não uma estatística de decks reais.\n"
            "- **Função:** o quanto a carta faz o mesmo que a original, pelo texto de regras. "
            "O tipo de carta pode ser diferente.\n"
            "- A escolha pesa 50% sinergia, 35% função e 15% economia. Preços em USD (cartas "
            "físicas), segundo a Scryfall.")
    st.caption("Sugestões automáticas: confira se o efeito serve para a sua estratégia antes de trocar.")


def pagina_budget():
    st.caption("Envie a lista de um deck que você pretende montar e veja uma versão mais barata, "
               "com cartas de efeito parecido.")

    # --- Linha de cima: depósito do arquivo (esquerda) e opções (direita) ----------
    topo_esq, topo_dir = st.columns(2, gap="large")
    with topo_esq:
        arquivo = st.file_uploader("Deposite aqui o arquivo .txt do deck", type=["txt"],
                                   key="budget_arquivo")
    with topo_dir:
        c1, c2, c3 = st.columns(3)
        limite = c1.number_input("Trocar cartas acima de (US$)", min_value=0.0,
                                 value=LIMITE_TROCA_PADRAO, step=0.5, key="budget_limite")
        nome_formato = c2.selectbox("Formato", ["Automático"] + list(NOMES_FORMATOS),
                                    key="budget_formato",
                                    help="Automático: Commander se o deck tiver comandante; "
                                         "senão, sem filtro de formato.")
        economia_impressao = c3.number_input(
            "Outra impressão só se economizar (US$)", min_value=0.0,
            value=ECONOMIA_MINIMA_IMPRESSAO_USD, step=0.5, key="budget_impressao",
            help="A mesma carta em outra coleção só é sugerida se economizar pelo menos este "
                 "valor e custar no máximo metade do preço atual.")
        rigor = st.select_slider("Semelhança exigida", options=list(RIGOR),
                                 value="Equilibrado", key="budget_rigor")
        gerar = st.button("Gerar versão budget", type="primary",
                          disabled=arquivo is None, key="budget_gerar")

    texto = arquivo.getvalue().decode("utf-8-sig", errors="replace") if arquivo else ""
    impressao_digital = hashlib.md5(texto.encode("utf-8")).hexdigest()

    espaco_progresso = st.empty()

    if gerar:
        st.session_state.pop("budget", None)
        cartas, _, avisos_leitura = carregar_deck_de_texto(texto)
        if not cartas:
            st.error("Não encontrei nenhuma carta no arquivo. "
                     "Use uma carta por linha, por exemplo: 1 Sol Ring.")
        else:
            try:
                barra = espaco_progresso.progress(0.0, text="Lendo as cartas na Scryfall...")
                entradas, falhas, avisos = resolver_cartas(cartas)
                formato = (formato_automatico(entradas) if nome_formato == "Automático"
                           else NOMES_FORMATOS[nome_formato])

                def andamento(fracao, mensagem):
                    barra.progress(min(1.0, fracao), text=mensagem)

                resultado = gerar_deck_budget(
                    entradas, formato, limite, RIGOR[rigor], andamento,
                    economia_minima_impressao=economia_impressao)
            except ErroScryfall as erro:
                espaco_progresso.empty()
                st.error(str(erro))
            else:
                espaco_progresso.empty()
                st.session_state["budget"] = {
                    "resultado": resultado,
                    "falhas": falhas,
                    "avisos": avisos_leitura + avisos,
                    "arquivo": impressao_digital,
                }

    estado = st.session_state.get("budget")
    if estado and estado["arquivo"] != impressao_digital:
        estado = None  # o arquivo mudou: o resultado antigo não vale mais

    # --- Dois blocos, mesma altura -------------------------------------------------
    esq, dir_ = st.columns(2, gap="large")
    with esq:
        st.subheader("Deck original")
        with st.container(border=True, height=ALTURA_BLOCOS):
            if texto:
                st.code(texto, language=None)
            else:
                st.caption("O deck enviado aparece aqui.")
    with dir_:
        st.subheader("Versão budget")
        with st.container(border=True, height=ALTURA_BLOCOS):
            if estado:
                _mostrar_resultado(estado)
            else:
                st.caption("A versão budget aparece aqui depois de gerada.")

    if estado:
        st.download_button("Baixar lista budget (.txt)", data=formatar_lista(estado["resultado"]),
                           file_name="deck_budget.txt", mime="text/plain")
