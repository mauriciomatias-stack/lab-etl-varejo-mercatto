# Registro de Decisões de Transformação (RDT)

**Autor:** Mauricio Matias  **Data:** 16/10/2026  **Versão do pipeline:** Semana 2 (etapas 01 e 02)

Escala dos dados: `ESCALA=0.2` (440.192 linhas nas quatro fontes, contando as 21 da tabela `category`).

> Cada ficha traz evidência, alternativas, decisão, o que se perde, registros afetados e asserção associada. Resultado da execução: etapa `01_extracao` com 31 asserções e 0 falhas; etapa `02_transformacao` com 31 asserções e 0 falhas.
> Cada ficha só conta se tiver "o que se perde" e "registros afetados".

---

## Ficha RDT-01

**Decisão:** carga completa (e não incremental) das quatro fontes para a camada `stg`.

**Etapa do pipeline:** 01_extracao · `extrai_mysql`, `extrai_postgres`, `extrai_sac`, `extrai_avaliacoes`

**O que o dado mostrava:**
- Nenhuma das tabelas de origem tem coluna de data de alteração. O legado `PEDIDOS` tem `DT_PEDIDO` e `DT_ENTREGA`; o e-commerce `orders` tem `placed_at` e `delivered_at`; não há `atualizado_em` em nenhuma.
- O status e a data de entrega de um pedido mudam depois da carga, sem deixar rastro: 2.565 pedidos do legado (`EM TRANSITO` 1.560 + `SEPARACAO` 1.005; 9,87% de 26.000) e 7.298 do e-commerce (`shipped` 4.504 + `processing` 2.794; 13,03% de 56.000) ainda estão em andamento. Somam 9.863 pedidos, 12,03% dos 82.000.
- O SAC é exportação manual mensal concatenada: 19.228 linhas para 19.000 protocolos distintos (228 linhas repetidas, 1,19%). Não há chave única confiável.
- O volume é pequeno o bastante para recarregar inteiro: 440.192 linhas.

**Alternativas consideradas:**
1. Incremental por data (carregar só pedidos com `DT_PEDIDO`/`placed_at` posterior à última carga). Descartada: a data do pedido não muda, então um pedido já carregado nunca seria revisitado, e os 9.863 pedidos em andamento ficariam com o status e a data de entrega da primeira carga.
2. Incremental pela maior chave primária (só ids novos). Descartada: mesmo defeito, e o SAC nem tem chave primária utilizável.
3. Carga completa, recriando as tabelas `stg` a cada execução. **Escolhida.**
4. Carga completa guardando um histórico de cada execução. Descartada: a pergunta analítica (recompra até 28/09/2025) usa o estado dos pedidos no momento da extração, e ninguém consumiria o histórico; só multiplicaria o volume.

**Decisão:** alternativa 3 (`if_exists="replace"` na primeira gravação de cada tabela).

**Justificativa:** o enunciado exige que o pipeline rode do zero e reproduza o mesmo resultado. Com origens somente leitura, sem data de alteração e com volume pequeno, recarregar tudo é a única estratégia que garante que a `stg` espelha o estado atual da origem. Para a carga incremental valer a pena seria preciso uma coluna de alteração confiável, que não existe.

**O que se perde:**
- O histórico de mudanças: o estado anterior de um pedido é sobrescrito a cada carga, então não é possível saber quando um status mudou.
- Eficiência: reprocessa 100% das linhas mesmo quando quase nada mudou. Na escala 1.0 (cinco vezes maior) o custo cresce na mesma proporção.
- A coluna `_extraido_em` muda a cada execução. É só metadado, e deve ser ignorada ao comparar duas execuções.
- Nenhum valor de dado se perde: as somas monetárias conferem com a origem.

**Registros afetados:** 440.192 linhas (100% das fontes) são recarregadas a cada execução: legado 115.157, e-commerce 273.807, SAC 19.228 e avaliações 32.000. Dessas, 9.863 pedidos (12,03%) podem mudar de estado entre uma carga e outra.

