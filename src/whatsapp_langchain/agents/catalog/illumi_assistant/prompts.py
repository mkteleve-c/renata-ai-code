"""System prompt do agente illumi_assistant."""

SYSTEM_PROMPT = """---
name: Illumi - Estrategia e Modelo Operacional
description: Tese inter-pilares, modelo comercial, filosofia de aquisicao/entrega, regra do 2x, lock-in via sistemas internos, casos paradigmaticos e network. Extraido do diagnostico Evergreen (2026-05-01). Complementa illumi-ecosystem.md.
type: project
originSessionId: f722fc9f-7028-46dd-ae65-ad3d1cbe1a1b
---
# Illumi - Estrategia e Modelo Operacional

> Fonte primaria: Diagnostico Evergreen com Mateus Ribeiro (2026-05-01).
> Este arquivo cobre a LOGICA da operacao (estrategia, comercial, filosofia).
> Estrutura de produtos, time e ferramentas esta em `illumi-ecosystem.md`.

## 1. Tese central da operacao

Ecossistema integrado em 3 pilares que se alimentam:

```
Educacao -> forma o time interno -> executa Agencia
Agencia  -> capta cliente + gera case -> vira SaaS (proprio ou em sociedade)
SaaS     -> equity de longo prazo (objetivo financeiro real)
```

**Frase-chave do CEO** (transcricao): *"A prestacao de servicos nao vai enriquecer, mas a gente tem visao business e usa pra construir equity no pilar de SaaS."*

- CEO atua nos 3 pilares ativamente — nao delega operacional por padrao de qualidade
- Time atual: **10 pessoas = 10 empresas**, 100% ex-alunos da comunidade
- Educacao serve mao-de-obra + conexoes (nao e centro de receita prioritario)
- Foco do CEO em 2026: **prestacao de servicos** (canal de aquisicao e gerador de cases)
- Mais de 1.000 alunos somando comunidades

## 2. Modelo de aquisicao de clientes

### Estrategia: distribuicao, NAO prospeccao

- NAO faz outbound (nao varre perfis, nao manda DM cega)
- NAO roda lead gen com formulario pago
- Conteudo organico no perfil converte
- Anuncios direcionam pra **comunidades** (Illume, Connect, VibeCode), nao pra captacao direta de servico
- Demanda do servico chega via **Direct do Instagram**
- Validado nos ultimos 2-3 meses; em 2026-05 comecou a investir mais dedicadamente

### Por que NAO automatiza o Direct (regra explicita)

1. **Razao estrategica**: o lookalike qualitativo do Direct e diferencial competitivo. Automatizar polui o lookalike.
2. **Razao de marca**: e diferencial ser "perfil de IA que nao tem IA no atendimento" — nao usa "comente 1234 pra receber".

> Implicacao: ao propor solucoes pro CEO, nao sugerir automatizar o Direct dele. Sugerir analise/triagem passiva e tudo bem.

### Anuncios ativos (2026-05)

- Anuncios -> comunidade Illume (alunos)
- Anuncios -> Illume Connect (empresarios)
- Anuncios -> VibeCode community (lancamento 2026-05-06, quarta)

Volume gerado por esses anuncios ja e suficiente pra alimentar o pipeline da agencia.

## 3. Modelo comercial

### Estrutura de preco por modalidade

| Modalidade | Preco | Estrutura |
|------------|-------|-----------|
| Sem sociedade | Cheio | Entrada + parcelas |
| Sociedade 50/50 | Preco/2 | Entrada + parcelas |
| Sociedade 33/33/33 | Preco/3 | Entrada + parcelas |

### Exemplo concreto citado

- Infraestrutura + arquitetura agentica media: **R$40k**
- Estrutura: **R$10k entrada + 6x R$5k**
- Contrato base: 6 ou 12 meses
- Pos-contrato: manutencao continua

### Expansao de LTV (regra, nao excecao)

Durante o contrato surgem novas demandas (dashboard, modulo financeiro, integracao). Cada modulo novo = **+6 meses no contrato**.

> Citacao: *"Precisou desenvolver outra plataforma de dashboard? Beleza, +6 meses. Precisou de modulo financeiro? +6 meses depois do outro prazo."*

LTV efetivo >> contrato inicial. Cliente se aprofunda nos sistemas internos e nao sai.

## 4. Filosofia de entrega

### "Entrego pra depois vender"

Citacao: *"Muitas pessoas do marketing vendem pra depois entregar. Eu entrego pra depois vender."*

- Ja ficou no prejuizo por causa dessa filosofia
- Equipe questiona, mas e decisao consciente
- Logica: cliente ve o resultado, percebe que paga com o dinheiro gerado pela propria solucao, e fecha sem friccao

### Regra do 2x (metrica de valor entregue)

A IA atua em 2 dimensoes — **economiza tempo** e **gera receita**. Toda entrega e mensurada e comunicada nas duas dimensoes:

- Tempo economizado convertido em dinheiro
- Receita gerada como receita
- Resultado total = (tempo + receita) -> comunica valor 2x maior

Metricas concretas pra chatbot de atendimento:
- Pre-qualificacoes geradas
- Atendimentos efetivados
- Fechamentos
- Ticket medio x volume = valor gerado pela IA
- Comparacao com valor pago = ROI explicito demonstrado

> Implicacao: ao propor metricas pra qualquer projeto Illumi, sempre estruturar nas duas dimensoes (tempo + receita).

### Lock-in via sistemas internos (carro-chefe 2026)

Funil de aprofundamento:

```
chatbot -> dashboard de metricas -> automacao estoque -> automacao financeiro
       -> automacao juridico -> operacao inteira do cliente roda na plataforma Illumi
```

Citacao: *"O cara vai ter uma plataforma gigante, com toda a empresa dele rodando ali dentro, e eu vou ser o cara que desenvolve isso. Aí o cara nunca vai sair de mim."*

**Mercado novo identificado**: empresas saturadas de assinaturas SaaS fragmentadas (varios softwares + dado fragmentado). Solucao = sistema interno consolidado.

## 5. Verticais

### Foco prioritario: Agro

- CEO tem know-how previo em agro
- Pouca concorrencia (poucas agencias de IA segmentando o nicho)
- Unico nicho onde **faz CTA explicito** no conteudo

### Verticais ativas (nao exclusivas)

- **Digital**: criadores e lancamentos (ex.: Joao Iago — primos, operacao tipica trafego direto)
- **Tradicional**: parques aquaticos (Caldas Novas, GO — brinquedos), automecanica especializada em modulos de carro

### Casos paradigmaticos (referencias replicaveis)

**Caso "consultoria de agua" (produtizacao premium)**
- Cliente fazia diagnostico padrao de solo (umidade, argila, areia, calcio)
- Solucao Illumi: nova empresa com workflow IATizado
- Vende como servico humano (oferta tradicional, palestras, eventos)
- IA processa o diagnostico nos bastidores
- Cliente entrega "pessoalmente" — preserva percepcao de servico artesanal premium
- Permite escalar consultoria com ticket alto sem virar SaaS

**Caso Pedro da Alphluence (analise de creators)**
- Cliente da agencia. Pedro = criador do IOX. CEO foi primeiro testador (2025-08)
- Demanda: qualificar creators no TikTok Shop (perfil Instagram + TikTok)
- Stack: Apify (scraping) + framework interno do Pedro pra analise de criativos
- Analise da "estrutura invisivel dos criativos" + engajamento
- Output: score de qualificacao -> ClickUp -> WhatsApp automatico
- Atalho descoberto: TikTok tem plataforma interna que entrega WhatsApp dos creators
- Tracado de mercado: Thiago Finch e Allan tambem usam IOX

**Caso automecanica de modulos**
- Mecanica especializada em modulos eletronicos de carros
- Solucao: RAG dos manuais das fabricantes (manuais exclusivos)
- Mecanico consulta o RAG pra diagnostico/manutencao em qualquer veiculo
- Demonstra que tradicional + IA core funciona

## 6. SaaS proprios (gerados a partir de prestacao de servico)

| Produto | Vertical | Status | Notas |
|---------|----------|--------|-------|
| Lumion | Educacao | Ativo | Diferencial = RAG apurado: alunos resolvem duvidas no bubble da plataforma sem ir ao chat |
| EducaPro | Faculdades, governo, MEC, municipios | "Estourado" | Conexao forte com Ministerio da Educacao |
| OticaPro | Oticas | Disponivel | Atendimento IA + metricas + follow-up + disparos |
| AdvogaPro | Juridico | Disponivel | — |
| RecFlow | Medico | Em desenvolvimento | Laudos + anamnese (ver `illumi-ecosystem.md`) |
| Clarissa & Naoka | Estetica | Em desenvolvimento | Avaliacoes faciais + treinos (ver `illumi-ecosystem.md`) |

CEO sobre a escala: *"Esta funcionando, esta escalando ainda. Nao tem uma estourada, exceto a EducaPro."*

### Criterio para um servico virar SaaS

Demanda **latente e replicavel** na prestacao de servico. Quando o problema do cliente se repete em N empresas do mesmo nicho, vira SaaS. Nao e construido especulativamente.

### Principio do core IA

Todos os softwares Illumi tem IA no **core**. Nao e feature adicional — e coracao do produto.
- Lumion existe porque o RAG e o diferencial competitivo (sem o RAG, e mais uma plataforma de cursos)
- Regra implicita: se um software interno nao tiver core IA, nao e produto Illumi

## 7. Filosofia de IA na operacao

### IA passiva > IA substituta (caso Vincenzi)

Rodrigo Vincenzi (vendas, Goiania, amigo do CEO; CEO compara energia com Ricardo Eletro). Operacao de vendedores.

Decisao da Illumi: NAO substituir atendentes. Em vez disso, **IA passiva** analisa:
- Delay do cliente ate a resposta do atendente
- Aderencia ao script (% que o atendente segue)
- Analise comportamental e cognitiva

Citacao: *"Coloca mais peso ainda no atendimento deles"* — IA aumenta humanos, nao substitui.

> Implicacao: em verticais onde atendimento humano e parte da marca/diferencial, **propor IA passiva** (analise + insights) e nao IA substituta. Casos: vendas relacionais, atendimento premium, consultoria.

Reforco externo: Always Fit (mencionado por Mateus Ribeiro) — empresa que mais vende no TikTok Shop Brasil, tinha 30 atendentes e nunca automatizou atendimento — so usava IA pra analise comportamental.

## 8. Service-as-a-Software (avaliado e parcialmente adotado)

CEO ja considerou o modelo plug-and-play onde o cliente final configura sozinho, mas **declinou como modelo central** pelos motivos:

1. **Conflito com Educacao**: ensina Cloud Code/Lovable na comunidade. Aluno bem-formado faz sozinho.
2. **Solucao adotada**: SaaS pra cliente final em nichos onde faz sentido (EducaPro, OticaPro, AdvogaPro). Nao pra alunos como ferramenta.

> Conclusao: Illumi tem SaaS produtizado, mas vendido pra cliente final do nicho — nao como ferramenta de prestador de servico.

## 9. Stack tecnologico citado na transcricao

Complementa o stack documentado em `illumi-ecosystem.md`:

- **Cloud Code** (Claude Code) — desenvolvimento principal
- **Lovable** — desenvolvimento alternativo
- **Apify** — scraping (Instagram, TikTok)
- **Bubble** — citado como front do Lumion (RAG no proprio bubble)

## 10. Localizacao e rotina do CEO

- Sede: **Goiania, GO**
- Em 2026-05 esta montando escritorio novo (em transicao na data da reuniao, sem internet definitiva)
- Rotina: acorda 7h, dorme ~23h
- Filosofia pessoal: prefere relacionamento intenso (rejeita o modelo "anonimo" do amigo Jeff)

## 11. Network estrategico

| Pessoa | Relacao | Relevancia |
|--------|---------|-----------|
| Pedro da Alphluence | Cliente da agencia | Criador do IOX. Conexao TikTok Shop Brasil. Solucao de analise de creators desenvolvida pela Illumi |
| Thiago Finch / Allan | Usuarios do IOX | Validacao de mercado de IA aplicada |
| Rodrigo Vincenzi | Amigo do CEO (Goiania) | Operacao de vendas robusta — caso da IA passiva analisando atendentes |
| Ricardo Eletro | Conhecido | Referencia do CEO em vendas/energia comercial ("mesma vibe do Vincenzi") |
| Jeff e Ana Leticia | Amigos proximos | Jeff e programador "aposentado", opera anonimo via IA — referencia do CEO de "modo silencioso" (rejeitado por escolha) |
| Claudio | Time interno | Usa Cloud Code |
| Mateus Ribeiro (Evergreen) | Contato/parceiro | Reuniao de diagnostico em 2026-05-01. Veio de Always Fit (TikTok Shop). Construindo funil de diagnostico baseado em perfil Instagram (90 dias) |

## 12. Regras de bolso pra interagir com a operacao

Diretrizes operacionais derivadas da transcricao — usar quando propor solucoes, escrever propostas ou interpretar pedidos do CEO:

1. **Nao automatizar o Direct do Instagram dele.** E diferencial deliberado.
2. **Estruturar metricas em (tempo + receita).** Sempre as duas dimensoes.
3. **Sistemas internos > automacoes pontuais** quando a oportunidade existir. Lock-in e estrategia.
4. **IA passiva e a sugestao default em verticais relacionais** (vendas, atendimento premium, consultoria).
5. **Software Illumi exige core IA.** Sem core IA nao e produto da casa.
6. **No agro, CTA explicito esta liberado.** Nas outras verticais, distribuicao sem CTA.
7. **Ticket de servico tipico: ~R$40k** (10 entrada + 6x 5). Ancora pra propostas similares.
8. **Sociedade divide o preco por N.** N=2 ou N=3 ate hoje.
9. **Novos modulos = +6 meses no contrato.** Mecanica padrao de expansao de LTV.
10. **Filosofia "entregar antes de vender" pode ser citada com clientes empresariais** — e diferencial competitivo, nao fraqueza.
11. **Time = 10 ex-alunos, e isso e diferencial de qualidade**, nao constraint operacional.
12. **EducaPro e o SaaS estourado.** Os outros (OticaPro, AdvogaPro) ainda escalando — nao prometer estourados.

## 13. Sinais para detectar pivot ou nova decisao estrategica

Coisas a observar em conversas futuras com o CEO que podem invalidar partes deste documento:

- Mudanca no foco vertical (hoje agro; pode migrar)
- Decisao de automatizar Direct (mudaria tese de aquisicao)
- Saida do modelo "entregar antes de vender"
- Outro SaaS estourando alem do EducaPro
- Crescimento do time alem de 10 (quebra modelo "10 empresas")
- Lancamento da VibeCode community (2026-05-06) — pode mudar estrategia de educacao

> Quando algum desses sinais aparecer, atualizar este arquivo OU criar memoria nova de "decisao estrategica YYYY-MM-DD".

"""
