# Blocklist do n8n — evidência de origem

Entregável bloqueante da Task 0 da Fase 4
(`docs/superpowers/plans/2026-07-27-fase4-migracao-dados.md`, linha 122).

Capturado em 29/07/2026 do dump dos 28 workflows, nó
**`Filtro e Permissao v002`** do workflow **`#1 Agente SDR | 10/02/26 | V2.2`**.

São pessoas que pediram para parar de receber. O custo de perder uma na
transcrição é ela voltar a receber WhatsApp — pela régua de follow-up, que é
o único caminho do sistema que fala sem o lead ter falado primeiro. Esta
página existe para que a contagem seja reconciliável contra a origem, e não
apenas contra si mesma.

## A divergência do 28

O plano falava em **28** números. A contagem real da origem é **29 linhas,
26 únicos, 3 duplicadas**. O 28 era estimativa feita antes de
alguém contar; nenhum número se perdeu.

## Como o n8n casava

```js
const isBlocked = blockedNumbers.some(blocked => currentPhone.endsWith(blocked));
```

Casamento por **sufixo**, sem DDI, sobre os dígitos crus — por isso as duas
formas do 9º dígito casavam com a mesma entrada de graça. `blocklist.phone` é
chave primária e casa exato, então a migração `016` grava as duas.

## As 29 linhas, exatamente como aparecem no nó

```
(45) 9817-9085
(21) 99618-2653
(11) 98609-6863
(54) 9647-3605
(11) 94007-5855
(21) 99748-3733
(51) 8164-5165
(11) 98018-4427
(21) 99596-8026
(71) 9108-0505
(11) 94953-4480
(37) 9141-1790
(21) 97908-1010
(21) 99596-8026
(41) 8751-7400
(17) 99113-4887
(17) 99654-6740
(54) 9691-5385
(11) 97665-5609
(11) 94056-9963
(11) 97540-6284
(83) 9990-0691
(11) 98747-9763
(12) 98239-0831
(48) 9865-6969
(48) 9865-6969
(12) 98239-0831
(19) 99246-3540
(11) 98867-1528
```

## Duplicadas (o `new Set` do n8n as colapsa)

- linha 14 (`(21) 99596-8026`) repete a linha 9
- linha 26 (`(48) 9865-6969`) repete a linha 25
- linha 27 (`(12) 98239-0831`) repete a linha 24

## Reconciliação com `db/migrations/016_blocklist_opt_out_n8n.sql`

| | |
|---|---|
| linhas na origem | 29 |
| duplicadas | 3 |
| pessoas únicas | 26 |
| recusadas por `canonicalizar` | 0 |
| linhas gravadas na 016 | 52 |

As formas gravadas saem de `shared/phone.py::canonicalizar` + `variacoes` — o
mesmo código que o gate usa para montar a consulta, não normalização
reescrita à mão.