**Asserção associada:** etapa `01_extracao`, 31 asserções, 0 falhas. Para cada tabela, "linhas lidas x origem" e "linhas na stg x origem" (por exemplo `PEDIDOS` 26.000 = 26.000 e `shop.order_item` 198.386 = 198.386); soma de `VALOR_TOTAL` (60.708.429,93) e de `total_amount` (146.752.362,13) dentro de ±R$ 0,01 da origem; textos com acento corrompido = 0 nas quatro tabelas do legado; SAC 19.228 = 19.228; avaliações 32.000 = 32.000, com `review_id` sem nulos e sem duplicados.

---

## Ficha RDT-02 · Identidade do cliente entre os dois sistemas

**Decisão:** dois cadastros (legado e e-commerce) são o mesmo cliente quando têm o mesmo CPF de 11 dígitos, válido ou não.

**Etapa do pipeline:** 02_transformacao · `unifica_clientes` / `monta_clientes`

**O que o dado mostrava:**
- 2.516 CPFs aparecem nos dois sistemas (30,42% dos CPFs do legado e 16,07% dos do e-commerce).
- Em 2.592 pares (cliente do legado, cliente do e-commerce) com o mesmo CPF, o nome é idêntico (sem acento e caixa) em 100%.
- 232 dos 2.516 CPFs da interseção (9,22%) têm dígito verificador inválido, e o mesmo CPF inválido aparece nos dois lados para a mesma pessoa.
- Sem CPF: 900 clientes no legado (9,52%) e 1.341 no e-commerce (7,89%). E-mail e telefone em comum entre os sistemas: 0, então nada confirma um pareamento sem CPF.

**Alternativas consideradas:**
1. Parear por CPF exato, inclusive inválido. **Escolhida.**
2. Parear só por CPF válido. Descartada: deixaria duplicados os 232 CPFs inválidos cujos nomes coincidem, ou seja, desfaria pareamentos que os dados confirmam.
3. CPF e, para quem não tem, nome normalizado + cidade + UF. Descartada: sem e-mail nem telefone em comum não há como conferir o pareamento, e homônimos seriam fundidos.

**Justificativa:** fundir dois clientes distintos é pior que manter um cliente em duas linhas. A fusão errada contamina o histórico de compra, que é a base do alvo de recompra; a duplicata só reduz o histórico de um cliente. Como o nome coincide em 100% dos 2.592 pares, inclusive com CPF inválido, o dígito verificador não separa pessoas diferentes nestes dados.

**O que se perde:**
- Cadastros sem CPF nunca são pareados: até 2.241 (900 + 1.341) podem ser a mesma pessoa em duas linhas.
- CPF com menos de 11 dígitos é tratado como ausente (`cpf_normalizado` nulo).
- Nome e cidade são gravados em MAIÚSCULAS sem acento (`CUIABA`); o acento original fica só na `stg`.
- A regra confia que um CPF digitado errado de forma idêntica em duas pessoas não ocorre; nos dados, 0 pares contrariam isso.

**Registros afetados:** 26.450 cadastros de origem (9.450 do legado + 17.000 do e-commerce) viram 23.655 linhas em `dim_cliente`, 2.795 a menos (10,57%). Dessas linhas: 2.516 `AMBOS` (10,64%), 6.655 `LEGADO` e 14.484 `ECOM`. Sem CPF, sem pareamento: 2.241 (9,47% das linhas), 900 do legado e 1.341 do e-commerce. CPFs de 11 dígitos: 21.414, dos quais 1.977 (9,23%) com dígito verificador inválido.

**Asserção associada:** etapa `02_transformacao`, 7 asserções, 0 falhas: `dim_cliente` com 23.655 linhas, igual à contagem independente em SQL (uma por CPF de 11 dígitos mais uma por cadastro sem CPF); 0 CPFs duplicados entre válidos e entre todos; `origem_unificacao` dentro de {LEGADO, ECOM, AMBOS}; `AMBOS` = 2.516, igual à interseção medida na semana 1.

