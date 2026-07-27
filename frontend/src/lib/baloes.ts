/**
 * Extrai os balões de uma resposta da Renata (agent_id "elevec_sdr") para
 * exibição no admin panel.
 *
 * O backend (`agents/catalog/elevec_sdr/saida.py`, `extrair_baloes`) grava o
 * JSON cru em `message_queue.response` de propósito — é o registro de
 * auditoria do output exato do modelo. Este módulo só formata a EXIBIÇÃO:
 * não persiste nada, não substitui a auditoria. É uma versão simplificada do
 * parser Python (sem log — não há para onde logar no lado do frontend — e
 * sem teto de balões, que é só uma preocupação de espaçamento no envio).
 *
 * Qualquer desvio de schema devolve o texto original inteiro como balão
 * único: nunca lança, nunca perde a resposta.
 */

const CERCA_ANCORADA = /^```(?:json)?\s*([\s\S]*?)\s*```$/i;
const CERCA_LIVRE = /```(?:json)?\s*([\s\S]*?)\s*```/i;

/**
 * Mesma ordem de tentativas do parser Python: cerca ancorada (o texto
 * inteiro está dentro de um bloco ```) primeiro, depois o texto bruto direto
 * (cobre um balão que só parece ter cerca porque contém ``` no meio de uma
 * string), e só por último a cerca livre (cobre preâmbulo/epílogo ao redor
 * da cerca, com risco de falso positivo que só vale correr por último).
 */
function candidatosJson(bruto: string): string[] {
  const candidatos: string[] = [];

  const ancorada = bruto.match(CERCA_ANCORADA);
  if (ancorada) candidatos.push(ancorada[1].trim());

  candidatos.push(bruto);

  const livre = bruto.match(CERCA_LIVRE);
  if (livre) {
    const conteudo = livre[1].trim();
    if (!candidatos.includes(conteudo)) candidatos.push(conteudo);
  }

  return candidatos;
}

function primeiroJsonValido(bruto: string): unknown {
  for (const candidato of candidatosJson(bruto)) {
    try {
      return JSON.parse(candidato);
    } catch {
      continue;
    }
  }
  return undefined;
}

export function extrairBaloes(texto: string | null | undefined): string[] {
  const bruto = (texto ?? "").trim();
  if (!bruto) return [""];

  const dados = primeiroJsonValido(bruto);
  if (dados === undefined || dados === null || typeof dados !== "object") {
    return [bruto];
  }
  if (Array.isArray(dados)) return [bruto];

  const mensagens = (dados as { messages?: unknown }).messages;
  if (!Array.isArray(mensagens)) return [bruto];

  // Qualquer item não-string invalida a lista inteira — mesma regra do
  // parser Python: preferimos o texto bruto a uma resposta mutilada.
  if (!mensagens.every((m): m is string => typeof m === "string")) {
    return [bruto];
  }

  const baloes = mensagens.map((m) => m.trim()).filter((m) => m.length > 0);
  return baloes.length > 0 ? baloes : [bruto];
}