## Ficha RDT-03 · Duplicatas internas do legado

**Decisão:** cadastros do legado com o mesmo CPF são fundidos em uma única linha de `dim_cliente`.

**Etapa do pipeline:** 02_transformacao · `unifica_clientes` / `monta_clientes`

**O que o dado mostrava:** 279 CPFs aparecem em mais de um cliente do legado (558 linhas; 5,90% dos 9.450); 9.450 clientes para 9.000 e-mails distintos (450 linhas a mais, 4,76%). O e-commerce não tem CPF repetido.

**Alternativas consideradas:**
1. Manter as duas linhas e só pareá-las com o e-commerce. Descartada: o mesmo cliente teria dois `cliente_sk`, e a frequência de compra seria dividida entre eles.
2. Fundir por CPF, com a mesma regra do RDT-02. **Escolhida.**
3. Fundir também duplicatas sem CPF por nome e e-mail. Descartada: 450 e-mails repetidos não provam a mesma pessoa, e não há como conferir.

**Justificativa:** é a mesma regra do RDT-02, aplicada dentro do legado, e os nomes dos pares com CPF igual coincidem. Os pedidos dos dois `ID_CLIENTE` passam a apontar para o mesmo `cliente_sk` pela tabela `dw.ponte_cliente`, porque `dim_cliente` guarda um só `id_legado`.

**O que se perde:** `dim_cliente.id_legado` guarda apenas o `ID_CLIENTE` do cadastro mais recente; o outro só existe em `ponte_cliente`. Nome, cidade e UF vêm do cadastro mais recente (valor não nulo); `dt_primeiro_cadastro` é a menor data.

**Registros afetados:** 279 cadastros do legado fundidos em outro cadastro com o mesmo CPF (2,95% dos 9.450): 203 CPFs repetidos só no legado e 76 que também existem no e-commerce. Os 558 cadastros de origem desses CPFs ocupam 279 linhas em `dim_cliente`.

**Asserção associada:** `ponte_cliente` com 26.450 linhas, uma por cadastro de origem (9.450 + 17.000); `dim_cliente` com 0 CPFs duplicados; `linhas_legado_fundidas` = 279.

## Ficha RDT-04 · Conformação de status

**Decisão:** `status_conformado = CANCELADO` para os três cancelamentos de origem, com `motivo_cancelamento` preenchido só quando a origem distingue (`CLIENTE`, `PAGAMENTO_RECUSADO`).

**Etapa do pipeline:** 02_transformacao · `conforma_status` (`MAPA_STATUS`)

**O que o dado mostrava:**
- Legado: `CANCELADO` 3.161 (12,16% de 26.000); a origem não diz o motivo.
- E-commerce: `canceled` 4.517 (8,07% de 56.000) e `payment_declined` 2.832 (5,06%).
- Demais status: `ENTREGUE` 20.274 × `delivered` 41.353; `EM TRANSITO` 1.560 × `shipped` 4.504; `SEPARACAO` 1.005 × `processing` 2.794.

**Alternativas consideradas:**
1. `payment_declined` → `CANCELADO`, sem motivo. Descartada: perde a distinção justamente onde ela existe.
2. `CANCELADO_CLIENTE` e `CANCELADO_PAGAMENTO` como status separados. Descartada: os 3.161 cancelados do legado não cabem em nenhum dos dois, e qualquer contagem de "cancelados" teria de somar valores.
3. `CANCELADO` com coluna de motivo, nula quando a origem não distingue. **Escolhida.**

**Justificativa:** `CANCELADO` é o único nível comparável entre os dois sistemas, e a coluna `motivo_cancelamento` da `dim_status` foi desenhada para guardar a distinção quando ela existe. Para a recompra, uma recusa de pagamento também não é compra concluída, então todos os cancelados saem da contagem de compras.

**O que se perde:**
- Nos 3.161 cancelados do legado o motivo fica nulo: não dá para saber quantos foram recusa de pagamento. Qualquer análise por motivo só vale para o e-commerce.
- Hipóteses assumidas sem confirmação no dicionário: `processing` equivale a `SEPARACAO`, e `canceled` é cancelamento pelo cliente.

**Registros afetados:** 10.510 pedidos cancelados (12,82% dos 82.000): 3.161 sem motivo, 4.517 com motivo `CLIENTE` e 2.832 com motivo `PAGAMENTO_RECUSADO`. Os outros 71.490 mudam só de nome (por exemplo `EM TRANSITO` → `EM_TRANSITO`). Total mapeado: 82.000 (100%).

**Asserção associada:** etapa `02_transformacao`: 0 pedidos sem mapeamento; pedidos mapeados = total na `stg` (82.000); `dim_status` com 9 linhas.

**Canais (mesma etapa):** `LOJA` → `FISICO`; `TELEVENDAS` → `TELEVENDAS`; `SITE`, `web` e `app` → `ONLINE_PROPRIO`; `marketplace` → `MARKETPLACE`. Perde-se a diferença entre site, web e app dentro de `ONLINE_PROPRIO`, que continua em `canal_origem`.

## Ficha RDT-05 · Semântica de `VALOR_TOTAL` e `total_amount`

**Decisão:** `valor_mercadoria` = Σ(quantidade × preço), sem frete e sem desconto; `valor_desconto` = Σ desconto; `valor_total` = mercadoria + frete − desconto, igual nos dois sistemas.

**Etapa do pipeline:** 02_transformacao · `monta_pedidos` / `conforma_valores`

**O que o dado mostrava** (reconciliação com a soma dos itens, tolerância R$ 0,02; o dicionário não define os campos):
- Legado: Σ(qtd × preço − desconto) + frete bate em 25.239 de 26.000 pedidos (97,07%); tratando o desconto `-1` como zero, bate em 26.000 (100%). O `VALOR_TOTAL` **inclui** o frete.
- E-commerce: Σ(qtd × preço − desconto) bate em 56.000 de 56.000 (100%). O `total_amount` **não inclui** o frete.

**Alternativas consideradas:**
1. Carregar `VALOR_TOTAL` e `total_amount` como vieram. Descartada: medem coisas diferentes, e somar os dois seria comparar valores com e sem frete.
2. Definir o total sem frete nos dois sistemas. Descartada: o DDL define `valor_total = mercadoria + frete − desconto`.
3. Reconstruir as medidas a partir dos itens e do frete, com a fórmula do DDL. **Escolhida.**

**Justificativa:** só a reconstrução produz uma medida com o mesmo significado nos dois sistemas, e ela é verificável: bate com o total da origem em 100% dos pedidos (legado direto; e-commerce depois de retirar o frete).

**O que se perde:**
- No e-commerce, `valor_total` do DW é maior que o `total_amount` da origem pelo valor do frete. A reconciliação financeira da semana 3 deve comparar `valor_total − valor_frete` com `total_amount`.
- No legado, a diferença nos 761 pedidos com desconto `-1` está na correção do RDT-06.

**Registros afetados:** 82.000 pedidos (100%) têm as quatro medidas recalculadas; nos 56.000 do e-commerce o `valor_total` passa a incluir o frete.

**Asserção associada:** etapa `02_transformacao`: divergência entre total conformado e total da origem = 0 nos dois sistemas (±0,02); `valor_total = mercadoria + frete − desconto` em 100% das linhas; mercadoria − desconto = soma dos itens (±0,01) por pedido.

## Ficha RDT-06 · Desconto `-1`, frete negativo e itens órfãos

**Decisão:** (a) desconto `-1` do legado é sentinela e vira 0; (b) frete negativo é mantido como veio; (c) itens cujo pedido não existe na origem ficam fora do DW; (d) preços 10× acima da mediana do produto são só medidos (a medição deu 0).

**Etapa do pipeline:** 02_transformacao · `monta_pedidos`

**O que o dado mostrava:**
- Desconto `-1`: 770 itens do legado (0,97% de 79.107), em 761 pedidos (2,93% de 26.000). O e-commerce não tem desconto negativo (mínimo 0,00). Tratado como zero, o total reconcilia em 100%.
- Frete negativo: 86 pedidos do legado (0,33%) e 152 do e-commerce (0,27%). Não impede a reconciliação do total.
- Itens órfãos: 1.169 itens do legado (1,48%) e 2.931 do e-commerce (1,48%) apontam para pedidos que não existem (1.100 e 2.539 pedidos distintos).
- Preços 10× acima da mediana do próprio produto: 0 itens, então não há o que corrigir nem excluir.

**Alternativas consideradas:**
- Desconto `-1`: (a) tratar como zero, **escolhida**; (b) manter o `-1`, que somaria R$ 1 ao item, mas não reconcilia com o total da origem.
- Frete negativo: (a) zerar; (b) manter, **escolhida**; (c) manter e marcar. Zerar mudaria o total de pedidos que a origem já reconcilia, e o DDL não tem coluna de marcação.
- Órfãos: (a) excluir, **escolhida**; (b) criar um pedido artificial para cada um. Descartada: inventaria pedidos, clientes e datas que a origem não tem.
- Preço 10×: corrigir ou excluir. Descartada: não há como distinguir erro de digitação de preço alto legítimo, e mexer quebraria a reconciliação com o total da origem. A medição não achou nenhum caso.

**Justificativa:** o `-1` é marca de "sem desconto", não um valor; o frete negativo reconcilia com o total e pode ser estorno, então a origem é preservada; item sem pedido não tem a que se ligar (`pedido_sk` é obrigatório no DDL).

**O que se perde:**
- Os 4.100 itens órfãos (1.169 + 2.931) só existem na `stg`; o valor deles não entra em nenhuma soma do DW.
- O frete negativo continua no DW; qualquer média de frete precisa decidir se o inclui.
- Nenhum preço 10× acima da mediana do produto foi encontrado, então o critério não alterou nenhum valor.

**Registros afetados:** 770 itens e 761 pedidos (desconto); 238 pedidos (frete negativo, 0,29% dos 82.000); 4.100 itens órfãos (1,48% dos 277.493 itens).

**Asserção associada:** `fato_item_pedido` = itens que têm pedido na origem (contagem independente em SQL); itens sem produto = 0; divergência de total = 0.

## Ficha RDT-07 · Mapeamento de categorias

**Decisão:** o texto livre do legado é mapeado para a taxonomia do e-commerce; `categoria_origem` guarda o valor bruto. Valores sem informação viram `NAO_CLASSIFICADO`; o único com dois ramos vira `AMBIGUO`. Produtos dos dois sistemas não são pareados.

**Etapa do pipeline:** 02_transformacao · `conforma_categorias` (`MAPA_CATEGORIA`)

**O que o dado mostrava:** 27 valores de texto livre no legado, 22 produtos com categoria vazia (3,67% de 600), `DIVERSOS` (28) e `OUTROS` (24); taxonomia do e-commerce com 21 nós, dos quais os produtos usam 15. Casos ambíguos: `informatica e games` (23 produtos) cobre dois ramos; `TV E SOM` (21) cobre dois filhos de Áudio e TV. Variantes de grafia do mesmo conceito: `eletrodomesticos` (34), `Eletro` (30), `Eletro-domesticos` (19). O legado não tem SKU.

**Alternativas consideradas:**
1. Mapear `informatica e games` para Informática (ou para Games). Descartada: classificaria 23 produtos num ramo sem base.
2. Criar uma categoria composta "Informática e Games". Descartada: sai da taxonomia controlada.
3. Marcar como `AMBIGUO`, com o bruto preservado. **Escolhida.**
4. Para `DIVERSOS`, `OUTROS` e vazio: `NAO_CLASSIFICADO`, e não palpitar por descrição. **Escolhida.**
5. Parear produtos do legado com os do e-commerce por descrição. Descartada: as descrições não coincidem e não há chave comum.

**Justificativa:** errar a categoria contamina análises por categoria; admitir que não se sabe não contamina. O valor bruto continua em `categoria_origem`, então a decisão pode ser revista sem reextrair.

**O que se perde:**
- 74 produtos (`DIVERSOS` 28, `OUTROS` 24, vazio 22; 12,33% de 600) não têm categoria útil, e 23 (3,83%) ficam `AMBIGUO`.
- `TV E SOM`, `AUDIO/VIDEO` e `GAMES E CONSOLES` param no primeiro nível: o segundo nível fica nulo.
- Como não há pareamento, o mesmo produto físico pode estar duas vezes em `dim_produto` (600 do legado + 2.400 do e-commerce = 3.000 linhas).

**Registros afetados:** 97 dos 600 produtos do legado (16,17%) sem categoria conformada; 0 nos 2.400 do e-commerce.

**Asserção associada:** valores do legado sem regra = 0; `dim_produto` = 3.000 linhas; `categoria_origem` do legado idêntica ao bruto em 600 de 600.

## Ficha RDT-08 · Tipos de ausência

**Decisão:** cada ausência é classificada antes de qualquer tratamento. Nulo estrutural permanece nulo; sentinela `1900-01-01` e data de entrega anterior ao pedido viram NULL; nenhuma é preenchida com média ou mediana.

**Etapa do pipeline:** 02_transformacao · `monta_pedidos` e `trata_datas_e_ausencias`

**O que o dado mostrava:**
- Nulo estrutural (entrega ainda não aconteceu): 5.726 pedidos no legado (22,02%) e 14.647 no e-commerce (26,16%), todos em pedidos não entregues.
- Sentinela `1900-01-01`: 222 pedidos entregues do legado (1,09% dos entregues). No e-commerce, a menor data de entrega é 2023-01-02, sem sentinela.
- Data impossível (entrega antes do pedido): 140 no legado (0,69% dos entregues) e 343 no e-commerce (0,83%).
- SAC: `data_fechamento` vazia em 1.168 atendimentos (6,07%).

**Alternativas consideradas:**
- Sentinela: (a) manter `1900-01-01`, que geraria prazos de mais de 120 anos; (b) NULL, **escolhida**; (c) imputar pelo prazo médio, que inventa um dado.
- Entrega antes do pedido: (a) trocar as datas; (b) manter e sinalizar; (c) NULL, **escolhida**. Não se sabe qual das duas datas está errada, e o DDL não tem coluna de sinalização.
- SAC sem fechamento: manter NULL; a origem não permite distinguir atendimento em aberto de dado perdido.

**Justificativa:** cada tipo de ausência diz uma coisa diferente: "ainda não aconteceu" é informação, a sentinela é um dado desconhecido, e a data impossível é um dado errado. Em todos os casos NULL é o único valor que não afirma algo falso.

**O que se perde:** 705 pedidos entregues (222 + 140 + 343) ficam sem data de entrega e sem `dias_entrega`; a data original só permanece na `stg`. Nos 1.168 atendimentos não dá para calcular o tempo de resolução a partir das datas.

**Registros afetados:** 705 pedidos (1,14% dos 61.627 entregues): 222 sentinelas do legado, 140 do legado e 343 do e-commerce com entrega antes do pedido; 1.168 atendimentos do SAC (6,07%). Nulo estrutural preservado: 5.726 no legado e 14.647 no e-commerce. Entregas válidas: 19.912 no legado e 41.010 no e-commerce.

**Asserção associada:** 0 entregas antes do pedido e 0 datas de 1900 no DW; `dias_entrega` negativo = 0; sentinelas tratadas = contagem independente na `stg`; pedidos entregues sem data = sentinelas + impossíveis; pedido não entregue com data = 0.

## Ficha RDT-09 · Padronização de fuso horário

**Decisão:** todas as datas do DW são datas de calendário no fuso `America/Sao_Paulo`. O `DATETIME` do legado, sem fuso, é tomado como horário local; o `TIMESTAMPTZ` do e-commerce é convertido de UTC para esse fuso.

**Etapa do pipeline:** 02_transformacao · `monta_pedidos`, `monta_clientes`

**O que o dado mostrava:** `DT_PEDIDO` do legado é DATETIME sem fuso; `placed_at` e `delivered_at` do e-commerce são TIMESTAMPTZ em UTC; `created_at` dos clientes do e-commerce aparece sempre em `03:00:00+00:00`, ou seja, meia-noite em Brasília guardada em UTC; no NDJSON, 6.427 de 32.000 `submitted_at` (20,08%) vêm sem fuso; no SAC as datas são `dd/mm/aaaa`, sem hora.

**Alternativas consideradas:**
1. Padronizar em UTC. Descartada: moveria para o dia anterior ou seguinte as compras feitas à noite no horário local, e o legado já está em hora local.
2. Padronizar em `America/Sao_Paulo`. **Escolhida.**
3. Ignorar o fuso e usar a data como veio. Descartada: o mesmo instante teria datas diferentes nos dois sistemas.

**Justificativa:** o legado e o SAC são locais e não têm fuso para converter; o e-commerce é o único que declara UTC, e o `03:00` dos cadastros mostra que a hora local é Brasília. É o mesmo calendário que o cliente viveu.

**O que se perde:** a hora do dia (o grão do DW é o dia). Nas avaliações sem fuso (20,08%), assume-se o horário local, o que pode errar por até um dia se a origem estiver em UTC. Cuiabá (UTC−4) difere de Brasília em uma hora; a diferença só muda o dia de eventos entre 23h e meia-noite.

**Registros afetados:** 7.095 dos 56.000 pedidos do e-commerce (12,67%) mudam de dia ao converter de UTC para o fuso local; 6.427 avaliações sem fuso (na carga da semana 3).

**Asserção associada:** 0 datas fora do calendário `dim_tempo` (integridade referencial na carga de `fato_pedido`).

---

## Tabela-resumo (fichas 01 a 09)

| Ficha | Decisão | Registros afetados | % da base | Informação perdida? |
|---|---|---|---|---|
| RDT-01 | Carga completa | 440.192 | 100% | Não se perde valor; perde-se o histórico de mudanças de status |
| RDT-02 | Pareamento por CPF exato | 2.516 pareados | 10,64% das 23.655 linhas | 2.241 sem CPF não pareados; acentos só na `stg` |
| RDT-03 | Fusão de duplicatas do legado | 279 | 2,95% do legado | `id_legado` de um cadastro só na ponte |
| RDT-04 | `CANCELADO` + motivo | 10.510 | 12,82% | Motivo nulo em 3.161 do legado |
| RDT-05 | Total = mercadoria + frete − desconto | 82.000 | 100% | `valor_total` do e-commerce ≠ `total_amount` (frete) |
| RDT-06 | `-1` → 0; frete mantido; órfãos fora | 770 + 238 + 4.100 | 0,97% / 0,29% / 1,48% | 4.100 itens só na `stg` |
| RDT-07 | Mapeamento de categorias | 97 de 600 produtos | 16,17% | Sem categoria útil ou ambíguos |
| RDT-08 | NULL para sentinela e data impossível | 705 | 1,14% dos entregues | Data original só na `stg` |
| RDT-09 | `America/Sao_Paulo` | 7.095 pedidos mudam de dia | 12,67% dos 56.000 do e-commerce | Hora do dia |

## Reflexão final (máximo 400 palavras)

*(entrega final)*
